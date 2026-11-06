"""Cooperative cancellation for long-running calls (tree walks, waits).

The MCP server runs each tool call inside a :class:`CancelScope`; the
accessibility-tree walks, selector matching and wait loops call :func:`check`
between steps, so a call that ran past its time limit, or that the client
cancelled, stops at the next step instead of running on for minutes.

Outside a scope :func:`check` costs one thread-local lookup and does nothing.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .errors import FailSafeTriggered

_local = threading.local()


class CallCancelled(FailSafeTriggered):
    """The running call was cancelled or exceeded its time limit.

    A :class:`~locum.errors.FailSafeTriggered` subclass, so no retry loop,
    tool wrapper or policy layer swallows it: the call unwinds at once.
    """


class CancelScope:
    """Cancel token for the calls made on this thread while the scope is active.

    Args:
        timeout: Seconds until :func:`check` raises (``None`` = no limit).
        clock: Monotonic clock (injectable for tests).
    """

    def __init__(self, timeout: Optional[float] = None, clock=time.monotonic) -> None:
        self._clock = clock
        self.deadline = None if timeout is None else clock() + float(timeout)
        self.timeout = timeout
        self._event = threading.Event()
        self.reason = ""
        self._prev: Optional["CancelScope"] = None

    def cancel(self, reason: str = "cancelled") -> None:
        """Cancel from any thread; the scope's thread raises at its next check."""
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def expired(self) -> bool:
        return self.deadline is not None and self._clock() >= self.deadline

    def check(self) -> None:
        if self._event.is_set():
            raise CallCancelled(f"call {self.reason or 'cancelled'}")
        if self.expired():
            self.reason = self.reason or f"timed out after {self.timeout:g}s"
            raise CallCancelled(f"call timed out after {self.timeout:g}s")

    def __enter__(self) -> "CancelScope":
        self._prev = getattr(_local, "scope", None)
        _local.scope = self
        return self

    def __exit__(self, *exc: object) -> None:
        _local.scope = self._prev


def check() -> None:
    """Raise :class:`CallCancelled` if the current thread's scope is cancelled or expired."""
    scope = getattr(_local, "scope", None)
    if scope is not None:
        scope.check()


def current() -> Optional[CancelScope]:
    return getattr(_local, "scope", None)


__all__ = ["CallCancelled", "CancelScope", "check", "current"]
