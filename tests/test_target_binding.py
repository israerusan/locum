"""Input target binding (review of 2026-11-06, live Windows run).

`locum type` typed into a chat window that had taken the focus, and its Enter
submitted the text there. These tests steal the focus between finding the target
and sending input -- and in the middle of the input -- and check that nothing
reaches the other window. Also: window-scoped observe leaks no other window, and
the CLI's --json / legacy-console contract.
"""

from __future__ import annotations

import io
import json

import pytest

from locum import Desktop, TargetLost, _target
from locum.backends.mock import MockBackend
from locum.cli import main
from locum.llm.observe import observe
from locum.llm.safety import Policy
from locum.llm.tools import DesktopToolkit

SECRET = "transfer 500 to acct 9"


@pytest.fixture
def scene():
    be = MockBackend(width=1200, height=800)
    chat = be.add_window("Chat", (600, 0, 500, 400), app="chat")
    box = chat.add_edit("Message", (620, 300, 400, 30))
    note = be.add_window("Untitled - Notepad", (0, 0, 500, 400), app="notepad")
    editor = note.add_edit("Text editor", (20, 40, 400, 300))
    other = note.add_edit("Find", (20, 350, 200, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, chat, box, note, editor, other


def steal_after(be, n, window, kind="type_text"):
    """Activate ``window`` after ``n`` text units / key presses reached the backend."""
    orig = getattr(be, kind)
    count = {"n": 0}

    def wrapped(*a, **k):
        res = orig(*a, **k)
        count["n"] += 1
        if count["n"] == n:
            be.activate_window(window.handle)
        return res

    setattr(be, kind, wrapped)


def chat_text(box):
    return box.value or ""


# ============================================================ library: Element input
def test_focus_stolen_during_element_type_stops_the_input(scene):
    be, d, chat, box, note, editor, _ = scene
    el = d.find('edit "Text editor"')
    steal_after(be, 3, chat, "key_down")
    with pytest.raises(TargetLost):
        el.type(SECRET + "\n", mode="keys", verify=False)
    assert chat_text(box) == ""  # not a single key reached the chat
    assert "enter" not in [e[1] for e in be.events if e[0] == "key_down"]


def test_focus_stolen_between_find_and_type(scene):
    be, d, chat, box, note, editor, _ = scene
    el = d.find('edit "Text editor"')
    el.focus()
    be.activate_window(chat.handle)  # a notification / chat takes the foreground
    # The element is focused again (its window comes back) or nothing is typed.
    try:
        el.type("hello", verify=False)
    except TargetLost:
        pass
    assert chat_text(box) == ""


def test_focus_moving_to_another_control_in_the_window_stops_typing(scene):
    be, d, chat, box, note, editor, other = scene
    el = d.find('edit "Text editor"')
    orig = be.key_down
    n = {"i": 0}

    def wrapped(key):
        orig(key)
        n["i"] += 1
        if n["i"] == 2:
            be._set_focus(other)  # an app moves the focus itself (auto-advance, a dialog field)

    be.key_down = wrapped
    with pytest.raises(TargetLost):
        el.type("abcdef", mode="keys", verify=False)
    assert (other.value or "") == ""


def test_tab_inside_the_text_may_move_the_focus_within_the_window(scene):
    be, d, chat, box, note, editor, other = scene
    editor.next_focus = other
    el = d.find('edit "Text editor"')
    el.type("ab\tcd", mode="keys", verify=False)  # Tab is part of the text: the next field gets "cd"
    assert chat_text(box) == ""


def test_click_on_an_element_covered_after_it_was_found_is_refused(scene):
    be, d, chat, box, note, editor, _ = scene
    t = _target.InputTarget(be, note.handle, title="Untitled - Notepad", strict=True)
    with _target.bind(t):
        with pytest.raises(TargetLost):
            d.click((700, 100))  # inside "Chat", which is not the bound window
    assert not [e for e in be.events if e[0] == "down"]


def test_releases_are_never_blocked(scene):
    be, d, chat, box, note, editor, _ = scene
    d.keyboard._raw_down("shift")
    be.activate_window(chat.handle)
    with _target.bind(_target.InputTarget(be, note.handle, strict=True)):
        d.keyboard.release_all()
    assert "shift" not in be.pressed_keys


def test_strict_target_refuses_when_the_backend_cannot_tell(scene):
    be, d, *_ = scene
    be.foreground_handle = lambda: None
    with _target.bind(_target.InputTarget(be, 12345, strict=True)):
        with pytest.raises(TargetLost, match="cannot tell"):
            d.keyboard.type("x")


# ============================================================ CLI
def run(argv, d):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, desktop=d, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_unscoped_type_press_hotkey_click_are_refused(scene):
    be, d, *_ = scene
    for argv in (["type", "hi"], ["press", "enter"], ["hotkey", "ctrl+s"], ["click", "10,10"]):
        code, out, err = run(argv, d)
        assert code == 2, argv
        assert "--window" in err and ("--focused" in err or "--anywhere" in err)
    assert not [e for e in be.events if e[0] in ("key_down", "down", "type")]


def test_cli_type_window_activates_types_and_reports(scene):
    be, d, chat, box, note, editor, _ = scene
    be.activate_window(chat.handle)
    be._set_focus(box)
    note._last_focus = editor
    code, out, err = run(["--json", "type", "hello", "--into", 'edit "Text editor"', "--window", "Notepad"], d)
    assert code == 0, err
    data = json.loads(out)
    assert data["window"] == "Untitled - Notepad" and data["verified"] is True
    assert editor.value == "hello" and chat_text(box) == ""


def test_cli_focus_stolen_mid_type_exits_1_with_one_json_error(scene):
    be, d, chat, box, note, editor, _ = scene
    note._last_focus = editor
    steal_after(be, 2, chat, "key_down")
    code, out, err = run(["--json", "type", SECRET + "\\n", "-e", "--mode", "keys", "--window", "Notepad"], d)
    assert code == 1
    data = json.loads(out)  # exactly one JSON document on stdout
    assert data["ok"] is False and data["error"]["type"] == "TargetLost" and data["error"]["exit_code"] == 1
    assert "TargetLost" in err
    assert chat_text(box) == ""


def test_cli_focused_still_stops_when_the_focus_changes_during_typing(scene):
    be, d, chat, box, note, editor, _ = scene
    be.activate_window(note.handle)
    be._set_focus(editor)
    steal_after(be, 2, chat, "key_down")
    code, _, err = run(["type", "abcdef", "--mode", "keys", "--focused"], d)
    assert code == 1 and "TargetLost" in err
    assert chat_text(box) == ""


def test_cli_click_point_in_window_covered_by_another_is_refused(scene):
    be, d, chat, box, note, editor, _ = scene
    be.add_window("Popup", (0, 0, 300, 300), app="other")  # covers part of Notepad, in front
    be.activate_window(note.handle)
    be.windows.insert(0, be.windows.pop(next(i for i, w in enumerate(be.windows) if w.title == "Popup")))
    be.activate_window = lambda h: None  # it cannot be raised above the popup
    code, _, err = run(["click", "50,50", "--window", "Notepad"], d)
    assert code == 1, err
    assert not [e for e in be.events if e[0] == "down"]


def test_cli_click_selector_inside_window(scene):
    be, d, chat, box, note, editor, _ = scene
    code, out, err = run(["--json", "click", 'edit "Text editor"', "--window", "Notepad"], d)
    assert code == 0, err
    assert json.loads(out)["window"] == "Untitled - Notepad"


# ============================================================ LLM tools
def test_tool_type_text_into_focus_is_bound_to_the_judged_window(scene):
    be, d, chat, box, note, editor, _ = scene
    be.activate_window(note.handle)
    be._set_focus(editor)
    kit = DesktopToolkit(d, Policy())
    steal_after(be, 1, chat, "key_down")
    r = kit.call("type_text", {"text": "ab\ncd"})
    assert r.is_error and "TargetLost" in r.text
    assert chat_text(box) == ""


def test_tool_press_keys_after_a_window_switch_is_refused(scene):
    be, d, chat, box, note, editor, _ = scene
    be.activate_window(note.handle)
    be._set_focus(editor)
    kit = DesktopToolkit(d, Policy())
    steal_after(be, 1, chat, "key_up")
    r = kit.call("press_keys", {"keys": ["a", "enter"]})
    assert r.is_error and "TargetLost" in r.text
    assert ("key_down", "enter") not in be.events


def test_computer_use_type_is_bound_to_the_judged_window(scene):
    from locum.llm.computer_use import ComputerUseAdapter

    be, d, chat, box, note, editor, _ = scene
    be.activate_window(note.handle)
    be._set_focus(editor)
    tool = ComputerUseAdapter(d, policy=Policy())
    steal_after(be, 1, chat, "type_text")  # the chat takes the focus after "abc", before the Enter
    r = tool.execute("type", {"text": "abc\ndef"})
    assert r.is_error and "TargetLost" in r.text
    assert chat_text(box) == "" and ("key_down", "enter") not in be.events


# ============================================================ observe --window
def test_window_observe_shows_no_other_window(scene):
    be, d, chat, box, note, editor, _ = scene
    chat.title = chat.root.name = "Bank - Statement 4711"
    be.activate_window(chat.handle)
    obs = observe(d, window="Notepad", screenshot=True)
    text = obs.text() + json.dumps(obs.to_dict())
    assert "Bank" not in text and "Statement" not in text and "chat" not in text
    assert [w.title for w in obs.windows] == ["Untitled - Notepad"]
    assert obs.active_window is None and any("not in the foreground" in n for n in obs.notes)


def test_window_observe_blanks_windows_in_front(scene):
    be, d, chat, box, note, editor, _ = scene
    pop = be.add_window("Secret", (100, 100, 200, 150), app="vault")  # in front of Notepad
    pop.add_button("Reveal", (110, 110, 80, 30))
    obs = observe(d, window="Notepad", screenshot=True)
    img = obs.screenshot
    sx = img.size[0] / 500.0
    assert img.getpixel((int(150 * sx), int(150 * sx)))[:3] == (96, 96, 96)
    assert "Secret" not in obs.text() and "Reveal" not in obs.text()
    assert any("blanked" in n for n in obs.notes)


# ============================================================ CLI output contract
def _cp1252():
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n")


def test_legacy_code_page_console_never_crashes(scene):
    be, d, chat, *_ = scene
    chat.title = chat.root.name = "⠋ Claude Code"
    be.activate_window(chat.handle)
    for argv in (["info"], ["windows"], ["observe"], ["tree", "--window", "Claude"]):
        raw, out = _cp1252()
        err = io.StringIO()
        code = main(argv, desktop=d, stdout=out, stderr=err)
        out.flush()
        assert code == 0, (argv, err.getvalue())
        assert b"Claude Code" in raw.getvalue()


def test_json_on_a_legacy_code_page_is_complete_and_exact(scene):
    be, d, chat, *_ = scene
    chat.title = chat.root.name = "⠋ Claude Code \U0001f680"
    be.activate_window(chat.handle)
    raw, out = _cp1252()
    code = main(["--json", "info"], desktop=d, stdout=out, stderr=io.StringIO())
    out.flush()
    assert code == 0
    data = json.loads(raw.getvalue().decode("cp1252"))
    assert data["active_window"]["title"] == "⠋ Claude Code \U0001f680"


@pytest.mark.parametrize("argv,ok", [
    (["info"], True), (["windows"], True), (["observe"], True), (["observe", "--window", "Notepad"], True),
    (["tree", "--window", "Notepad"], True), (["find", 'edit "Text editor"'], True),
    (["find", 'button "Nope"', "--timeout", "0"], False), (["find", "edit", "--all"], True),
    (["inspect", "100,100"], True), (["window-that-does-not-exist"], None),
    (["type", "x", "--window", "No Such Window", "--timeout", "0"], False),
    (["click", 'button "Nope"', "--timeout", "0"], False),
    (["type", "x"], False),
])
def test_json_mode_prints_exactly_one_document(scene, argv, ok):
    be, d, *_ = scene
    code, out, err = run(["--json", *argv], d)
    if ok is None:  # argparse usage error: no command ran
        assert code == 2
        return
    data = json.loads(out)  # raises if there is anything but one JSON document
    assert (code == 0) is ok, (argv, code, err)
    if not ok:
        assert data["ok"] is False and data["error"]["exit_code"] == code and err.strip()
