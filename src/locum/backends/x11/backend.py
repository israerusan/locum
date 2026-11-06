"""The composed :class:`X11Backend` class.

Construction, capabilities and connection lifecycle live here; the primitives
come from the per-concern mixins (input, screen, windows, clipboard,
accessibility).
"""

from __future__ import annotations

import os
import threading
import weakref
from typing import Any, Dict, List, Optional, Tuple

from ...errors import BackendError, BackendUnavailable
from ...types import Rect
from ..base import (
    Backend,
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOW_CAPTURE,
    CAP_WINDOWS,
)
from ._xcore import _ErrorSink, _abandon_display, _make_error_handler, _xlib
from .accessibility import _AccessibilityMixin
from .clipboard import _ClipboardMixin
from .input import _InputMixin, _InputState, _cleanup
from .screen import _ScreenMixin
from .windowing import _WindowsMixin


# --------------------------------------------------------------------------- backend
class X11Backend(_InputMixin, _ScreenMixin, _WindowsMixin, _ClipboardMixin, _AccessibilityMixin, Backend):
    """Backend for X11 desktops (see module docstring).

    Args:
        display: X display name (default: ``$DISPLAY``).
        screenshot_method: ``"auto"`` (mss, falling back to XGetImage),
            ``"mss"`` or ``"xlib"``.
        clipboard_tool: force a clipboard helper (``"xclip"``, ``"xsel"``,
            ``"wl-clipboard"``), ``"native"`` (pure python-xlib selection
            handling) or ``"none"``; default: a helper if installed, else native.
        accessibility: enable AT-SPI support when available. An AT-SPI module
            object may be passed instead of ``True`` (used for testing).
        remap_settle: seconds a scratch keycode must stay bound after its last
            use before it may be re-bound to another character. Prevents clients
            from looking up a key after its mapping already changed.
        max_scratch_keycodes: upper bound on spare keycodes used for unicode
            typing (``None`` = all spare keycodes).

    Raises:
        BackendUnavailable: python-xlib is missing or the display cannot be opened.
    """

    name = "x11"

    def __init__(
        self,
        display: Optional[str] = None,
        *,
        screenshot_method: str = "auto",
        clipboard_tool: Optional[str] = None,
        accessibility: Any = True,
        remap_settle: float = 0.05,
        max_scratch_keycodes: Optional[int] = None,
    ) -> None:
        try:
            xl = _xlib()
        except ImportError as exc:
            raise BackendUnavailable(
                f"python-xlib is required for the X11 backend ({exc}); install it with `pip install python-xlib`"
            ) from exc
        self._xl = xl
        if screenshot_method not in ("auto", "mss", "xlib"):
            raise ValueError("screenshot_method must be 'auto', 'mss' or 'xlib'")
        display_name = display or os.environ.get("DISPLAY")
        if not display_name:
            hint = ""
            if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
                hint = ("; this is a Wayland session without XWayland. Start XWayland, or opt in to the "
                        "experimental Wayland backend (not yet validated on a real compositor) with "
                        "LOCUM_BACKEND=wayland")
            raise BackendUnavailable(f"No X display: $DISPLAY is not set and no display name was given{hint}")
        try:
            self._d = xl.display.Display(display_name)
        except Exception as exc:
            raise BackendUnavailable(f"Cannot open X display {display_name!r}: {exc}") from exc

        self.display_name = display_name
        self._lock = threading.RLock()
        self._screen = self._d.screen()
        self._root = self._screen.root
        self._xtest = bool(self._d.has_extension("XTEST"))
        self._randr = bool(self._d.has_extension("RANDR"))
        self._min_kc = int(self._d.display.info.min_keycode)
        self._max_kc = int(self._d.display.info.max_keycode)
        self._keymap: List[Tuple[int, ...]] = []
        self._keymap_dirty = True
        self._modmap_dirty = True
        self._numlock_mask = 0
        self._scratch_pool: Optional[List[int]] = None
        self._scratch_borrow = False
        self._foreign_scratch: frozenset = frozenset()  # keycodes bound by other locum backends
        self._max_scratch = max_scratch_keycodes
        self._held_keys: Dict[str, Tuple[int, Optional[int]]] = {}  # key -> (keycode, extra shift keycode)
        self._async_errors = _ErrorSink()
        # A closure (not a bound method) so the display does not keep ``self`` alive.
        self._d.set_error_handler(_make_error_handler(self._async_errors))
        self._screenshot_method = screenshot_method
        self._mss_local = threading.local()
        self._mss_broken = screenshot_method == "xlib"
        self._mss_failures = 0  # consecutive failed mss grabs
        self._restore_geometry: Dict[int, Rect] = {}
        self._iconified: set = set()  # windows we unmapped to "minimize" them without a WM
        try:
            self._clip_cmds = self._detect_clipboard(clipboard_tool)
        except Exception:
            self._d.close()
            raise
        if accessibility is True:
            self._atspi_source: Any = "auto"
        elif accessibility:
            self._atspi_source = accessibility
        else:
            self._atspi_source = None
        self._atspi_mod: Any = None
        self._atspi_checked = False
        self._state = _InputState(self._d, self._lock, float(remap_settle), display_name)
        self._finalizer = weakref.finalize(self, _cleanup, self._state)

    # =================================================================== basics
    @property
    def capabilities(self) -> frozenset:
        caps = {CAP_SCREENSHOT, CAP_WINDOWS}
        if self._xtest:
            caps |= {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_HSCROLL}
        if self._clip_cmds is not None or self._clip_mode != "none":
            caps.add(CAP_CLIPBOARD)
        if self._atspi() is not None:
            caps.add(CAP_ACCESSIBILITY)
        if self._has_composite():
            caps.add(CAP_WINDOW_CAPTURE)
        return frozenset(caps)

    @property
    def closed(self) -> bool:
        return self._state.closed

    def close(self) -> None:
        """Release held keys/buttons, restore remapped keycodes and close the X connections."""
        self._finalizer()

    def __enter__(self) -> "X11Backend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _check(self) -> None:
        if self._state.closed:
            raise BackendError("X11 backend has been closed")

    def _desync(self) -> None:
        """Drop the display connection after an interrupt left it mid-request (see ``_x_errors``)."""
        st = self._state
        if st.closed or st.desynced:
            return
        st.desynced = True
        _abandon_display(self._d)

    def _reconnect(self) -> None:
        """Replace a connection dropped by :meth:`_desync` with a fresh one."""
        with self._lock:
            st = self._state
            if st.closed:
                raise BackendError("X11 backend has been closed")
            if not st.desynced:
                return
            try:
                d = self._xl.display.Display(self.display_name)
            except Exception as exc:
                raise BackendError(f"Lost connection to X display {self.display_name!r} and could not reconnect: {exc}") from exc
            d.set_error_handler(_make_error_handler(self._async_errors))
            self._d = st.display = d
            self._screen = d.screen()
            self._root = self._screen.root
            self._keymap_dirty = True
            self._modmap_dirty = True
            st.desynced = False

    def _sync(self) -> None:
        try:
            self._d.sync()
        except self._xl.error.ConnectionClosedError as exc:
            self._check()  # closed by close() in another thread
            raise BackendError(f"Lost connection to X display {self.display_name!r}: {exc}") from exc

    def _screen_size(self) -> Tuple[int, int]:
        try:
            g = self._root.get_geometry()
            return int(g.width), int(g.height)
        except self._xl.error.XError:
            return int(self._screen.width_in_pixels), int(self._screen.height_in_pixels)

    def __repr__(self) -> str:
        return f"<X11Backend display={self.display_name!r}{' closed' if self.closed else ''}>"
