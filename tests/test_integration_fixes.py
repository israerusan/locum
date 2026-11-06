"""Regression tests for cross-module integration fixes.

Several backends do not give descendant accessibility nodes the top-level
window handle (AT-SPI resolves handles only for window/dialog roles, UIA
returns child HWNDs or nothing) nor an ``app`` attribute. The
``foreign_nodes`` fixture makes MockNode behave that way, so the
window/app resolution used by the LLM policy and Element is exercised the way
it runs on X11/Windows.
"""

from __future__ import annotations

import pytest

from locum._nodeinfo import ancestors, node_app, owning_window, window_at
from locum.backends.mock import MockBackend, MockNode
from locum.desktop import Desktop
from locum.element import add_action_listener, remove_action_listener
from locum.errors import ElementNotFound
from locum.llm.agent import Agent
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.safety import Policy, keys_activate
from locum.llm.tools import DesktopToolkit
from locum.rpa.flow import run_flow
from locum.rpa.recorder import Recorder
from locum.types import Point


@pytest.fixture
def foreign_nodes(monkeypatch):
    """MockNode with X11/UIA semantics: handle only on window roots, no 'app' attribute."""
    monkeypatch.setattr(MockNode, "attributes", lambda self: dict(self.widget.attrs))
    monkeypatch.setattr(
        MockNode,
        "handle",
        property(lambda self: self.widget.window.handle if self.widget.window and self.role in ("window", "dialog") else None),
    )


def overlapping():
    be = MockBackend()
    back = be.add_window("Back", (100, 100, 400, 300), app="back", pid=11)
    target = back.add_button("Target", (10, 10, 80, 30))
    front = be.add_window("Front", (100, 100, 400, 300), app="front", pid=22)  # covers Back completely
    front.add_button("Danger", (10, 10, 80, 30))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0, failsafe=False)
    return be, d, back, front, target


def payment_form():
    be = MockBackend()
    paid = []
    win = be.add_window("Pay", (100, 100, 500, 300), app="erp")
    win.add_edit("Amount", (10, 10, 200, 30), value="5")
    win.add_button("Submit Payment", (10, 60, 160, 30), on_click=lambda w: paid.append(True))
    other = be.add_window("Notes", (700, 100, 400, 300), app="notepad", activate=False)
    other.add_edit("Text Editor", (10, 10, 300, 200))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0, failsafe=False)
    return be, d, paid


def _focus(d, selector):
    d.find(selector).node.focus()
    assert d.focused().name == selector.split('"')[1]


# ======================================================================== _nodeinfo


@pytest.mark.parametrize("foreign", [False, True])
def test_owning_window_and_node_app(request, foreign):
    if foreign:
        request.getfixturevalue("foreign_nodes")
    be, d, back, front, target = overlapping()
    node = d.find('window "Back" button "Target"').node
    w = owning_window(be, node)
    assert w is not None and w.title == "Back"
    assert node_app(be, node) == "back"
    assert [a.role for a in ancestors(node)][-1] == "window"
    assert window_at(be, Point(150, 150)).title == "Front"
    assert window_at(be, Point(5000, 5000)) is None
    assert owning_window(be, None) is None and node_app(be, None) == ""


# ============================================================ finding: keyboard bypass


def test_toolkit_enter_on_focused_control_needs_confirmation():
    be, d, paid = payment_form()
    asked = []
    kit = DesktopToolkit(d, policy=Policy(confirm=lambda desc, det: asked.append((desc, det)) or False), find_timeout=0)
    assert "declined" in kit.call("click", {"selector": 'button "Submit Payment"'}).text
    _focus(d, 'button "Submit Payment"')
    r = kit.call("press_keys", {"keys": ["enter"]})
    assert r.is_error and "declined" in r.text and paid == []
    assert "Submit Payment" in asked[-1][0] and asked[-1][1]["target"] == "Submit Payment"
    for tool, args in (("hotkey", {"combo": "ctrl+enter"}), ("press_keys", {"keys": ["space"]}),
                       ("type_text", {"text": " "})):
        assert "declined" in kit.call(tool, args).text, tool
    assert paid == []
    # Navigation keys do not activate: no confirmation, but the app is still reported.
    n = len(asked)
    assert not kit.call("press_keys", {"keys": ["tab"]}).is_error
    assert len(asked) == n


def test_toolkit_keyboard_respects_allowed_apps_of_focused_element():
    be, d, paid = payment_form()
    kit = DesktopToolkit(d, policy=Policy.permissive(allowed_apps=["notepad"]), find_timeout=0)
    _focus(d, 'button "Submit Payment"')
    # The active window is "Pay" (erp): typing/pressing there must be blocked.
    assert "not allowed" in kit.call("press_keys", {"keys": ["enter"]}).text
    assert "not allowed" in kit.call("type_text", {"text": "x"}).text
    assert paid == []


def test_computer_use_return_and_typed_newline_need_confirmation():
    be, d, paid = payment_form()
    asked = []
    cu = ComputerUseAdapter(d, policy=Policy(confirm=lambda desc, det: asked.append(det) or False))
    _focus(d, 'button "Submit Payment"')
    for action, params in (("key", {"text": "Return"}), ("key", {"text": "KP_Enter"}), ("type", {"text": "ok\n"})):
        r = cu.execute(action, params)
        assert r.is_error and "declined" in r.text, action
        assert asked[-1]["target"] == "Submit Payment" and asked[-1]["target_app"] == "erp"
    assert paid == []
    n = len(asked)
    assert not cu.execute("key", {"text": "Tab"}).is_error and len(asked) == n


def test_keys_activate():
    assert keys_activate(["enter"]) and keys_activate(["ctrl+Return"]) and keys_activate([["space"]])
    assert keys_activate(["a\nb"]) and keys_activate([["num_enter"]])
    assert not keys_activate(["tab", "ctrl+s", "down"]) and not keys_activate([])


# ========================================================= finding: app of descendants


def test_policy_resolves_app_of_descendants_with_foreign_handles(foreign_nodes):
    be, d, back, front, target = overlapping()
    kit = DesktopToolkit(d, policy=Policy.permissive(allowed_apps=["front"]), find_timeout=0)
    r = kit.call("click", {"selector": 'window "Back" button "Target"'})
    assert r.is_error and "'back' is not allowed" in r.text
    kit2 = DesktopToolkit(d, policy=Policy.permissive(blocked_apps=["front"]), find_timeout=0)
    assert "blocked" in kit2.call("click", {"x": 150, "y": 150}).text  # coordinate on Front's button
    cu = ComputerUseAdapter(d, policy=Policy.permissive(blocked_apps=["front"]))
    x, y = cu.to_image(150, 150)
    assert "blocked" in cu.execute("left_click", {"coordinate": [x, y]}).text
    assert target.attrs.get("clicked") is None


def test_point_without_accessible_falls_back_to_window_rect():
    be = MockBackend()
    be.add_window("Blank", (100, 100, 300, 300), app="paint")  # no widgets
    d = Desktop(be, sleep=lambda s: None, default_timeout=0, failsafe=False)
    cu = ComputerUseAdapter(d, policy=Policy.permissive(blocked_apps=["paint"]))
    x, y = cu.to_image(200, 200)
    assert "blocked" in cu.execute("left_click", {"coordinate": [x, y]}).text


# ============================================================== finding: drag endpoints


def test_drag_checks_both_endpoint_apps():
    be, d, paid = payment_form()
    kit = DesktopToolkit(d, policy=Policy.permissive(allowed_apps=["erp"]), find_timeout=0)
    erp_pt, note_pt = {"x": 150, "y": 125}, {"x": 900, "y": 200}
    for s, e in ((erp_pt, note_pt), (note_pt, erp_pt)):
        r = kit.call("drag", {"start_x": s["x"], "start_y": s["y"], "end_x": e["x"], "end_y": e["y"]})
        assert r.is_error and "'notepad' is not allowed" in r.text
    cu = ComputerUseAdapter(d, policy=Policy.permissive(allowed_apps=["erp"]))
    a, b = list(cu.to_image(900, 200)), list(cu.to_image(150, 125))
    r = cu.execute("left_click_drag", {"start_coordinate": a, "coordinate": b})  # notepad -> erp
    assert r.is_error and "notepad" in r.text
    ok = DesktopToolkit(d, policy=Policy.permissive(allowed_apps=["erp", "notepad"]), find_timeout=0)
    assert not ok.call("drag", {"start_x": 150, "start_y": 125, "end_x": 900, "end_y": 200}).is_error


def test_policy_target_apps_context():
    p = Policy.permissive(allowed_apps=["a"], blocked_apps=["z"])
    p.check("click", {}, context={"app": "a", "target_apps": ["a"]})
    with pytest.raises(Exception, match="'b' is not allowed"):
        p.check("click", {}, context={"app": "a", "target_app": "a", "target_apps": ["a", "b"]})
    with pytest.raises(Exception, match="blocked"):
        Policy.permissive(blocked_apps=["z"]).check("click", {}, context={"target_apps": ["x", "z"]})


# =============================================================== finding: agent policy


def test_agent_policy_rebinds_prepared_toolkit_and_computer():
    from locum.llm import ScriptedClient, computer_block, scripted_response, tool_use_block

    be, d, paid = payment_form()
    kit = DesktopToolkit(d, find_timeout=0)
    client = ScriptedClient([scripted_response(tool_use_block("set_value", {"selector": 'edit "Amount"', "value": "999"})),
                             scripted_response(tool_use_block("done", {"summary": "ok"}))])
    pol = Policy(dry_run=True)
    ag = Agent(d, client=client, toolkit=kit, policy=pol, initial_observation=False)
    ag.run("x")
    assert d.find('edit "Amount"').value == "5"
    assert kit.policy is pol and "DRY-RUN" in client.requests[0]["system"]

    cu = ComputerUseAdapter(d)
    x, y = cu.to_image(150, 175)
    client = ScriptedClient([scripted_response(computer_block("left_click", {"coordinate": [x, y]})),
                             scripted_response(tool_use_block("done", {"summary": "ok"}))])
    ag = Agent(d, client=client, mode="computer_use", computer=cu, policy=Policy(dry_run=True), initial_observation=False)
    ag.run("x")
    assert paid == [] and cu.policy is ag.policy and cu.audit is ag.audit

    # Without an explicit policy the executing object's policy is adopted (and reported).
    cu2 = ComputerUseAdapter(d, policy=Policy(dry_run=True))
    ag2 = Agent(d, client=ScriptedClient([]), mode="computer_use", computer=cu2, toolkit=DesktopToolkit(d))
    assert ag2.policy is cu2.policy and ag2.toolkit.policy is cu2.policy and "DRY-RUN" in ag2.system()


# ===================================================== finding: occluded click fallback


def test_occluded_double_click_activates_owning_window(foreign_nodes):
    be, d, back, front, target = overlapping()
    d.double_click('window "Back" button "Target"')
    assert d.active_window().title == "Back"
    downs = [e for e in be.events if e[0] == "down"]
    assert be.element_at(*downs[-1][2:4]).name == "Target"
    assert d.find('button "Target"').window.title == "Back"


def test_occluded_click_refuses_when_window_cannot_be_raised(foreign_nodes, monkeypatch):
    be, d, back, front, target = overlapping()
    monkeypatch.setattr(be, "activate_window", lambda h: None)  # window manager ignores the request
    el = d.find('window "Back" button "Target"')
    be.clear_events()
    with pytest.raises(ElementNotFound, match="covered"):
        el.click(clicks=2)
    assert not [e for e in be.events if e[0] == "down"]


# ======================================================================= finding: clock


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(float(s), 0.001)


def test_scoped_waits_use_desktop_clock_and_interval():
    import time

    be, d0, paid = payment_form()
    clock = _FakeClock()
    d = Desktop(be, sleep=clock.sleep, clock=clock, poll_interval=0.5, failsafe=False)
    win = d.window("Pay")
    el = d.find('edit "Amount"')
    started = time.monotonic()
    for call in (lambda: win.find('button "Nope"', timeout=3), lambda: el.find("button", timeout=3),
                 lambda: d.find('button "Nope"', root=win, timeout=3)):
        with pytest.raises(ElementNotFound):
            call()
    with pytest.raises(Exception):
        el.wait_until("gone", timeout=3)
    with pytest.raises(Exception):
        win.wait_closed(timeout=3)
    assert time.monotonic() - started < 1.0
    assert 14.0 <= clock.t <= 17.0  # five 3s waits on the fake clock at 0.5s steps


# ==================================================================== finding: recorder


def test_recorder_captures_native_actions_and_replays():
    def demo():
        be = MockBackend()
        saved = []
        win = be.add_window("Form", (100, 100, 500, 400), app="erp")
        win.add_edit("Vendor", (10, 10, 200, 30), automation_id="vendor")
        win.add_checkbox("Urgent", (10, 50, 100, 20), automation_id="urgent")
        win.add_button("Save", (10, 90, 80, 30), automation_id="save",
                       on_click=lambda w: saved.append((win.find("Vendor").value, win.find("Urgent").checked)))
        return be, Desktop(be, sleep=lambda s: None, default_timeout=0, failsafe=False), saved

    be, d, saved = demo()
    rec = Recorder(d).start(source="backend")
    d.find("edit#vendor").set_text("ACME")
    d.find("checkbox#urgent").toggle()
    d.find("button#save").invoke()
    actions = rec.stop()
    assert [a.kind for a in actions] == ["set_text", "toggle", "invoke"]
    assert saved == [("ACME", True)]
    assert ".set_text('ACME')" in rec.to_python() and ".invoke()" in rec.to_python()

    be2, d2, saved2 = demo()
    report = run_flow(rec.to_flow(), d2)
    assert report.ok and saved2 == [("ACME", True)]
    # listener is gone after stop
    d.find("edit#vendor").set_text("later")
    assert len(rec.actions) == 3


def test_action_listener_registry():
    be, d, paid = payment_form()
    seen = []

    def listener(kind, el, data):
        seen.append((kind, el.name, data))

    add_action_listener(be, listener)
    d.find('edit "Amount"').set_text("7")
    d.find('button "Submit Payment"').invoke()
    remove_action_listener(be, listener)
    remove_action_listener(be, listener)  # idempotent
    d.find('edit "Amount"').set_text("8")
    assert [(k, n, {f: v for f, v in data.items() if f != "verification"}) for k, n, data in seen] == [
        ("set_text", "Amount", {"text": "7"}), ("invoke", "Submit Payment", {})]
    # the native set_text reports how its read-back went
    assert seen[0][2]["verification"].status == "verified"


# ================================================================ finding: image paths


def test_missing_image_path_is_reported_not_parsed_as_selector(tmp_path):
    be, d, paid = payment_form()
    for target in ("icons/ok_button.png", "ok.png", str(tmp_path / "nope.jpg")):
        with pytest.raises(FileNotFoundError, match="not found"):
            d.click(target)
    with pytest.raises(ElementNotFound):  # ordinary selectors are untouched
        d.click('button "Missing"')
