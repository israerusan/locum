""":class:`WindowsBackend`, composed from the per-concern mixins of this
package; it keeps construction, capabilities and the lifecycle (``close``,
``_check``, ``release_all``).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...errors import BackendError, BackendUnavailable
from ...types import MouseButton
from ..base import (
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOWS,
    Backend,
)
from ._win32api import INPUT, _win32
from .accessibility import _AccessibilityMixin
from .clipboard import _ClipboardMixin
from .input import _Held, _InputMixin
from .process import _ProcessMixin
from .screen import _ScreenMixin, enable_dpi_awareness
from .windowing import _WindowsMixin


# =========================================================================== backend
class WindowsBackend(
    _InputMixin,
    _ScreenMixin,
    _WindowsMixin,
    _ProcessMixin,
    _ClipboardMixin,
    _AccessibilityMixin,
    Backend,
):
    """Backend for Windows 7+ (best on Windows 10/11). See the module docstring.

    Args:
        dpi_aware: make the process per-monitor DPI aware (strongly recommended;
            without it coordinates are virtualised on scaled displays).
        screenshot_method: ``"auto"`` (mss, falling back to GDI), ``"mss"`` or ``"gdi"``.
        accessibility: enable UI Automation (needs ``comtypes``).
        uia_view: tree view for :class:`UIANode` children: ``"control"`` (default),
            ``"raw"`` or ``"content"``.
        uia_timeout_ms: UIA connection timeout for unresponsive apps (None = system default).
        clipboard_retries: attempts to open a clipboard held by another process.
        clipboard_retry_delay: seconds between those attempts.
        clipboard_history: let text written by :meth:`set_clipboard` enter Windows
            Clipboard History / cloud clipboard (default False: it is excluded, so
            pasted passwords and bank data are not kept there).
        respect_swapped_buttons: when the user swapped mouse buttons, map
            ``"left"`` to the *primary* button (as users expect).
        type_char_delay: seconds between characters in :meth:`type_text`, and the
            minimum gap between consecutive :meth:`key_down` presses.
        click_key_settle: minimum seconds between a mouse button release and the
            next keyboard event. Applications handle a click asynchronously;
            Windows 11 Notepad, clicked on text that was just typed, loses the
            Ctrl of an immediately following Ctrl+A and types ``a`` (verified on
            a real machine; 50 ms was already enough). Only the first key after a
            click waits, so typing and repeated clicks keep full speed.
        sleep: sleep function for clipboard retries and input pacing.

    Raises:
        BackendUnavailable: not running on Windows.
    """

    name = "windows"

    def __init__(
        self,
        *,
        dpi_aware: bool = True,
        screenshot_method: str = "auto",
        accessibility: bool = True,
        uia_view: str = "control",
        uia_timeout_ms: Optional[int] = 5000,
        clipboard_retries: int = 20,
        clipboard_retry_delay: float = 0.01,
        clipboard_history: bool = False,
        respect_swapped_buttons: bool = True,
        type_char_delay: float = 0.01,
        click_key_settle: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        _api: Any = None,
    ) -> None:
        if _api is None and sys.platform != "win32":
            raise BackendUnavailable(f"The Windows backend requires Windows (running on {sys.platform!r})")
        if screenshot_method not in ("auto", "mss", "gdi"):
            raise ValueError("screenshot_method must be 'auto', 'mss' or 'gdi'")
        if uia_view not in ("control", "raw", "content"):
            raise ValueError("uia_view must be 'control', 'raw' or 'content'")
        try:
            self._api = _api if _api is not None else _win32()
        except BackendUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - platform specific
            raise BackendUnavailable(f"Cannot load the Win32 API: {exc}") from exc
        self.dpi_mode = enable_dpi_awareness(self._api if _api is not None else None) if dpi_aware else "unchanged"
        self._screenshot_method = screenshot_method
        self._mss_broken = screenshot_method == "gdi"
        self._mss_local = threading.local()
        self._mss_lock = threading.Lock()
        self._mss_closers: List[Any] = []  # weakref.finalize objects closing per-thread mss grabbers
        self._accessibility = bool(accessibility)
        self._uia_view = uia_view
        self._uia_timeout = uia_timeout_ms
        self._uia_ctx: Any = None
        self._uia_error: Optional[str] = None
        self._clip_retries = max(1, int(clipboard_retries))
        self._clip_delay = float(clipboard_retry_delay)
        self._clip_history = bool(clipboard_history)
        self._clip_exclusions: Optional[List[int]] = None
        self._pid_names: Dict[int, str] = {}
        self._respect_swap = respect_swapped_buttons
        self._sleep = sleep
        if type_char_delay < 0:
            raise ValueError("type_char_delay must be >= 0")
        self._type_char_delay = float(type_char_delay)
        if click_key_settle < 0:
            raise ValueError("click_key_settle must be >= 0")
        self._click_key_settle = float(click_key_settle)
        self._last_button_up: Optional[float] = None
        self._last_key_down: Optional[float] = None
        self._lock = threading.RLock()
        self._held: Dict[str, _Held] = {}
        self._buttons_down: Dict[MouseButton, INPUT] = {}
        self._closed = False

    # =================================================================== basics
    @property
    def capabilities(self) -> frozenset:
        caps = {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_SCREENSHOT, CAP_WINDOWS, CAP_CLIPBOARD, CAP_HSCROLL}
        if self._accessibility and self._uia_available():
            caps.add(CAP_ACCESSIBILITY)
        return frozenset(caps)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Release keys/buttons still held by this backend and free screenshot resources."""
        if self._closed:
            return
        try:
            self.release_all()
        except Exception:
            pass
        try:
            self._close_change_tracker()
        except Exception:
            pass
        with self._mss_lock:
            closers, self._mss_closers = self._mss_closers, []
        for closer in closers:
            closer()  # closes that thread's grabber now (no-op if its thread already ended)
        self._closed = True

    def __enter__(self) -> "WindowsBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _check(self) -> None:
        if self._closed:
            raise BackendError("Windows backend has been closed")

    def _error(self, what: str) -> BackendError:
        code = self._api.get_last_error()
        return BackendError(f"{what} failed: {self._api.format_error(code)} (error {code})")

    def release_all(self) -> None:
        """Release every key and mouse button pressed through this backend (reverse order)."""
        with self._lock:
            if self._closed:
                self._held.clear()
                self._buttons_down.clear()
                return
            # Each key/button is released and forgotten separately: one whose release
            # Windows rejects (secure desktop, locked workstation) stays recorded so
            # a later call can retry it; the first error is raised at the end.
            first: Optional[BaseException] = None
            pending: List[Tuple[Dict[Any, Any], Any, List[INPUT]]] = [
                (self._held, name, self._held[name].release) for name in reversed(list(self._held))
            ]
            pending += [(self._buttons_down, b, [ev]) for b, ev in list(self._buttons_down.items())]
            for table, k, events in pending:
                try:
                    self._send(events)
                except BackendError as exc:
                    if first is None:
                        first = exc
                    continue
                table.pop(k, None)
            if first is not None:
                raise first

    def __repr__(self) -> str:
        return f"<WindowsBackend dpi={self.dpi_mode}{' closed' if self._closed else ''}>"
