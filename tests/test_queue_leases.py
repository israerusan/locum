"""WorkQueue leases: expiry reclaim, renew, release, dead workers, lost leases, the process() heartbeat."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time

import pytest

import pypilot.rpa.queue as queue_mod
from pypilot.rpa.queue import FAILED, IN_PROGRESS, NEW, SUCCESS, LeaseLostError, WorkQueue


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(queue_mod, "_now", lambda: now[0])
    return now


def test_claim_records_worker_and_lease(tmp_path, clock):
    q = WorkQueue("q", tmp_path / "q.json", worker_id="robot-1", lease_duration=90)
    q.add({"n": 1}, reference="x")
    it = q.next()
    assert it.worker_id == "robot-1" and it.lease_expires_at == clock[0] + 90 and it.lease_id
    disk = json.loads((tmp_path / "q.json").read_text())["items"][0]
    assert disk["worker_id"] == "robot-1" and disk["lease_expires_at"] == clock[0] + 90
    q.complete(it)
    assert it.lease_id is None and it.lease_expires_at is None and it.worker_id == "robot-1"


def test_default_worker_id_and_lease():
    q = WorkQueue()
    assert q.lease_duration == 30 * 60
    host, pid, suffix = q.worker_id.rsplit(":", 2)
    assert host == socket.gethostname() and int(pid) > 0 and suffix
    assert WorkQueue().worker_id != q.worker_id
    with pytest.raises(ValueError):
        WorkQueue(lease_duration=0)
    with pytest.raises(ValueError):
        WorkQueue(lock_timeout=0)


def test_expired_lease_is_abandoned_and_old_owner_cannot_overwrite(tmp_path, clock):
    path = tmp_path / "q.json"
    a = WorkQueue("q", path, worker_id="A", lease_duration=60)
    a.add({"n": 1}, reference="x")
    mine = a.next()
    b = WorkQueue.open(path, worker_id="B", lease_duration=60)
    assert b.next() is None and b.get("x").status == IN_PROGRESS  # lease still live
    clock[0] += 61
    assert b.next() is None
    it = b.get("x")
    assert it.status == FAILED and it.error_type == "Abandoned" and "lease" in it.error
    with pytest.raises(LeaseLostError):
        a.complete(mine)  # A woke up too late: it must not overwrite B's decision
    assert WorkQueue.open(path).get("x").status == FAILED


def test_expired_lease_requeue_reclaims_with_attempt_counted(tmp_path, clock):
    path = tmp_path / "q.json"
    a = WorkQueue("q", path, worker_id="A", lease_duration=60, on_abandoned="requeue")
    a.add({"n": 1}, reference="x")
    mine = a.next()
    clock[0] += 61
    b = WorkQueue.open(path, worker_id="B", lease_duration=60, on_abandoned="requeue")
    theirs = b.next()
    assert theirs is not None and theirs.reference == "x"
    assert theirs.attempts == 2 and theirs.worker_id == "B"
    assert any(h["event"] == "abandoned" and h.get("worker") == "A" for h in theirs.history)
    with pytest.raises(LeaseLostError):
        a.fail(mine, "too late")
    b.complete(theirs)
    assert a.refresh().get("x").status == SUCCESS


def test_renew_keeps_the_item(tmp_path, clock):
    path = tmp_path / "q.json"
    a = WorkQueue("q", path, worker_id="A", lease_duration=60)
    a.add({"n": 1}, reference="x")
    mine = a.next()
    b = WorkQueue.open(path, worker_id="B")
    for _ in range(3):
        clock[0] += 50
        a.renew(mine)
        assert b.next() is None and b.get("x").status == IN_PROGRESS
    assert a.renew(mine, duration=5).lease_expires_at == clock[0] + 5
    with pytest.raises(LeaseLostError):
        b.renew("x")  # not b's claim
    with pytest.raises(LeaseLostError):
        b.complete("x")  # A is alive and holds it
    a.complete(mine)
    with pytest.raises(LeaseLostError):
        a.renew(mine)  # finished: nothing to renew
    with pytest.raises(ValueError):
        a.renew(mine, duration=0)


def test_release_returns_item_without_counting_attempt(tmp_path):
    q = WorkQueue("q", tmp_path / "q.json")
    q.add({"n": 1}, reference="x")
    it = q.next()
    q.release(it)
    assert it.status == NEW and it.attempts == 0 and it.worker_id is None and it.lease_id is None
    assert WorkQueue.open(tmp_path / "q.json").get("x").status == NEW
    with pytest.raises(LeaseLostError):
        q.release(it)


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _fake_claim(path, **fields):
    data = json.loads(path.read_text())
    data["items"][0].update({"status": IN_PROGRESS, "attempts": 1, "worker_id": "ghost",
                             "lease_id": "abc", "lease_expires_at": time.time() + 3600, **fields})
    path.write_text(json.dumps(data))


def test_dead_local_worker_is_reclaimed_before_lease_expiry(tmp_path):
    path = tmp_path / "q.json"
    WorkQueue("q", path).add({"n": 1}, reference="x")
    _fake_claim(path, worker_host=socket.gethostname(), worker_pid=_dead_pid())
    assert WorkQueue.open(path, reclaim_dead_workers=False).get("x").status == IN_PROGRESS
    it = WorkQueue.open(path).get("x")
    assert it.status == FAILED and it.error_type == "Abandoned" and "no longer running" in it.error


def test_live_or_remote_worker_is_not_reclaimed_early(tmp_path):
    import os

    path = tmp_path / "q.json"
    WorkQueue("q", path).add({"n": 1}, reference="x")
    _fake_claim(path, worker_host=socket.gethostname(), worker_pid=os.getpid())
    assert WorkQueue.open(path).get("x").status == IN_PROGRESS
    _fake_claim(path, worker_host="some-other-host", worker_pid=_dead_pid())
    assert WorkQueue.open(path).get("x").status == IN_PROGRESS  # cannot check another machine's pids


def test_process_heartbeat_renews_lease_during_long_handler(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path, worker_id="A", lease_duration=0.3)
    q.add({"n": 1}, reference="x")
    other = WorkQueue.open(path, worker_id="B", lease_duration=0.3)
    seen = []

    def slow(item):
        deadline = time.monotonic() + 1.2  # four leases long
        while time.monotonic() < deadline:
            assert other.next() is None
            seen.append(other.get("x").status)
            time.sleep(0.05)
        return {"done": True}

    stats = q.process(slow, retries=0)
    assert set(seen) == {IN_PROGRESS}
    assert stats["processed"] == 1 and stats["lost_ids"] == []
    it = q.get("x")
    assert it.status == SUCCESS and it.attempts == 1 and it.output == {"done": True}


def test_process_skips_item_whose_lease_was_lost(tmp_path, clock):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path, worker_id="A", lease_duration=60, on_abandoned="requeue")
    q.add({"n": 1}, reference="x")
    other = WorkQueue.open(path, worker_id="B", lease_duration=60, on_abandoned="requeue")
    stolen = []

    def stalls(item):
        clock[0] += 61  # A stalls past its lease without renewing...
        stolen.append(other.next())  # ...and B takes the item over
        return {"by": "A"}

    stats = q.process(stalls, retries=0)
    assert stats["processed"] == 0 and stats["lost_ids"] == [stolen[0].id]
    assert stats[IN_PROGRESS] == 1  # B still owns it
    other.complete(stolen[0], {"by": "B"})
    assert q.refresh().get("x").output == {"by": "B"}


def test_in_memory_queue_never_abandons_its_own_items(clock):
    q = WorkQueue("mem", lease_duration=1)
    q.add({"n": 1})
    q.add({"n": 2})
    first = q.next()
    clock[0] += 100
    second = q.next()
    assert first.status == IN_PROGRESS and second.status == IN_PROGRESS
    q.complete(first)
    q.complete(second)
    assert q.summary()[SUCCESS] == 2
