"""Regression tests for round-2 bug-hunt findings in locum.rpa (mock backend / fakes only)."""
import sys
import threading
import time

import pytest

from locum import failsafe
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.rpa import Recorder, Robot, run_flow, validate_flow
from locum.rpa.recorder import RecordedAction


@pytest.fixture(autouse=True)
def _clear_abort():
    failsafe.reset_abort()
    yield
    failsafe.reset_abort()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _robot(tmp_path, **kw):
    kw.setdefault("sleep", lambda s: None)
    return Robot(name="r", run_dir=tmp_path, save_reports=False, screenshot_on_error=False, **kw)


# -- 20: a task that overruns its timeout and then raises is not retried ---------------
def test_task_overrun_then_raise_is_not_retried(tmp_path):
    clock = _Clock()
    r = _robot(tmp_path, clock=clock)
    posted = []

    @r.task(retries=2, timeout=60)
    def post_payment():
        posted.append("PAY")
        clock.t += 61
        raise RuntimeError("confirmation dialog not found")

    report = r.run()
    t = report.find("post_payment")
    assert posted == ["PAY"]
    assert t.attempts == 1 and t.status == "failed"
    assert t.error_type == "ActionTimeout"
    assert "confirmation dialog not found" in t.error


def test_task_failing_within_timeout_is_still_retried(tmp_path):
    clock = _Clock()
    r = _robot(tmp_path, clock=clock)
    calls = []

    @r.task(retries=2, timeout=60)
    def flaky():
        calls.append(1)
        clock.t += 1
        if len(calls) < 3:
            raise RuntimeError("transient")

    report = r.run()
    assert report.ok and len(calls) == 3


# -- 21: abort during a flow step's retry wait records the step as failed --------------
def _flow_desktop():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Invoice", (100, 100, 600, 400), app="erp")
    w.add_button("Save", (20, 160, 80, 30))

    def sleep(s):
        failsafe.abort("kill hotkey during the retry wait")

    return Desktop(be, sleep=sleep, default_timeout=0)


def test_flow_abort_in_retry_wait_marks_step_failed():
    rep = run_flow({"steps": [{"click": 'button "Post"', "retries": 2, "retry_delay": 1}]}, _flow_desktop())
    s = rep.steps[0]
    assert rep.aborted
    assert s.status == "failed" and s.error and s.finished_at is not None
    assert s.attempts == 1


def test_flow_abort_in_nested_retry_wait_marks_nested_step_failed():
    doc = {"steps": [{"for_each": {"items": [1, 2], "steps": [
        {"click": 'button "Post"', "retries": 1, "retry_delay": 0.5}]}}]}
    rep = run_flow(doc, _flow_desktop())
    assert rep.aborted
    nested = [s for s in rep.steps if s.path == "steps[0].for_each.steps[0]"]
    assert nested and all(s.status == "failed" for s in nested)


# -- 22: a step / settings timeout of "${var}" is interpolated --------------------------
def _save_desktop():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Invoice", (100, 100, 600, 400), app="erp")
    w.add_button("Save", (20, 160, 80, 30))
    return Desktop(be, sleep=lambda s: None, default_timeout=0)


def test_step_timeout_variable_is_interpolated():
    doc = {"vars": {"t": 2}, "steps": [{"click": 'button "Save"', "timeout": "${t}"}]}
    assert validate_flow(doc) == []
    rep = run_flow(doc, _save_desktop())
    assert rep.ok, rep.error


def test_settings_timeout_variable_is_interpolated():
    doc = {"vars": {"t": 2}, "settings": {"timeout": "${t}"}, "steps": [{"click": 'button "Save"'}]}
    rep = run_flow(doc, _save_desktop())
    assert rep.ok, rep.error


def test_step_timeout_bad_value_still_fails():
    doc = {"vars": {"t": "soon"}, "steps": [{"click": 'button "Save"', "timeout": "${t}"}]}
    rep = run_flow(doc, _save_desktop())
    assert not rep.ok and "timeout must be a number" in rep.error


# -- 23: robot retry waits honour the abort flag ----------------------------------------
def test_retry_wait_without_desktop_ends_on_abort(tmp_path):
    r = Robot(name="r", run_dir=tmp_path, save_reports=False, screenshot_on_error=False)
    calls = []

    @r.task(retries=1, retry_delay=3.0)
    def t():
        calls.append(1)
        if len(calls) == 1:
            threading.Timer(0.1, failsafe.abort, args=("kill hotkey",)).start()
            raise RuntimeError("transient")

    t0 = time.monotonic()
    report = r.run()
    assert time.monotonic() - t0 < 2.0
    assert report.aborted and len(calls) == 1


def test_retry_wait_with_injected_sleep_checks_abort(tmp_path):
    r = _robot(tmp_path, sleep=lambda s: failsafe.abort("kill hotkey"))
    calls = []

    @r.task(retries=1, retry_delay=1.0)
    def t():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")

    report = r.run()
    assert report.aborted and not report.ok and len(calls) == 1


def test_step_retry_wait_with_injected_sleep_checks_abort(tmp_path):
    r = _robot(tmp_path, sleep=lambda s: failsafe.abort("kill hotkey"))
    calls = []

    @r.task
    def t(robot):
        def body():
            calls.append(1)
            raise RuntimeError("transient")
        robot.step("s", retries=2, retry_delay=1.0).run(body)

    report = r.run()
    assert report.aborted and len(calls) == 1


# -- 24: a pasted Tab/newline replays as a paste ----------------------------------------
def _form():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Invoice", (100, 100, 600, 400), app="erp")
    a = w.add_edit("Notes", (20, 40, 200, 28))
    b = w.add_edit("Amount", (20, 80, 200, 28))
    return Desktop(be, sleep=lambda s: None, default_timeout=0), a, b


def test_pasted_control_chars_replay_as_paste():
    d, notes, amount = _form()
    rec = Recorder(d).start(source="backend")
    d.click('edit "Notes"')
    d.type("A\tB", mode="clipboard")
    rec.stop()
    flow = rec.to_flow()
    step = flow["steps"][-1]["type"]
    assert step["text"] == "A\tB" and step["mode"] == "clipboard"
    assert "mode='clipboard'" in rec.to_python()
    d2, notes2, amount2 = _form()
    run_flow(flow, d2)
    assert (notes2.value, amount2.value) == (notes.value, amount.value) == ("A\tB", "")


def test_plain_paste_still_recorded_as_plain_type():
    d, notes, _ = _form()
    rec = Recorder(d).start(source="backend")
    d.click('edit "Notes"')
    d.type("hello", mode="clipboard")
    rec.stop()
    step = rec.to_flow()["steps"][-1]["type"]
    assert step == {"text": "hello", "into": 'window "Invoice" edit "Notes"'}


def test_recorded_action_mode_without_selector():
    a = RecordedAction("type", text="x\ny", mode="clipboard")
    assert a.to_flow_step() == {"type": {"text": "x\ny", "mode": "clipboard"}}
    assert a.to_python() == "d.type('x\\ny', mode='clipboard')"
    assert a.to_dict()["mode"] == "clipboard"
    assert RecordedAction("type", text="x").to_flow_step() == {"type": "x"}


# -- 25: SystemExit in a teardown hook does not skip the others -------------------------
def test_teardown_systemexit_runs_remaining_hooks(tmp_path):
    r = _robot(tmp_path)
    ran = []

    @r.task
    def t():
        pass

    @r.teardown
    def first():
        ran.append("first")
        sys.exit(3)

    @r.teardown
    def close_app():
        ran.append("close_app")

    with pytest.raises(SystemExit):
        r.run()
    assert ran == ["first", "close_app"]
    assert r.report.aborted
