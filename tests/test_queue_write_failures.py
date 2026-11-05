"""WorkQueue: a failed write keeps nothing of its transaction; a removed item is a lost lease, not a KeyError."""

from __future__ import annotations

import json

import pytest

import locum.rpa.queue as queue_mod
from locum.rpa.queue import IN_PROGRESS, NEW, SUCCESS, LeaseLostError, WorkQueue


def _disk_status(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return {it["reference"]: it["status"] for it in data["items"]}


def _fail_replace_once(monkeypatch, exc=PermissionError("sharing violation")):
    real = queue_mod._replace
    state = {"armed": True}

    def flaky(src, dst):
        if state["armed"]:
            state["armed"] = False
            raise exc
        return real(src, dst)

    monkeypatch.setattr(queue_mod, "_replace", flaky)
    return state


def test_next_whose_write_fails_leaves_item_new(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="a")
    q.add({"n": 2}, reference="b")
    _fail_replace_once(monkeypatch)
    with pytest.raises(PermissionError):
        q.next()
    assert _disk_status(path) == {"a": NEW, "b": NEW}
    # The claim that was never persisted is not kept: the next claims get both items, once each.
    first, second = q.next(), q.next()
    assert {first.reference, second.reference} == {"a", "b"}
    assert q.next() is None
    assert _disk_status(path) == {"a": IN_PROGRESS, "b": IN_PROGRESS}


def test_process_after_failed_next_runs_every_item(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add_many([{"n": i} for i in range(3)])
    _fail_replace_once(monkeypatch)
    with pytest.raises(PermissionError):
        q.next()
    seen = []
    stats = q.process(lambda it: seen.append(it["n"]))
    assert sorted(seen) == [0, 1, 2]
    assert stats[SUCCESS] == 3 and stats[IN_PROGRESS] == 0
    assert set(_disk_status(path).values()) == {SUCCESS}
    assert WorkQueue.open(path).summary()[SUCCESS] == 3


def test_complete_whose_write_fails_is_not_persisted_later(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="a")
    q.add({"n": 2}, reference="b")
    a = q.next()
    _fail_replace_once(monkeypatch)
    with pytest.raises(PermissionError):
        q.complete(a)
    # An unrelated later write must not smuggle the unwritten "success" onto disk.
    q.add({"n": 3}, reference="c")
    assert _disk_status(path)["a"] == IN_PROGRESS
    assert a.status == IN_PROGRESS
    # The worker still holds the lease and can complete the item for real.
    q.complete(a, {"ok": True})
    assert _disk_status(path)["a"] == SUCCESS


def test_failed_write_rolls_back_generation(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="a")
    gen = json.loads(path.read_text(encoding="utf-8"))["generation"]
    _fail_replace_once(monkeypatch, OSError(28, "No space left on device"))
    with pytest.raises(OSError):
        q.add({"n": 2}, reference="b")
    assert q._generation == gen
    assert "b" not in q  # re-read from the file
    q.add({"n": 2}, reference="b")
    assert json.loads(path.read_text(encoding="utf-8"))["generation"] == gen + 1


def test_failed_first_write_leaves_empty_queue(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    _fail_replace_once(monkeypatch)
    with pytest.raises(PermissionError):
        q.add({"n": 1}, reference="a")
    assert not path.exists()
    q.add({"n": 2}, reference="b")
    assert _disk_status(path) == {"b": NEW}
    assert [it.reference for it in q.items] == ["b"]


def test_complete_of_item_removed_by_other_worker_raises_lease_lost(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="a")
    a = q.next()
    other = WorkQueue("q", path)
    other.remove(a.id)
    with pytest.raises(LeaseLostError, match="removed"):
        q.complete(a)
    with pytest.raises(KeyError):  # an id this worker never held stays a KeyError
        q.complete("missing")


@pytest.mark.parametrize("call", ["fail", "renew", "release"])
def test_other_operations_on_removed_item_raise_lease_lost(tmp_path, call):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="a")
    a = q.next()
    WorkQueue("q", path).clear()
    with pytest.raises(LeaseLostError):
        if call == "fail":
            q.fail(a, "boom")
        else:
            getattr(q, call)(a)


def test_process_skips_items_removed_mid_handler(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add_many([{"n": i} for i in range(3)])
    other = WorkQueue("q", path)
    seen = []

    def handler(it):
        seen.append(it["n"])
        if it["n"] == 0:
            other.refresh()
            other.remove(it.id)

    stats = q.process(handler)
    assert sorted(seen) == [0, 1, 2]
    assert len(stats["lost_ids"]) == 1 and stats["processed"] == 2
    assert stats[SUCCESS] == 2 and stats["total"] == 2


def test_retry_bookkeeping_on_removed_item_is_lost_lease(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("q", path)
    q.add({"n": 1}, reference="a")
    other = WorkQueue("q", path)

    def handler(it):
        other.refresh()
        other.remove(it.id)
        raise RuntimeError("system error -> would retry")

    stats = q.process(handler, retries=2)
    assert stats["lost_ids"] and stats["processed"] == 0


def test_in_memory_remove_of_claimed_item(tmp_path):
    q = WorkQueue("q")
    q.add({"n": 1}, reference="a")
    a = q.next()
    q.remove(a)
    with pytest.raises(LeaseLostError):
        q.complete(a)
