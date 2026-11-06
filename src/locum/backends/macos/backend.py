"""The :class:`MacOSBackend` class: construction, capabilities, lifecycle and permissions.

Its other methods live in the per-concern mixins it is composed from.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...errors import BackendError, BackendUnavailable, NotSupported
from ...types import MouseButton
from ..base import (
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOW_CAPTURE,
    CAP_WINDOWS,
    Backend,
)
from .constants import ACCESSIBILITY_HELP, _INSTALL_HINT, kCGEventSourceStateHIDSystemState
from .keys import KeySpec, KeyboardLayoutMap, _CarbonKeyboardLayouts
from .events import ClickTracker
from .displays import DisplayLayout
from .ax import _AXApi
from .input_mixin import _InputMixin
from .screen_mixin import _ScreenMixin
from .windows_mixin import _WindowsMixin
from .clipboard_mixin import _ClipboardMixin
from .accessibility_mixin import _AccessibilityMixin


# =============================================================================
# The backend
# =============================================================================

class MacOSBackend(_InputMixin, _ScreenMixin, _WindowsMixin, _ClipboardMixin, _AccessibilityMixin, Backend):
    """Native macOS backend (Quartz events, CGWindowList, AXUIElement, NSPasteboard).

    All coordinates are physical pixels (see the module docstring); conversion
    to/from Quartz points happens per display.

    Args:
        check_permissions: verify Accessibility permission before posting input
            and raise a helpful :class:`BackendError` instead of letting macOS
            silently drop the events.
        double_click_interval: seconds between presses that still count as a
            multi-click (default: the user's system setting).
        layout_ttl: seconds the display layout is cached before re-querying.

    Raises:
        BackendUnavailable: not running on macOS, or pyobjc is not installed.
    """

    name = "macos"

    def __init__(self, *, check_permissions: bool = True, double_click_interval: Optional[float] = None,
                 layout_ttl: float = 1.0):
        if sys.platform != "darwin":
            raise BackendUnavailable(f"MacOSBackend requires macOS (this is {sys.platform!r})")
        try:
            import Quartz  # type: ignore
        except ImportError as exc:
            raise BackendUnavailable(f"pyobjc Quartz is not installed ({exc}); {_INSTALL_HINT}") from exc
        try:
            import AppKit  # type: ignore
        except ImportError:
            AppKit = None
        ax: Optional[_AXApi] = None
        try:
            import ApplicationServices  # type: ignore

            ax = _AXApi(ApplicationServices, Quartz)
        except Exception:
            ax = None
        if double_click_interval is None and AppKit is not None:
            try:
                double_click_interval = float(AppKit.NSEvent.doubleClickInterval())
            except Exception:
                double_click_interval = None
        try:
            kbd_layouts: Any = _CarbonKeyboardLayouts()
        except Exception:  # pragma: no cover - Carbon is always present on macOS
            kbd_layouts = None
        self._setup(Quartz, AppKit, ax, check_permissions=check_permissions,
                    double_click_interval=double_click_interval or 0.5, layout_ttl=layout_ttl,
                    keyboard_layouts=kbd_layouts)
        self._keyboard_map()  # read the layout now (we are most likely on the main thread)

    @classmethod
    def _from_parts(cls, quartz: Any, appkit: Any = None, ax: Any = None, *, layout: Optional[DisplayLayout] = None,
                    check_permissions: bool = False, double_click_interval: float = 0.5,
                    clock: Callable[[], float] = time.monotonic,
                    keyboard_layout: Optional[KeyboardLayoutMap] = None,
                    keyboard_layouts: Any = None) -> "MacOSBackend":
        """Build a backend from explicit native modules (used for tests and embedding).

        With ``layout`` given the display layout is fixed and never re-queried.
        ``keyboard_layout`` fixes the keyboard layout map (default: US-ANSI);
        ``keyboard_layouts`` is a provider with a ``current(known_fingerprint)``
        method (like :class:`_CarbonKeyboardLayouts`) that is polled for changes.
        """
        self = cls.__new__(cls)
        self._setup(quartz, appkit, ax, check_permissions=check_permissions,
                    double_click_interval=double_click_interval, layout_ttl=1.0, clock=clock,
                    keyboard_layouts=keyboard_layouts)
        if layout is not None:
            self._layout = layout
            self._layout_ttl = None
        if keyboard_layout is not None:
            self._kbd_map = keyboard_layout
            self._kbd_fixed = True
        return self

    def _setup(self, quartz: Any, appkit: Any, ax: Optional[_AXApi], *, check_permissions: bool,
               double_click_interval: float, layout_ttl: Optional[float],
               clock: Callable[[], float] = time.monotonic, keyboard_layouts: Any = None) -> None:
        self._q = quartz
        self._appkit = appkit
        self._ax = ax
        self._check_permissions = check_permissions
        self._lock = threading.RLock()
        self._clicks = ClickTracker(interval=double_click_interval, clock=clock)
        self._layout: Optional[DisplayLayout] = None
        self._layout_time = 0.0
        self._layout_ttl = layout_ttl
        self._held_buttons: List[MouseButton] = []
        self._held_mods: Dict[str, int] = {}
        self._pressed_keys: Dict[str, KeySpec] = {}
        self._app_names: Dict[int, str] = {}
        self._restore_rects: Dict[Any, Tuple[float, float, float, float]] = {}
        self._tls = threading.local()
        self._trusted = False
        self._warned_capture = False
        self._closed = False
        self._clock = clock
        self._kbd_provider = keyboard_layouts
        self._kbd_map: KeyboardLayoutMap = KeyboardLayoutMap.us_ansi()
        self._kbd_fingerprint: Optional[bytes] = None
        self._kbd_checked: Optional[float] = None
        self._warned_layout = False
        self._kbd_fixed = False
        try:
            self._source = quartz.CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
        except Exception:
            self._source = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def capabilities(self) -> frozenset:
        caps = {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_SCREENSHOT, CAP_WINDOWS, CAP_HSCROLL}
        if self._appkit is not None:
            caps.add(CAP_CLIPBOARD)
        if self._ax is not None:
            caps.add(CAP_ACCESSIBILITY)
        if getattr(self._q, "CGWindowListCreateImage", None) is not None:
            caps.add(CAP_WINDOW_CAPTURE)
        return frozenset(caps)

    def close(self) -> None:
        """Release any buttons/keys still held by this backend (never leave stuck input)."""
        if self._closed:
            return
        try:
            self.release_all()
        except Exception:
            pass
        sct = getattr(self._tls, "mss", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
            self._tls.mss = None
        self._closed = True

    def release_all(self) -> None:
        """Release every mouse button and key pressed through this backend."""
        for button in list(self._held_buttons):
            self.mouse_up(button)
        for key in list(self._pressed_keys):
            self.key_up(key)
        for key in list(self._held_mods):
            self.key_up(key)

    def __enter__(self) -> "MacOSBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- permissions
    def permissions(self) -> Dict[str, Optional[bool]]:
        """``{"accessibility": bool|None, "screen_recording": bool|None}`` (None = unknown)."""
        acc = self._ax.is_trusted() if self._ax is not None else None
        rec: Optional[bool] = None
        fn = getattr(self._q, "CGPreflightScreenCaptureAccess", None)
        if fn is not None:
            try:
                rec = bool(fn())
            except Exception:
                rec = None
        return {"accessibility": acc, "screen_recording": rec}

    def request_permissions(self) -> Dict[str, Optional[bool]]:
        """Show the macOS permission prompts for Accessibility and Screen Recording."""
        if self._ax is not None:
            self._ax.prompt_trust()
        fn = getattr(self._q, "CGRequestScreenCaptureAccess", None)
        if fn is not None:
            try:
                fn()
            except Exception:
                pass
        return self.permissions()

    def _require_trust(self) -> None:
        if self._trusted or not self._check_permissions or self._ax is None:
            return
        if self._ax.is_trusted():
            self._trusted = True
            return
        raise BackendError(ACCESSIBILITY_HELP.format(exe=sys.executable))

    def _require_ax(self) -> _AXApi:
        if self._ax is None:
            raise NotSupported(
                f"Accessibility API unavailable: pyobjc-framework-ApplicationServices is not installed; {_INSTALL_HINT}"
            )
        if not self._ax.is_trusted():
            raise BackendError(ACCESSIBILITY_HELP.format(exe=sys.executable))
        return self._ax
