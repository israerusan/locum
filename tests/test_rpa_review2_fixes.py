"""Regression tests for the second review round of the RPA robot and work queue."""

from __future__ import annotations

import os
import threading

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.errors import FailSafeTriggered
from pypilot.rpa import Robot, WorkQueue
from pypilot.rpa.queue import FAILED as Q_FAILED
from pypilot.rpa.queue import NEW
from pypilot.rpa.report import FAILED, PASSED, SKIPPED


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_robot(tmp_path, **kw):
    kw.setdefault("save_reports", False)
    kw.setdefault("screenshot_on_error", False)
    kw.setdefault("sleep", lambda s: None)
    return Robot(name=kw.pop("name", "r"), run_dir=tmp_path / "runs", **kw)


# --------------------------------------------------------------- task deadline
def test_task_finishing_after_its_timeout_is_not_retried(tmp_path):
    clock = FakeClock()
    r = make_robot(tmp_path, clock=clock)
    posted = []

    @r.task(retries=2, timeout=60)
    def post_payment():
        posted.append("PAY INV-1")
        clock.advance(61)
        return "doc 4711"

    report = r.run()
    assert posted == ["PAY INV-1"]  # the money moved once
    t = report.find("post_payment")
    assert t.status == FAILED and t.error_type == "ActionTimeout" and t.attempts == 1
    assert t.data["result"] == "doc 4711" and "not retried" in t.data["note"]


def test_task_deadline_at_step_exit_is_not_retried(tmp_path):
    clock = FakeClock()
    r = make_robot(tmp_path, clock=clock)
    runs = []

    @r.task(retries=2, timeout=5)
    def t():
        runs.append(1)
        with r.step("post"):
            clock.advance(10)

    report = r.run()
    assert runs == [1] and report.find("t").attempts == 1


def test_step_continue_on_error_does_not_swallow_task_deadline(tmp_path):
    clock = FakeClock()
    r = make_robot(tmp_path, clock=clock, sleep=clock.advance)
    actions = []

    @r.task(timeout=30)
    def t1():
        with r.step("optional: close popup", continue_on_error=True):
            r.sleep(60)
        actions.append("clicked Post")

    report = r.run()
    assert actions == []
    step = report.find("optional: close popup")
    assert step.status == FAILED and not step.handled and step.error_type == "ActionTimeout"
    assert report.find("t1").error_type == "ActionTimeout"


def test_retried_step_does_not_retry_task_deadline(tmp_path):
    clock = FakeClock()
    r = make_robot(tmp_path, clock=clock)
    tries = []

    @r.task(timeout=5)
    def t():
        for attempt in r.step("inner", retries=3):
            with attempt:
                tries.append(1)
                clock.advance(10)
                r.check_timeout()

    report = r.run()
    assert tries == [1]
    assert report.find("inner").status == FAILED and report.find("inner").error_type == "ActionTimeout"


def test_queue_stops_on_robot_task_deadline(tmp_path):
    clock = FakeClock()
    r = make_robot(tmp_path, clock=clock)
    q = WorkQueue("pay")
    q.add_many([{"inv": i} for i in range(3)], reference_prefix="x")
    handled = []

    def handle(item):
        with r.step(f"post {item['inv']}"):
            handled.append(item["inv"])
            clock.advance(10)

    @r.task(timeout=5)
    def t():
        q.process(handle, retries=2)

    report = r.run()
    assert handled == [0]
    assert [i.status for i in q.items] == [Q_FAILED, NEW, NEW]
    assert q.items[0].attempts == 1
    assert report.find("t").error_type == "ActionTimeout"


# ------------------------------------------------------- retried step outcomes
def test_retried_step_interrupted_is_failed(tmp_path):
    r = make_robot(tmp_path)

    @r.task
    def t():
        for attempt in r.step("post journal", retries=2):
            with attempt:
                raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        r.run()
    s = r.report.find("post journal")
    assert s.status == FAILED and s.error_type == "KeyboardInterrupt" and s.finished_at is not None


def test_step_run_form_interrupted_is_failed(tmp_path):
    r = make_robot(tmp_path)

    def read_total():
        raise KeyboardInterrupt

    @r.task
    def t():
        r.step("read total", retries=2).run(read_total)

    with pytest.raises(KeyboardInterrupt):
        r.run()
    assert r.report.find("read total").status == FAILED


def test_retried_step_timeout_on_attempt_entry_is_failed(tmp_path):
    clock = FakeClock()
    r = make_robot(tmp_path, clock=clock, sleep=clock.advance)

    @r.task(timeout=5)
    def t():
        for attempt in r.step("login", retries=3, retry_delay=10):
            with attempt:
                raise RuntimeError("login button not found")

    report = r.run()
    s = report.find("login")
    assert s.status == FAILED and s.error_type == "ActionTimeout"
    assert s.data["retry_errors"] == ["RuntimeError: login button not found"]


def test_retried_step_left_by_error_outside_attempt_is_failed(tmp_path):
    r = make_robot(tmp_path)

    @r.task
    def t():
        try:
            for attempt in r.step("lookup", retries=2):
                raise ValueError("bad row")
        except ValueError:
            pass

    report = r.run()
    s = report.find("lookup")
    assert s.status == FAILED and "left before attempt 1 completed" in s.error


def test_retried_step_return_and_break_still_pass(tmp_path):
    r = make_robot(tmp_path)
    n = []

    @r.task
    def t():
        for attempt in r.step("read", retries=2):
            with attempt:
                n.append(1)
                if len(n) == 1:
                    raise RuntimeError("flaky")
                return 42

    @r.task
    def t2():
        for attempt in r.step("once", retries=2):
            with attempt:
                pass
            break

    report = r.run()
    assert report.ok, report.summary()
    assert report.find("read").status == PASSED and report.find("read").attempts == 2
    assert report.find("once").status == PASSED


# ------------------------------------------------------ abort during retry wait
def test_abort_during_task_retry_wait_fails_task(tmp_path):
    from pypilot import failsafe as fs

    d = Desktop(MockBackend(width=800, height=600))
    r = Robot(d, name="r3", run_dir=tmp_path, save_reports=False, screenshot_on_error=False)
    calls = []

    @r.task(retries=2, retry_delay=2)
    def pay():
        calls.append(1)
        threading.Timer(0.05, lambda: fs.abort("kill hotkey")).start()  # kill hotkey during the retry wait
        raise RuntimeError("SAP not responding")

    try:
        report = r.run()
    finally:
        fs.reset_abort()
    t = report.find("pay")
    assert report.aborted and calls == [1]
    assert t.status == FAILED and t.finished_at is not None and t.error_type == "FailSafeTriggered"
    assert t.data["retry_errors"] == ["RuntimeError: SAP not responding"]


def test_abort_during_step_retry_wait_fails_step(tmp_path):
    def sleep(s):
        raise FailSafeTriggered("kill hotkey")

    r = make_robot(tmp_path, sleep=sleep)

    @r.task
    def t():
        for attempt in r.step("approve payment run", retries=2, retry_delay=1):
            with attempt:
                raise RuntimeError("button not found")

    report = r.run()
    s = report.find("approve payment run")
    assert report.aborted
    assert s.status == FAILED and s.finished_at is not None and s.error_type == "FailSafeTriggered"


def test_unfinished_step_is_never_reported_passed(tmp_path):
    r = make_robot(tmp_path)
    r.start()
    r.step("left open").__enter__()
    report = r.finish()
    s = report.find("left open")
    assert s.status == FAILED and s.finished_at is not None and not report.ok


# ------------------------------------------------------------- run() lifecycle
def test_run_other_exception_runs_teardown_and_releases_robot(tmp_path):
    r = make_robot(tmp_path, save_reports=True)
    td = []
    r.teardown(lambda: td.append("logout"))
    first = []

    @r.task
    def t():
        if not first:
            first.append(1)
            raise SystemExit("bad config")

    @r.task
    def t2():
        pass

    with pytest.raises(SystemExit):
        r.run()
    report = r.report
    assert td == ["logout"] and report.aborted and "SystemExit" in report.error
    assert report.find("t2").status == SKIPPED
    assert os.path.exists(os.path.join(report.run_dir, "report.json"))
    assert r.run().ok  # not stuck "already running"


def test_hook_signature_checked_at_registration(tmp_path):
    r = make_robot(tmp_path)
    with pytest.raises(TypeError, match="Setup hook"):
        @r.setup
        def login(robot, user):
            pass
    with pytest.raises(TypeError, match="Teardown hook"):
        r.teardown(lambda a, b: None)
    assert r.run().ok


def test_step_outside_run_is_adopted_by_run(tmp_path):
    r = make_robot(tmp_path)

    @r.task
    def t():
        pass

    with r.step("check config"):
        pass
    report = r.run()
    assert report.ok and [s.name for s in report.steps] == ["check config", "t"]
    assert r.run().ok


def test_run_inside_open_adhoc_step_still_refused(tmp_path):
    r = make_robot(tmp_path)

    @r.task
    def t():
        pass

    with r.step("outer"):
        with pytest.raises(RuntimeError, match="already running"):
            r.run()


# ----------------------------------------------------------------- work queue
def test_queue_blank_reference_raises_and_adds_nothing(tmp_path):
    p = tmp_path / "inv.csv"
    p.write_text("invoice_no,vendor,amount\n1001,ACME,100.00\n,Globex,2500.00\n,Initech,730.10\n1002,Umbrella,50.00\n")
    q = WorkQueue("pay")
    with pytest.raises(ValueError, match="item 1: reference field 'invoice_no' is empty"):
        q.load(p, reference_field="invoice_no")
    assert len(q) == 0
    with pytest.raises(ValueError, match="empty"):
        q.add_many([{"k": None}], reference_field="k")


def test_queue_duplicate_reference_within_one_load_raises(tmp_path):
    q = WorkQueue("pay")
    with pytest.raises(ValueError, match="is also item 0, with different data"):
        q.add_many([{"k": "1", "amt": 1}, {"k": "2", "amt": 5}, {"k": "1", "amt": 2}], reference_field="k")
    assert len(q) == 0
    same = q.add_many([{"k": "9", "amt": 1}, {"k": "9", "amt": 1}], reference_field="k")  # the same row twice
    assert len(q) == 1 and same[0] is same[1]
    q.clear()
    first = q.add_many([{"k": "1"}, {"k": "2"}], reference_field="k")
    assert q.add_many([{"k": "1"}, {"k": "2"}], reference_field="k") == first  # reload stays idempotent


def test_queue_interrupt_during_retry_wait_requeues_item(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("pay", path=path)
    q.add({"inv": "A"}, reference="A")

    def handler(item):
        raise RuntimeError("SAP timeout")

    def sleep(s):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        q.process(handler, retries=1, retry_delay=5, sleep=sleep)
    it = q.get("A")
    assert it.status == NEW and it.attempts == 1
    assert WorkQueue("pay", path=path).get("A").status == NEW


@pytest.mark.parametrize("exc", [KeyboardInterrupt, FailSafeTriggered])
def test_queue_interrupt_inside_handler_is_not_replayed(tmp_path, exc):
    # Ctrl+C / kill hotkey while the handler runs: the payment may already be submitted.
    path = tmp_path / "pay.queue.json"
    q = WorkQueue("payments", path=path)
    q.add({"invoice": "INV-1"}, reference="INV-1")
    bank = []

    def pay(item):
        bank.append(item["invoice"])
        raise exc("operator stop")

    with pytest.raises(exc):
        q.process(pay)
    it = WorkQueue.open(path).get("INV-1")
    assert it.status == Q_FAILED and it.error_type == "Interrupted"
    WorkQueue.open(path).process(lambda i: bank.append(i["invoice"]))
    assert bank == ["INV-1"]  # never paid twice


def test_queue_interrupt_inside_handler_requeue_policy(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path=path, on_abandoned="requeue")
    q.add({"n": 1}, reference="A")

    def handler(item):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        q.process(handler)
    it = WorkQueue.open(path, on_abandoned="requeue").get("A")
    assert it.status == NEW and it.attempts == 1  # the attempt counts
