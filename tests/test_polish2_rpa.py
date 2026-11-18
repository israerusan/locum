"""Polish pass 2 (RPA): queue paths/interrupts/perf, report writes and HTML scrubbing, recorder start()."""

from __future__ import annotations

import json
import os
import threading

import pytest

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.rpa import Recorder, RunReport, StepResult, WorkQueue, synced_location
from locum.rpa import queue as queue_mod
from locum.rpa import report as report_mod
from locum.rpa.queue import FAILED, IN_PROGRESS, NEW


@pytest.fixture(autouse=True)
def _no_real_onedrive(monkeypatch):
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


# ------------------------------------------------------------------ R1: extended-length paths
@pytest.mark.skipif(os.name != "nt", reason="\\\\?\\ paths exist on Windows only")
def test_extended_length_local_path_is_not_a_network_share(tmp_path):
    local = "\\\\?\\" + str(tmp_path / "q.json")
    assert synced_location(local) == ""
    assert synced_location("\\\\.\\" + str(tmp_path / "q.json")) == ""
    q = WorkQueue("q", path=local)
    q.add({"invoice": "4711"})
    assert (tmp_path / "q.json").is_file()
    # The extended forms of real shares and synced folders are still refused.
    assert "network share" in synced_location("\\\\?\\UNC\\fileserver\\finance\\q.json")
    assert "cloud-synced" in synced_location("\\\\?\\" + str(tmp_path / "Dropbox" / "q.json"))


# ------------------------------------------------------------------ R2: links into synced folders
def _link_dir(link, target):
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def test_link_into_synced_folder_is_refused(tmp_path):
    synced = tmp_path / "OneDrive - Contoso" / "Queues"
    synced.mkdir(parents=True)
    link = tmp_path / "local-looking"
    try:
        _link_dir(link, synced)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - no link privilege
        pytest.skip(f"cannot create a directory link here: {exc}")
    assert "cloud-synced" in synced_location(link / "q.json")
    with pytest.raises(ValueError, match="allow_synced"):
        WorkQueue("q", path=link / "q.json")
    assert not (synced / "q.json").exists()
    # A link to a local folder is fine.
    plain = tmp_path / "plain"
    plain.mkdir()
    _link_dir(tmp_path / "plain-link", plain)
    assert synced_location(tmp_path / "plain-link" / "q.json") == ""


# ------------------------------------------------------------------ R3: SystemExit
def test_system_exit_in_handler_does_not_leave_a_live_lease(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path=path)
    q.add({"i": 1}, reference="a")

    def handler(item):
        raise SystemExit(3)  # sys.exit() in a handler (or a library calling it)

    with pytest.raises(SystemExit):
        q.process(handler)
    item = q.get("a")
    assert item.status == FAILED and item.error_type == "Interrupted"
    assert item.lease_id is None and item.lease_expires_at is None
    # The file agrees: another worker in this process does not wait 30 minutes for the lease.
    other = WorkQueue.open(path)
    assert other.get("a").status == FAILED and other.get("a").lease_id is None


def test_system_exit_during_retry_wait_requeues_the_item(tmp_path):
    q = WorkQueue("q", path=tmp_path / "q.json")
    q.add({"i": 1}, reference="a")

    def handler(item):
        raise RuntimeError("flaky")

    def sleep(_s):
        raise SystemExit(0)

    with pytest.raises(SystemExit):
        q.process(handler, retries=2, retry_delay=1.0, sleep=sleep)
    item = q.get("a")
    assert item.status == NEW and item.attempts == 1 and item.lease_id is None
    assert item.status != IN_PROGRESS


# ------------------------------------------------------------------ R4/R5: perf paths keep output
class _Obj:
    def to_dict(self):
        return {"k": (1, 2)}


def test_jsonable_fast_path_matches_the_generic_conversion():
    import datetime as dt

    value = {"a": 1, "b": [1, "x", None, 2.5, True, {"c": (3, 4)}], 5: {"d": dt.date(2026, 1, 2)},
             "e": {"f": [{"g": _Obj()}]}, "s": {1}, "t": "text"}
    assert queue_mod._jsonable(value) == {
        "a": 1, "b": [1, "x", None, 2.5, True, {"c": [3, 4]}], "5": {"d": "2026-01-02"},
        "e": {"f": [{"g": {"k": [1, 2]}}]}, "s": [1], "t": "text"}


def test_export_csv_columns_keep_first_seen_order(tmp_path):
    q = WorkQueue("q")
    q.add({"b": 1, "a": 2})
    q.add({"c": 3, "a": 4})
    it = q.add({"b": 5})
    q.next()
    q.complete(q.items[0], {"y": 1, "x": 2})
    del it
    out = tmp_path / "q.csv"
    q.export_csv(out)
    header = out.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header == ["id", "reference", "status", "attempts", "error_type", "error", "b", "a", "c", "output.y", "output.x"]


# ------------------------------------------------------------------ R6: report writes
def test_failed_report_write_leaves_no_temp_file(tmp_path, monkeypatch):
    rep = RunReport(name="r", run_id="1")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(queue_mod.os, "replace", boom)
    monkeypatch.setattr(report_mod.os, "replace", boom)
    with pytest.raises(OSError):
        rep.save_json(tmp_path / "report.json")
    assert [p.name for p in tmp_path.iterdir()] == []


def test_threads_saving_one_report_do_not_collide(tmp_path):
    rep = RunReport(name="r", run_id="1", steps=[StepResult(name=f"s{i}") for i in range(50)])
    target = tmp_path / "report.json"
    errors = []
    start = threading.Event()

    def save():
        start.wait(5)
        try:
            for _ in range(25):
                rep.save_json(target)
        except BaseException as exc:  # pragma: no cover - the bug
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(6)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(60)
    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "r"
    assert [p.name for p in tmp_path.iterdir()] == ["report.json"]


# ------------------------------------------------------------------ report HTML scrub
def test_html_scrubs_a_secret_inside_data_url_lookalike_text():
    from locum._secret import Secret

    Secret("hunter2Zebra")  # registers it
    rep = RunReport(name="r", run_id="1",
                    steps=[StepResult(name="login", status="failed",
                                      error="pasted data:image/png;base64,hunter2Zebra into the field")])
    page = rep.to_html()
    assert "hunter2Zebra" not in page
    assert "data:image/png;base64,***" in page


# ------------------------------------------------------------------ R8: recorder start() failure
def test_recorder_start_failure_after_hooks_leaves_it_stopped(monkeypatch):
    be = MockBackend()
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    r = Recorder(d, use_selectors=False, trust_unknown_focus=True)

    def broken():
        raise RuntimeError("listener registry unavailable")

    monkeypatch.setattr(r, "_install_action_listener", broken)
    with pytest.raises(RuntimeError, match="listener registry"):
        r.start(source="backend")
    assert not r.is_recording and r.source is None
    assert r._hooks == {}
    assert not getattr(be, "_locum_recorder_hooks", None)  # the backend is unhooked
    monkeypatch.undo()
    r.start(source="backend")  # a second start works
    assert r.is_recording
    r.stop()


# ------------------------------------------------------------------ robot context manager
def test_system_exit_in_robot_with_block_marks_the_run_aborted(tmp_path):
    from locum.rpa import Robot

    d = Desktop(MockBackend(), sleep=lambda s: None, default_timeout=0)
    robot = Robot(d, "r", run_dir=tmp_path, save_reports=False, policy=None)
    with pytest.raises(SystemExit):
        with robot:
            with robot.step("work"):
                raise SystemExit(2)
    assert robot.report.aborted and "SystemExit" in robot.report.error  # as Robot.run() reports it
