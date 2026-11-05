"""What an allow rule really authorizes: the control that is pressed, and only the keys that act on it."""

from __future__ import annotations

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.errors import PolicyViolation
from pypilot.llm.safety import (
    AuditLog, GuardedDesktop, Policy, combo_acts_on_target, pressed_control, target_identity,
)
from pypilot.llm.tools import DesktopToolkit
from pypilot.rpa import Robot, run_flow
from pypilot.rpa.flow import FlowStepError


class _Backend(MockBackend):
    """Like UIA/AT-SPI, the focused element knows its parents (the mock's plain node does not)."""

    def focused_element(self):
        return self._node_with_parents(self._focus)


@pytest.fixture
def app():
    be = _Backend(width=1280, height=800)
    win = be.add_window("ERP", (100, 100, 500, 400), app="erp")
    toolbar = win.add("pane", "", (20, 200, 300, 60), automation_id="toolbar")
    w = {
        "amount": win.add_edit("Amount", (20, 90, 250, 28), automation_id="amount"),
        "post": win.add_button("Post", (120, 340, 80, 30)),
        "toolbar": toolbar,
        "pay": toolbar.add_button("Pay now", (120, 210, 100, 40)),
        "win": win,
    }
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, w


def _recorder():
    asked = []
    return asked, (lambda desc, details: asked.append(desc) or False)


def _keys_sent(be):
    return [e for e in be.events if e and e[0] in ("key", "press", "key_down", "hotkey")]


# ---------------------------------------------------------------- keys under a selector rule


@pytest.mark.parametrize("combo", ["alt+f4", "f8", "escape", "alt+p", "ctrl+enter", "ctrl+shift+s"])
def test_selector_rule_for_a_field_does_not_authorize_shortcuts(app, combo):
    be, d, w = app
    asked, confirm = _recorder()
    g = GuardedDesktop(d, Policy(allow=["edit#amount"], confirm_patterns=(), confirm_keys=(), confirm=confirm))
    g.find("edit#amount").focus()
    be.events.clear()
    with pytest.raises(PolicyViolation, match="declined"):
        g.press(combo)
    assert len(asked) == 1 and be.events == []


def test_selector_rule_for_a_field_still_authorizes_editing_it(app):
    be, d, w = app
    asked, confirm = _recorder()
    g = GuardedDesktop(d, Policy(allow=["edit#amount"], confirm_patterns=(), confirm=confirm))
    field = g.find("edit#amount")
    field.type("120")
    g.press("backspace")
    g.press("ctrl+a")
    g.hotkey("ctrl", "v")
    g.press("shift+tab", "tab")  # navigation is never gated
    field.press("enter")  # Enter on the authorized field itself (no default button here)
    assert asked == []


def test_keys_listed_by_the_rule_are_authorized(app):
    be, d, w = app
    asked, confirm = _recorder()
    rule = {"selector": "edit#amount", "keys": "f8"}
    g = GuardedDesktop(d, Policy(allow=[rule], confirm_patterns=(), confirm=confirm))
    g.find("edit#amount").focus()
    g.press("f8")
    assert asked == []
    with pytest.raises(PolicyViolation):
        g.press("f9")
    assert len(asked) == 1


def test_toolkit_applies_the_same_key_scope(app):
    be, d, w = app
    asked, confirm = _recorder()
    tk = DesktopToolkit(d, Policy(allow=["edit#amount"], confirm_patterns=(), confirm_keys=(), confirm=confirm),
                        AuditLog(), find_timeout=0.1)
    assert not tk.call("click", {"selector": "edit#amount"}).is_error
    assert not tk.call("hotkey", {"combo": "ctrl+a"}).is_error
    assert asked == []
    assert tk.call("press_keys", {"keys": ["alt+f4"]}).is_error
    assert tk.call("hotkey", {"combo": "f8"}).is_error
    assert len(asked) == 2


def test_characters_in_a_non_text_control_need_a_keys_rule():
    class _Node:
        role = "button"
        states = ()

    assert combo_acts_on_target(frozenset({"enter"}), _Node())
    assert combo_acts_on_target(frozenset({"space"}), _Node())
    assert not combo_acts_on_target(frozenset({"p"}), _Node())  # may be an access key in a dialog
    assert not combo_acts_on_target(frozenset({"delete"}), _Node())  # deletes a selected row
    assert not combo_acts_on_target(frozenset({"ctrl", "enter"}), _Node())


# ---------------------------------------------------------------- the control actually pressed


def test_rule_on_a_container_does_not_authorize_the_child_button_clicked(app):
    be, d, w = app
    asked, confirm = _recorder()
    policy = Policy(allow=["pane#toolbar"], confirm_patterns=(), confirm=confirm)
    g = GuardedDesktop(d, policy)
    with pytest.raises(PolicyViolation, match="declined"):
        g.click("pane#toolbar")
    with pytest.raises(PolicyViolation, match="declined"):
        g.find("pane#toolbar").click()
    tk = DesktopToolkit(d, Policy(allow=["pane#toolbar"], confirm_patterns=(), confirm=confirm), AuditLog(),
                        find_timeout=0.1)
    assert tk.call("click", {"selector": "pane#toolbar"}).is_error
    assert w["pay"].clicks == 0 and len(asked) == 3


def test_rule_on_the_child_button_authorizes_the_click(app):
    be, d, w = app
    asked, confirm = _recorder()
    g = GuardedDesktop(d, Policy(allow=['button "Pay now"'], confirm_patterns=(), confirm=confirm))
    g.click("pane#toolbar")
    assert w["pay"].clicks == 1 and asked == []


def test_decorative_child_still_presses_the_container():
    be = _Backend(width=800, height=600)
    win = be.add_window("ERP", (0, 0, 400, 300), app="erp")
    save = win.add_button("Save", (20, 20, 120, 40))
    save.add("image", "", (60, 30, 20, 20))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    asked, confirm = _recorder()
    g = GuardedDesktop(d, Policy(allow=['button "Save"'], confirm_patterns=(), confirm=confirm))
    g.find('button "Save"').click()
    assert save.clicks == 1 and asked == []


def test_pressed_control_helper():
    class N:
        def __init__(self, role, parent=None):
            self.role, self.parent = role, parent
            self.name, self.value, self.states = "", None, ()

        def actions(self):
            return ()

    win = N("window")
    pane = N("pane", win)
    button = N("button", pane)
    icon = N("image", button)
    assert pressed_control(icon, pane) is button
    assert pressed_control(icon, button) is button
    assert pressed_control(icon) is button
    lone = N("text", pane)
    assert pressed_control(lone, pane) is pane
    assert pressed_control(lone) is lone


# ---------------------------------------------------------------- refusals inside flow containers


def test_flow_container_with_retries_does_not_ask_again(app):
    be, d, w = app
    asked, confirm = _recorder()
    flow = {"steps": [{"for_each": {"items": [1], "steps": [{"click": 'button "Post"'}]}, "retries": 3,
                       "retry_delay": 0}]}
    report = run_flow(flow, d, policy=Policy(allow=[]), confirm=confirm)
    assert not report.ok
    assert asked == ["click Post"] and w["post"].clicks == 0  # asked once, not once per attempt
    assert not any(s.data.get("retry_errors") for s in report.steps)


def test_nested_refusal_detection():
    from pypilot.rpa.flow import _is_refusal

    inner = FlowStepError("inner", original=PolicyViolation("declined"))
    assert _is_refusal(FlowStepError("outer", original=inner))
    assert not _is_refusal(FlowStepError("outer", original=FlowStepError("inner", original=ValueError())))


def test_robot_does_not_retry_a_wrapped_refusal(app, tmp_path):
    be, d, w = app
    asked, confirm = _recorder()
    robot = Robot(d, run_dir=tmp_path, save_reports=False, screenshot_on_error=False, policy=Policy(allow=[]),
                  confirm=confirm)

    @robot.task(retries=3, retry_delay=0)
    def post():
        try:
            robot.desktop.click('button "Post"')
        except PolicyViolation as exc:
            raise FlowStepError("step failed", original=exc) from exc

    report = robot.run()
    assert not report.ok
    task = next(s for s in report.steps if s.kind == "task")
    assert task.attempts == 1 and len(asked) == 1 and w["post"].clicks == 0


# ---------------------------------------------------------------- bounded identity search


class _Huge:
    """A nameless container with many children, counting how many are fetched."""

    def __init__(self, n=10000):
        self.n, self.fetched = n, 0
        self.role, self.name, self.value, self.automation_id, self.parent, self.states = "pane", "", None, "", None, ()

    def actions(self):
        return ()

    def children(self):
        for _ in range(self.n):
            self.fetched += 1
            yield _Leaf(self)


class _Leaf:
    def __init__(self, parent):
        self.parent, self.role, self.name, self.value, self.automation_id, self.states = parent, "text", "", None, "", ()
        self.bounds = None

    def actions(self):
        return ()

    def children(self):
        return []


def test_identity_search_fetches_a_bounded_number_of_children():
    from pypilot.types import Point

    huge = _Huge()
    assert target_identity(huge, Point(5, 5)) == ""
    assert huge.fetched <= 64


def test_identity_search_uses_one_hit_test_when_a_backend_is_given():
    from pypilot.types import Point

    huge = _Huge()
    named = _Leaf(huge)
    named.name = "Pay now"

    class _BE:
        calls = 0

        def element_at(self, x, y):
            _BE.calls += 1
            return named

    assert target_identity(huge, Point(5, 5), _BE()) == "Pay now"
    assert huge.fetched == 0 and _BE.calls == 1
