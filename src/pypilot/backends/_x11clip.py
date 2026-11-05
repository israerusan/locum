"""Native X11 CLIPBOARD support (no xclip/xsel needed).

X11 has no clipboard *storage*: the application that "copied" owns the
``CLIPBOARD`` selection and must answer ``SelectionRequest`` events for as
long as the data should stay available. :class:`X11Clipboard` therefore uses
its own display connection and a small daemon thread that serves requests
while this process owns the selection. Reading converts the selection to
``UTF8_STRING`` (falling back to ``STRING``); both reading and serving
support the ``INCR`` protocol, so large texts transfer in chunks.

Like any X11 client without a clipboard manager, the copied text disappears
when the process exits.
"""

from __future__ import annotations

import select
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..errors import BackendError

_PROP_NAME = "PYPILOT_CLIPBOARD"
#: Seconds an incremental (INCR) transfer may stall before it is abandoned.
_INCR_TIMEOUT = 30.0


def _open_display(name: str) -> Any:
    from Xlib import display, error

    try:
        return display.Display(name)
    except error.DisplayError as exc:
        raise BackendError(f"Cannot open X display {name!r} for the clipboard: {exc}") from exc


def _abandon(d: Any) -> None:
    """Drop a connection without talking to the server.

    An interrupt (Ctrl+C) inside a python-xlib round trip leaves the connection
    believing a reply is still being read: ``close()`` (which flushes) and every
    later request would block forever.
    """
    try:
        d.display.close_internal("client")
    except Exception:
        try:
            d.display.socket.close()
        except Exception:
            pass


class _IncrTransfer:
    """State of one outgoing INCR transfer (ICCCM 2.7.2)."""

    __slots__ = ("requestor", "prop", "ptype", "data", "offset", "deadline")

    def __init__(self, requestor: Any, prop: int, ptype: int, data: bytes) -> None:
        self.requestor = requestor
        self.prop = prop
        self.ptype = ptype
        self.data = data
        self.offset = 0
        self.deadline = time.monotonic() + _INCR_TIMEOUT


class X11Clipboard:
    """Read and own the X11 ``CLIPBOARD`` selection using python-xlib only."""

    def __init__(self, display_name: str, *, timeout: float = 2.0) -> None:
        from Xlib import X, Xatom, display  # noqa: F401  (import check)

        self._display_name = display_name
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._text: Optional[str] = None
        self._d: Any = None  # owner connection (created lazily on first set)
        self._win: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._incr: Dict[Tuple[int, int], _IncrTransfer] = {}  # (requestor xid, property) -> transfer
        self._served_at = 0.0  # monotonic time an application last received our text (fully)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _wait_event(d: Any, predicate: Any, deadline: float) -> Any:
        while True:
            while d.pending_events():
                ev = d.next_event()
                if predicate(ev):
                    return ev
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            select.select([d.fileno()], [], [], min(remaining, 0.05))

    # --------------------------------------------------------------------- read
    def get(self) -> str:
        """Current clipboard text, or ``""`` when nobody owns the clipboard / it holds no text."""
        with self._lock:
            if self._text is not None and self._d is not None:
                try:
                    clip = self._d.intern_atom("CLIPBOARD")
                    if self._d.get_selection_owner(clip) == self._win:
                        return self._text
                except Exception:
                    pass
                except BaseException:
                    self._drop_owner()
                    raise
        from Xlib import X, error

        d = _open_display(self._display_name)
        interrupted = False
        try:
            clip = d.intern_atom("CLIPBOARD")
            if d.get_selection_owner(clip) == X.NONE:
                return ""
            win = d.screen().root.create_window(-10, -10, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask)
            prop = d.intern_atom(_PROP_NAME)
            for target_name in ("UTF8_STRING", "STRING"):
                target = d.intern_atom(target_name)
                win.convert_selection(clip, target, prop, X.CurrentTime)
                d.flush()
                ev = self._wait_event(
                    d, lambda e: e.type == X.SelectionNotify and e.requestor == win, time.monotonic() + self.timeout
                )
                if ev is None:
                    raise BackendError("Timed out waiting for the clipboard owner to answer")
                if ev.property == X.NONE:
                    continue  # owner refused this target; try the next one
                data = self._read_property(d, win, prop)
                if data is None:
                    continue
                encoding = "utf-8" if target_name == "UTF8_STRING" else "latin-1"
                return data.decode(encoding, "replace")
            return ""
        except error.ConnectionClosedError as exc:
            raise BackendError(f"Lost connection to X display {self._display_name!r}: {exc}") from exc
        except BaseException as exc:
            interrupted = not isinstance(exc, Exception)
            raise
        finally:
            if interrupted:
                _abandon(d)
            else:
                try:
                    d.close()
                except Exception:
                    pass

    def _read_property(self, d: Any, win: Any, prop: int) -> Optional[bytes]:
        from Xlib import X

        reply = win.get_full_property(prop, X.AnyPropertyType)
        if reply is None:
            return None
        incr = d.intern_atom("INCR")
        if reply.property_type != incr:
            win.delete_property(prop)
            d.flush()
            return _as_bytes(reply.value)
        # INCR: delete the property to request each chunk; a zero-length chunk ends the transfer.
        chunks: List[bytes] = []
        win.delete_property(prop)
        d.flush()
        deadline = time.monotonic() + max(self.timeout, 10.0)
        while True:
            ev = self._wait_event(
                d,
                lambda e: e.type == X.PropertyNotify and e.atom == prop and e.state == X.PropertyNewValue,
                deadline,
            )
            if ev is None:
                raise BackendError("Timed out during an incremental clipboard transfer")
            part = win.get_full_property(prop, X.AnyPropertyType)
            win.delete_property(prop)
            d.flush()
            chunk = _as_bytes(part.value) if part is not None else b""
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    # -------------------------------------------------------------------- write
    def set(self, text: str) -> None:
        """Take ownership of ``CLIPBOARD`` and serve ``text`` to any application that pastes."""
        from Xlib import X, error

        with self._lock:
            try:
                if self._d is None:
                    d = _open_display(self._display_name)
                    # Requests to vanished requestor windows fail asynchronously; ignore them.
                    d.set_error_handler(lambda *args: None)
                    self._d = d
                    self._win = d.screen().root.create_window(-10, -10, 1, 1, 0, X.CopyFromParent)
                    self._stop.clear()
                    self._thread = threading.Thread(target=self._serve, name="pypilot-x11-clipboard", daemon=True)
                    self._thread.start()
                self._text = text
                clip = self._d.intern_atom("CLIPBOARD")
                self._win.set_selection_owner(clip, X.CurrentTime)
                self._d.flush()
                owner = self._d.get_selection_owner(clip)
            except error.ConnectionClosedError as exc:
                raise BackendError(f"Lost connection to X display {self._display_name!r}: {exc}") from exc
            except Exception:
                raise
            except BaseException:
                self._drop_owner()  # the next set() opens a fresh connection
                raise
            if owner != self._win:
                raise BackendError("Could not take ownership of the X11 CLIPBOARD selection")

    def _serve(self) -> None:
        from Xlib import X

        d = self._d
        while not self._stop.is_set() and self._d is d:  # _drop_owner() may have replaced it
            try:
                # A round trip made by set() may already have queued events
                # without the socket staying readable: only block when idle.
                if not d.pending_events():
                    select.select([d.fileno()], [], [], 0.2)
                with self._lock:
                    while d.pending_events():
                        ev = d.next_event()
                        if ev.type == X.SelectionRequest:
                            self._answer(ev)
                        elif ev.type == X.PropertyNotify:
                            self._incr_next(ev)
                        elif ev.type == X.SelectionClear:
                            # The event may be stale: if set() took the selection
                            # back meanwhile, the new text must stay.
                            clip = d.intern_atom("CLIPBOARD")
                            if ev.atom == clip and d.get_selection_owner(clip) != self._win:
                                self._text = None
                    self._expire_incr()
            except Exception:
                if self._stop.is_set():
                    return
                time.sleep(0.05)

    def _max_chunk(self) -> int:
        """Largest property we write in one request (stay well under the maximum request size)."""
        return max(4096, self._d.display.info.max_request_length * 4 - 1024)

    def _answer(self, ev: Any) -> None:
        from Xlib import X, Xatom
        from Xlib.protocol import event as xevent

        d = self._d
        utf8, text_atom, targets = (d.intern_atom(n) for n in ("UTF8_STRING", "TEXT", "TARGETS"))
        prop = ev.property if ev.property != X.NONE else ev.target  # obsolete clients pass None
        text = self._text
        try:
            if text is None:
                prop = X.NONE
            elif ev.target == targets:
                ev.requestor.change_property(prop, Xatom.ATOM, 32, [targets, utf8, text_atom, Xatom.STRING])
            elif ev.target in (utf8, text_atom, Xatom.STRING):
                if ev.target == Xatom.STRING:
                    data, ptype = text.encode("latin-1", "replace"), Xatom.STRING
                else:
                    data, ptype = text.encode("utf-8"), utf8
                if len(data) > self._max_chunk():
                    # Too large for one request: INCR. Announce the size, then send
                    # a chunk each time the requestor deletes the property.
                    ev.requestor.change_attributes(event_mask=X.PropertyChangeMask)
                    ev.requestor.change_property(prop, d.intern_atom("INCR"), 32, [len(data)])
                    self._incr[(ev.requestor.id, prop)] = _IncrTransfer(ev.requestor, prop, ptype, data)
                else:
                    ev.requestor.change_property(prop, ptype, 8, data)
                    self._served_at = time.monotonic()
            else:
                prop = X.NONE
        except Exception:
            prop = X.NONE
        notify = xevent.SelectionNotify(
            time=ev.time, requestor=ev.requestor, selection=ev.selection, target=ev.target, property=prop
        )
        try:
            ev.requestor.send_event(notify, event_mask=0)
            d.flush()
        except Exception:
            pass

    def _incr_next(self, ev: Any) -> None:
        """Send the next INCR chunk once the requestor deleted the previous one."""
        from Xlib import X

        if ev.state != X.PropertyDelete:
            return
        key = (ev.window.id, ev.atom)
        t = self._incr.get(key)
        if t is None:
            return
        chunk = t.data[t.offset : t.offset + self._max_chunk()]
        try:
            t.requestor.change_property(t.prop, t.ptype, 8, chunk)
        except Exception:
            chunk = b""
        t.offset += len(chunk)
        t.deadline = time.monotonic() + _INCR_TIMEOUT
        if not chunk:  # the zero-length chunk just written ends the transfer
            self._finish_incr(key)
            self._served_at = time.monotonic()
        self._d.flush()

    def _finish_incr(self, key: Tuple[int, int]) -> None:
        t = self._incr.pop(key, None)
        if t is not None and not any(k[0] == key[0] for k in self._incr):
            try:
                t.requestor.change_attributes(event_mask=0)
            except Exception:
                pass

    def _expire_incr(self) -> None:
        now = time.monotonic()
        for key in [k for k, t in self._incr.items() if t.deadline < now]:
            self._finish_incr(key)

    def wait_read(self, since: float, timeout: float) -> bool:
        """Wait until an application has received our text (a paste) at or after
        monotonic time ``since``; False after ``timeout`` seconds or when we no longer own it."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                if self._served_at >= since:
                    return True
                if self._text is None or self._d is None:
                    return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    # -------------------------------------------------------------------- close
    def _drop_owner(self) -> None:
        """Abandon the owner connection after an interrupt left it mid-request."""
        with self._lock:
            self._stop.set()
            if self._d is not None:
                _abandon(self._d)
            self._d = self._win = self._thread = None
            self._text = None
            self._incr.clear()

    def close(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        with self._lock:
            if self._d is not None:
                try:
                    self._d.close()
                except Exception:
                    pass
            self._d = self._win = self._thread = None
            self._text = None
            self._incr.clear()


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("latin-1", "replace")
    try:
        return bytes(bytearray(value))
    except Exception:
        return b""
