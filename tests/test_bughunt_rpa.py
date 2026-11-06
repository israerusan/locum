"""Regression tests for bug-hunt findings in locum.rpa (mock backend only)."""
import csv

import pytest

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import FailSafeTriggered, WorkflowError
from locum.rpa import Recorder, Robot, run_flow, validate_flow
from locum.rpa.queue import WorkQueue


def _scene():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Invoice", (100, 100, 600, 400), app="erp")
    w.add_edit("Customer", (20, 40, 200, 28))
    w.add_button("Save", (20, 160, 80, 30))
    return be, Desktop(be, sleep=lambda s: None, default_timeout=0)


# -- unresolvable ${var} in retries / continue_on_error ----------------------------
def test_undefined_var_in_retries_fails_the_step():
    _, d = _scene()
    rep = run_flow({"steps": [{"log": "hello", "retries": "${nope}"}]}, d)
    assert not rep.ok
    assert rep.steps[0].status == "failed"
    assert "nope" in rep.steps[0].error
    assert rep.steps[0].finished_at is not None


def test_undefined_var_in_continue_on_error_is_caught_by_try():
    _, d = _scene()
    rep = run_flow({"steps": [{"try": {
        "steps": [{"log": "x", "continue_on_error": "${nope}"}],
        "catch": [{"set_var": {"caught": "yes"}}]}}]}, d)
    assert rep.ok
    assert rep.variables.get("caught") == "yes"
    inner = [s for s in rep.steps if s.path == "steps[0].try.steps[0]"][0]
    assert inner.status == "failed" and inner.handled


# -- recorded set_text without a selector -----------------------------------------
def test_recorded_set_text_without_selector_replays():
    _, d = _scene()
    rec = Recorder(d, use_selectors=False).start(source="backend")
    d.window("Invoice").find('edit "Customer"').set_text("ACME")
    rec.stop()
    flow = rec.to_flow()
    assert flow["steps"][0]["set_text"]["target"].count(",") == 1
    _, d2 = _scene()
    rep = run_flow(flow, d2)
    assert rep.ok, [s.error for s in rep.steps]
    assert d2.find('edit "Customer"').text == "ACME"


def test_flow_set_text_point_target_replaces_text():
    be, d = _scene()
    el = d.find('edit "Customer"')
    el.set_text("old value")
    c = el.center
    rep = run_flow({"steps": [{"set_text": {"target": f"{c.x},{c.y}", "text": "new"}}]}, d)
    assert rep.ok, [s.error for s in rep.steps]
    assert d.find('edit "Customer"').text == "new"


# -- interrupt / fail-safe in a teardown hook -------------------------------------
def _robot(d, tmp_path, name="t"):
    return Robot(d, name=name, run_dir=str(tmp_path), screenshot_on_error=False, save_reports=False,
                 sleep=lambda s: None)


def test_keyboard_interrupt_in_teardown_propagates_and_aborts(tmp_path):
    _, d = _scene()
    r = _robot(d, tmp_path)
    r.task(lambda: None)
    calls = []

    def first():
        calls.append("first")
        raise KeyboardInterrupt()

    def second():
        calls.append("second")

    r.teardown(first)
    r.teardown(second)
    with pytest.raises(KeyboardInterrupt):
        r.run()
    assert calls == ["first", "second"]  # the other hooks still ran
    assert r.report.aborted
    assert r.report.error and "KeyboardInterrupt" in r.report.error
    assert r.report.finished_at is not None
    assert not r._active


def test_failsafe_in_teardown_marks_report_aborted(tmp_path):
    _, d = _scene()
    r = _robot(d, tmp_path, "t2")
    r.task(lambda: None)

    def td():
        raise FailSafeTriggered("kill hotkey")

    r.teardown(td)
    rep = r.run()
    assert rep.aborted
    assert "FailSafeTriggered" in rep.error


# -- for_each / repeat as: loop ---------------------------------------------------
def test_as_loop_is_rejected():
    doc = {"steps": [{"for_each": {"items": ["INV-1", "INV-2"], "as": "loop",
                                   "steps": [{"set_var": {"last": "${loop}"}}]}}]}
    assert any("reserved" in p for p in validate_flow(doc))
    doc2 = {"steps": [{"repeat": {"times": 2, "as": "loop", "steps": [{"log": "x"}]}}]}
    assert any("reserved" in p for p in validate_flow(doc2))
    _, d = _scene()
    with pytest.raises(WorkflowError, match="reserved"):
        run_flow(doc, d)


def test_as_other_name_still_works():
    _, d = _scene()
    rep = run_flow({"steps": [{"for_each": {"items": ["INV-1", "INV-2"], "as": "row",
                                            "steps": [{"set_var": {"last": "${row}"}}]}}]}, d)
    assert rep.ok and rep.variables.get("last") == "INV-2"


# -- export_csv header collisions -------------------------------------------------
def test_export_csv_payload_field_does_not_shadow_base_column(tmp_path):
    q = WorkQueue("inv")
    it = q.add({"invoice": "A1", "status": "unpaid", "error": "none"})
    q.complete(it.id, {"posted": True})
    out = q.export_csv(tmp_path / "out.csv")
    with open(out, newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert len(header) == len(set(header))
    with open(out, newline="", encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
    assert row["status"] == "success"
    assert row["payload.status"] == "unpaid"
    assert row["payload.error"] == "none"
    assert row["invoice"] == "A1"
    assert row["output.posted"] == "True"
