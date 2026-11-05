"""RPA (Robot, flows, plain desktop facade) behind the same Policy as the LLM tools."""

from __future__ import annotations

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.element import Element
from pypilot.errors import PolicyViolation
from pypilot.llm.safety import AuditLog, GuardedDesktop, GuardedElement, GuardedWindow, Policy
from pypilot.rpa import FlowRunner, Robot, RunReport, run_flow
from pypilot.rpa.report import FAILED


class _Backend(MockBackend):
    """Like UIA/AT-SPI, the focused element knows its parents (the mock's plain node does not)."""

    def focused_element(self):
        return self._node_with_parents(self._focus)


@pytest.fixture
def app():
    be = _Backend(width=1280, height=800)
    notes = be.add_window("Notes", (700, 100, 400, 300), app="notepad")
    notes.add_edit("Text", (10, 40, 380, 200))
    win = be.add_window("Invoice 42", (100, 100, 500, 400), app="erp")
    w = {
        "ok": win.add_button("OK", (20, 340, 80, 30)),
        "post": win.add_button("Post", (120, 340, 80, 30)),
        "apply": win.add_button("Apply", (220, 340, 80, 30)),
        "icon": win.add_button("", (320, 340, 30, 30)),
        "agree": win.add_checkbox("Agree", (20, 140, 150, 24)),
        "amount": win.add_edit("Amount", (20, 90, 250, 28), automation_id="amount"),
        "win": win,
    }
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, w


def _guard(d, allow=None, **kw):
    asked = []
    kw.setdefault("confirm", lambda desc, details: asked.append(details["reason"]) or False)
    return GuardedDesktop(d, Policy(allow=allow, **kw)), asked


# ------------------------------------------------------------------ guarded desktop


def test_desktop_click_is_checked_before_acting(app):
    be, d, w = app
    g, asked = _guard(d, ['window "Invoice 42" button "OK"'])
    g.click('button "OK"')
    assert w["ok"].clicks == 1
    with pytest.raises(PolicyViolation, match="declined"):
        g.click('button "Apply"')
    assert w["apply"].clicks == 0 and asked == ["no allow rule authorizes 'click' on 'Apply'"]
    c = w["post"].screen_rect.center
    with pytest.raises(PolicyViolation):
        g.click(c.x, c.y)  # coordinates are hit-tested and judged too
    assert w["post"].clicks == 0


def test_default_policy_gates_keywords_and_nameless_controls(app):
    be, d, w = app
    g, asked = _guard(d)
    g.click('button "OK"')
    g.find('edit "Amount"').type("100")
    assert w["amount"].value == "100" and asked == []
    with pytest.raises(PolicyViolation):
        g.click('button "Post"')  # keyword tripwire
    with pytest.raises(PolicyViolation):
        g.click("button:nth(3)")  # icon-only button: unknown target
    assert w["post"].clicks == 0 and w["icon"].clicks == 0
    assert asked[0].startswith("'Post'") and asked[1] == "the control this input reaches cannot be identified"


def test_elements_and_windows_reached_through_find_are_guarded(app):
    be, d, w = app
    g, asked = _guard(d, ['button "OK"'])
    el = g.find('button "Apply"')
    assert isinstance(el, GuardedElement) and isinstance(el.unwrapped, Element)
    assert el.name == "Apply" and el == d.find('button "Apply"')
    for action in (el.click, el.invoke, el.double_click):
        with pytest.raises(PolicyViolation):
            action()
    assert w["apply"].clicks == 0
    assert all(isinstance(e, GuardedElement) for e in g.find_all("button"))
    win = g.window("Invoice 42")
    assert isinstance(win, GuardedWindow)
    with pytest.raises(PolicyViolation):
        win.find('checkbox "Agree"').toggle()
    assert w["agree"].checked is False
    win.activate()  # focusing a window commits nothing
    with pytest.raises(PolicyViolation):
        win.close()
    assert any(x.title == "Invoice 42" for x in d.windows())
    assert isinstance(g.find('button "OK"').parent, GuardedElement)


def test_guarded_elements_can_be_passed_back_to_the_desktop(app):
    be, d, w = app
    g, asked = _guard(d, ['button "OK"'])
    ok = g.find('button "OK"')
    g.click(ok)
    g.move(g.find('button "Apply"'))  # hovering is not consequential
    assert w["ok"].clicks == 1 and asked == []
    g.screenshot(region=g.find('button "OK"'))  # read-only passthrough unwraps arguments


def test_text_entry_keys_launch_and_clipboard(app):
    be, d, w = app
    g, asked = _guard(d, ["edit#amount", "keys=ctrl+a", "launch=notepad"], confirm_keys=())
    g.find("edit#amount").set_text("12.50")
    assert w["amount"].value == "12.50"
    g.find("edit#amount").focus()
    g.hotkey("ctrl+a")
    g.press("tab", "shift+tab")  # navigation only
    g.find('button "Apply"').focus()
    with pytest.raises(PolicyViolation):
        g.press("enter")
    assert w["apply"].clicks == 0
    g.launch("notepad.exe")
    with pytest.raises(PolicyViolation):
        g.launch("powershell -c whoami")
    with pytest.raises(PolicyViolation):
        g.clipboard_set("4111")
    assert be.launched == ["notepad.exe"] and be.clipboard == ""
    assert g.clipboard_get() == ""  # read-only


def test_typing_after_tab_is_rechecked_on_the_new_focus(app):
    be, d, w = app
    d.find("edit#amount").focus()
    g, asked = _guard(d, ["edit#amount"])
    with pytest.raises(PolicyViolation, match="already sent"):
        g.type("100\t\n")  # the Tab moves on; the Enter would land elsewhere
    assert w["amount"].value == "100"


def test_mouse_and_keyboard_objects_are_guarded(app):
    be, d, w = app
    g, asked = _guard(d, [])
    c = w["ok"].screen_rect.center
    with pytest.raises(PolicyViolation):
        g.mouse.click(c)
    with pytest.raises(PolicyViolation):
        g.keyboard.type("x")
    with pytest.raises(PolicyViolation):
        g.keyboard.press("enter")
    g.mouse.move(c)  # not consequential
    assert w["ok"].clicks == 0
    assert g.mouse.position() == c


def test_dry_run_skips_actions_and_audits_decisions(app):
    be, d, w = app
    audit = AuditLog()
    g = GuardedDesktop(d, Policy(allow=['button "OK"'], dry_run=True), audit=audit)
    g.click('button "OK"')
    g.click('button "Apply"')  # would need confirmation; dry-run reports it
    assert w["ok"].clicks == 0 and w["apply"].clicks == 0
    statuses = [(e["tool"], e["status"]) for e in audit.events]
    assert statuses == [("click", "dry_run"), ("click", "dry_run")]
    assert "allowed_by" in audit.events[0] and "needs_confirmation" in audit.events[1]


def test_audit_records_allowed_and_blocked_actions(app):
    be, d, w = app
    audit = AuditLog()
    g = GuardedDesktop(d, Policy(allow=["edit#amount"]), audit=audit)
    g.find("edit#amount").type("7")
    with pytest.raises(PolicyViolation):
        g.click('button "Apply"')
    assert [e["status"] for e in audit.events] == ["allowed", "blocked"]
    assert audit.events[0]["allowed_by"] == "AllowRule(selector='edit#amount')"
    assert "no confirm callback" in audit.events[1]["error"]


def test_confirm_argument_overrides_policy_callback(app):
    be, d, w = app
    asked = []
    g = GuardedDesktop(d, Policy(allow=[]), confirm=lambda desc, details: asked.append(desc) or True)
    g.click('button "Apply"')
    assert w["apply"].clicks == 1 and asked == ["click Apply"]


def test_guard_does_not_stack(app):
    be, d, w = app
    g = GuardedDesktop(GuardedDesktop(d, Policy()), Policy(allow=[]))
    assert g.unwrapped is d
    with pytest.raises(TypeError):
        GuardedDesktop(d, policy="strict")


# ------------------------------------------------------------------------- robot


def test_robot_without_policy_is_ungated(app, tmp_path):
    be, d, w = app
    r = Robot(d, run_dir=tmp_path, save_reports=False)
    assert r.desktop is d and r.policy is None and r.audit is None
    r.desktop.click('button "Post"')
    assert w["post"].clicks == 1


def test_robot_with_policy_gates_its_desktop_and_never_retries_refusals(app, tmp_path):
    be, d, w = app
    asked = []
    r = Robot(d, run_dir=tmp_path, save_reports=False, screenshot_on_error=False,
              policy=Policy.strict(allow=['button "OK"']), confirm=lambda desc, x: asked.append(desc) or False)
    assert isinstance(r.desktop, GuardedDesktop) and r.desktop.unwrapped is d

    @r.task(retries=3, retry_delay=0)
    def post():
        r.desktop.click('button "OK"')
        r.desktop.find('button "Apply"').click()

    report = r.run()
    assert not report.ok
    task = next(s for s in report.steps if s.kind == "task")
    assert task.status == FAILED and task.attempts == 1 and task.error_type == "PolicyViolation"
    assert asked == ["click 'Apply'"]  # asked once, not once per retry
    assert w["ok"].clicks == 1 and w["apply"].clicks == 0
    assert [e["status"] for e in r.audit.events] == ["allowed", "blocked"]


def test_robot_step_retries_skip_policy_violations(app, tmp_path):
    be, d, w = app
    r = Robot(d, run_dir=tmp_path, save_reports=False, screenshot_on_error=False, policy=Policy(allow=[]))
    with pytest.raises(PolicyViolation):
        with r:
            for attempt in r.step("apply", retries=2):
                with attempt:
                    r.desktop.click('button "Apply"')
    step = next(s for s in r.report.steps if s.name == "apply")
    assert step.attempts == 1


def test_robot_desktop_replaced_or_policy_changed_rewraps(app, tmp_path):
    be, d, w = app
    r = Robot(d, run_dir=tmp_path, save_reports=False, policy=Policy(allow=[]))
    first = r.desktop
    assert r.desktop is first
    d2 = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    r.desktop = d2
    assert r.desktop.unwrapped is d2
    r.policy = Policy.permissive()
    assert r.desktop.policy is r.policy


# ------------------------------------------------------------------------- flows


def test_run_flow_without_policy_is_ungated(app):
    be, d, w = app
    report = run_flow({"steps": [{"click": 'button "Post"'}]}, d)
    assert report.ok and w["post"].clicks == 1
    assert "policy" not in report.metadata


def test_run_flow_with_policy_fails_the_refused_step_without_retrying(app):
    be, d, w = app
    asked = []
    audit = AuditLog()
    flow = {
        "settings": {"retries": 3, "retry_delay": 0},
        "steps": [
            {"set_text": {"target": "edit#amount", "text": "12.50"}},
            {"click": 'button "OK"'},
            {"click": 'button "Apply"'},
            {"click": 'button "Post"'},
        ],
    }
    report = run_flow(flow, d, policy=Policy(allow=["edit#amount", 'button "OK"']),
                      confirm=lambda desc, x: asked.append(desc) or False, audit=audit)
    assert not report.ok and "PolicyViolation" in report.error
    assert report.metadata["policy"] == "allow-list"
    failed = next(s for s in report.steps if s.status == FAILED)
    assert failed.path == "steps[2]" and failed.attempts == 1
    assert asked == ["click Apply"]
    assert w["amount"].value == "12.50" and w["ok"].clicks == 1 and w["apply"].clicks == 0 and w["post"].clicks == 0
    assert [e["status"] for e in audit.events] == ["allowed", "allowed", "blocked"]


def test_flow_try_catch_and_loops_see_policy_violations(app):
    be, d, w = app
    flow = {
        "steps": [
            {"try": {"steps": [{"invoke": 'button "Apply"'}], "catch": [{"set_var": {"err": "${error.type}"}}]}},
            {"for_each": {"items": ["OK", "Apply"], "on_error": "continue", "steps": [{"click": 'button "${item}"'}]}},
            {"activate_window": "Invoice 42"},
            {"press": "tab"},
        ]
    }
    report = run_flow(flow, d, policy=Policy(allow=['button "OK"']))
    assert report.ok, report.error
    assert report.variables["err"] == "PolicyViolation"
    assert w["ok"].clicks == 1 and w["apply"].clicks == 0


def test_flow_runner_accepts_a_policy(app):
    be, d, w = app
    import time

    report = RunReport(name="x", run_id="x", started_at=time.time())
    runner = FlowRunner(d, report, policy=Policy(allow=[]))
    assert isinstance(runner.desktop, GuardedDesktop)


def test_guard_enter_in_an_allowed_field_is_judged_by_the_default_button(app):
    be, d, w = app
    w["win"].add_button("Pay now", (320, 300, 100, 30), attrs={"default": True})
    g, asked = _guard(d, ["edit#amount"], confirm_patterns=())
    g.find("edit#amount").type("12")
    with pytest.raises(PolicyViolation):
        g.find("edit#amount").type("3\n")
    d.find("edit#amount").focus()
    with pytest.raises(PolicyViolation):
        g.press("enter")
    assert len(asked) == 2 and all("Pay now" in a for a in asked)
