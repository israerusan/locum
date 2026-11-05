from __future__ import annotations

import json
import logging
import os

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.errors import ElementNotFound, FailSafeTriggered
from pypilot.rpa import BusinessException, Robot, RunReport, StepResult
from pypilot.rpa.report import FAILED, PASSED, SKIPPED


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def desk():
    be = MockBackend(width=800, height=600)
    w = be.add_window("App", (50, 50, 400, 300))
    w.add_button("OK", (10, 40, 80, 30))
    w.add_edit("Name", (10, 90, 200, 28))
    return Desktop(be, sleep=lambda s: None, default_timeout=0)


@pytest.fixture
def robot(desk, tmp_path):
    sleeps = []
    r = Robot(desk, name="unit robot", run_dir=tmp_path / "runs", sleep=sleeps.append)
    r.sleeps = sleeps
    return r


def test_task_registration_forms(robot):
    @robot.task
    def bare():
        return 1

    @robot.task(name="named", retries=2)
    def other(rb):
        assert rb is robot

    assert list(robot.tasks) == ["bare", "named"]
    assert robot.tasks["named"].pass_robot is True
    assert robot.tasks["bare"].pass_robot is False
    with pytest.raises(ValueError):
        robot.task(name="bare")(lambda: None)
    with pytest.raises(TypeError):
        robot.task(name="two")(lambda a, b: None)
    with pytest.raises(ValueError):
        robot.task(retries=-1)


def test_run_all_tasks_pass_and_reports_saved(robot, desk):
    order = []

    @robot.task
    def first():
        with robot.step("click ok"):
            desk.click('button "OK"')
            order.append("first")

    @robot.task
    def second(rb):
        with rb.step("type"):
            desk.find('edit "Name"').set_text("hi")
        rb.vars["done"] = True
        order.append("second")
        return {"rows": 3}

    report = robot.run()
    assert report.ok, report.summary()
    assert order == ["first", "second"]
    assert robot.vars["done"] is True
    tasks = report.tasks
    assert [t.name for t in tasks] == ["first", "second"]
    assert tasks[1].data["result"] == {"rows": 3}
    step = report.find("click ok")
    assert step.path == "first > click ok" and step.depth == 1 and step.status == PASSED
    run_path = robot.run_dir / report.run_id
    assert (run_path / "report.json").is_file()
    assert (run_path / "report.html").is_file()
    loaded = RunReport.load_json(run_path / "report.json")
    assert loaded.ok and len(loaded.steps) == len(report.steps)
    assert report.run_dir == str(run_path)


def test_select_tasks_and_unknown_task(robot):
    ran = []
    robot.task(name="a")(lambda: ran.append("a"))
    robot.task(name="b")(lambda: ran.append("b"))
    report = robot.run("b", save=False)
    assert ran == ["b"] and report.ok
    with pytest.raises(KeyError, match="nope"):
        robot.run("nope")


def test_task_retries_with_backoff_then_pass(robot):
    calls = []

    @robot.task(retries=3, retry_delay=1.0, backoff=2.0)
    def flaky():
        calls.append(1)
        with robot.step("inner"):
            if len(calls) < 3:
                raise RuntimeError("transient")

    report = robot.run(save=False)
    assert report.ok, report.summary()
    task = report.find("flaky")
    assert task.attempts == 3 and task.status == PASSED
    assert robot.sleeps == [1.0, 2.0]
    inner_failures = [s for s in report.steps if s.name == "inner" and s.status == FAILED]
    assert len(inner_failures) == 2 and all(s.handled for s in inner_failures)
    assert any("retrying" in rec["message"] for rec in report.logs)


def test_failed_task_stops_run_and_screenshot_saved(robot, desk):
    @robot.task
    def broken():
        with robot.step("find missing"):
            desk.find('button "Missing"', timeout=0)

    @robot.task
    def after():
        pass

    report = robot.run()
    assert not report.ok
    step = report.find("find missing")
    assert step.status == FAILED and step.error_type == "ElementNotFound"
    assert step.screenshot and os.path.isfile(step.screenshot)
    assert os.path.dirname(step.screenshot) == str(robot.run_dir / report.run_id)
    task = report.find("broken")
    assert task.screenshot == step.screenshot  # same exception -> one screenshot
    assert "Traceback" in task.traceback
    assert report.find("after").status == SKIPPED
    html = (robot.run_dir / report.run_id / "report.html").read_text()
    assert "data:image/png;base64," in html
    assert "ElementNotFound" in html


def test_business_exception_not_retried(robot):
    calls = []

    @robot.task(retries=5, retry_delay=0)
    def rule():
        calls.append(1)
        raise BusinessException("invoice already paid")

    report = robot.run(save=False)
    assert calls == [1]
    assert report.find("rule").error_type == "BusinessException"


def test_continue_on_error_task(robot):
    @robot.task(continue_on_error=True)
    def bad():
        raise ValueError("boom")

    @robot.task
    def good():
        pass

    report = robot.run(save=False)
    assert report.find("good").status == PASSED
    assert report.find("bad").status == FAILED
    assert not report.ok  # the failure still counts


def test_step_continue_on_error_is_handled(robot):
    @robot.task
    def t():
        with robot.step("optional", continue_on_error=True):
            raise RuntimeError("meh")
        robot.vars["after"] = True

    report = robot.run(save=False)
    assert report.ok
    assert robot.vars["after"]
    s = report.find("optional")
    assert s.status == FAILED and s.handled
    assert report.counts()["handled"] == 1


def test_step_iteration_retries(robot):
    tries = []

    @robot.task
    def t():
        for attempt in robot.step("login", retries=2, retry_delay=0.5):
            with attempt:
                tries.append(attempt.number)
                if attempt.number < 3:
                    raise ElementNotFound("not yet")

    report = robot.run(save=False)
    assert report.ok
    assert tries == [1, 2, 3]
    assert report.find("login").attempts == 3
    assert robot.sleeps == [0.5, 1.0]


def test_step_iteration_exhausted_reraises(robot):
    @robot.task
    def t():
        for attempt in robot.step("never", retries=1):
            with attempt:
                raise KeyError("x")

    report = robot.run(save=False)
    assert report.find("never").status == FAILED
    assert report.find("never").attempts == 2
    assert report.find("t").error_type == "KeyError"


def test_step_run_and_decorator(robot):
    n = {"calls": 0}

    def compute(x):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("first fails")
        return x * 2

    @robot.step("decorated", retries=1)
    def deco(v):
        return v + 1

    @robot.task
    def t():
        robot.vars["v"] = robot.step("compute", retries=1).run(compute, 21)
        robot.vars["w"] = deco(1)

    report = robot.run(save=False)
    assert report.ok
    assert robot.vars == {"v": 42, "w": 2}


def test_plain_with_retries_fails_loudly(robot):
    """A plain 'with' cannot retry: retries>0 must not be silently ignored (regression)."""
    ran = []

    @robot.task
    def t():
        with robot.step("once", retries=3):
            ran.append(1)

    report = robot.run(save=False)
    assert not report.ok and ran == []
    assert "plain 'with' block runs only once" in report.tasks[0].error
    with pytest.raises(TypeError, match="for attempt in robot.step"):
        with robot.step("direct", retries=1):
            pass
    with robot.step("no retries"):  # retries=0 stays fine
        pass


def test_iterable_step_retries_transient_error(robot):
    n = []

    @robot.task
    def t():
        for attempt in robot.step("flaky", retries=3):
            with attempt:
                n.append(1)
                if len(n) == 1:
                    raise RuntimeError("transient")

    report = robot.run(save=False)
    assert report.ok and len(n) == 2


def test_timeout_cooperative(desk, tmp_path):
    clock = FakeClock()
    r = Robot(desk, run_dir=tmp_path, sleep=lambda s: None, clock=clock, save_reports=False)

    @r.task(timeout=5)
    def slow():
        with r.step("a"):
            clock.advance(10)
        with r.step("b"):  # never reached: step "a" notices the deadline on exit
            pass

    report = r.run()
    assert report.find("slow").error_type == "ActionTimeout"
    assert report.find("a").status == FAILED
    assert report.find("b") is None


def test_timeout_after_return(desk, tmp_path):
    clock = FakeClock()
    r = Robot(desk, run_dir=tmp_path, sleep=lambda s: None, clock=clock, save_reports=False, screenshot_on_error=False)

    @r.task(timeout=1)
    def slow():
        clock.advance(2)

    report = r.run()
    t = report.find("slow")
    assert t.status == FAILED and "timeout" in t.error


def test_failsafe_aborts_run(robot):
    @robot.task(retries=3)
    def t1():
        raise FailSafeTriggered("corner")

    @robot.task
    def t2():
        pass

    teardown_ran = []
    robot.teardown(lambda: teardown_ran.append(1))
    report = robot.run(save=False)
    assert report.aborted and not report.ok
    assert report.find("t1").attempts == 1
    assert report.find("t2").status == SKIPPED
    assert teardown_ran == [1]


def test_setup_failure_skips_tasks_teardown_runs(robot):
    ran = []
    robot.setup(lambda: (_ for _ in ()).throw(RuntimeError("no app")))

    @robot.teardown
    def cleanup(rb):
        ran.append(rb)

    robot.task(name="t")(lambda: ran.append("t"))
    report = robot.run(save=False)
    assert ran == [robot]
    assert report.find("t").status == SKIPPED
    assert report.find("setup:<lambda>").status == FAILED


def test_structured_logging(robot, caplog):
    @robot.task
    def t():
        with robot.step("s"):
            robot.log("saved invoice", invoice=42, level="info")
        robot.log("debug only", level="debug")

    with caplog.at_level(logging.DEBUG, logger="pypilot.robot"):
        report = robot.run(save=False)
    rec = next(r for r in report.logs if r["message"] == "saved invoice")
    assert rec["invoice"] == 42 and rec["path"] == "t > s" and rec["level"] == "info"
    assert not any(r["message"] == "debug only" for r in report.logs)
    py = [r for r in caplog.records if getattr(r, "pypilot", {}).get("message") == "saved invoice"]
    assert py and py[0].pypilot["invoice"] == 42
    with pytest.raises(ValueError):
        robot.log("x", level="loud")


def test_context_manager_adhoc_run(robot, desk):
    with robot as r:
        with r.step("adhoc"):
            desk.click('button "OK"')
    report = robot.report
    assert report.ok and report.finished_at is not None
    assert (robot.run_dir / report.run_id / "report.json").is_file()


def test_screenshot_failure_is_tolerated(tmp_path):
    class Broken:
        def screenshot(self, *a, **k):
            raise RuntimeError("no display")

        def sleep(self, s):
            pass

    r = Robot(Broken(), run_dir=tmp_path, save_reports=False)

    @r.task
    def t():
        raise RuntimeError("boom")

    report = r.run()
    assert report.find("t").screenshot is None
    assert any("screenshot failed" in rec["message"] for rec in report.logs)


# ------------------------------------------------------------------ report

def test_report_html_escapes_and_embeds(tmp_path):
    from PIL import Image

    img_path = tmp_path / "shot.png"
    Image.new("RGB", (1200, 800), (200, 10, 10)).save(img_path)
    rep = RunReport(name="<script>alert(1)</script>", run_id="r1", started_at=1.0, finished_at=3.5)
    rep.add(StepResult(name='click "<b>x</b>"', status=FAILED, error="bad & <worse>", error_type="ValueError",
                       screenshot=str(img_path), data={"k": "<v>"}))
    rep.add(StepResult(name="ok", status=PASSED))
    rep.logs.append({"time": 2.0, "level": "error", "message": "<img src=x onerror=alert(1)>", "path": "p"})
    html = rep.to_html()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert "bad &amp; &lt;worse&gt;" in html
    assert html.count("data:image/png;base64,") == 1
    assert "<style>" in html and "<link" not in html
    out = rep.save_html(tmp_path / "sub" / "r.html")
    assert os.path.isfile(out)


def test_report_json_roundtrip_and_counts(tmp_path):
    rep = RunReport(name="n", run_id="id", started_at=10.0, finished_at=12.0, metadata={"x": object()})
    rep.add(StepResult(name="a", status=PASSED, duration=0.5))
    rep.add(StepResult(name="b", status=FAILED, handled=True, error="e"))
    rep.add(StepResult(name="c", status=SKIPPED))
    assert rep.ok
    assert rep.counts() == {"passed": 1, "failed": 1, "skipped": 1, "handled": 1, "total": 3}
    path = rep.save_json(tmp_path / "r.json")
    data = json.loads(open(path).read())
    assert data["ok"] is True and data["duration"] == 2.0
    back = RunReport.load_json(path)
    assert [s.name for s in back.steps] == ["a", "b", "c"]
    assert back.steps[1].handled
    rep.add(StepResult(name="d", status=FAILED))
    assert not rep.ok and [s.name for s in rep.failures] == ["d"]
    assert "FAILED" in rep.summary()
    with pytest.raises(ValueError):
        StepResult(name="x", status="weird")


def test_report_timestamps_near_epoch_do_not_crash():
    """Windows raises OSError [Errno 22] for naive fromtimestamp() near 1970 west of UTC."""
    from pypilot.rpa import report as rep

    for ts in (0.0, 1.5, 86399.0):
        assert rep._iso(ts).startswith(("1969-12-31", "1970-01-0"))
