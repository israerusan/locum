"""Activating input on a control with no usable identity is an unknown target (default policy)."""

from __future__ import annotations

import pytest

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.safety import UNKNOWN_TARGET_REASON, AuditLog, Policy, target_identity
from locum.llm.tools import DesktopToolkit


def _fake_time():
    now = [0.0]

    def sleep(seconds):
        now[0] += max(0.0, float(seconds))

    return {"sleep": sleep, "clock": lambda: now[0]}


@pytest.fixture
def app():
    be = MockBackend(width=1280, height=800)
    d = Desktop(be, failsafe=False, default_timeout=0.2, **_fake_time())
    win = be.add_window("Invoice", (100, 100, 600, 400), app="erp")
    w = {
        "ok": win.add_button("OK", (20, 340, 80, 30)),
        "icon": win.add_button("", (120, 340, 30, 30)),  # icon-only: no name, no id, no value
        "icon_id": win.add_button("", (170, 340, 30, 30), automation_id="btnRefresh"),
        "labelled": win.add_button("", (220, 330, 120, 50)),
        "blank": win.add_edit("", (20, 50, 250, 28)),
        "amount": win.add_edit("Amount", (20, 90, 250, 28)),
    }
    # A nameless button whose visible caption is a child text under the click point.
    w["labelled"].add("text", "Continue", (230, 340, 100, 30), value="Continue")
    return be, d, w


def _center(widget):
    return widget.screen_rect.center


def _toolkit(d, **policy_kw):
    asked = []
    policy = Policy(confirm=lambda desc, details: asked.append(details["reason"]) or False, **policy_kw)
    return DesktopToolkit(d, policy, AuditLog(), find_timeout=0.1), asked


def test_named_non_sensitive_button_runs_without_confirmation(app):
    be, d, w = app
    tk, asked = _toolkit(d)
    c = _center(w["ok"])
    r = tk.call("click", {"x": c.x, "y": c.y})
    assert not r.is_error, r.text
    assert w["ok"].clicks == 1 and asked == []
    r = tk.call("click", {"selector": 'button "OK"'})
    assert not r.is_error and w["ok"].clicks == 2 and asked == []


def test_coordinate_click_on_nameless_button_needs_confirmation(app):
    be, d, w = app
    tk, asked = _toolkit(d)
    c = _center(w["icon"])
    r = tk.call("click", {"x": c.x, "y": c.y})
    assert r.is_error and "declined" in r.text
    assert asked == [UNKNOWN_TARGET_REASON]
    assert w["icon"].clicks == 0


def test_nameless_button_blocked_without_confirm_callback(app):
    be, d, w = app
    tk = DesktopToolkit(d, Policy(), AuditLog(), find_timeout=0.1)
    c = _center(w["icon"])
    r = tk.call("click", {"x": c.x, "y": c.y})
    assert r.is_error and "no confirm callback" in r.text
    assert w["icon"].clicks == 0


def test_selector_text_is_not_an_identity(app):
    be, d, w = app
    tk, asked = _toolkit(d)
    r = tk.call("click", {"selector": "button:nth(1)"})  # the icon-only button, reached by position
    assert r.is_error and asked == [UNKNOWN_TARGET_REASON]
    assert w["icon"].clicks == 0


def test_automation_id_counts_as_identity(app):
    be, d, w = app
    tk, asked = _toolkit(d)
    c = _center(w["icon_id"])
    r = tk.call("click", {"x": c.x, "y": c.y})
    assert not r.is_error and asked == [] and w["icon_id"].clicks == 1


def test_named_descendant_at_click_point_counts_as_identity(app):
    be, d, w = app
    tk, asked = _toolkit(d)
    r = tk.call("click", {"selector": "button:nth(3)"})  # nameless, its caption child sits under the centre
    assert not r.is_error, r.text
    assert asked == []
    assert any(e[0] == "down" for e in be.events)  # the mouse went down on it
    node = d.find("button:nth(3)").node
    assert target_identity(node, _center(w["labelled"])) == "Continue"
    assert target_identity(node, None) == ""


def test_identity_heuristic_still_catches_keywords(app):
    be, d, w = app
    w["labelled"].children[0].name = w["labelled"].children[0].value = "Submit"
    tk, asked = _toolkit(d)
    r = tk.call("click", {"selector": "button:nth(3)"})
    assert r.is_error and asked and "Submit" in asked[0]


def test_enter_on_focused_nameless_button_needs_confirmation(app):
    be, d, w = app
    d.find("button:nth(1)").focus()
    tk, asked = _toolkit(d)
    r = tk.call("press_keys", {"keys": ["enter"]})
    assert r.is_error and asked == [UNKNOWN_TARGET_REASON]
    r = tk.call("hotkey", {"combo": "space"})
    assert r.is_error and len(asked) == 2
    assert w["icon"].clicks == 0


def test_typing_into_focused_nameless_field_is_editing_not_activation(app):
    be, d, w = app
    d.find("edit:nth(0)").focus()
    tk, asked = _toolkit(d)
    r = tk.call("type_text", {"text": "hello world"})
    assert not r.is_error, r.text
    assert asked == [] and w["blank"].value == "hello world"


def test_type_text_into_nameless_field_by_selector_needs_confirmation(app):
    be, d, w = app
    tk, asked = _toolkit(d)
    r = tk.call("type_text", {"selector": "edit:nth(0)", "text": "100"})
    assert r.is_error and asked == [UNKNOWN_TARGET_REASON]
    r = tk.call("set_value", {"selector": "edit:nth(0)", "value": "100"})
    assert r.is_error and len(asked) == 2
    assert w["blank"].value == ""
    r = tk.call("type_text", {"selector": 'edit "Amount"', "text": "100"})
    assert not r.is_error and w["amount"].value == "100"


def test_unknown_target_allow_and_permissive_let_nameless_clicks_run(app):
    be, d, w = app
    c = _center(w["icon"])
    tk = DesktopToolkit(d, Policy(unknown_target="allow"), AuditLog(), find_timeout=0.1)
    assert not tk.call("click", {"x": c.x, "y": c.y}).is_error
    tk = DesktopToolkit(d, Policy.permissive(), AuditLog(), find_timeout=0.1)
    assert not tk.call("click", {"x": c.x, "y": c.y}).is_error
    assert w["icon"].clicks == 2


def test_unknown_target_block_refuses_nameless_click(app):
    be, d, w = app
    c = _center(w["icon"])
    tk = DesktopToolkit(d, Policy(unknown_target="block", confirm=lambda *a: True), AuditLog(), find_timeout=0.1)
    r = tk.call("click", {"x": c.x, "y": c.y})
    assert r.is_error and "unknown_target='block'" in r.text
    assert w["icon"].clicks == 0


def test_right_click_and_scroll_on_nameless_control_are_not_activations(app):
    be, d, w = app
    tk, asked = _toolkit(d)
    c = _center(w["icon"])
    assert not tk.call("right_click", {"x": c.x, "y": c.y}).is_error
    assert not tk.call("move_mouse", {"x": c.x, "y": c.y}).is_error
    assert asked == []


# ---------------------------------------------------------------------- computer use


def _cu(d, **policy_kw):
    asked = []
    policy = Policy(confirm=lambda desc, details: asked.append(details["reason"]) or False, **policy_kw)
    return ComputerUseAdapter(d, policy=policy), asked


def test_computer_use_click_on_nameless_button_needs_confirmation(app):
    be, d, w = app
    cu, asked = _cu(d)
    p = cu.to_image(*_center(w["icon"]))
    r = cu.execute("left_click", {"coordinate": [p.x, p.y]})
    assert r.is_error and asked == [UNKNOWN_TARGET_REASON] and w["icon"].clicks == 0
    p = cu.to_image(*_center(w["ok"]))
    r = cu.execute("left_click", {"coordinate": [p.x, p.y]})
    assert not r.is_error and w["ok"].clicks == 1 and len(asked) == 1


def test_computer_use_enter_on_focused_nameless_button_needs_confirmation(app):
    be, d, w = app
    d.find("button:nth(1)").focus()
    cu, asked = _cu(d)
    r = cu.execute("key", {"text": "Return"})
    assert r.is_error and asked == [UNKNOWN_TARGET_REASON]
    d.find("edit:nth(0)").focus()
    r = cu.execute("type", {"text": "a b"})
    assert not r.is_error and w["blank"].value == "a b"
