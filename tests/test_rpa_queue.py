from __future__ import annotations

import csv
import json

import pytest

from pypilot.errors import FailSafeTriggered
from pypilot.rpa import BusinessException, WorkQueue
from pypilot.rpa.queue import BUSINESS_EXCEPTION, FAILED, IN_PROGRESS, NEW, SUCCESS, QueueItem


def test_add_and_item_access():
    q = WorkQueue("q")
    a = q.add({"n": 1}, reference="A")
    b = q.add({"n": 2})
    assert len(q) == 2 and a.status == NEW
    assert a["n"] == 1 and a.get("missing", 5) == 5 and "n" in a
    assert q.get("A") is a and q.get(b.id) is b
    assert q.add({"n": 99}, reference="A") is a  # duplicate reference ignored
    assert len(q) == 2
    with pytest.raises(TypeError):
        q.add(["not", "a", "dict"])  # type: ignore[arg-type]


def test_priority_order_and_next():
    q = WorkQueue()
    low = q.add({"x": "low"})
    high = q.add({"x": "high"}, priority=5)
    assert q.pending() == [high, low]
    item = q.next()
    assert item is high and item.status == IN_PROGRESS and item.attempts == 1
    q.complete(item, {"ok": True})
    assert item.status == SUCCESS and item.output == {"ok": True}


def test_process_success_business_and_system_failures():
    q = WorkQueue("invoices")
    for i, amount in enumerate([10, -1, 7, 3]):
        q.add({"amount": amount, "i": i}, reference=f"inv{i}")
    calls = {}
    sleeps = []

    def handler(item):
        calls[item.reference] = calls.get(item.reference, 0) + 1
        if item["amount"] < 0:
            raise BusinessException("amount must be positive")
        if item["amount"] == 7:
            raise RuntimeError("app crashed")
        if item["amount"] == 3 and calls[item.reference] == 1:
            raise TimeoutError("slow app")
        return {"posted": item["amount"]}

    stats = q.process(handler, retries=2, retry_delay=0.5, backoff=2, sleep=sleeps.append)
    assert calls == {"inv0": 1, "inv1": 1, "inv2": 3, "inv3": 2}
    assert q.get("inv0").status == SUCCESS and q.get("inv0").output == {"posted": 10}
    biz = q.get("inv1")
    assert biz.status == BUSINESS_EXCEPTION and biz.error_type == "BusinessException" and biz.attempts == 1
    sysfail = q.get("inv2")
    assert sysfail.status == FAILED and sysfail.attempts == 3 and "app crashed" in sysfail.error
    assert "Traceback" in sysfail.traceback
    assert q.get("inv3").status == SUCCESS and q.get("inv3").attempts == 2
    assert sleeps == [0.5, 1.0, 0.5]
    assert stats["processed"] == 4
    assert stats[SUCCESS] == 2 and stats[FAILED] == 1 and stats[BUSINESS_EXCEPTION] == 1
    assert stats["success_rate"] == 0.5 and stats["remaining"] == 0
    assert stats["attempts"] == 1 + 1 + 3 + 2


def test_process_limit_stop_on_failure_and_non_dict_result():
    q = WorkQueue()
    for i in range(5):
        q.add({"i": i})
    seen = []
    stats = q.process(lambda it: it["i"] * 10, limit=2, on_item=seen.append)
    assert stats["processed"] == 2 and len(seen) == 2
    assert seen[0].output == {"result": 0}
    stats = q.process(lambda it: (_ for _ in ()).throw(ValueError("x")), retries=0, stop_on_failure=True)
    assert stats["processed"] == 1 and stats[NEW] == 2
    with pytest.raises(ValueError):
        q.process(lambda it: None, retries=-1)


def test_persistence_and_resume(tmp_path):
    path = tmp_path / "q.json"
    q = WorkQueue("persist", path)
    q.add_many([{"i": i} for i in range(4)], reference_prefix="src")
    assert path.is_file()
    done = []

    def handler(item):
        if item["i"] == 2:
            raise FailSafeTriggered("user abort")
        done.append(item["i"])

    with pytest.raises(FailSafeTriggered):
        q.process(handler)
    assert done == [0, 1]
    interrupted = q.get("src#2")
    # The handler was running: its side effect may have happened -> a human checks it.
    assert interrupted.status == FAILED and interrupted.error_type == "Interrupted"
    q.retry_items()  # the human checked it
    assert interrupted.status == NEW

    # Simulate a crash mid-transaction: an item left in_progress in the file.
    data = json.loads(path.read_text())
    data["items"][3]["status"] = IN_PROGRESS
    path.write_text(json.dumps(data))

    q2 = WorkQueue.open(path, on_abandoned="requeue")
    assert q2.resumed and q2.name == "persist"
    assert q2.get("src#3").status == NEW
    assert any(h["event"] == "abandoned" for h in q2.get("src#3").history)
    assert [it.status for it in q2.items] == [SUCCESS, SUCCESS, NEW, NEW]
    # Re-adding the same source is idempotent thanks to references.
    q2.add_many([{"i": i} for i in range(4)], reference_prefix="src")
    assert len(q2) == 4
    stats = q2.process(lambda it: done.append(it["i"]))
    assert done == [0, 1, 2, 3] and stats[SUCCESS] == 4 and stats["processed"] == 2


def test_corrupt_queue_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ValueError, match="not a valid queue file"):
        WorkQueue(path=p)
    p.write_text('{"foo": 1}')
    with pytest.raises(ValueError, match="no 'items' list"):
        WorkQueue(path=p)


def test_load_csv_and_json(tmp_path):
    c = tmp_path / "rows.csv"
    c.write_text("﻿id,name , amount\n1,Ann,10\n\n2,Bob,20\n", encoding="utf-8")
    q = WorkQueue()
    items = q.load_csv(c, reference_field="id")
    assert [i.reference for i in items] == ["1", "2"]
    assert items[0].payload == {"id": "1", "name": "Ann", "amount": "10"}
    assert q.load_csv(c, reference_field="id")[0] is items[0]  # idempotent
    assert len(q) == 2

    j = tmp_path / "rows.json"
    j.write_text(json.dumps({"items": [{"a": 1}, {"a": 2}]}))
    q2 = WorkQueue()
    got = q2.load(j)
    assert [i.reference for i in got] == [WorkQueue.source_reference(j, {"a": 1}), WorkQueue.source_reference(j, {"a": 2})]
    assert all(i.reference.startswith(str(j.resolve()) + "#") for i in got)
    j.write_text(json.dumps([{"a": 1}, "bad"]))
    with pytest.raises(TypeError):
        WorkQueue().load_json(j)
    with pytest.raises(KeyError, match="reference field"):
        WorkQueue().load_csv(c, reference_field="nope")
    with pytest.raises(ValueError, match="Unsupported"):
        WorkQueue().load(tmp_path / "x.txt")


def test_load_tsv(tmp_path):
    t = tmp_path / "rows.tsv"
    t.write_text("a\tb\n1\t2\n")
    q = WorkQueue()
    assert q.load(t)[0].payload == {"a": "1", "b": "2"}


def test_load_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["id", "qty", None])
    ws.append([7, 3, "x"])
    ws.append([None, None, None])
    ws.append([8, 1.5, None])
    p = tmp_path / "book.xlsx"
    wb.save(p)
    q = WorkQueue()
    items = q.load(p, sheet="Data", reference_field="id")
    assert [i.reference for i in items] == ["7", "8"]
    assert items[0].payload == {"id": 7, "qty": 3, "column3": "x"}
    with pytest.raises(ValueError, match="no sheet"):
        WorkQueue().load_xlsx(p, sheet="Nope")


def test_retry_items_remove_clear_export(tmp_path):
    q = WorkQueue()
    a = q.add({"v": 1}, reference="a")
    q.add({"v": 2}, reference="b")
    q.process(lambda it: (_ for _ in ()).throw(RuntimeError("down")) if it["v"] == 1 else {"ok": 1}, retries=0)
    assert a.status == FAILED
    requeued = q.retry_items()
    assert requeued == [a] and a.status == NEW and a.attempts == 0 and a.error is None
    out = q.export_csv(tmp_path / "out.csv")
    rows = list(csv.DictReader(open(out)))
    assert rows[1]["output.ok"] == "1" and rows[0]["status"] == NEW
    q.remove("a")
    assert "a" not in q and len(q) == 1
    q.clear()
    assert len(q) == 0
    with pytest.raises(KeyError):
        q.complete("missing")


def test_summary_empty_and_repr():
    q = WorkQueue("empty")
    s = q.summary()
    assert s["total"] == 0 and s["success_rate"] == 0.0
    assert "empty" in repr(q)


def test_item_roundtrip():
    it = QueueItem(payload={"a": 1}, reference="r")
    back = QueueItem.from_dict(it.to_dict())
    assert back.payload == {"a": 1} and back.reference == "r" and back.id == it.id
    with pytest.raises(ValueError):
        QueueItem(payload={}, status="weird")


def test_fail_with_message_and_explicit_business():
    q = WorkQueue()
    it = q.add({})
    q.next()
    q.fail(it, "bad data", business=True)
    assert it.status == BUSINESS_EXCEPTION and it.error == "bad data" and it.error_type is None
