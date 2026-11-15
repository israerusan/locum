"""Regression tests for the 0.4.4 polish pass (one test per fixed defect)."""

from __future__ import annotations

import json
import time
import unicodedata

import numpy as np
import pytest

from locum.backends import mock
from locum.backends.mock import MockBackend, MockNode
from locum.desktop import Desktop
from locum.errors import ActionTimeout
from locum.llm import AuditLog
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.safety import GuardedDesktop, Policy
from locum.llm.tools import DesktopToolkit


def _desk(be: MockBackend) -> Desktop:
    return Desktop(be, failsafe=False, sleep=lambda s: None)


def _decline(log):
    return lambda desc, det: log.append(desc) or False


# ------------------------------------------------------------------ llm / safety
def test_browser_launch_refuses_a_switch_as_url(monkeypatch):
    import locum.browser.cdp as cdp

    started = []
    monkeypatch.setattr(cdp.subprocess, "Popen", lambda args, **kw: started.append(args))
    tk = DesktopToolkit(_desk(MockBackend()), Policy.permissive())
    r = tk.call("browser_launch", {"browser": "chrome", "url": "--gpu-launcher=cmd /c calc"})
    assert r.is_error and "command-line switch" in r.text and started == []
    with pytest.raises(ValueError, match="command-line switch"):
        cdp.Browser.launch("chrome", url=" --renderer-cmd-prefix=x")


def test_strict_denies_browser_launch_and_app_rule_does_not_authorize_it():
    assert "browser_launch" in Policy.strict().deny_tools
    assert "browser_launch" not in Policy.strict(allow="tool=browser_launch").deny_tools
    asked = []
    pol = Policy.strict(allow=["app=Google Chrome"], deny_tools=(), confirm=_decline(asked))
    ctx = {"target_app": "Google Chrome", "target_apps": ["Google Chrome"], "command": "chrome"}
    with pytest.raises(Exception):
        pol.check("browser_launch", {"browser": "chrome"}, context=ctx)
    assert asked  # a human was asked: the app= rule does not cover starting the browser


def _pay_form():
    be = MockBackend()
    w = be.add_window("Invoice", (0, 0, 800, 600), app="erp")
    amt = w.add("edit", "Amount", (20, 20, 200, 30))
    w.add_button("&Pay now", (20, 80, 100, 30))
    be._set_focus(amt)
    return be


@pytest.mark.parametrize("text", ["alt+p", "Control_L+s"])
def test_computer_use_hold_key_goes_through_the_shortcut_tripwire(text):
    asked = []
    cu = ComputerUseAdapter(_desk(_pay_form()), policy=Policy.default(confirm=_decline(asked)))
    r = cu.execute("hold_key", {"text": text, "duration": 0.01})
    assert r.is_error and asked


def test_guarded_hold_checks_access_keys():
    asked = []
    g = GuardedDesktop(_desk(_pay_form()), Policy.default(), confirm=_decline(asked))
    with pytest.raises(Exception):
        g.hold("alt", "p").__enter__()
    assert asked and "Pay now" in asked[-1]


def test_password_value_is_never_a_target_label(monkeypatch):
    secret = "S3cr3t-Pa55!"
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))
    be = MockBackend()
    w = be.add_window("Bank login", (100, 100, 500, 300), app="chrome")
    pwd = w.add("edit", "", (20, 60, 200, 30), attrs={"password": True})
    pwd.value = secret
    be._set_focus(pwd)
    asked, audit = [], AuditLog()
    kit = DesktopToolkit(_desk(be), Policy(allow=[], confirm=lambda d, det: asked.append((d, det)) or True), audit)
    r = kit.call("press_keys", {"keys": ["enter"]})
    assert secret not in r.text and secret not in str(asked) and secret not in str(audit.events)


def test_changes_only_redraws_boxes_with_the_kept_ids(monkeypatch):
    import locum.vision as vision

    drawn = []
    orig = vision.annotate
    monkeypatch.setattr(vision, "annotate", lambda img, boxes, **kw: drawn.append(
        {(b.left, b.top): lbl for b, lbl, *_ in boxes}) or orig(img, boxes, **kw))
    be = MockBackend()
    w = be.add_window("App", (0, 0, 800, 600), app="app")
    w.add_button("Cancel", (20, 100, 100, 30))
    w.add_button("Save", (20, 200, 100, 30))
    tk = DesktopToolkit(_desk(be), Policy.permissive())
    tk.call("observe", {})
    w.add_button("Delete all", (20, 20, 100, 30))
    w.root.children.insert(0, w.root.children.pop())  # the new button comes first in tree order
    tk.call("observe", {"changes_only": True, "include_screenshot": True})
    ids = {e.name: str(e.id) for e in tk.last_observation.elements}
    assert drawn[-1] == {(20, 20): ids["Delete all"], (20, 100): ids["Cancel"], (20, 200): ids["Save"]}
    assert ids["Cancel"] == "1"


@pytest.mark.parametrize("policy", [Policy.permissive, lambda **kw: Policy(confirm_patterns=(), **kw)])
def test_toolspec_confirm_asks_even_without_confirm_patterns(policy):
    asked = []
    pol = policy(confirm=_decline(asked))
    with pytest.raises(Exception):
        pol.check("release_payments", {}, context={"require_confirmation": True})
    assert asked


def test_mcp_wait_seconds_extends_the_deadline():
    from locum.llm.mcp_server import MCPServer

    srv = MCPServer(_desk(MockBackend()), Policy(), call_timeout=1)
    assert srv._timeout_for({"seconds": 30}) == 30 + srv.TIMEOUT_MARGIN
    assert srv._timeout_for({"timeout": 5, "seconds": 30}) == 5 + srv.TIMEOUT_MARGIN


# ------------------------------------------------------------- element / selectors
def test_indeterminate_box_is_not_unchecked():
    be = MockBackend()
    w = be.add_window("W")
    cb = w.add_checkbox("Tri", (10, 10, 100, 20))

    class TriNode(MockNode):
        @property
        def states(self):
            return frozenset({"enabled", "visible", "focusable", "indeterminate"})

        def toggle(self):
            return True

    from locum.element import Element

    el = Element(TriNode(be, cb), _desk(be))
    assert el._check_state() == "mixed"
    with pytest.raises(ActionTimeout):
        el.wait_until("unchecked", timeout=0)


@pytest.mark.parametrize("shown,typed,mode", [("-$5.00", "-5", None), ("($1,250.00)", "-1250", "amount"),
                                              ("-€5", "-5", None), ("+$3", "3", None)])
def test_currency_symbol_after_sign(shown, typed, mode):
    from locum.element import _matches, _same_text

    assert (_matches(shown, typed, mode, ".") if mode else _same_text(shown, typed, "."))


def test_set_text_append_moves_the_caret_to_the_end():
    be = MockBackend()
    w = be.add_window("W", (0, 0, 400, 300))
    w.add("edit", "Name", (10, 10, 200, 30), value="abc")
    d = _desk(be)
    d.find('edit "Name"').set_text("def", clear=False, native=False, verify=False)
    keys = [e for e in be.events if e[0] in ("key_down", "keydown", "down") and "end" in str(e)]
    assert keys, be.events[-20:]


@pytest.mark.parametrize("title", ["ดี", "ดู", "फ़ाइल में", unicodedata.normalize("NFD", "Café")])
def test_stable_title_keeps_combining_marks_of_letters(title):
    from locum._nodeinfo import stable_title

    assert stable_title(title) == title


def test_stable_title_still_strips_emoji_decorations():
    from locum._nodeinfo import stable_title

    assert stable_title("Done ✔️") == "Done" and stable_title("✔️ Done") == "Done"


def test_heal_union_counts_each_node_once():
    from locum.heal import HealOptions, heal

    be = MockBackend()
    w = be.add_window("W")
    for i in range(30):
        w.add_text(f"label {i}", (0, i * 10, 50, 10))
    w.add_button("Save as", (200, 10, 80, 30))
    roots = be.accessibility_roots()
    opts = HealOptions(max_nodes=40)
    assert heal('button "Save"', roots, options=opts, log=False)
    out = heal('button "Save", button "Speichern"', roots, options=opts, log=False)
    assert out and not out.truncated


def test_heal_tolerates_none_roots():
    from locum.heal import heal

    be = MockBackend()
    be.add_window("A").add_button("Save as", (0, 0, 10, 10))
    be.add_window("B").add_button("Other", (0, 0, 10, 10))
    roots = be.accessibility_roots()
    out = heal('window "A" button "Save"', [None] + roots, log=False)
    assert out and out.info.new_selector == heal('window "A" button "Save"', roots, log=False).info.new_selector


@pytest.mark.parametrize("sel", ["button[role^=Butt]", "button[role*=Button]", '[role%="Butt*"]'])
def test_role_operators_ignore_case(sel):
    be = MockBackend()
    be.add_window("W", (0, 0, 400, 300)).add_button("OK", (10, 10, 50, 20))
    assert len(_desk(be).find_all(sel)) == 1


# ------------------------------------------------------------------ a11y tab walk
def test_tab_walk_reports_a_trap_on_the_starting_control():
    from locum.a11y import tab_walk

    be = MockBackend()
    d = Desktop(backend=be)
    w = be.add_window("Form")
    one = w.add_button("One", (10, 10, 80, 30))
    w.add_button("Two", (10, 50, 80, 30))
    w.add_edit("Name", (10, 90, 200, 30))
    be._focus_next = lambda wid: None  # Tab does nothing: the focus is trapped on "One"
    be._set_focus(one)
    walk = tab_walk(d, "Form", max_tabs=10, settle=0)
    assert walk.stopped == "trap" and not walk.complete
    rules = [f.rule for f in walk.findings]
    assert "tab-trap" in rules and "tab-unreachable" not in rules


# --------------------------------------------------------------- desktop / cli core
def test_numpy_integer_coordinates():
    d = _desk(MockBackend())
    assert d.move(np.int64(100), np.int64(200)) == (100, 200)


def test_screenshot_paths_with_dots_and_no_extension(tmp_path):
    d = _desk(MockBackend())
    for name in ("shot_10.30.45", "noext"):
        d.screenshot(path=tmp_path / name)
        assert (tmp_path / name).read_bytes()[:4] == b"\x89PNG"


def test_input_lock_registry_does_not_keep_backends_alive():
    import gc
    import weakref

    from locum import _inputlock

    be = MockBackend()
    ref = weakref.ref(be)
    with _inputlock.desktop_input(_desk(be)):
        pass
    del be
    gc.collect()
    assert ref() is None


def test_input_lock_accepts_an_infinite_timeout():
    from locum import _inputlock

    with _inputlock.desktop_input(_desk(MockBackend()), timeout=float("inf")):
        pass


def test_is_on_screen_counts_mirrored_monitors_once(monkeypatch):
    from locum.types import Monitor, Rect

    be = MockBackend(width=1920, height=1080)
    w = be.add_window("W", (-300, 100, 800, 600))
    monkeypatch.setattr(be, "monitors", lambda: [Monitor(0, Rect(0, 0, 1920, 1080), True),
                                                 Monitor(1, Rect(0, 0, 1920, 1080), False)])
    assert _desk(be).window("W").is_on_screen() is False and w is not None


def _cli(args, be=None):
    import io

    from locum.cli import main

    out, err = io.StringIO(), io.StringIO()
    code = main(args, stdout=out, stderr=err, desktop=_desk(be or MockBackend()) if be is not False else None)
    return code, out.getvalue(), err.getvalue()


def test_cli_json_without_command_writes_the_error_document():
    code, out, _ = _cli(["--json"], be=False)
    doc = json.loads(out)
    assert code == 2 and doc["error"]["type"] == "UsageError"


def test_cli_existing_file_named_like_a_selector_is_not_an_image(tmp_path, monkeypatch):
    from locum.cli import _is_image_target

    monkeypatch.chdir(tmp_path)
    (tmp_path / "edit").write_text("x")
    assert not _is_image_target("edit") and _is_image_target("patch.PNG")


def test_cli_locate_click_without_anywhere_is_a_usage_error_without_a_match(tmp_path):
    from PIL import Image

    img = tmp_path / "nothing.png"
    Image.new("RGB", (9, 9), (1, 2, 3)).save(img)
    code, _, _ = _cli(["locate", str(img), "--click"])
    assert code == 2


def test_cli_click_native_invoke_is_reported_not_failed(monkeypatch):
    from locum.cli import DemoBackend

    be = DemoBackend()
    orig = MockNode.bounds
    monkeypatch.setattr(MockNode, "bounds", property(lambda self: None if self.widget.name == "Clear" else orig.fget(self)))
    code, out, err = _cli(["--json", "click", 'button "Clear"', "--window", "Invoice"], be)
    assert code == 0, err
    doc = json.loads(out)
    assert doc["clicked"] is None and doc["native"] is True and be._status.name == "Cleared"


def test_selftest_survives_an_unreadable_pointer(tmp_path):
    from locum import selftest
    from locum.errors import NotSupported

    d = selftest._mock_desktop()

    def nopos():
        raise NotSupported("Wayland does not let applications read the pointer position")

    d.backend.mouse_position = nopos
    st = selftest.SelfTest(d, str(tmp_path), log=lambda s: None)
    st.run()
    assert (tmp_path / "report.txt").exists() and st.checks


def test_paste_unread_mid_text_stops_typing():
    from locum.keyboard import Keyboard

    be = MockBackend()
    be.wait_clipboard_read = lambda since, timeout: False  # the app never reads the paste
    kb = Keyboard(be, sleep=lambda s: None)
    units = [("paste", "café"), ("text", " more")]
    with pytest.raises(ActionTimeout):
        kb._run_units(units, 0.0, first=True)


# ------------------------------------------------------------------------- rpa
def test_html_report_masks_a_secret_inside_json_dumps():
    import html as _html

    from locum._secret import Secret, scrub

    pw = Secret('s3cr"et&pw')
    shown = _html.escape(json.dumps({"connection": f"pwd={pw.reveal()}"}))
    assert "s3cr" not in scrub(shown)


def test_parent_inline_flow_resolves_paths_in_its_own_directory(tmp_path):
    from PIL import Image

    from locum.rpa.flow import run_flow

    (tmp_path / "sub").mkdir()
    Image.new("RGB", (10, 10), (255, 0, 0)).save(tmp_path / "ok.png")
    (tmp_path / "main.yaml").write_text(
        "flows:\n  wait_ok:\n    - wait_image: {image: ok.png, timeout: 0}\n"
        "steps:\n  - call: wait_ok\n  - call: sub/child.yaml\n")
    (tmp_path / "sub" / "child.yaml").write_text("steps:\n  - call: wait_ok\n")
    be = MockBackend()
    be.paint(Image.new("RGB", (10, 10), (255, 0, 0)), (50, 50))
    rep = run_flow(str(tmp_path / "main.yaml"), Desktop(backend=be), policy=None, run_dir=str(tmp_path / "runs"))
    assert rep.ok, [(st.path, st.error) for st in rep.steps]


def test_flow_report_marks_ctrl_c_as_aborted(tmp_path):
    from locum.rpa.flow import run_flow

    d = Desktop(backend=MockBackend())

    def boom(s):
        raise KeyboardInterrupt()

    d.sleep = boom
    with pytest.raises(KeyboardInterrupt):
        run_flow({"steps": [{"log": "start"}, {"sleep": 5}]}, d, run_dir=str(tmp_path), policy=None)
    data = json.loads(next(tmp_path.glob("*/report.json")).read_text())
    assert data["aborted"] is True and data["error"]


def test_queue_reclaims_its_own_expired_claim(tmp_path):
    from locum.rpa import queue as Q

    q = Q.WorkQueue("t", tmp_path / "q.json", lease_duration=0.3)
    q.add({"n": 1})
    q.next()  # claimed, then the worker stops without finishing it
    time.sleep(0.4)
    q.refresh()
    assert q.items[0].status != Q.IN_PROGRESS


def test_crash_in_retry_wait_does_not_use_up_an_attempt(tmp_path):
    from locum.rpa.queue import WorkQueue

    p = tmp_path / "q.json"
    q = WorkQueue("t", p, lease_duration=0.3, on_abandoned="requeue")
    q.add({"n": 1})
    calls = []

    def handler(it):
        calls.append(1)
        raise RuntimeError("app crashed")

    def die(s):
        raise SystemExit("killed")

    with pytest.raises(SystemExit):
        q.process(handler, retries=1, retry_delay=5, sleep=die)
    time.sleep(0.4)
    q2 = WorkQueue("t", p, on_abandoned="requeue")
    q2.process(handler, retries=1)
    assert len(calls) == 2  # the retry the crash interrupted still ran


# -------------------------------------------------------------------- backends
def test_mock_focused_element_has_its_parents():
    be = _pay_form()
    assert be.focused_element().parent is not None


def test_pyautogui_write_with_a_held_modifier_presses_keys(monkeypatch):
    from locum.compat import pyautogui as pag

    be = MockBackend()
    w = be.add_window("W", (0, 0, 400, 300))
    f = w.add("edit", "F", (10, 10, 200, 30), value="old text")
    be._set_focus(f)
    monkeypatch.setattr(pag, "_DESKTOP", _desk(be), raising=False)
    monkeypatch.setattr(pag, "_d", lambda: pag._DESKTOP)
    monkeypatch.setattr(pag, "PAUSE", 0)
    pag.keyDown("ctrl")
    pag.write("a")
    pag.keyUp("ctrl")
    pag.write("Z")
    assert f.value == "Z"


def test_x11_clipboard_never_picks_wl_clipboard_on_its_own(monkeypatch):
    from locum.backends.x11.clipboard import _ClipboardMixin

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name if name.startswith("wl-") else None)
    assert _ClipboardMixin()._detect_clipboard(None) is None


# ------------------------------------------------------------- X11: owned windows
@pytest.mark.x11
def test_x11_dialogs_and_popups_belong_to_their_window(xvfb_display):
    pytest.importorskip("Xlib")
    from Xlib import X, display

    from locum import _target
    from locum.backends.x11 import X11Backend

    d = display.Display(xvfb_display)
    s = d.screen()
    pid_atom, card = d.intern_atom("_NET_WM_PID"), d.intern_atom("CARDINAL")
    made = []

    def window(x, y, w, h, **kw):
        win = s.root.create_window(x, y, w, h, 0, s.root_depth, X.InputOutput, X.CopyFromParent,
                                   background_pixel=s.white_pixel, **kw)
        win.change_property(pid_atom, card, 32, [4242])
        made.append(win)
        return win

    main = window(50, 50, 300, 200)
    main.set_wm_name("Editor")
    dialog = window(80, 80, 120, 80)
    dialog.set_wm_name("Save?")
    dialog.set_wm_transient_for(main)
    menu = window(100, 200, 150, 200, override_redirect=True)  # a pop-up menu below the window
    stranger = window(600, 50, 100, 100)
    stranger.set_wm_name("Other")
    for w in made:
        w.map()
    d.sync()
    time.sleep(0.2)
    be = X11Backend(display=xvfb_display, accessibility=False, clipboard_tool="none")
    try:
        assert be.window_belongs_to(dialog.id, main.id)
        assert be.window_handle_at(120, 350) == menu.id
        assert be.window_belongs_to(menu.id, main.id)
        assert not be.window_belongs_to(stranger.id, main.id)
        assert not be.window_belongs_to(main.id, dialog.id)
        _target.InputTarget(be, main.id, title="Editor", strict=True).check("button", point=(120, 350))
    finally:
        be.close()
        for w in made:
            w.destroy()
        d.close()
