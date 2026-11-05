"""Several workers (processes, threads, instances) sharing one WorkQueue file: no item lost or run twice."""

from __future__ import annotations

import collections
import json
import multiprocessing
import os
import sys
import threading

import pytest

from pypilot.rpa.queue import IN_PROGRESS, NEW, SUCCESS, WorkQueue

sys.path.insert(0, os.path.dirname(__file__))
import _queue_workers  # noqa: E402


def _final_items(path):
    return json.loads(path.read_text(encoding="utf-8"))["items"]


def test_processes_drain_one_file_exactly_once(tmp_path):
    path = tmp_path / "shared.queue.json"
    q = WorkQueue("shared", path)
    q.add_many([{"n": i} for i in range(200)], reference_prefix="row")
    ids = {it.id for it in q.items}
    logs = tmp_path / "logs"
    logs.mkdir()

    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    workers = [ctx.Process(target=_queue_workers.drain, args=(str(path), f"w{n}", str(logs), start))
               for n in range(4)]
    for w in workers:
        w.start()
    start.set()
    for w in workers:
        w.join(timeout=240)
    for w in workers:
        if w.is_alive():
            w.terminate()
    assert [w.exitcode for w in workers] == [0, 0, 0, 0]

    runs = collections.Counter()
    for log in logs.iterdir():
        runs.update(line for line in log.read_text(encoding="utf-8").splitlines() if line)
    assert set(runs) == ids  # nothing lost
    assert set(runs.values()) == {1}  # nothing run twice

    items = _final_items(path)
    assert len(items) == 200
    assert {it["status"] for it in items} == {SUCCESS}
    assert {it["attempts"] for it in items} == {1}
    for it in items:  # the worker that finished it is the one that ran it
        assert it["output"]["by"] == it["worker_id"]
        assert it["lease_id"] is None and it["lease_expires_at"] is None


def test_threads_on_two_instances_never_share_an_item(tmp_path):
    path = tmp_path / "q.json"
    seed = WorkQueue("q", path)
    seed.add_many([{"n": i} for i in range(120)], reference_prefix="r")
    queues = [WorkQueue.open(path, worker_id="A"), WorkQueue.open(path, worker_id="B")]
    taken = []
    taken_lock = threading.Lock()
    errors = []

    def worker(q):
        try:
            while True:
                it = q.next()
                if it is None:
                    return
                with taken_lock:
                    taken.append(it.id)
                q.complete(it, {"ok": True})
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(queues[n % 2],)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors
    assert len(taken) == 120 and len(set(taken)) == 120
    items = _final_items(path)
    assert len(items) == 120 and {it["status"] for it in items} == {SUCCESS}
    assert {it["attempts"] for it in items} == {1}


def test_threads_on_one_in_memory_queue_never_share_an_item():
    q = WorkQueue("mem")
    q.add_many([{"n": i} for i in range(300)])
    taken = []
    lock = threading.Lock()

    def worker():
        while True:
            it = q.next()
            if it is None:
                return
            with lock:
                taken.append(it.id)
            q.complete(it)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(taken) == 300 and len(set(taken)) == 300
    assert q.summary()[SUCCESS] == 300


def test_concurrent_saves_never_regress_state(tmp_path):
    path = tmp_path / "q.json"
    a = WorkQueue("q", path, worker_id="A")
    b = WorkQueue.open(path, worker_id="B")  # b's view is empty and stale from now on
    item = a.add({"n": 1}, reference="x")
    b.add({"n": 2}, reference="y")  # reloads a's item first instead of dropping it
    assert {it.reference for it in b.items} == {"x", "y"}
    a.complete(a.next())
    b.save()  # an explicit save of b's (stale) view must not undo a's completion
    disk = {it["reference"]: it["status"] for it in _final_items(path)}
    assert disk == {"x": SUCCESS, "y": NEW}
    assert b.get("x").status == SUCCESS and item.status == SUCCESS


def test_crashed_worker_process_is_recovered_before_its_lease_expires(tmp_path):
    path = tmp_path / "q.json"
    WorkQueue("q", path).add({"n": 1}, reference="x")
    out = tmp_path / "claimed.txt"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_queue_workers.claim_one, args=(str(path), str(out), 3600.0))
    proc.start()
    proc.join(timeout=120)
    assert proc.exitcode == 0 and out.read_text(encoding="utf-8")
    assert _final_items(path)[0]["status"] == IN_PROGRESS  # it "crashed" holding the item

    # The worker process is gone, so its hour-long lease does not block recovery.
    it = WorkQueue.open(path).get("x")
    assert it.status == "failed" and it.error_type == "Abandoned"
    assert "no longer running" in it.error


@pytest.mark.parametrize("policy", ["fail", "requeue"])
def test_live_claims_survive_another_worker_opening_the_file(tmp_path, policy):
    path = tmp_path / "q.json"
    a = WorkQueue("q", path, worker_id="A", on_abandoned=policy)
    a.add({"n": 1}, reference="x")
    claimed = a.next()
    b = WorkQueue.open(path, worker_id="B", on_abandoned=policy)  # used to mark every in_progress item abandoned
    assert b.get("x").status == IN_PROGRESS and b.get("x").worker_id == "A"
    assert b.next() is None
    a.complete(claimed)
    assert b.refresh().get("x").status == SUCCESS
