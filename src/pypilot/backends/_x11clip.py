"""Native X11 CLIPBOARD support (no xclip/xsel needed).

X11 has no clipboard *storage*: the application that "copied" owns the
``CLIPBOARD`` selection and must answer ``SelectionRequest`` events for as
long as the data should stay available. :class:`X11Clipboard` therefore uses
its own display connection and a small daemon thread that serves requests
while this process owns the selection. Reading converts the selection to
``UTF8_STRING`` (falling back to ``STRING``) and supports the ``INCR``
protocol for large transfers.

Like any X11 client without a clipboard manager, the copied text disappears
when the process exits.
"""

from __future__ import annotations

import select
import threading
import time
from typing import Any, List, Optional

from ..errors import BackendError

_PROP_NAME = "PYPILOT_CLIPBOARD"


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
        from Xlib import X, display

        d = display.Display(self._display_name)
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
        finally:
            d.close()

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
        from Xlib import X, display

        with self._lock:
            if self._d is None:
                self._d = display.Display(self._display_name)
                self._win = self._d.screen().root.create_window(-10, -10, 1, 1, 0, X.CopyFromParent)
                self._stop.clear()
                self._thread = threading.Thread(target=self._serve, name="pypilot-x11-clipboard", daemon=True)
                self._thread.start()
            self._text = text
            clip = self._d.intern_atom("CLIPBOARD")
            self._win.set_selection_owner(clip, X.CurrentTime)
            self._d.flush()
            if self._d.get_selection_owner(clip) != self._win:
                raise BackendError("Could not take ownership of the X11 CLIPBOARD selection")

    def _serve(self) -> None:
        from Xlib import X

        d = self._d
        while not self._stop.is_set():
            try:
                select.select([d.fileno()], [], [], 0.2)
                with self._lock:
                    while d.pending_events():
                        ev = d.next_event()
                        if ev.type == X.SelectionRequest:
                            self._answer(ev)
                        elif ev.type == X.SelectionClear:
                            self._text = None
            except Exception:
                if self._stop.is_set():
                    return
                time.sleep(0.05)

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
                # Stay well under the maximum request size (INCR sending is not implemented).
                if len(data) > max(4096, d.display.info.max_request_length * 4 - 1024):
                    prop = X.NONE
                else:
                    ev.requestor.change_property(prop, ptype, 8, data)
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

    # -------------------------------------------------------------------- close
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


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("latin-1", "replace")
    try:
        return bytes(bytearray(value))
    except Exception:
        return b""
