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
  keeps finished items.
* Claiming an item (:meth:`WorkQueue.next`, used by :meth:`WorkQueue.process`)
  gives the worker a **lease**: the item records ``worker_id`` and
  ``lease_expires_at`` (default ``lease_duration`` 30 minutes).
  :meth:`WorkQueue.process` renews the lease automatically while the handler
  runs (every ``lease_duration / 3``); code that calls ``next()`` itself must
  call :meth:`WorkQueue.renew` for items that take longer than the lease. An
  ``in_progress`` item is *abandoned* -- its worker died -- when its lease has
  expired, when it has no lease at all (files written by older locum
  versions), or when its worker process on this machine no longer exists
  (``reclaim_dead_workers=True``, the default). Abandoned items may already
  have had their side effects (the payment went out, then the robot died), so
  by default they are marked ``failed`` (error type ``Abandoned``) for a human
  to check, like Orchestrator's *Abandoned* status; ``on_abandoned="requeue"``
  puts them back to ``new`` instead, still bounded by ``process(retries=...)``
  because :attr:`QueueItem.attempts` is persisted and enforced across runs.
  An item whose handler is interrupted (Ctrl+C, kill hotkey, fail-safe) is
  handled the same way (``failed`` with error type ``Interrupted`` by default).
  A worker whose lease was taken over (it was considered dead) gets
  :class:`LeaseLostError` from ``complete``/``fail``/``renew`` instead of
  overwriting the new owner's state; the same happens when another worker
  removed the item (``remove``/``clear``) while this one was running it.
  ``process()`` skips such items and lists them in ``lost_ids``.

Concurrency guarantees (``path=...`` with ``autosave=True``, the default):

* Several threads, several :class:`WorkQueue` instances and several
  **processes on one machine** may share one queue file on a **local
  filesystem**. Every mutating operation (``add``/``load``, ``next``,
  ``complete``, ``fail``, ``renew``, ``release``, ``retry_items``, ``remove``,
  ``clear``, ``save``) runs under an exclusive OS lock on the sidecar file
  ``<path>.lock`` (``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX)
  plus an in-process lock, re-reads the file if another writer changed it (a
  ``generation`` counter in the file, bumped on every write), applies the
  change, writes a temp file, ``flush`` + ``os.fsync``, ``os.replace``, and on
  POSIX fsyncs the directory. No snapshot taken outside the lock is ever
  written, so an item is claimed by exactly one worker and no update is lost.
* If an operation or its write fails (disk full, a Windows sharing violation
  that outlasts the retries, Ctrl+C), the exception propagates and nothing of
  that operation is kept: claims it made are dropped (a ``next()`` that raised
  leaves its item ``new``) and the next operation re-reads the file, so state
  that was never written is never written later either.
* Read-only accessors (``items``, ``get``, ``pending``, ``summary``...) show
  this instance's view as of its last operation; :meth:`WorkQueue.refresh`
  re-reads the file (``process()`` does so before returning its summary).
* Exactly-once *processing* is only as strong as the lease: an item is run
  twice only if its worker stalls past the lease without renewing and
  ``on_abandoned="requeue"`` hands it to another worker (with the default
  ``"fail"`` it goes to a human instead).

Not guaranteed / unsupported: network shares (SMB, NFS...), and workers on
**several machines** sharing one file (file locks are unreliable there, and
"is that worker's process still alive" cannot be checked); a queue opened with
``autosave=False`` is single-owner -- its explicit :meth:`WorkQueue.save`
writes that instance's state under the lock without merging other writers'
changes. Older locum versions do not take the lock, so do not mix them with
this one on a shared file. A jump of the system clock larger than the lease
can expire leases early.
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

import contextlib
import csv
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import socket
import threading
import time
import traceback as _tb
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from .. import failsafe as _failsafe
from ..errors import FailSafeTriggered, LocumError

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None  # type: ignore[assignment]

__all__ = [
    "BusinessException",
    "LeaseLostError",
    "QueueItem",
    "WorkQueue",
    "NEW",
    "IN_PROGRESS",
    "SUCCESS",
    "FAILED",
    "BUSINESS_EXCEPTION",
    "ITEM_STATUSES",
    "ABANDONED_POLICIES",
    "synced_location",
    "default_queue_dir",
]

#: Attribute :class:`~locum.rpa.robot.Robot` sets on the ``ActionTimeout`` of a task's own deadline.
TASK_DEADLINE_ATTR = "_locum_task_deadline"

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
#: Still 1: version-1 files load unchanged and the new keys (``generation``, lease fields) are optional.
_FORMAT_VERSION = 1
#: Default lease of a claimed item, seconds (renewed every third of it by :meth:`WorkQueue.process`).
DEFAULT_LEASE_DURATION = 30 * 60.0
#: Default time to wait for the queue file lock before raising ``TimeoutError``, seconds.
DEFAULT_LOCK_TIMEOUT = 60.0

_HOST = socket.gethostname()
_HEADER_RE = re.compile(rb'^\{\s*"format":\s*"locum\.workqueue",\s*"version":\s*\d+,\s*"generation":\s*(\d+)')


class BusinessException(LocumError):
    """A business-rule failure for one transaction (invalid data, rule violated).

    Unlike system exceptions it is deterministic, so retrying is pointless:
    queues mark the item ``business_exception`` immediately and robots do not
    retry tasks/steps that raise it.
    """


class LeaseLostError(LocumError):
    """This worker no longer holds (or never held) the lease of an ``in_progress`` item.

    Raised by :meth:`WorkQueue.complete`, :meth:`WorkQueue.fail`,
    :meth:`WorkQueue.renew` and :meth:`WorkQueue.release` instead of
    overwriting the state another worker owns: the lease expired (or this
    worker was considered dead) and the item was reclaimed, the item is
    being processed by another live worker, or the item was removed from the
    queue while this worker held it.
    """


def _now() -> float:
    return time.time()


def _default_worker_id() -> str:
    return f"{_HOST}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _pid_alive(pid: int) -> bool:
    """Whether process ``pid`` exists on this machine. Unknown answers count as alive (never steal)."""
    if pid <= 0:
        return True
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such process
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                k32.CloseHandle(handle)
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:  # PermissionError (someone else's process) and anything unexpected
        return True
    return True


# ------------------------------------------------------------------ file locking
_PATH_LOCKS: Dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _reset_path_locks() -> None:  # pragma: no cover - runs in forked children
    global _PATH_LOCKS_GUARD
    _PATH_LOCKS.clear()
    _PATH_LOCKS_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):  # a lock held by another thread of the parent would never be released
    os.register_at_fork(after_in_child=_reset_path_locks)


def _path_lock(key: str) -> threading.Lock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = threading.Lock()
        return lock


def _os_trylock(fd: int) -> None:
    """Take the exclusive OS lock on ``fd`` or raise ``OSError`` if another holder has it."""
    if _msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
    elif _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    if _msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
    elif _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)


@contextlib.contextmanager
def _file_lock(lock_path: pathlib.Path, timeout: float) -> Iterator[None]:
    """Exclusive lock shared by threads (in-process lock) and processes (OS lock on ``lock_path``)."""
    deadline = time.monotonic() + timeout
    plock = _path_lock(os.path.normcase(os.path.abspath(str(lock_path))))
    if not plock.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"timed out after {timeout:g}s waiting for the queue lock {lock_path}")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            delay = 0.001
            while True:
                try:
                    _os_trylock(fd)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out after {timeout:g}s waiting for the queue lock {lock_path} "
                                           "(held by another process)") from None
                    time.sleep(delay)
                    delay = min(delay * 2, 0.05)
            try:
                yield
            finally:
                _os_unlock(fd)
        finally:
            os.close(fd)
    finally:
        plock.release()


def _peek_generation(path: pathlib.Path) -> Optional[int]:
    """The ``generation`` of a file this module wrote (read from its first bytes), else ``None``."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return None
    m = _HEADER_RE.match(head)
    return int(m.group(1)) if m else None


def _replace(src: pathlib.Path, dst: pathlib.Path) -> None:
    # On Windows a reader without FILE_SHARE_DELETE (antivirus, an indexer, an editor) briefly blocks the replace.
    for attempt in range(40):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 39:
                raise
            time.sleep(0.025)


def _fsync_dir(directory: pathlib.Path) -> None:
    if os.name == "nt":  # directories cannot be opened for fsync on Windows; NTFS journals the rename
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _same_file(a: pathlib.Path, b: pathlib.Path) -> bool:
    def norm(p: pathlib.Path) -> str:
        return os.path.normcase(os.path.abspath(str(p)))

    return norm(a) == norm(b)


#: Environment variables the OneDrive client sets to its folders.
_SYNC_ENV_VARS = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
#: Folder names of common sync clients (compared case-insensitively, per path component).
_SYNC_FOLDERS = frozenset({"onedrive", "dropbox", "google drive", "my drive", "icloud drive", "iclouddrive",
                           "mobile documents", "box", "box sync"})


def _is_under(path: str, root: str) -> bool:
    try:
        p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives
        return False


def _is_remote_drive(path: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return False
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False


def synced_location(path: PathLike) -> str:
    """Why ``path`` is not a safe place for a live queue file ("" if it is).

    Flags cloud-synced folders (the OneDrive folders named by ``%OneDrive%`` /
    ``%OneDriveCommercial%`` / ``%OneDriveConsumer%``, any ``OneDrive`` /
    ``OneDrive - <org>`` / Dropbox / Google Drive / iCloud / Box folder) and
    network locations (UNC paths, mapped network drives).
    """
    p = os.path.abspath(os.path.expanduser(str(path)))
    if p.startswith("\\\\") or p.startswith("//"):
        return "a network share (UNC path)"
    for var in _SYNC_ENV_VARS:
        root = os.environ.get(var)
        if root and _is_under(p, root):
            return f"a OneDrive folder ({root})"
    for part in pathlib.PurePath(p).parts:
        low = part.lower().strip("\\/")
        if low in _SYNC_FOLDERS or low.startswith("onedrive - "):
            return f"a cloud-synced folder ({part!r})"
    if _is_remote_drive(p):
        return "a network drive"
    return ""


def default_queue_dir() -> pathlib.Path:
    """A local, unsynced folder for live queue files.

    ``%LOCALAPPDATA%\\locum\\queues`` on Windows (never roamed or synced),
    ``$XDG_STATE_HOME/locum/queues`` (default ``~/.local/state/locum/queues``)
    elsewhere. Not created until used.
    """
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return pathlib.Path(os.environ["LOCALAPPDATA"]) / "locum" / "queues"
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return pathlib.Path(base) / "locum" / "queues"


def _atomic_write(target: pathlib.Path, text: str) -> None:
    """Write ``text`` to a temp file, fsync it, rename it over ``target``, fsync the directory."""
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(target.parent)


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


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
    #: Worker that claimed the item most recently (kept after it finishes, for auditing).
    worker_id: Optional[str] = None
    #: Host name and process id of that worker (used to detect a dead worker on this machine).
    worker_host: Optional[str] = None
    worker_pid: Optional[int] = None
    #: Identifies one claim; ``None`` unless the item is ``in_progress``.
    lease_id: Optional[str] = None
    #: Epoch seconds after which an ``in_progress`` item counts as abandoned; ``None`` unless ``in_progress``.
    lease_expires_at: Optional[float] = None

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
            "worker_id": self.worker_id,
            "worker_host": self.worker_host,
            "worker_pid": self.worker_pid,
            "lease_id": self.lease_id,
            "lease_expires_at": self.lease_expires_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QueueItem":
        pid = d.get("worker_pid")
        expires = d.get("lease_expires_at")
        return cls(
            worker_id=d.get("worker_id"),
            worker_host=d.get("worker_host"),
            worker_pid=int(pid) if pid is not None else None,
            lease_id=d.get("lease_id"),
            lease_expires_at=float(expires) if expires is not None else None,
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
            (resume); abandoned ``in_progress`` items are handled per ``on_abandoned``.
            Several processes on one machine may share it (see module docstring).
        autosave: Save after every change (only when ``path`` is set). With
            ``False`` the instance is the file's only owner: nothing is
            written until :meth:`save`, which does not merge other writers' changes.
        unique_references: Ignore :meth:`add` calls whose ``reference`` is
            already in the queue (returns the existing item instead).
        on_abandoned: What to do with ``in_progress`` items whose worker died
            mid-transaction, possibly after the side effect (lease expired, no
            lease, or worker process gone): ``"fail"`` (default) marks them
            ``failed`` with error type ``Abandoned`` so a human can check and
            :meth:`retry_items` them; ``"requeue"`` puts them back to ``new``
            (their attempt still counts towards ``process(retries=...)``). Also
            applies to an item whose handler :meth:`process` sees interrupted
            (error type ``Interrupted``).
        worker_id: This worker's name, recorded on the items it claims
            (default ``"<host>:<pid>:<random>"``).
        lease_duration: Seconds a claimed item stays reserved for this worker
            without a :meth:`renew` (default 30 minutes; :meth:`process`
            renews automatically).
        lock_timeout: Seconds to wait for the queue file lock before raising
            ``TimeoutError`` (default 60).
        reclaim_dead_workers: Treat an ``in_progress`` item as abandoned before
            its lease expires when the worker that claimed it was a process on
            this machine that no longer exists (default ``True``; disable when
            workers with the same host name run in separate PID namespaces).
        allow_synced: Accept a ``path`` inside a cloud-synced folder (OneDrive,
            Dropbox, Google Drive, iCloud) or on a network drive. Refused by
            default (``ValueError``): a sync client replaces, locks and
            "conflict-copies" files behind the queue's back, which defeats the
            file lock and the atomic rename. Keep the live queue in a local
            folder (:func:`default_queue_dir`) and :meth:`export` finished
            state into the synced folder instead.
    """

    def __init__(
        self,
        name: str = "queue",
        path: Optional[PathLike] = None,
        *,
        autosave: bool = True,
        unique_references: bool = True,
        on_abandoned: str = "fail",
        worker_id: Optional[str] = None,
        lease_duration: float = DEFAULT_LEASE_DURATION,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        reclaim_dead_workers: bool = True,
        allow_synced: bool = False,
    ):
        if path is not None and not allow_synced:
            why = synced_location(path)
            if why:
                raise ValueError(
                    f"WorkQueue path {str(path)!r} is in {why}: the queue's file lock and atomic rename are "
                    "only reliable on a local, unsynced disk. Keep the live queue local "
                    f"(e.g. {str(default_queue_dir())!r}) and copy finished state with WorkQueue.export(); "
                    "pass allow_synced=True to override"
                )
        if on_abandoned not in ABANDONED_POLICIES:
            raise ValueError(f"on_abandoned must be one of {', '.join(ABANDONED_POLICIES)}, got {on_abandoned!r}")
        if not float(lease_duration) > 0:
            raise ValueError("lease_duration must be > 0")
        if not float(lock_timeout) > 0:
            raise ValueError("lock_timeout must be > 0")
        self.on_abandoned = on_abandoned
        self.name = name
        self.path: Optional[pathlib.Path] = pathlib.Path(path).expanduser() if path is not None else None
        self.autosave = autosave
        self.unique_references = unique_references
        self.worker_id = str(worker_id) if worker_id is not None else _default_worker_id()
        self.lease_duration = float(lease_duration)
        self.lock_timeout = float(lock_timeout)
        self.reclaim_dead_workers = bool(reclaim_dead_workers)
        self._items: List[QueueItem] = []
        self._by_id: Dict[str, QueueItem] = {}
        self._by_ref: Dict[str, QueueItem] = {}
        self._lock = threading.RLock()
        self._claims: Dict[str, str] = {}  # item id -> lease_id of the claims this instance holds
        self._generation = 0
        self._disk_sig: Optional[Tuple[int, int, int]] = None
        self._resync = False  # a transaction failed: memory may differ from the file, re-read it
        self._gone_claims: set = set()  # ids of items we had claimed that were removed from the queue
        self._txn_depth = 0
        self._dirty = False
        self.created_at = _now()
        self.resumed = False
        self._initial_load = True  # the first load takes the stored name / created_at
        if self.path is not None and self.path.exists():
            if self.autosave:
                with self._txn():
                    pass  # load, recover abandoned items, and write the recovery
            else:
                with self._lock, _file_lock(self._lock_path(self.path), self.lock_timeout):
                    self._refresh_locked()
            self.resumed = True
        self._initial_load = False

    # ---------------------------------------------------------- transactions
    @staticmethod
    def _lock_path(path: pathlib.Path) -> pathlib.Path:
        return path.with_name(path.name + ".lock")

    def _persistent(self) -> bool:
        return self.path is not None and self.autosave

    def _touch(self) -> None:
        """Mark the state changed: the enclosing transaction writes it."""
        self._dirty = True

    @contextlib.contextmanager
    def _txn(self) -> Iterator[None]:
        """Run a mutation atomically: thread lock; with persistence also file lock, reload, write.

        Re-entrant: a nested call (``add_many`` -> ``add``) joins the outer
        transaction, which writes once at the end.
        """
        with self._lock:
            if self._txn_depth or not self._persistent():
                yield
                return
            assert self.path is not None
            with _file_lock(self._lock_path(self.path), self.lock_timeout):
                self._dirty = False
                claims_before = dict(self._claims)
                try:
                    self._txn_depth += 1
                    try:
                        if self._refresh_locked():
                            self._dirty = True
                        claims_before = dict(self._claims)  # recovery may end claims; that is disk truth now
                        yield
                    finally:
                        self._txn_depth -= 1
                    if self._dirty:
                        self._write_locked(self.path)
                except BaseException:
                    # The mutation or its write failed, so the file does not have this transaction's
                    # changes. Memory may: forget the claims it made (a next() whose caller got the
                    # exception must not keep the item reserved) and re-read the file next time.
                    self._claims = claims_before
                    self._resync = True
                    try:
                        self._refresh_locked()  # best effort now; otherwise the next transaction does it
                    except Exception:
                        self._resync = True
                    raise
                finally:
                    self._dirty = False

    def refresh(self) -> "WorkQueue":
        """Re-read the queue file (other workers' changes) and recover abandoned items. Returns ``self``.

        Existing :class:`QueueItem` objects are updated in place. With
        ``autosave=False`` this replaces unsaved in-memory changes with the file's state.
        """
        if self.path is None:
            return self
        if self._persistent():
            with self._txn():
                pass
        elif self.path.exists():
            with self._lock, _file_lock(self._lock_path(self.path), self.lock_timeout):
                self._disk_sig = None
                self._refresh_locked()
        return self

    def _refresh_locked(self) -> bool:
        """With the file lock held: load the file if another writer changed it. True if recovery changed items."""
        assert self.path is not None
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            st = None
        if st is not None:
            gen = _peek_generation(self.path)
            sig = (gen if gen is not None else -1, st.st_size, st.st_mtime_ns)
            if gen is None or self._resync or sig != self._disk_sig:
                self._apply_file(self._read_file(self.path))
                self._disk_sig = sig
        elif self._resync:
            # A failed transaction before the file was ever written: the truth is an empty queue.
            self._apply_file({"items": []})
            self._disk_sig = None
        self._resync = False
        return self._recover_abandoned()

    @staticmethod
    def _read_file(path: pathlib.Path) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not a valid queue file: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError(f"{path} is not a locum work queue file (no 'items' list)")
        return data

    def _apply_file(self, data: Dict[str, Any]) -> None:
        """Make the file's state ours, updating the existing :class:`QueueItem` objects in place."""
        if self._initial_load:
            self.name = str(data.get("name") or self.name)
            self.created_at = float(data.get("created_at") or self.created_at)
        try:
            self._generation = max(self._generation, int(data.get("generation") or 0))
        except (TypeError, ValueError):
            pass
        items: List[QueueItem] = []
        by_id: Dict[str, QueueItem] = {}
        by_ref: Dict[str, QueueItem] = {}
        for raw in data["items"]:
            fresh = QueueItem.from_dict(raw)
            if fresh.id in by_id:
                continue
            current = self._by_id.get(fresh.id)
            if current is not None:
                if self._owns(current) and fresh.status == IN_PROGRESS and fresh.lease_id == current.lease_id:
                    fresh = current  # our live claim: only we change it, keep what the handler did to it
                else:
                    for f in dataclasses.fields(QueueItem):
                        setattr(current, f.name, getattr(fresh, f.name))
                    fresh = current
            items.append(fresh)
            by_id[fresh.id] = fresh
            if fresh.reference is not None and fresh.reference not in by_ref:
                by_ref[fresh.reference] = fresh
        self._items, self._by_id, self._by_ref = items, by_id, by_ref
        for item_id in [i for i in self._claims if i not in by_id]:
            self._gone_claims.add(item_id)  # removed by another worker while we held it
            del self._claims[item_id]

    def _write_locked(self, target: pathlib.Path) -> None:
        previous = self._generation
        self._generation += 1
        try:
            text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)
            _atomic_write(target, text)
        except BaseException:
            self._generation = previous  # nothing was written
            raise
        try:
            st = os.stat(target)
            self._disk_sig = (self._generation, st.st_size, st.st_mtime_ns)
        except OSError:
            self._disk_sig = None  # written, but re-read it next time to be sure

    # ----------------------------------------------------------------- leases
    def _owns(self, it: QueueItem) -> bool:
        claim = self._claims.get(it.id)
        return claim is not None and it.status == IN_PROGRESS and it.lease_id == claim

    def _abandon_reason(self, it: QueueItem, now: float) -> Optional[str]:
        if it.status != IN_PROGRESS or self._owns(it):
            return None
        if it.lease_expires_at is None:
            return "it has no lease (written by an older locum or a run that crashed)"
        if now >= it.lease_expires_at:
            return f"the lease of worker {it.worker_id!r} expired"
        if (self.reclaim_dead_workers and it.worker_host == _HOST and it.worker_pid is not None
                and not _pid_alive(int(it.worker_pid))):
            return f"worker {it.worker_id!r} (pid {it.worker_pid}) is no longer running"
        return None

    def _recover_abandoned(self) -> bool:
        """Apply ``on_abandoned`` to ``in_progress`` items whose worker died. True if anything changed."""
        if self.path is None:
            return False  # in memory there is no other worker: every in_progress item is one of ours
        now = _now()
        changed = False
        for it in self._items:
            reason = self._abandon_reason(it, now)
            if reason is None:
                continue
            changed = True
            previous = it.worker_id
            if self.on_abandoned == "requeue":
                self._reset(it, "abandoned", reset_attempts=False)
                it.history[-1].update({"attempt": it.attempts, "reason": reason, "worker": previous})
            else:
                it.status = FAILED
                it.finished_at = now
                it.error = ("abandoned: the previous run stopped while processing this item; "
                            "its side effects may already have happened - check, then retry_items() if needed"
                            f" ({reason})")
                it.error_type = "Abandoned"
                it.traceback = None
                self._end_lease(it)
                it.history.append({"time": now, "event": "abandoned", "attempt": it.attempts,
                                   "reason": reason, "worker": previous})
        return changed

    def _end_lease(self, it: QueueItem) -> None:
        it.lease_id = None
        it.lease_expires_at = None
        self._claims.pop(it.id, None)

    def _check_owner(self, it: QueueItem) -> None:
        """Raise :class:`LeaseLostError` if ``it`` is (or was) claimed and this worker is not the owner."""
        claim = self._claims.get(it.id)
        if claim is not None:
            if not self._owns(it):
                self._claims.pop(it.id, None)
                by = f" by worker {it.worker_id!r}" if it.status == IN_PROGRESS else f"; it is now {it.status!r}"
                raise LeaseLostError(f"Item {it.id} ({it.reference or 'no reference'}) of queue {self.name!r}: "
                                     f"this worker's lease was lost (it was reclaimed{by})")
        elif it.status == IN_PROGRESS and it.lease_id is not None and self._abandon_reason(it, _now()) is None:
            raise LeaseLostError(f"Item {it.id} ({it.reference or 'no reference'}) of queue {self.name!r} "
                                 f"is being processed by worker {it.worker_id!r}")

    def renew(self, item: Union[QueueItem, str], duration: Optional[float] = None) -> QueueItem:
        """Extend this worker's lease on ``item`` to ``duration`` (default ``lease_duration``) seconds from now.

        Raises :class:`LeaseLostError` when this worker does not hold the item's lease.
        """
        d = self.lease_duration if duration is None else float(duration)
        if not d > 0:
            raise ValueError("duration must be > 0")
        with self._txn():
            it = self._held_item(item)
            if not self._owns(it):
                self._check_owner(it)
                raise LeaseLostError(f"Item {it.id} of queue {self.name!r} is not claimed by this worker")
            it.lease_expires_at = _now() + d
            self._touch()
            return it

    def release(self, item: Union[QueueItem, str]) -> QueueItem:
        """Give a claimed item back (``new``) without running it; the attempt does not count."""
        with self._txn():
            it = self._held_item(item)
            if not self._owns(it):
                self._check_owner(it)
                raise LeaseLostError(f"Item {it.id} of queue {self.name!r} is not claimed by this worker")
            it.attempts = max(0, it.attempts - 1)
            self._reset(it, "released", reset_attempts=False)
            self._touch()
            return it

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
        with self._txn():
            if ref is not None and self.unique_references and ref in self._by_ref:
                return self._by_ref[ref]
            item = QueueItem(payload=dict(payload), reference=ref, priority=int(priority))
            while item.id in self._by_id:  # astronomically unlikely, but cheap to guard
                item.id = uuid.uuid4().hex[:12]
            self._append(item)
            self._touch()
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
        with self._txn():  # one lock, one write for the whole batch
            for payload, ref in entries:
                added.append(self.add(payload, reference=ref, priority=priority))
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
        entries: List[tuple] = []
        for idx, payload in enumerate(rows):  # validate every row before adding any
            if not isinstance(payload, dict):
                raise TypeError(f"item {idx}: payload must be a dict, not {type(payload).__name__}")
            base = self.source_reference(path, payload)
            seen[base] = seen.get(base, 0) + 1
            entries.append((payload, base if seen[base] == 1 else f"{base}~{seen[base]}"))
        with self._txn():  # one lock, one write for the whole file
            for payload, ref in entries:
                added.append(self.add(payload, reference=ref, priority=priority))
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
        """Claim the next pending item (highest priority first): mark it ``in_progress`` with a lease for this worker.

        With a queue file this is atomic across threads and processes: the file
        is re-read under its lock first, so no two workers get the same item,
        and abandoned items (expired lease, dead worker) are recovered per
        ``on_abandoned`` before choosing. The lease lasts ``lease_duration``
        seconds; call :meth:`renew` for longer work (:meth:`process` does).
        """
        with self._txn():
            pending = self.pending()
            if not pending:
                return None
            item = pending[0]
            self._start(item)
            self._touch()
            return item

    def _start(self, item: QueueItem) -> None:
        now = _now()
        item.status = IN_PROGRESS
        item.attempts += 1
        item.started_at = item.started_at or now
        item.finished_at = None
        item.worker_id = self.worker_id
        item.worker_host = _HOST
        item.worker_pid = os.getpid()
        item.lease_id = uuid.uuid4().hex
        item.lease_expires_at = now + self.lease_duration
        self._claims[item.id] = item.lease_id
        item.history.append({"time": now, "event": "started", "attempt": item.attempts, "worker": self.worker_id})

    def _item(self, item: Union[QueueItem, str]) -> QueueItem:
        found = self.get(item) if isinstance(item, str) else item
        if found is None or found.id not in self._by_id:
            raise KeyError(f"Item {item!r} is not in queue {self.name!r}")
        return found

    def _held_item(self, item: Union[QueueItem, str]) -> QueueItem:
        """Like :meth:`_item`, but an item this worker had claimed that was removed meanwhile is a lost lease."""
        item_id = item if isinstance(item, str) else item.id
        if item_id not in self._by_id:
            held = (item_id in self._gone_claims or item_id in self._claims
                    or (isinstance(item, QueueItem) and item.status == IN_PROGRESS
                        and item.worker_id == self.worker_id and item.lease_id is not None))
            if held:
                self._gone_claims.discard(item_id)
                self._claims.pop(item_id, None)
                raise LeaseLostError(f"Item {item_id} of queue {self.name!r} was removed from the queue "
                                     "while this worker was processing it")
        return self._item(item)

    def complete(self, item: Union[QueueItem, str], output: Optional[Dict[str, Any]] = None) -> QueueItem:
        """Mark ``item`` ``success`` (merging ``output`` into :attr:`QueueItem.output`).

        Raises :class:`LeaseLostError` if another worker owns the item now.
        """
        with self._txn():
            it = self._held_item(item)
            self._check_owner(it)
            self._end_lease(it)
            it.status = SUCCESS
            it.finished_at = _now()
            it.error = it.error_type = it.traceback = None
            if output:
                it.output.update(output)
            it.history.append({"time": it.finished_at, "event": SUCCESS, "attempt": it.attempts})
            self._touch()
            return it

    def fail(self, item: Union[QueueItem, str], error: Union[BaseException, str], *, business: Optional[bool] = None) -> QueueItem:
        """Mark ``item`` ``failed`` or ``business_exception``.

        ``business=None`` decides from the error: :class:`BusinessException` instances are business failures.
        Raises :class:`LeaseLostError` if another worker owns the item now.
        """
        with self._txn():
            it = self._held_item(item)
            self._check_owner(it)
            self._end_lease(it)
            is_business = isinstance(error, BusinessException) if business is None else bool(business)
            it.status = BUSINESS_EXCEPTION if is_business else FAILED
            it.finished_at = _now()
            self._set_error(it, error)
            it.history.append({"time": it.finished_at, "event": it.status, "attempt": it.attempts, "error": it.error})
            self._touch()
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
        with self._txn():
            items = self.by_status(*statuses)
            for it in items:
                self._reset(it, "requeued", reset_attempts=reset_attempts)
            if items:
                self._touch()
            return items

    def _reset(self, it: QueueItem, event: str, *, reset_attempts: bool) -> None:
        self._end_lease(it)
        it.worker_id = it.worker_host = it.worker_pid = None
        it.status = NEW
        it.started_at = it.finished_at = None
        it.error = it.error_type = it.traceback = None
        if reset_attempts:
            it.attempts = 0
        it.history.append({"time": _now(), "event": event})

    def remove(self, item: Union[QueueItem, str]) -> QueueItem:
        with self._txn():
            it = self._item(item)
            self._items.remove(it)
            del self._by_id[it.id]
            if self._claims.pop(it.id, None) is not None:
                self._gone_claims.add(it.id)
            if it.reference is not None and self._by_ref.get(it.reference) is it:
                del self._by_ref[it.reference]
            self._touch()
            return it

    def clear(self) -> None:
        with self._txn():
            self._items.clear()
            self._by_id.clear()
            self._by_ref.clear()
            self._gone_claims.update(self._claims)
            self._claims.clear()
            self._touch()

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
                :func:`locum.failsafe.abort` (the kill hotkey) ends the wait at once.

        :class:`~locum.errors.FailSafeTriggered` and ``KeyboardInterrupt``
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

        Several workers (threads or processes) may call ``process`` on one
        queue file at once: each claims its items with :meth:`next`, and the
        lease of the current item is renewed every ``lease_duration / 3``
        seconds while its handler runs and during retry waits. If the lease is
        lost anyway (this worker stalled past it and another worker reclaimed
        the item), the item is left to its new owner and listed in ``lost_ids``.

        Returns:
            :meth:`summary` (re-read from the file, so it covers every worker)
            plus ``processed`` (items finished by this call), ``processed_ids``
            and ``lost_ids``.
        """
        if int(retries) < 0:
            raise ValueError("retries must be >= 0")
        if retry_delay < 0:
            raise ValueError("retry_delay must be >= 0")
        processed: List[str] = []
        lost: List[str] = []
        while limit is None or len(processed) < int(limit):
            item = self.next()
            if item is None:
                break
            heartbeat = self._start_heartbeat(item)
            try:
                self._process_one(item, handler, retries=int(retries), retry_delay=float(retry_delay),
                                  backoff=float(backoff), sleep=sleep)
            except LeaseLostError:
                # Another worker took the item over (this one was considered dead): its state is theirs now.
                lost.append(item.id)
                continue
            finally:
                if heartbeat is not None:
                    heartbeat()
            processed.append(item.id)
            if on_item is not None:
                on_item(item)
            if stop_on_failure and item.status == FAILED:
                break
        if self._persistent():
            self.refresh()  # the summary covers every worker's progress
        stats = self.summary()
        stats["processed"] = len(processed)
        stats["processed_ids"] = processed
        stats["lost_ids"] = lost
        return stats

    def _process_one(self, item: QueueItem, handler: Callable[[QueueItem], Any], *, retries: int,
                     retry_delay: float, backoff: float, sleep: Callable[[float], Any]) -> None:
        delay = retry_delay
        budget = retries + 1
        if item.attempts > budget:
            # Earlier runs already used every attempt (e.g. the item crashed the process each time).
            self.fail(item, f"retry limit reached: {item.attempts - 1} earlier attempt(s), retries={retries}", business=False)
            return
        while True:
            try:
                result = handler(item)
            except BusinessException as exc:
                self.fail(item, exc, business=True)
                return
            except (FailSafeTriggered, KeyboardInterrupt) as exc:
                self._interrupted_in_handler(item, exc)
                raise
            except Exception as exc:
                if getattr(exc, TASK_DEADLINE_ATTR, False):
                    # The robot task's deadline: every later attempt would fail at once. Stop here.
                    try:
                        self.fail(item, exc, business=False)
                    except LeaseLostError:
                        pass
                    raise
                if item.attempts >= budget:
                    self.fail(item, exc, business=False)
                    return
                with self._txn():
                    self._held_item(item)
                    self._check_owner(item)
                    self._set_error(item, exc)
                    item.history.append({"time": _now(), "event": "retry", "attempt": item.attempts, "error": item.error})
                    item.attempts += 1
                    self._touch()
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
                delay *= backoff
                continue
            if isinstance(result, dict):
                self.complete(item, result)
            else:
                self.complete(item, None if result is None else {"result": _jsonable(result)})
            return

    def _start_heartbeat(self, item: QueueItem) -> Optional[Callable[[], None]]:
        """Renew ``item``'s lease every ``lease_duration / 3`` in a thread; returns the function that stops it."""
        if not self._persistent():
            return None
        stop = threading.Event()
        interval = max(self.lease_duration / 3.0, 0.01)

        def beat() -> None:
            while not stop.wait(interval):
                try:
                    self.renew(item)
                except LeaseLostError:
                    return
                except Exception:  # a lock timeout or a transient I/O error: try again next beat
                    continue

        thread = threading.Thread(target=beat, name=f"locum-queue-lease-{item.id}", daemon=True)
        thread.start()

        def stop_heartbeat() -> None:
            stop.set()
            thread.join()

        return stop_heartbeat

    def _interrupted_in_wait(self, item: QueueItem) -> None:
        with self._txn():
            if not self._owns(item):
                return  # another worker has it now
            # Stopped by the human between attempts: no attempt was running, so nothing
            # can be half done. The attempt it was waiting for does not count.
            item.attempts = max(0, item.attempts - 1)
            self._reset(item, "interrupted", reset_attempts=False)
            self._touch()

    def _interrupted_in_handler(self, item: QueueItem, exc: BaseException) -> None:
        with self._txn():
            if not self._owns(item):
                return  # another worker has it now
            self._touch()
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
                self._end_lease(item)
                item.history.append({"time": item.finished_at, "event": "interrupted", "attempt": item.attempts,
                                     "error": item.error})

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
                "format": "locum.workqueue",
                "version": _FORMAT_VERSION,
                # Keep "generation" third: _peek_generation reads it from the first bytes of the file.
                "generation": self._generation,
                "name": self.name,
                "created_at": self.created_at,
                "saved": _iso(_now()),
                "summary": self.summary(),
                "items": [it.to_dict() for it in self._items],
            }

    def save(self, path: Optional[PathLike] = None) -> str:
        """Write the queue state to ``path`` (default :attr:`path`) atomically and durably. Returns the path.

        Saving to the queue's own file (with ``autosave``) first re-reads it under
        the lock, so other workers' changes are kept, never overwritten by an
        older snapshot. With ``autosave=False`` this instance's state is written
        as is (single owner). Another ``path`` gets a copy of this instance's state.
        Every write goes to a temp file that is fsynced, then renamed over the target.
        """
        target = pathlib.Path(path).expanduser() if path is not None else self.path
        if target is None:
            raise ValueError("No path given and the queue has no persistence path")
        with self._lock:
            if self.path is not None and _same_file(target, self.path):
                if self._persistent():
                    with self._txn():
                        self._touch()
                else:
                    with _file_lock(self._lock_path(self.path), self.lock_timeout):
                        self._generation = max(self._generation, _peek_generation(self.path) or 0)
                        self._write_locked(self.path)
                return str(target)
            with _file_lock(self._lock_path(target), self.lock_timeout):
                snapshot = self.to_dict()
                snapshot["generation"] = max(self._generation, _peek_generation(target) or 0) + 1
                _atomic_write(target, json.dumps(snapshot, indent=2, ensure_ascii=False, default=str))
        return str(target)

    def export(self, path: PathLike) -> str:
        """Copy this queue's current state to ``path`` as JSON (atomic temp + rename). Returns the path.

        Unlike :meth:`save` to another path, this takes no ``.lock`` sidecar at
        the destination and never merges with it: the copy is an archive (a
        report for a synced vault, an audit trail), not a second live queue.
        The live file is re-read first so the copy includes every worker's
        changes. Open an exported file with ``WorkQueue.open(..., allow_synced=True)``
        only to read it.
        """
        with self._lock:
            if self.path is not None and self._persistent():
                self.refresh()
            snapshot = self.to_dict()
        target = pathlib.Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, json.dumps(snapshot, indent=2, ensure_ascii=False, default=str))
        return str(target)

    def export_csv(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        """Write a flat CSV report: id, reference, status, attempts, error, payload and output fields.

        Output fields are ``output.<k>`` columns; a payload field whose name is already a
        column (``status``, ``id``, ``error``...) is written as ``payload.<k>``.
        """
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
            # A payload field named like a fixed column (status, id, error...) or an output.*
            # column is written as payload.<k>: duplicate headers make DictReader / pandas
            # read the payload value in place of the queue's own column.
            taken = set(base) | set(output_keys)
            payload_headers = [f"payload.{k}" if str(k) in taken else k for k in payload_keys]
            w.writerow(base + payload_headers + output_keys)
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
