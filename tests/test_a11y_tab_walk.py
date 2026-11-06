"""The opt-in keyboard walk of ``locum.a11y`` (``tab_walk=True`` / ``--tab-walk``).

Mock backend only: the simulated desktop moves its focus on Tab / Shift+Tab
through the enabled buttons, edits and checkboxes of the active window in
document order. Nothing reaches the real desktop.
"""

from __future__ import annotations

import io
import json

import pytest

from locum import cli
from locum.a11y import RULES, TAB_RULES, audit, tab_walk
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import TargetLost
from locum.llm.safety import Policy


def desk(be):
    return Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)


def form():
    be = MockBackend(width=1200, height=800)
    w = be.add_window("Form", (0, 0, 800, 600), app="formapp")
    name = w.add_edit("Name", (20, 20, 200, 28))
    email = w.add_edit("Email", (20, 60, 200, 28))
    ok = w.add_button("OK", (20, 100, 80, 30))
    cancel = w.add_button("Cancel", (120, 100, 80, 30))
    other = be.add_window("Other", (820, 0, 300, 300), app="other", activate=False)
    other_box = other.add_edit("Chat", (830, 20, 200, 28))
    be._raise(w)
    be._set_focus(name)
    return be, w, (name, email, ok, cancel), other, other_box


def keys(be):
    return [e for e in be.events if e[0] in ("key_down", "type_text", "a11y_set_value", "click", "mouse_down")]


def rules_of(findings):
    return [f.rule for f in findings]


# ------------------------------------------------------------------ the walk itself

def test_clean_form_cycles_and_restores_focus():
    be, _w, (name, email, ok, cancel), _o, _ob = form()
    report = audit(desk(be), "Form", tab_walk=True)
    walk = report.tab_walk
    assert walk is not None and walk.complete and walk.stopped == "cycle"
    assert [s.name for s in walk.stops] == ["Email", "OK", "Cancel", "Name"]
    assert walk.presses == 5  # four stops, then back to the first one
    assert not [f for f in report.findings if f.rule in TAB_RULES]
    assert walk.restored_focus is True and be._focus is name
    assert all(s.selector for s in walk.stops)
    d = report.to_dict()
    assert d["tab_walk"]["complete"] is True and len(d["tab_walk"]["stops"]) == 4
    json.dumps(d)
    text = report.format_text()
    assert "tab order of 'Form' (4 stops, 5 Tab presses, cycle complete)" in text


def test_only_tab_reaches_the_backend():
    be, _w, _c, _o, _ob = form()
    be.events.clear()
    audit(desk(be), "Form", tab_walk=True, reverse_check=True)
    downs = [e for e in be.events if e[0] == "key_down"]
    assert {e[1] for e in downs} <= {"tab", "shift"}
    assert not [e for e in be.events if e[0] in ("type_text", "click", "mouse_down", "a11y_set_value")]


def test_static_audit_sends_nothing_without_the_flag():
    be, _w, _c, _o, _ob = form()
    be.events.clear()
    report = audit(desk(be), "Form")
    assert report.tab_walk is None and "tab_walk" not in report.to_dict()
    assert not [e for e in be.events if e[0] == "key_down"]


def test_reverse_check_passes_on_a_symmetric_form():
    be, _w, (name, *_rest), _o, _ob = form()
    walk = tab_walk(desk(be), "Form", reverse_check=True)
    assert walk.reverse_ok is True and walk.reverse_presses == 4
    assert be._focus is name


def test_reverse_check_reports_asymmetric_order(monkeypatch):
    be, _w, (name, email, ok, cancel), _o, _ob = form()
    orig = be._focus_next

    def skewed(w):
        if "shift" in be._mods() and w is ok:
            be._focus = cancel  # Shift+Tab from OK should reach Email
            return
        orig(w)

    monkeypatch.setattr(be, "_focus_next", skewed)
    walk = tab_walk(desk(be), "Form", reverse_check=True)
    assert walk.reverse_ok is False
    assert rules_of(walk.findings) == ["tab-order-asymmetric"]


def test_window_is_required():
    be, *_ = form()
    with pytest.raises(ValueError, match="needs one target window"):
        audit(desk(be), tab_walk=True)
    with pytest.raises(ValueError):
        tab_walk(desk(be), None)
    with pytest.raises(ValueError, match="max_tabs"):
        tab_walk(desk(be), "Form", max_tabs=0)


# ------------------------------------------------------------------ findings

def test_unnamed_invisible_and_unreachable():
    be = MockBackend(width=1200, height=800)
    w = be.add_window("Form", (0, 0, 800, 600))
    first = w.add_edit("First", (20, 20, 200, 28))
    w.add_button("", (20, 60, 80, 30))                     # unnamed stop
    w.add_button("Far away", (5000, 60, 80, 30))           # outside its window: not visible
    w.add("togglebutton", "Bold", (20, 140, 40, 40))       # mock Tab never visits toggle buttons
    be._set_focus(first)
    walk = tab_walk(desk(be), "Form")
    assert walk.complete
    assert "tab-focus-unnamed" in rules_of(walk.findings)
    assert "tab-focus-invisible" in rules_of(walk.findings)
    assert [s.visible for s in walk.stops] == [True, False, True]


def test_unreachable_focusable_control(monkeypatch):
    be, _w, (name, email, ok, cancel), _o, _ob = form()
    orig = be._focus_next

    def skip_cancel(w):
        orig(w)
        if be._focus is cancel:
            orig(cancel)

    monkeypatch.setattr(be, "_focus_next", skip_cancel)
    report = audit(desk(be), "Form", tab_walk=True)
    unreachable = [f for f in report.findings if f.rule == "tab-unreachable"]
    assert [f.name for f in unreachable] == ["Cancel"]
    assert unreachable[0].severity == "warning"


def test_backwards_jump_is_info():
    be = MockBackend(width=1200, height=800)
    w = be.add_window("Form", (0, 0, 800, 600))
    top = w.add_button("Top", (20, 20, 80, 30))       # Tab order Top -> Bottom -> Middle -> Top
    w.add_button("Bottom", (20, 300, 80, 30))
    w.add_button("Middle", (20, 150, 80, 30))
    be._set_focus(top)
    walk = tab_walk(desk(be), "Form")
    back = [f for f in walk.findings if f.rule == "tab-order-backwards"]
    assert [f.name for f in back] == ["Middle"]
    assert back[0].severity == "info" and "above" in back[0].message


def test_wrap_around_is_not_a_backwards_jump():
    be = MockBackend(width=1200, height=800)
    w = be.add_window("Form", (0, 0, 800, 600))
    w.add_button("A", (20, 20, 80, 30))
    w.add_button("B", (120, 20, 80, 30))
    c = w.add_button("C", (20, 100, 80, 30))
    be._set_focus(c)  # the walk starts at the end: C -> A is the wrap, not a defect
    walk = tab_walk(desk(be), "Form")
    assert walk.complete and not walk.findings


def test_trap_focus_does_not_move(monkeypatch):
    be, _w, (name, email, ok, cancel), _o, _ob = form()
    orig = be._focus_next

    def stuck(w):
        if w is ok:
            return  # OK swallows Tab
        orig(w)

    monkeypatch.setattr(be, "_focus_next", stuck)
    walk = tab_walk(desk(be), "Form")
    assert walk.stopped == "trap" and not walk.complete
    trap = [f for f in walk.findings if f.rule == "tab-trap"]
    assert trap and trap[0].name == "OK" and trap[0].severity == "error"
    assert "tab-unreachable not checked" in " ".join(walk.notes)
    assert be._focus is name  # restored


def test_trap_loop_among_a_subset(monkeypatch):
    be, _w, (name, email, ok, cancel), _o, _ob = form()
    orig = be._focus_next

    def loop(w):
        if w is cancel:
            be._focus = ok  # Cancel -> OK -> Cancel ... never back to Name / Email
            return
        orig(w)

    monkeypatch.setattr(be, "_focus_next", loop)
    walk = tab_walk(desk(be), "Form")
    assert walk.stopped == "trap"
    assert "loops among stops 2-3" in walk.findings[0].message


def test_tab_typed_as_text_stops_immediately(monkeypatch):
    be = MockBackend(width=1200, height=800)
    w = be.add_window("Editor", (0, 0, 800, 600))
    doc = w.add_edit("Body", (20, 20, 600, 400))
    w.add_button("Save", (20, 450, 80, 30))
    be._set_focus(doc)
    orig = be._apply_key

    def editor_keys(key):
        if key == "tab" and be._focus is doc:
            doc.value = (doc.value or "") + "\t"
            return
        orig(key)

    monkeypatch.setattr(be, "_apply_key", editor_keys)
    walk = tab_walk(desk(be), "Editor")
    assert walk.presses == 1 and walk.stopped == "trap"
    assert "takes Tab as text" in walk.findings[0].message


def test_focus_lost(monkeypatch):
    be, _w, (name, email, ok, cancel), _o, _ob = form()
    orig = be._focus_next

    def lose(w):
        if w is email:
            be._focus = None
            return
        orig(w)

    monkeypatch.setattr(be, "_focus_next", lose)
    walk = tab_walk(desk(be), "Form")
    assert walk.stopped == "focus-lost"
    assert rules_of(walk.findings) == ["tab-focus-lost"]


def test_nothing_focused_is_a_note_not_a_finding():
    be, w, *_ = form()
    be._set_focus(None)
    w._last_focus = None  # activating the window must not bring a focused field back
    walk = tab_walk(desk(be), "Form")
    assert walk.stopped == "no-focus" and walk.presses == 1 and not walk.findings
    assert "click it" in walk.notes[0]
    assert walk.restored_focus is None


def test_focus_escaping_the_window(monkeypatch):
    be, _w, (name, email, ok, cancel), other, other_box = form()
    orig = be._focus_next

    def escape(w):
        if w is ok:
            be._raise(other)
            be._focus = other_box
            return
        orig(w)

    monkeypatch.setattr(be, "_focus_next", escape)
    be.events.clear()
    walk = tab_walk(desk(be), "Form")
    assert walk.stopped == "escape"
    assert rules_of(walk.findings) == ["tab-escape"]
    # No further key was sent once the focus was in the other window.
    assert [e[1] for e in be.events if e[0] == "key_down"].count("tab") == 3


def test_another_window_taking_the_focus_stops_the_walk_before_the_next_press():
    be, _w, (name, email, ok, cancel), other, other_box = form()
    count = {"n": 0}
    orig = be.key_down

    def key_down(key):
        orig(key)
        count["n"] += 1
        if count["n"] == 2:  # the user clicks into the chat window mid-walk
            be._raise(other)
            be._focus = other_box

    be.key_down = key_down
    walk = tab_walk(desk(be), "Form")
    # The escape is noticed after the press that coincided with it; nothing further is sent.
    assert walk.stopped in ("escape", "interrupted")
    assert other_box.value in (None, "")
    assert count["n"] == 2


def test_every_press_is_bound_to_the_window_under_the_input_lock(monkeypatch):
    from locum import _inputlock, _target

    be, w, *_ = form()
    entered = []
    orig_lock = _inputlock.desktop_input

    def spy_lock(desktop, timeout=None):
        entered.append(desktop)
        return orig_lock(desktop, timeout)

    monkeypatch.setattr(_inputlock, "desktop_input", spy_lock)
    bound = []
    orig = be.key_down

    def key_down(key):
        t = _target.current()
        bound.append((t.handle, t.strict) if t is not None else None)
        orig(key)

    be.key_down = key_down
    walk = tab_walk(desk(be), "Form")
    assert walk.complete and len(entered) == 1
    assert bound and set(bound) == {(w.handle, True)}


def test_refuses_when_window_cannot_be_brought_to_front(monkeypatch):
    be, *_ = form()
    monkeypatch.setattr(be, "foreground_handle", lambda: None)
    with pytest.raises(TargetLost, match="no Tab was sent"):
        tab_walk(desk(be), "Form")


def test_max_tabs_partial():
    be, *_ = form()
    walk = tab_walk(desk(be), "Form", max_tabs=2)
    assert walk.presses == 2 and walk.stopped == "max_tabs" and not walk.complete
    assert any("partial" in n for n in walk.notes)


def test_rules_filter():
    be, *_ = form()
    with pytest.raises(ValueError, match="unknown rule"):
        tab_walk(desk(be), "Form", rules=["nope"])
    assert TAB_RULES <= set(RULES)


# ------------------------------------------------------------------ policy

def test_policy_dry_run_presses_nothing():
    be, *_ = form()
    be.events.clear()
    walk = tab_walk(desk(be), "Form", policy=Policy(dry_run=True))
    assert not [e for e in be.events if e[0] == "key_down"]
    assert walk.stopped == "dry-run" and walk.presses == 0 and not walk.stops


def test_policy_refusal_stops_the_walk():
    be, *_ = form()
    walk = tab_walk(desk(be), "Form", policy=Policy(deny_tools=["press_keys"]))
    assert walk.stopped == "refused" and walk.presses == 0


def test_guarded_desktop_routes_presses_through_the_policy():
    from locum.llm.safety import AuditLog, GuardedDesktop

    be, *_ = form()
    log = AuditLog()
    g = GuardedDesktop(desk(be), Policy(), audit=log)
    walk = tab_walk(g, "Form")
    assert walk.complete
    tools = [e.get("tool") for e in log.events]
    assert tools.count("press_keys") == walk.presses


# ------------------------------------------------------------------ CLI

def run_cli(be, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), desktop=desk(be), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_tab_walk_requires_window():
    be, *_ = form()
    be.events.clear()
    code, _out, err = run_cli(be, "a11y-audit", "--tab-walk")
    assert code == cli.EXIT_USAGE and "--window" in err
    assert not [e for e in be.events if e[0] == "key_down"]


def test_cli_tab_walk_json():
    be, _w, (name, *_r), _o, _ob = form()
    code, out, _err = run_cli(be, "--json", "a11y-audit", "--window", "Form", "--tab-walk", "--reverse",
                              "--max-tabs", "50")
    data = json.loads(out)
    assert code == cli.EXIT_OK, data
    assert data["tab_walk"]["complete"] is True and data["tab_walk"]["reverse_ok"] is True
    assert [s["name"] for s in data["tab_walk"]["stops"]] == ["Email", "OK", "Cancel", "Name"]
    assert be._focus is name


def test_cli_tab_walk_text_and_positional_window():
    be, *_ = form()
    code, out, _err = run_cli(be, "a11y-audit", "Form", "--tab-walk", "--policy", "default")
    assert code == cli.EXIT_OK
    assert "tab order of 'Form'" in out


def test_cli_flag_misuse_is_usage_error():
    be, *_ = form()
    assert run_cli(be, "a11y-audit", "Form", "--reverse")[0] == cli.EXIT_USAGE
    assert run_cli(be, "a11y-audit", "Form", "--window", "Other", "--tab-walk")[0] == cli.EXIT_USAGE
    assert run_cli(be, "a11y-audit", "--window", "Form", "--tab-walk", "--max-tabs", "0")[0] == cli.EXIT_USAGE
