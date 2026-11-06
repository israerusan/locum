"""The desktop input lock: one owner of the mouse, keyboard and focus at a time.

The input guard (:mod:`locum._target`) *detects* that input would reach the
wrong window and refuses it. It cannot stop two workers that each drive the
same desktop from interleaving: worker A clicks a field, worker B's keystroke
moves the focus, A's text goes into B's dialog. Queue concurrency does not help
either -- every worker shares one physical mouse and one keyboard focus.

This lock serialises the *input*, not the work: an :class:`~locum.element.Element`
action (focus -> select all -> type -> read back) and every top-level
:class:`~locum.keyboard.Keyboard` / :class:`~locum.mouse.Mouse` call hold it, so
a whole compound action finishes before another thread or process may send
input. Accessibility-native actions (set-value, invoke) send no input and do
not take it, so SAP sessions or other workers can keep processing in parallel
and only queue up for the moments they need the mouse or keyboard.

Scope:

* Backends that drive the real desktop (Windows, macOS, X11) share one lock per
  interactive session across **all processes** of the user: a named mutex
  ``Local\\locum-desktop-input`` on Windows (released by the OS if its owner
  dies), an ``flock`` on ``$TMPDIR/locum-desktop-input-<uid>[-<display>].lock``
  on POSIX.
* Other backends (the mock desktop) get a lock per backend object, inside the
  process: two simulated desktops never contend. A backend can opt in to a
  named lock with a ``desktop_lock_name`` attribute (the tests do).
* The lock is re-entrant per thread: nested calls (``Element.click`` ->
  ``Mouse.click`` -> ``Mouse.move``) take it once.
* Releases (``key_up``, ``release_all``) never wait for it: a held key or
  button is always released.

``LOCUM_DESKTOP_LOCK=0`` disables it. :func:`desktop_input` holds it around a
multi-step sequence of your own; :func:`try_desktop_input` takes it only if it
is free right now (an idle "keep awake" nudger must never wait for, or
interrupt, a worker). A wait longer than the timeout (``Desktop(input_lock_timeout=...)``,
default 30 s) raises :class:`~locum.errors.DesktopBusy` naming the holder.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterator, Optional

from .errors import DesktopBusy

__all__ = ["DEFAULT_TIMEOUT", "LOCK_NAME", "hold", "desktop_input", "try_desktop_input", "locked", "holder", "enabled"]

#: Seconds to wait for the lock before raising :class:`DesktopBusy`.
DEFAULT_TIMEOUT = 30.0
#: Base name of the session-wide lock.
LOCK_NAME = "locum-desktop-input"
_ENV = "LOCUM_DESKTOP_LOCK"
_REAL_BACKENDS = frozenset({"windows", "macos", "x11"})
_POLL = 0.02


def enabled() -> bool:
    """Whether the lock is active (``LOCUM_DESKTOP_LOCK`` is not ``0``/``false``/``off``)."""
    return os.environ.get(_ENV, "1").strip().lower() not in ("0", "false", "off", "no")


# ------------------------------------------------------------- OS-level locks
class _OsLock:
    """The cross-process part. ``acquire(timeout) -> bool``; ``release()``. Not re-entrant."""

    def __init__(self, name: str) -> None:
        self.name = name

    def acquire(self, timeout: float) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def release(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class _WinMutex(_OsLock):
    """A named Win32 mutex in the session namespace (``Local\\``).

    A mutex is owned by the OS thread that acquired it, which is exactly the
    thread that releases it here (the owner thread of :class:`_Lock`). If the
    owning process dies, Windows hands the mutex to the next waiter with
    ``WAIT_ABANDONED``: the previous owner's input is over, so that counts as
    acquired.
    """

    WAIT_OBJECT_0 = 0x0
    WAIT_ABANDONED = 0x80
    WAIT_TIMEOUT = 0x102

    def __init__(self, name: str) -> None:
        super().__init__(name)
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k32.ReleaseMutex.restype = wintypes.BOOL
        self._k32 = k32
        self._ctypes = ctypes
        handle = k32.CreateMutexW(None, False, "Local\\" + name)
        if not handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"CreateMutexW({name!r}) failed: {ctypes.FormatError(err)}")
        self._handle = handle
        self.abandoned = False

    def acquire(self, timeout: float) -> bool:
        ms = 0xFFFFFFFE if timeout is None else max(0, int(timeout * 1000))
        # Wait in slices so a Ctrl+C in the waiting thread is honoured.
        deadline = time.monotonic() + (timeout if timeout is not None else 1e9)
        while True:
            slice_ms = min(ms, 200)
            rc = self._k32.WaitForSingleObject(self._handle, slice_ms)
            if rc in (self.WAIT_OBJECT_0, self.WAIT_ABANDONED):
                self.abandoned = rc == self.WAIT_ABANDONED
                return True
            if rc != self.WAIT_TIMEOUT:
                err = self._ctypes.get_last_error()
                raise OSError(err, f"WaitForSingleObject failed: {self._ctypes.FormatError(err)}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            ms = int(remaining * 1000)

    def release(self) -> None:
        self._k32.ReleaseMutex(self._handle)


class _FlockLock(_OsLock):
    """``fcntl.flock`` on a file in the temp directory (released by the kernel if the process dies)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        uid = getattr(os, "getuid", lambda: 0)()
        display = os.environ.get("DISPLAY", "").replace(":", "_").replace("/", "_")
        suffix = f"-{uid}" + (f"-{display}" if display else "")
        self.path = os.path.join(tempfile.gettempdir(), f"{name}{suffix}.lock")
        self._fd: Optional[int] = None

    def acquire(self, timeout: float) -> bool:
        import fcntl

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd, fd = fd, -1
                    return True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(_POLL)
        finally:
            if fd >= 0:
                os.close(fd)

    def release(self) -> None:
        import fcntl

        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _os_lock(name: str) -> _OsLock:
    if sys.platform == "win32":
        return _WinMutex(name)
    return _FlockLock(name)


# ------------------------------------------------------------- the lock
class _Lock:
    """Re-entrant per thread; exclusive across threads (and, with ``os_lock``, processes)."""

    def __init__(self, key: Any, os_lock: Optional[_OsLock]) -> None:
        self.key = key
        self._mutex = threading.Lock()  # held while some thread of this process owns the lock
        self._os = os_lock
        self.owner: Optional[int] = None
        self.depth = 0
        self.since: Optional[float] = None

    def acquire(self, timeout: float) -> bool:
        me = threading.get_ident()
        if self.owner == me:
            self.depth += 1
            return True
        start = time.monotonic()
        if not self._mutex.acquire(timeout=max(0.0, timeout)):
            return False
        if self._os is not None:
            try:
                got = self._os.acquire(max(0.0, timeout - (time.monotonic() - start)))
            except BaseException:
                self._mutex.release()
                raise
            if not got:
                self._mutex.release()
                return False
            _write_owner(self._os)
        self.owner, self.depth, self.since = me, 1, time.time()
        return True

    def release(self) -> None:
        if self.owner != threading.get_ident():
            raise RuntimeError("desktop input lock released by a thread that does not hold it")
        self.depth -= 1
        if self.depth:
            return
        self.owner, self.since = None, None
        try:
            if self._os is not None:
                self._os.release()
        finally:
            self._mutex.release()

    @property
    def held_here(self) -> bool:
        return self.owner == threading.get_ident()


_LOCKS: Dict[Any, _Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _backend_of(obj: Any) -> Any:
    """A Desktop / Keyboard / Mouse / Element -> its backend (or ``obj`` itself)."""
    be = getattr(obj, "backend", None)
    return obj if be is None else be


def _lock_name(backend: Any) -> Optional[str]:
    name = getattr(backend, "desktop_lock_name", None)
    if name:
        return str(name)
    if getattr(backend, "name", None) in _REAL_BACKENDS:
        return LOCK_NAME
    return None


def _lock_for(obj: Any) -> _Lock:
    backend = _backend_of(obj)
    name = _lock_name(backend)
    key: Any = ("named", name) if name else ("backend", id(backend))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None or (name is None and getattr(lock, "_backend_ref", None) is not backend):
            lock = _Lock(key, _os_lock(name) if name else None)
            if name is None:
                lock._backend_ref = backend  # type: ignore[attr-defined]  # id() reuse guard
            _LOCKS[key] = lock
        return lock


def _timeout_of(obj: Any, timeout: Optional[float]) -> float:
    if timeout is not None:
        return float(timeout)
    for o in (obj, getattr(obj, "desktop", None)):
        t = getattr(o, "input_lock_timeout", None) if o is not None else None
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            return float(t)
    return DEFAULT_TIMEOUT


# ------------------------------------------------------------- owner sidecar
def _owner_path(os_lock: _OsLock) -> str:
    return os.path.join(tempfile.gettempdir(), f"{os_lock.name}.owner")


def _write_owner(os_lock: _OsLock) -> None:
    """Best effort: who holds the lock, for the :class:`DesktopBusy` message."""
    try:
        with open(_owner_path(os_lock), "w", encoding="utf-8") as fh:
            fh.write(f"pid {os.getpid()} ({os.path.basename(sys.argv[0] or 'python')}), "
                     f"thread {threading.current_thread().name}, since {time.strftime('%H:%M:%S')}")
    except OSError:
        pass


def holder(obj: Any = None) -> str:
    """A description of the current holder of ``obj``'s desktop lock ("" if unknown)."""
    lock = _lock_for(obj) if obj is not None else _LOCKS.get(("named", LOCK_NAME))
    if lock is None:
        return ""
    if lock.owner is not None:
        return f"thread {lock.owner} of this process (pid {os.getpid()})"
    if lock._os is not None:
        try:
            with open(_owner_path(lock._os), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    return ""


# ------------------------------------------------------------- public API
@contextlib.contextmanager
def hold(obj: Any, timeout: Optional[float] = None) -> Iterator[None]:
    """Hold ``obj``'s desktop input lock (re-entrant) for the ``with`` block.

    ``obj`` is a Desktop, Keyboard, Mouse, Element or backend. Raises
    :class:`DesktopBusy` after ``timeout`` seconds (default: ``obj``'s
    ``input_lock_timeout``, else :data:`DEFAULT_TIMEOUT`).
    """
    if not enabled():
        yield
        return
    lock = _lock_for(obj)
    t = _timeout_of(obj, timeout)
    if not lock.acquire(t):
        who = holder(obj)
        raise DesktopBusy(
            f"the desktop input lock was busy for {t:g} s" + (f" (held by {who})" if who else "")
            + "; another worker is using the mouse/keyboard. Raise Desktop(input_lock_timeout=...) "
            "or run the workers' input one after another"
        )
    try:
        yield
    finally:
        lock.release()


def desktop_input(desktop: Any, timeout: Optional[float] = None) -> "contextlib.AbstractContextManager[None]":
    """Hold the desktop input lock around a multi-step sequence of your own::

        with locum.desktop_input(d):
            d.hotkey("ctrl", "s")
            d.wait("window[name~='Save As']")
            d.find("edit[name='File name:']").set_text(path)
    """
    return hold(desktop, timeout)


@contextlib.contextmanager
def try_desktop_input(desktop: Any) -> Iterator[bool]:
    """``with try_desktop_input(d) as got:`` -- ``got`` is False (and nothing is held)
    when another thread or process is using the desktop right now. Never waits."""
    if not enabled():
        yield True
        return
    lock = _lock_for(desktop)
    if not lock.acquire(0.0):
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Method decorator: hold ``self``'s desktop input lock for the call."""
    import functools

    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with hold(self):
            return fn(self, *args, **kwargs)

    return wrapper
