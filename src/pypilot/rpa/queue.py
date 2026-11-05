"""Orchestrator-style transactional work queues.

A :class:`WorkQueue` holds *transactions* (one :class:`QueueItem` per unit of
work, with a ``dict`` payload) and processes them with a handler, tracking a
status per item exactly like UiPath Orchestrator queues:

``new`` -> ``in_progress`` -> ``success`` | ``failed`` | ``business_exception``

* A :class:`BusinessException` (bad input data, a rule violation: "invoice
  already paid") marks the item ``business_exception`` and is **never retried**.
* Any other exception is a *system exception* (the app crashed, an element was
  not found...) and is retried up to ``retries`` times before the item is
  marked ``failed``.
* With ``path=...`` the queue is persisted to JSON after every transition, so a
  crashed or interrupted run **resumes** where it stopped: re-opening the queue
  keeps finished items. Items left ``in_progress`` by a crash may already have
  had their side effects (the payment went out, then the robot died), so by
  default they are marked ``failed`` (error type ``Abandoned``) for a human to
  check, like Orchestrator's *Abandoned* status; ``on_abandoned="requeue"``
  puts them back to ``new`` instead, still bounded by ``process(retries=...)``
  because :attr:`QueueItem.attempts` is persisted and enforced across runs.
  An item whose handler is interrupted (Ctrl+C, kill hotkey, fail-safe) is
  handled the same way (``failed`` with error type ``Interrupted`` by default).
* Items loaded from files get a ``reference`` derived from the file's resolved
  path and the row's *content* (``/abs/path/invoices.csv#<hash>``), and
  duplicate references are ignored, so re-running "load CSV then process" is
  idempotent, rows inserted into the file later are picked up, and a different
  file with the same name is not mistaken for the first one. When the data has
  a business key (an invoice number), pass ``reference_field=`` -- it is the
  only reference that survives edits to a row or moving the file. A row whose
  key is blank, or two different rows of one load with the same key, raise
  ``ValueError`` (nothing is added): a transaction is never dropped silently.

::

    q = WorkQueue("invoices", path="invoices.queue.json")
    q.load("invoices.csv", reference_field="invoice_no")

    def handle(item):
        if float(item["amount"]) <= 0:
            raise BusinessException("amount must be positive")
        d.find('edit "Amount"').set_text(item["amount"])
        return {"posted": True}          # stored in item.output

    print(q.process(handle, retries=2))
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import json
import os
import pathlib
import threading
import time
import traceback as _tb
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Union

from .. import failsafe as _failsafe
from ..errors import FailSafeTriggered, PypilotError

__all__ = [
    "BusinessException",
    "QueueItem",
    "WorkQueue",
    "NEW",
    "IN_PROGRESS",
    "SUCCESS",
    "FAILED",
    "BUSINESS_EXCEPTION",
    "ITEM_STATUSES",
    "ABANDONED_POLICIES",
]

#: Attribute :class:`~pypilot.rpa.robot.Robot` sets on the ``ActionTimeout`` of a task's own deadline.
TASK_DEADLINE_ATTR = "_pypilot_task_deadline"

NEW = "new"
IN_PROGRESS = "in_progress"
SUCCESS = "success"
FAILED = "failed"
BUSINESS_EXCEPTION = "business_exception"
#: Every valid :attr:`QueueItem.status`.
ITEM_STATUSES = (NEW, IN_PROGRESS, SUCCESS, FAILED, BUSINESS_EXCEPTION)
_FINAL = (SUCCESS, FAILED, BUSINESS_EXCEPTION)
#: Valid ``on_abandoned`` choices of :class:`WorkQueue`.
ABANDONED_POLICIES = ("fail", "requeue")

PathLike = Union[str, "os.PathLike[str]"]
_FORMAT_VERSION = 1


class BusinessException(PypilotError):
    """A business-rule failure for one transaction (invalid data, rule violated).

    Unlike system exceptions it is deterministic, so retrying is pointless:
    queues mark the item ``business_exception`` immediately and robots do not
    retry tasks/steps that raise it.
    """


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict())
        except Exception:
            pass
    return str(value)


@dataclass
class QueueItem:
    """One transaction.

    ``item["field"]`` / ``item.get("field")`` read the payload, so handlers can
    treat the item like the row it came from.
    """

    payload: Dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    reference: Optional[str] = None
    status: str = NEW
    priority: int = 0
    attempts: int = 0
    created_at: float = field(default_factory=_now)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    traceback: Optional[str] = None
    output: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.payload, dict):
            raise TypeError(f"payload must be a dict, not {type(self.payload).__name__}")
        if self.status not in ITEM_STATUSES:
            raise ValueError(f"status must be one of {', '.join(ITEM_STATUSES)}, got {self.status!r}")

    # payload access
    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.payload

    @property
    def is_final(self) -> bool:
        return self.status in _FINAL

    @property
    def duration(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0.0, self.finished_at - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "reference": self.reference,
            "status": self.status,
            "priority": self.priority,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "error_type": self.error_type,
            "traceback": self.traceback,
            "payload": _jsonable(self.payload),
            "output": _jsonable(self.output),
            "history": _jsonable(self.history),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QueueItem":
        return cls(
            payload=dict(d.get("payload") or {}),
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            reference=d.get("reference"),
            status=str(d.get("status", NEW)),
            priority=int(d.get("priority", 0) or 0),
            attempts=int(d.get("attempts", 0) or 0),
            created_at=float(d.get("created_at") or _now()),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            error=d.get("error"),
            error_type=d.get("error_type"),
            traceback=d.get("traceback"),
            output=dict(d.get("output") or {}),
            history=list(d.get("history") or []),
        )

    def __repr__(self) -> str:
        ref = f" ref={self.reference!r}" if self.reference else ""
        return f"<QueueItem {self.id}{ref} {self.status} attempts={self.attempts}>"


class WorkQueue:
    """A persistent, resumable transaction queue (see module docstring).

    Args:
        name: Queue name (used in summaries/reports).
        path: JSON file to persist to. If it exists, its state is loaded
            (resume); items left ``in_progress`` are handled per ``on_abandoned``.
        autosave: Save after every change (only when ``path`` is set).
        unique_references: Ignore :meth:`add` calls whose ``reference`` is
            already in the queue (returns the existing item instead).
        on_abandoned: What to do on resume with items a previous run left
            ``in_progress`` (it died mid-transaction, possibly after the side
            effect): ``"fail"`` (default) marks them ``failed`` with error type
            ``Abandoned`` so a human can check and :meth:`retry_items` them;
            ``"requeue"`` puts them back to ``new`` (their attempt still counts
            towards ``process(retries=...)``). Also applies to an item whose
            handler :meth:`process` sees interrupted (error type ``Interrupted``).
    """

    def __init__(
        self,
        name: str = "queue",
        path: Optional[PathLike] = None,
        *,
        autosave: bool = True,
        unique_references: bool = True,
        on_abandoned: str = "fail",
    ):
        if on_abandoned not in ABANDONED_POLICIES:
            raise ValueError(f"on_abandoned must be one of {', '.join(ABANDONED_POLICIES)}, got {on_abandoned!r}")
        self.on_abandoned = on_abandoned
        self.name = name
        self.path: Optional[pathlib.Path] = pathlib.Path(path).expanduser() if path is not None else None
        self.autosave = autosave
        self.unique_references = unique_references
        self._items: List[QueueItem] = []
        self._by_id: Dict[str, QueueItem] = {}
        self._by_ref: Dict[str, QueueItem] = {}
        self._lock = threading.RLock()
        self.created_at = _now()
        self.resumed = False
        if self.path is not None and self.path.exists():
            self._load_state(self.path)
            self.resumed = True

    # ---------------------------------------------------------------- basics
    @classmethod
    def open(cls, path: PathLike, name: Optional[str] = None, **kw: Any) -> "WorkQueue":
        """Open (resume) the queue persisted at ``path``; its stored name wins unless ``name`` is given."""
        q = cls(name or "queue", path, **kw)
        if name:
            q.name = name
        return q

    @property
    def items(self) -> List[QueueItem]:
        """All items in processing order (snapshot list)."""
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[QueueItem]:
        return iter(self.items)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, QueueItem):
            return item.id in self._by_id
        return item in self._by_id or item in self._by_ref

    def get(self, id_or_reference: str) -> Optional[QueueItem]:
        """Look an item up by id or reference."""
        with self._lock:
            return self._by_id.get(id_or_reference) or self._by_ref.get(id_or_reference)

    def by_status(self, *statuses: str) -> List[QueueItem]:
        for s in statuses:
            if s not in ITEM_STATUSES:
                raise ValueError(f"Unknown status {s!r}; expected one of {', '.join(ITEM_STATUSES)}")
        with self._lock:
            return [i for i in self._items if i.status in statuses]

    def pending(self) -> List[QueueItem]:
        """Items still to process (``new``), highest priority first, then insertion order."""
        with self._lock:
            new = [(i, it) for i, it in enumerate(self._items) if it.status == NEW]
        return [it for _, it in sorted(new, key=lambda p: (-p[1].priority, p[0]))]

    # ------------------------------------------------------------------ adding
    def add(self, payload: Dict[str, Any], *, reference: Optional[str] = None, priority: int = 0) -> QueueItem:
        """Add a transaction. With ``unique_references`` a known ``reference`` returns the existing item."""
        if not isinstance(payload, dict):
            raise TypeError(f"payload must be a dict, not {type(payload).__name__}")
        ref = None if reference is None else str(reference)
        with self._lock:
            if ref is not None and self.unique_references and ref in self._by_ref:
                return self._by_ref[ref]
            item = QueueItem(payload=dict(payload), reference=ref, priority=int(priority))
            while item.id in self._by_id:  # astronomically unlikely, but cheap to guard
                item.id = uuid.uuid4().hex[:12]
            self._append(item)
            self._autosave()
            return item

    def _append(self, item: QueueItem) -> None:
        self._items.append(item)
        self._by_id[item.id] = item
        if item.reference is not None and item.reference not in self._by_ref:
            self._by_ref[item.reference] = item

    def add_many(
        self,
        payloads: Iterable[Dict[str, Any]],
        *,
        reference_field: Optional[str] = None,
        reference_prefix: Optional[str] = None,
        priority: int = 0,
    ) -> List[QueueItem]:
        """Add several payloads (saved once at the end).

        ``reference_field`` takes each item's reference from that payload field;
        otherwise ``reference_prefix`` (if given) yields ``"<prefix>#<index>"``.
        With ``reference_field``, a missing field raises ``KeyError``, and a blank
        value, or a value shared by two *different* payloads of this call, raises
        ``ValueError``; every payload is checked before any is added. An exact
        duplicate payload (the same row exported twice) and a reference already
        in the queue (an earlier load) return the existing item, so reloading
        is idempotent.
        """
        added: List[QueueItem] = []
        entries: List[tuple] = []
        first_row: Dict[str, tuple] = {}
        for idx, payload in enumerate(payloads):
            if not isinstance(payload, dict):
                raise TypeError(f"item {idx}: payload must be a dict, not {type(payload).__name__}")
            ref: Optional[str] = None
            if reference_field is not None:
                if reference_field not in payload:
                    raise KeyError(f"item {idx}: reference field {reference_field!r} missing (fields: {', '.join(map(str, payload))})")
                value = payload[reference_field]
                if value is None or str(value).strip() == "":
                    raise ValueError(f"item {idx}: reference field {reference_field!r} is empty; every item needs its own reference")
                ref = str(value)
                if ref in first_row and first_row[ref][1] != payload:
                    raise ValueError(f"item {idx}: reference {ref!r} (field {reference_field!r}) is also item {first_row[ref][0]}, "
                                     "with different data; references must be unique")
                first_row.setdefault(ref, (idx, payload))
            elif reference_prefix is not None:
                ref = f"{reference_prefix}#{idx}"
            entries.append((payload, ref))
        with self._lock:
            saved, self.autosave = self.autosave, False
            try:
                for payload, ref in entries:
                    added.append(self.add(payload, reference=ref, priority=priority))
            finally:
                self.autosave = saved
            self._autosave()
        return added

    def load_csv(
        self,
        path: PathLike,
        *,
        delimiter: str = ",",
        encoding: str = "utf-8-sig",
        reference_field: Optional[str] = None,
        priority: int = 0,
        skip_blank: bool = True,
        strict: bool = True,
    ) -> List[QueueItem]:
        """Add one item per CSV row (header row = field names). Returns the items (existing ones on resume).

        With ``strict`` (the default) a row with more fields than the header (an
        unquoted ``1,250.00`` would otherwise queue ``amount="1"``) or fewer (a
        truncated line) raises ``ValueError`` naming the line, before anything is
        added; ``strict=False`` drops the extra fields and blanks the missing ones.
        """
        p = pathlib.Path(path).expanduser()
        with open(p, "r", encoding=encoding, newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            if reader.fieldnames is None:
                return []
            width = len(reader.fieldnames)
            rows = []
            for row in reader:
                extra = row.get(None) or []  # type: ignore[call-overload]
                missing = [k for k, v in row.items() if k is not None and v is None]
                clean = {str(k).strip(): (v if v is not None else "") for k, v in row.items() if k is not None}
                if skip_blank and all(str(v).strip() == "" for v in list(clean.values()) + list(extra)):
                    continue
                if strict and (extra or missing):
                    got = width + len(extra) if extra else width - len(missing)
                    raise ValueError(
                        f"{p}:{reader.line_num}: expected {width} field(s) like the header, got {got}"
                        + (" (quote values that contain the delimiter, e.g. \"1,234.56\")" if extra else "")
                        + "; nothing was added (pass strict=False to accept such rows)"
                    )
                rows.append(clean)
        return self._add_source_rows(p, rows, reference_field, priority)

    def load_json(
        self,
        path: PathLike,
        *,
        reference_field: Optional[str] = None,
        key: Optional[str] = None,
        priority: int = 0,
    ) -> List[QueueItem]:
        """Add items from a JSON file: a list of objects, or an object whose ``key`` (default ``"items"``) is one."""
        p = pathlib.Path(path).expanduser()
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            k = key or "items"
            if k not in data:
                raise ValueError(f"{p}: expected a list of objects or an object with an {k!r} list")
            data = data[k]
        if not isinstance(data, list):
            raise ValueError(f"{p}: expected a list of objects, got {type(data).__name__}")
        return self._add_source_rows(p, data, reference_field, priority)

    def load_xlsx(
        self,
        path: PathLike,
        *,
        sheet: Optional[str] = None,
        reference_field: Optional[str] = None,
        priority: int = 0,
    ) -> List[QueueItem]:
        """Add one item per worksheet row (first row = headers). Requires ``openpyxl``."""
        try:
            import openpyxl  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ImportError("Reading .xlsx files requires openpyxl: pip install openpyxl") from exc
        p = pathlib.Path(path).expanduser()
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        try:
            if sheet is not None:
                if sheet not in wb.sheetnames:
                    raise ValueError(f"{p}: no sheet {sheet!r} (sheets: {', '.join(wb.sheetnames)})")
                ws = wb[sheet]
            else:
                ws = wb.worksheets[0]
            rows_iter = ws.iter_rows(values_only=True)
            header = next(rows_iter, None)
            if header is None:
                return []
            names = [str(h).strip() if h is not None else f"column{i + 1}" for i, h in enumerate(header)]
            rows = []
            for values in rows_iter:
                if values is None or all(v is None or str(v).strip() == "" for v in values):
                    continue
                rows.append({names[i]: _jsonable(v) if v is not None else "" for i, v in enumerate(values) if i < len(names)})
        finally:
            wb.close()
        return self._add_source_rows(p, rows, reference_field, priority)

    @staticmethod
    def source_reference(path: PathLike, payload: Any, occurrence: int = 1) -> str:
        """The reference :meth:`load` gives a row without ``reference_field``.

        ``"<resolved path>#<sha256 of the row's canonical JSON, 16 hex>"`` -- stable
        when rows are inserted, removed or reordered in the file, and distinct for
        same-named files in different folders. The ``occurrence``-th identical row
        of one file (2, 3...) gets a ``~<n>`` suffix so genuine duplicates are kept.
        """
        try:
            resolved = str(pathlib.Path(path).expanduser().resolve())
        except OSError:
            resolved = str(pathlib.Path(path).expanduser().absolute())
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        ref = f"{resolved}#{digest}"
        return ref if occurrence <= 1 else f"{ref}~{occurrence}"

    def _add_source_rows(self, path: pathlib.Path, rows: Sequence[Any], reference_field: Optional[str], priority: int) -> List[QueueItem]:
        if reference_field is not None:
            return self.add_many(rows, reference_field=reference_field, priority=priority)
        added: List[QueueItem] = []
        seen: Dict[str, int] = {}
        with self._lock:
            saved, self.autosave = self.autosave, False
            try:
                for idx, payload in enumerate(rows):
                    if not isinstance(payload, dict):
                        raise TypeError(f"item {idx}: payload must be a dict, not {type(payload).__name__}")
                    base = self.source_reference(path, payload)
                    seen[base] = seen.get(base, 0) + 1
                    ref = base if seen[base] == 1 else f"{base}~{seen[base]}"
                    added.append(self.add(payload, reference=ref, priority=priority))
            finally:
                self.autosave = saved
            self._autosave()
        return added

    def load(self, path: PathLike, **kw: Any) -> List[QueueItem]:
        """Dispatch on the extension: ``.csv``/``.tsv``, ``.json``, ``.xlsx``/``.xlsm``."""
        ext = pathlib.Path(path).suffix.lower()
        if ext == ".csv":
            return self.load_csv(path, **kw)
        if ext == ".tsv":
            kw.setdefault("delimiter", "\t")
            return self.load_csv(path, **kw)
        if ext == ".json":
            return self.load_json(path, **kw)
        if ext in (".xlsx", ".xlsm"):
            return self.load_xlsx(path, **kw)
        raise ValueError(f"Unsupported queue source {str(path)!r}: expected .csv, .tsv, .json or .xlsx")

    # ------------------------------------------------------------ transactions
    def next(self) -> Optional[QueueItem]:
        """Take the next pending item (highest priority first) and mark it ``in_progress``."""
        with self._lock:
            pending = self.pending()
            if not pending:
                return None
            item = pending[0]
            self._start(item)
            self._autosave()
            return item

    def _start(self, item: QueueItem) -> None:
        item.status = IN_PROGRESS
        item.attempts += 1
        item.started_at = item.started_at or _now()
        item.finished_at = None
        item.history.append({"time": _now(), "event": "started", "attempt": item.attempts})

    def _item(self, item: Union[QueueItem, str]) -> QueueItem:
        found = self.get(item) if isinstance(item, str) else item
        if found is None or found.id not in self._by_id:
            raise KeyError(f"Item {item!r} is not in queue {self.name!r}")
        return found

    def complete(self, item: Union[QueueItem, str], output: Optional[Dict[str, Any]] = None) -> QueueItem:
        """Mark ``item`` ``success`` (merging ``output`` into :attr:`QueueItem.output`)."""
        with self._lock:
            it = self._item(item)
            it.status = SUCCESS
            it.finished_at = _now()
            it.error = it.error_type = it.traceback = None
            if output:
                it.output.update(output)
            it.history.append({"time": it.finished_at, "event": SUCCESS, "attempt": it.attempts})
            self._autosave()
            return it

    def fail(self, item: Union[QueueItem, str], error: Union[BaseException, str], *, business: Optional[bool] = None) -> QueueItem:
        """Mark ``item`` ``failed`` or ``business_exception``.

        ``business=None`` decides from the error: :class:`BusinessException` instances are business failures.
        """
        with self._lock:
            it = self._item(item)
            is_business = isinstance(error, BusinessException) if business is None else bool(business)
            it.status = BUSINESS_EXCEPTION if is_business else FAILED
            it.finished_at = _now()
            self._set_error(it, error)
            it.history.append({"time": it.finished_at, "event": it.status, "attempt": it.attempts, "error": it.error})
            self._autosave()
            return it

    @staticmethod
    def _set_error(it: QueueItem, error: Union[BaseException, str]) -> None:
        if isinstance(error, BaseException):
            it.error = str(error) or type(error).__name__
            it.error_type = type(error).__name__
            it.traceback = "".join(_tb.format_exception(type(error), error, error.__traceback__)) if error.__traceback__ else None
        else:
            it.error, it.error_type, it.traceback = str(error), None, None

    def retry_items(self, statuses: Sequence[str] = (FAILED,), *, reset_attempts: bool = True) -> List[QueueItem]:
        """Put items with the given statuses back to ``new`` (e.g. retry everything that failed)."""
        with self._lock:
            items = self.by_status(*statuses)
            for it in items:
                self._reset(it, "requeued", reset_attempts=reset_attempts)
            self._autosave()
            return items

    @staticmethod
    def _reset(it: QueueItem, event: str, *, reset_attempts: bool) -> None:
        it.status = NEW
        it.started_at = it.finished_at = None
        it.error = it.error_type = it.traceback = None
        if reset_attempts:
            it.attempts = 0
        it.history.append({"time": _now(), "event": event})

    def remove(self, item: Union[QueueItem, str]) -> QueueItem:
        with self._lock:
            it = self._item(item)
            self._items.remove(it)
            del self._by_id[it.id]
            if it.reference is not None and self._by_ref.get(it.reference) is it:
                del self._by_ref[it.reference]
            self._autosave()
            return it

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._by_id.clear()
            self._by_ref.clear()
            self._autosave()

    def process(
        self,
        handler: Callable[[QueueItem], Any],
        *,
        retries: int = 1,
        retry_delay: float = 0.0,
        backoff: float = 2.0,
        limit: Optional[int] = None,
        stop_on_failure: bool = False,
        on_item: Optional[Callable[[QueueItem], Any]] = None,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> Dict[str, Any]:
        """Process every pending item with ``handler(item)``.

        Args:
            handler: Called with the :class:`QueueItem`. A returned ``dict`` is
                merged into :attr:`QueueItem.output`. Raise
                :class:`BusinessException` for business failures (no retry);
                any other exception is a system failure, retried ``retries``
                times (``retries=1`` = up to 2 attempts). Attempts are counted
                per item across runs (:attr:`QueueItem.attempts` is persisted), so
                an item that keeps crashing the process ends ``failed`` instead of
                being replayed on every restart.
            retry_delay / backoff: Delay before the first retry, multiplied by ``backoff`` each time.
            limit: Process at most this many items in this call.
            stop_on_failure: Stop after the first item that ends ``failed``.
            on_item: Callback invoked with each item after it reaches a final status.
            sleep: Sleep function for retry delays (inject ``lambda s: None`` in tests);
                :func:`pypilot.failsafe.abort` (the kill hotkey) ends the wait at once.

        :class:`~pypilot.errors.FailSafeTriggered` and ``KeyboardInterrupt``
        stop processing immediately, the queue is saved and the exception
        propagates. If the handler was running, its side effect (the payment)
        may already have happened, so the item is treated like one a crash left
        ``in_progress``: per ``on_abandoned``, ``failed`` with error type
        ``Interrupted`` for a human to check (default), or back to ``new``
        (``"requeue"``; the attempt counts). An interrupt during a retry wait
        (no attempt running) puts the item back to ``new`` without counting
        the attempt it was waiting for. When the enclosing robot task runs out of time
        (its ``timeout``), the current item is marked ``failed`` without retry
        (it may be half done), processing stops and the timeout propagates; the
        remaining items stay ``new``.

        Returns:
            :meth:`summary` plus ``processed`` (items finished by this call) and
            ``processed_ids``.
        """
        if int(retries) < 0:
            raise ValueError("retries must be >= 0")
        if retry_delay < 0:
            raise ValueError("retry_delay must be >= 0")
        processed: List[str] = []
        while limit is None or len(processed) < int(limit):
            item = self.next()
            if item is None:
                break
            delay = float(retry_delay)
            budget = int(retries) + 1
            if item.attempts > budget:
                # Earlier runs already used every attempt (e.g. the item crashed the process each time).
                self.fail(item, f"retry limit reached: {item.attempts - 1} earlier attempt(s), retries={int(retries)}", business=False)
                processed.append(item.id)
                if on_item is not None:
                    on_item(item)
                if stop_on_failure:
                    break
                continue
            while True:
                try:
                    result = handler(item)
                except BusinessException as exc:
                    self.fail(item, exc, business=True)
                    break
                except (FailSafeTriggered, KeyboardInterrupt) as exc:
                    self._interrupted_in_handler(item, exc)
                    raise
                except Exception as exc:
                    if getattr(exc, TASK_DEADLINE_ATTR, False):
                        # The robot task's deadline: every later attempt would fail at once. Stop here.
                        self.fail(item, exc, business=False)
                        raise
                    if item.attempts >= budget:
                        self.fail(item, exc, business=False)
                        break
                    with self._lock:
                        self._set_error(item, exc)
                        item.history.append({"time": _now(), "event": "retry", "attempt": item.attempts, "error": item.error})
                        item.attempts += 1
                        self._autosave()
                    if delay > 0:
                        try:
                            if sleep is time.sleep:
                                # Wakes up at once on abort() (kill hotkey) instead of after the whole delay.
                                _failsafe.abortable_sleep(delay)
                            else:
                                sleep(delay)
                            _failsafe._raise_if_aborted()
                        except (FailSafeTriggered, KeyboardInterrupt):
                            self._interrupted_in_wait(item)  # the attempt it was waiting for never started
                            raise
                    delay *= float(backoff)
                    continue
                self.complete(item, result if isinstance(result, dict) else None)
                if result is not None and not isinstance(result, dict):
                    with self._lock:
                        item.output["result"] = _jsonable(result)
                        self._autosave()
                break
            processed.append(item.id)
            if on_item is not None:
                on_item(item)
            if stop_on_failure and item.status == FAILED:
                break
        stats = self.summary()
        stats["processed"] = len(processed)
        stats["processed_ids"] = processed
        return stats

    def _interrupted_in_wait(self, item: QueueItem) -> None:
        with self._lock:
            # Stopped by the human between attempts: no attempt was running, so nothing
            # can be half done. The attempt it was waiting for does not count.
            item.attempts = max(0, item.attempts - 1)
            self._reset(item, "interrupted", reset_attempts=False)
            self._autosave()

    def _interrupted_in_handler(self, item: QueueItem, exc: BaseException) -> None:
        with self._lock:
            # Stopped while the handler was running: its side effect (the payment) may
            # already have happened, exactly like a crash mid-transaction -> on_abandoned.
            if self.on_abandoned == "requeue":
                self._reset(item, "interrupted", reset_attempts=False)  # the attempt still counts
            else:
                item.status = FAILED
                item.finished_at = _now()
                reason = str(exc) or type(exc).__name__
                item.error = (f"interrupted while processing this item ({reason}); "
                              "its side effects may already have happened - check, then retry_items() if needed")
                item.error_type = "Interrupted"
                item.traceback = None
                item.history.append({"time": item.finished_at, "event": "interrupted", "attempt": item.attempts,
                                     "error": item.error})
            self._autosave()

    # -------------------------------------------------------------------- stats
    def summary(self) -> Dict[str, Any]:
        """Counts per status plus ``total``, ``done``, ``success_rate`` (of finished items),
        ``attempts`` and ``avg_duration`` (seconds, finished items)."""
        with self._lock:
            items = list(self._items)
        counts = {s: 0 for s in ITEM_STATUSES}
        for it in items:
            counts[it.status] += 1
        done = counts[SUCCESS] + counts[FAILED] + counts[BUSINESS_EXCEPTION]
        durations = [d for d in (it.duration for it in items if it.is_final) if d is not None]
        return {
            "name": self.name,
            "total": len(items),
            **counts,
            "done": done,
            "remaining": counts[NEW] + counts[IN_PROGRESS],
            "success_rate": round(counts[SUCCESS] / done, 4) if done else 0.0,
            "attempts": sum(it.attempts for it in items),
            "avg_duration": round(sum(durations) / len(durations), 6) if durations else 0.0,
        }

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "format": "pypilot.workqueue",
                "version": _FORMAT_VERSION,
                "name": self.name,
                "created_at": self.created_at,
                "saved": _iso(_now()),
                "summary": self.summary(),
                "items": [it.to_dict() for it in self._items],
            }

    def save(self, path: Optional[PathLike] = None) -> str:
        """Write the queue state to ``path`` (default :attr:`path`) atomically. Returns the path."""
        target = pathlib.Path(path).expanduser() if path is not None else self.path
        if target is None:
            raise ValueError("No path given and the queue has no persistence path")
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
        return str(target)

    def _autosave(self) -> None:
        if self.autosave and self.path is not None:
            self.save()

    def _load_state(self, path: pathlib.Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not a valid queue file: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError(f"{path} is not a pypilot work queue file (no 'items' list)")
        self.name = str(data.get("name") or self.name)
        self.created_at = float(data.get("created_at") or self.created_at)
        for raw in data["items"]:
            item = QueueItem.from_dict(raw)
            if item.status == IN_PROGRESS:  # the previous run died mid-transaction
                if self.on_abandoned == "requeue":
                    self._reset(item, "abandoned", reset_attempts=False)
                else:
                    item.status = FAILED
                    item.finished_at = _now()
                    item.error = ("abandoned: the previous run stopped while processing this item; "
                                  "its side effects may already have happened - check, then retry_items() if needed")
                    item.error_type = "Abandoned"
                    item.traceback = None
                    item.history.append({"time": item.finished_at, "event": "abandoned", "attempt": item.attempts})
            if item.id in self._by_id:
                continue
            self._append(item)

    def export_csv(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        """Write a flat CSV report: id, reference, status, attempts, error, payload and output fields."""
        with self._lock:
            items = list(self._items)
        payload_keys: List[str] = []
        output_keys: List[str] = []
        for it in items:
            for k in it.payload:
                if k not in payload_keys:
                    payload_keys.append(k)
            for k in it.output:
                if f"output.{k}" not in output_keys:
                    output_keys.append(f"output.{k}")
        base = ["id", "reference", "status", "attempts", "error_type", "error"]
        p = pathlib.Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding=encoding, newline="") as fh:
            w = csv.writer(fh)
            w.writerow(base + payload_keys + output_keys)
            for it in items:
                row = [it.id, it.reference or "", it.status, it.attempts, it.error_type or "", it.error or ""]
                row += [_cell(it.payload.get(k, "")) for k in payload_keys]
                row += [_cell(it.output.get(k[len("output."):], "")) for k in output_keys]
                w.writerow(row)
        return str(p)

    def __repr__(self) -> str:
        s = self.summary()
        return f"<WorkQueue {self.name!r} total={s['total']} new={s[NEW]} success={s[SUCCESS]} failed={s[FAILED]} business={s[BUSINESS_EXCEPTION]}>"


def _cell(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return "" if v is None else str(v)
