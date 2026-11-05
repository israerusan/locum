"""Lazy python-xlib loading and low-level X helpers of the X11 backend.

The :class:`_XlibModules` holder, connection helpers, the asynchronous error
sink and the :func:`_x_errors` decorator that turns python-xlib failures into
locum errors.
"""

from __future__ import annotations

import collections
import functools
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple

from ...errors import BackendError, WindowNotFound

if TYPE_CHECKING:  # pragma: no cover
    from .backend import X11Backend  # noqa: F401  (string annotation in _x_errors)


# --------------------------------------------------------------------------- lazy imports

class _XlibModules:
    """Holder for lazily imported python-xlib modules."""

    def __init__(self) -> None:
        from Xlib import X, Xatom, display, error  # noqa: F401
        from Xlib.ext import randr, xtest
        from Xlib.protocol import event

        self.X = X
        self.Xatom = Xatom
        self.display = display
        self.error = error
        self.randr = randr
        self.xtest = xtest
        self.event = event


_XLIB: Optional[_XlibModules] = None


def _xlib() -> _XlibModules:
    global _XLIB
    if _XLIB is None:
        _XLIB = _XlibModules()
    return _XLIB


def _close_quietly(obj: Any) -> None:
    try:
        obj.close()
    except Exception:
        pass


def _abandon_display(d: Any) -> None:
    """Drop a python-xlib connection without talking to the server.

    ``Display.close()`` flushes first, which blocks forever on a connection an
    interrupt left half way through a request/reply exchange (its internal
    send/receive lock or "receiving" flag is never reset). Closing the socket
    directly also wakes other threads blocked on it (they get a connection error).
    """
    try:
        d.display.close_internal("client")
    except Exception:
        try:
            d.display.socket.close()
        except Exception:
            pass


class _ErrorSink:
    """Recent asynchronous X errors, numbered so callers can tell which are new."""

    def __init__(self, keep: int = 64) -> None:
        self.seq = 0
        self.recent: "collections.deque[Tuple[int, Any]]" = collections.deque(maxlen=keep)

    def since(self, seq: int) -> List[Any]:
        return [err for n, err in list(self.recent) if n > seq]


def _make_error_handler(sink: _ErrorSink) -> Callable[..., None]:
    """Error handler for X requests without replies (they fail asynchronously).

    Recent errors are kept in ``sink`` so window operations can turn them into
    exceptions after an ``XSync``.
    """

    def handler(err: Any, request: Any = None) -> None:
        sink.seq += 1
        sink.recent.append((sink.seq, err))

    return handler


def _x_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn raw python-xlib failures inside a backend primitive into locum errors:
    a lost/closed connection -> :class:`BackendError`, a window that vanished
    mid-operation -> :class:`WindowNotFound`, any other X error -> :class:`BackendError`."""

    @functools.wraps(fn)
    def wrapper(self: "X11Backend", *args: Any, **kwargs: Any) -> Any:
        if self._state.desynced:
            self._reconnect()
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:
            xe = _xlib().error
            if isinstance(exc, (xe.ConnectionClosedError, xe.DisplayError)):
                if self._state.closed:
                    raise BackendError("X11 backend has been closed") from exc
                raise BackendError(f"Lost connection to X display {self.display_name!r}: {exc}") from exc
            if isinstance(exc, (xe.BadWindow, xe.BadDrawable)):
                raise WindowNotFound(f"X11 window vanished during {fn.__name__}: {exc}") from exc
            if isinstance(exc, xe.XError):
                raise BackendError(f"X error during {fn.__name__}: {exc}") from exc
            raise
        except BaseException:
            # KeyboardInterrupt (Ctrl+C) / SystemExit, typically while python-xlib waited
            # for a reply: the connection is out of sync and would block every later call
            # (and the exit-time cleanup). Drop it; the next call reconnects.
            self._desync()
            raise

    return wrapper
