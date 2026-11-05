"""WorkQueue persistence: fsync before rename, generation counter, file lock, old-format files."""

from __future__ import annotations

import json
import os
import threading

import pytest

import pypilot.rpa.queue as queue_mod
from pypilot.rpa.queue import FAILED, IN_PROGRESS, NEW, SUCCESS, WorkQueue

# A queue file exactly as pypilot 0.1.0 (before leases) wrote it.
OLD_FORMAT = {
    "format": "pypilot.workqueue",
    "version": 1,
    "name": "legacy",
    "created_at": 1700000000.0,
    "saved": "2023-11-14T22:13:20+00:00",
    "summary": {"name": "legacy", "total": 3},
    "items": [
        {"id": "aaaaaaaaaaaa", "reference": "done", "status": "success", "priority": 0, "attempts": 1,
         "created_at": 1700000000.0, "started_at": 1700000001.0, "finished_at": 1700000002.0,
         "error": None, "error_type": None, "traceback": None, "payload": {"n": 1}, "output": {"ok": True},
         "history": [{"time": 1700000001.0, "event": "started", "attempt": 1}]},
        {"id": "bbbbbbbbbbbb", "reference": "crashed", "status": "in_progress", "priority": 0, "attempts": 1,
         "created_at": 1700000000.0, "started_at": 1700000003.0, "finished_at": None,
         "error": None, "error_type": None, "traceback": None, "payload": {"n": 2}, "output": {}, "history": []},
        {"id": "cccccccccccc", "reference": "todo", "status": "new", "priority": 5, "attempts": 0,
         "created_at": 1700000000.0, "started_at": None, "finished_at": None,
         "error": None, "error_type": None, "traceback": None, "payload": {"n": 3}, "output": {}, "history": []},
    ],
}


def test_old_format_file_loads_and_is_upgraded_in_place(tmp_path):
    path = tmp_path / "legacy.queue.json"
    path.write_text(json.dumps(OLD_FORMAT, indent=2), encoding="utf-8")
    q = WorkQueue.open(path)
    assert q.resumed and q.name == "legacy" and q.created_at == 1700000000.0
    assert q.get("done").status == SUCCESS and q.get("done").output == {"ok": True}
    crashed = q.get("crashed")  # an old in_progress row has no lease: its run died
    assert crashed.status == FAILED and crashed.error_type == "Abandoned"
    assert q.get("todo").status == NEW and q.get("todo").worker_id is None
    item = q.next()
    assert item.reference == "todo"
    q.complete(item)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["generation"] >= 1
    assert {it["reference"]: it["status"] for it in data["items"]} == {"done": SUCCESS, "crashed": FAILED, "todo": SUCCESS}
    again = WorkQueue.open(path, on_abandoned="requeue")
    assert again.get("todo").worker_id == q.worker_id and again.pending() == []


def test_old_format_in_progress_requeue_policy(tmp_path):
    path = tmp_path / "legacy.queue.json"
    path.write_text(json.dumps(OLD_FORMAT), encoding="utf-8")
    q = WorkQueue.open(path, on_abandoned="requeue")
    assert q.get("crashed").status == NEW and q.get("crashed").attempts == 1
    assert [it.reference for it in q.pending()] == ["todo", "crashed"]


def test_every_write_fsyncs_before_rename(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        events.append(("replace", os.path.basename(str(dst))))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1})
    replaces = [i for i, e in enumerate(events) if e == ("replace", "q.json")]
    assert len(replaces) == 1
    assert "fsync" in events[: replaces[0]]  # the data reached the disk before it became the queue file
    if os.name != "nt":
        assert "fsync" in events[replaces[0] + 1:]  # and so did the rename (directory fsync)
    events.clear()
    q.complete(q.next())
    assert events.count(("replace", "q.json")) == 2 and events.count("fsync") >= 2
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_generation_increases_on_every_write(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)

    def gen():
        return json.loads(path.read_text(encoding="utf-8"))["generation"]

    q.add({"n": 1})
    g1 = gen()
    q.next()
    g2 = gen()
    other = WorkQueue.open(path)
    other.add({"n": 2})
    g3 = gen()
    q.add({"n": 3})  # q is stale (g2): it must reload other's write, then write g3 + 1
    assert g1 < g2 < g3 < gen()
    assert len(WorkQueue.open(path)) == 3


def test_external_edit_with_same_generation_is_reloaded(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="x")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["priority"] = 9  # a human edits the file, generation untouched
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    q.add({"n": 2}, reference="y")
    assert q.get("x").priority == 9
    assert json.loads(path.read_text(encoding="utf-8"))["items"][0]["priority"] == 9


def test_lock_file_and_lock_timeout(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path, lock_timeout=0.2)
    q.add({"n": 1})
    lock_path = tmp_path / "q.json.lock"
    assert lock_path.exists()
    held, release = threading.Event(), threading.Event()

    def holder():
        with queue_mod._file_lock(lock_path, 5):
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(5)
        with pytest.raises(TimeoutError, match="queue lock"):
            q.add({"n": 2})
    finally:
        release.set()
        t.join()
    q.add({"n": 2})
    assert len(WorkQueue.open(path)) == 2


def test_autosave_false_writes_only_on_save(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path, autosave=False)
    q.add({"n": 1}, reference="x")
    q.complete(q.next())
    stats = q.process(lambda it: None)
    assert not path.exists() and stats[SUCCESS] == 1
    q.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["items"][0]["status"] == SUCCESS and data["generation"] == 1
    q2 = WorkQueue("q", path, autosave=False)
    assert q2.get("x").status == SUCCESS


def test_save_to_another_path_writes_a_copy(tmp_path):
    q = WorkQueue("q", tmp_path / "q.json")
    q.add({"n": 1}, reference="x")
    copy = tmp_path / "backup" / "copy.json"
    assert q.save(copy) == str(copy)
    assert WorkQueue.open(copy).get("x").status == NEW
    assert q.save() == str(tmp_path / "q.json")


def test_failed_mutation_rereads_file(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="x")
    with pytest.raises(KeyError):
        q.complete("missing")
    assert q.get("x").status == NEW
    q.complete(q.next())
    assert WorkQueue.open(path).get("x").status == SUCCESS


def test_interrupted_item_in_shared_file_keeps_lease_cleared(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="x")

    def stop(item):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        q.process(stop)
    it = json.loads(path.read_text(encoding="utf-8"))["items"][0]
    assert it["status"] == FAILED and it["error_type"] == "Interrupted"
    assert it["lease_id"] is None and it["lease_expires_at"] is None
    assert IN_PROGRESS not in {i.status for i in WorkQueue.open(path).items}
