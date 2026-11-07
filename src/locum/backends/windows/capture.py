"""Off-screen window capture for the Windows backend: the :class:`_CaptureMixin`.

``PrintWindow`` asks a window to render itself into a memory DC, so the image
shows the window's own content even where other windows cover it or it extends
past the monitors. ``PW_RENDERFULLCONTENT`` (Windows 8.1+) renders through DWM,
which also captures DirectX / Chromium / UWP content; when that comes back
blank, the classic ``WM_PRINT`` path (flags 0) is tried. A classic print
whose client area comes back black (content drawn through DirectX, which
``WM_PRINT`` cannot see) counts as a failure too.

The capture runs with the calling thread per-monitor DPI aware, so window
rectangles and the bitmap are in physical pixels whatever the process
awareness; a classic print of a DPI-*unaware* window (which paints at its
logical size) is scaled up to the physical size.

A minimized window has no current content: Windows only keeps its caption-size
placeholder. :meth:`capture_window` then raises :class:`NotSupported`, unless
``restore_minimized`` asks it to show the window with ``SW_SHOWNOACTIVATE`` (no
activation, no focus change), capture it and minimize it again with
``SW_SHOWMINNOACTIVE``.
"""

from __future__ import annotations

import contextlib
import ctypes
from typing import Any, Iterator, List, Optional, Tuple

from ...errors import NotSupported
from ...types import Rect
from ..base import is_blank_image
from ._win32api import (
    DIB_RGB_COLORS,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    DPI_AWARENESS_UNAWARE,
    LPVOID,
    MONITOR_DEFAULTTONEAREST,
    POINT,
    PW_RENDERFULLCONTENT,
    RECT,
    SW_SHOWMINNOACTIVE,
    SW_SHOWNOACTIVATE,
    WINDOWPLACEMENT,
    WPF_RESTORETOMAXIMIZED,
)
from .screen import _bgrx_image, _bitmap_info

#: Largest bitmap side PrintWindow is asked to fill (GDI's own coordinate limit).
_MAX_SIDE = 32767


#: Highest channel value a pixel of a "black" client area may have (PrintWindow failure).
_BLACK_MAX = 8


def _is_black(img: Any) -> bool:
    """Whether every pixel of ``img`` is (near-)black."""
    ext = img.getextrema()
    if ext and not isinstance(ext[0], tuple):  # single-band image
        ext = (ext,)
    return all(hi <= _BLACK_MAX for _lo, hi in ext)


def _is_parked(r: Rect) -> bool:
    """Whether ``r`` is a minimized window's placeholder rect (parked at -32000, or caption sized)."""
    return r.left <= -30000 or r.top <= -30000 or (r.width <= 200 and r.height <= 40)


class _CaptureMixin:
    """``capture_window`` of :class:`WindowsBackend`."""

    #: Seconds a minimized window shown for ``restore_minimized`` is given to paint.
    RESTORE_SETTLE = 0.25

    def _can_print_window(self) -> bool:
        a = self._api
        return getattr(a, "PrintWindow", None) is not None and getattr(a, "CreateDIBSection", None) is not None

    @contextlib.contextmanager
    def _per_monitor_dpi(self) -> Iterator[None]:
        """Run the block with this thread per-monitor (v2) DPI aware, then restore its awareness."""
        fn = getattr(self._api, "SetThreadDpiAwarenessContext", None)
        old: Any = None
        if fn is not None:
            try:
                old = fn(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
            except Exception:
                old = None
        try:
            yield
        finally:
            if old:
                try:
                    fn(old if isinstance(old, ctypes.c_void_p) else ctypes.c_void_p(int(old)))
                except Exception:
                    pass

    def _print_window_raw(self, hwnd: int, width: int, height: int, flags: int) -> Optional[Any]:
        """``PrintWindow(hwnd, flags)`` into a ``width`` x ``height`` 32-bpp DIB section;
        ``None`` when PrintWindow reports failure."""
        a = self._api
        hdc_screen = a.GetDC(None)
        if not hdc_screen:
            raise self._error("GetDC")
        hdc_mem = hbm = None
        raw: Optional[bytes] = None
        try:
            hdc_mem = a.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                raise self._error("CreateCompatibleDC")
            bmi = _bitmap_info(width, height)
            bits = LPVOID()
            hbm = a.CreateDIBSection(hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not hbm or not bits.value:
                raise self._error("CreateDIBSection")
            old = a.SelectObject(hdc_mem, hbm)
            try:
                ok = bool(a.PrintWindow(hwnd, hdc_mem, flags))
                flush = getattr(a, "GdiFlush", None)
                if flush is not None:
                    flush()  # GDI batches drawing: finish it before reading the bits
                if ok:
                    raw = ctypes.string_at(bits.value, width * height * 4)
            finally:
                a.SelectObject(hdc_mem, old)
        finally:
            if hbm:
                a.DeleteObject(hbm)
            if hdc_mem:
                a.DeleteDC(hdc_mem)
            a.ReleaseDC(None, hdc_screen)
        return None if raw is None else _bgrx_image(width, height, raw)

    def _unaware_scale(self, hwnd: int) -> float:
        """The monitor scale of a DPI-*unaware* window (1.0 for aware windows or when unknown)."""
        a = self._api
        get_ctx = getattr(a, "GetWindowDpiAwarenessContext", None)
        get_aw = getattr(a, "GetAwarenessFromDpiAwarenessContext", None)
        mon_of = getattr(a, "MonitorFromWindow", None)
        if get_ctx is None or get_aw is None or mon_of is None:
            return 1.0
        try:
            if int(get_aw(get_ctx(hwnd))) != DPI_AWARENESS_UNAWARE:
                return 1.0
            hmon = mon_of(hwnd, MONITOR_DEFAULTTONEAREST)
            return float(self._monitor_scale(hmon)) if hmon else 1.0
        except Exception:
            return 1.0

    def _client_box(self, hwnd: int, outer: Rect) -> Optional[Tuple[int, int, int, int]]:
        """The client area as a crop box of the outer-window image (None when unknown or empty)."""
        a = self._api
        get_client, to_screen = getattr(a, "GetClientRect", None), getattr(a, "ClientToScreen", None)
        if get_client is None or to_screen is None:
            return None
        rc, pt = RECT(), POINT(0, 0)
        try:
            if not get_client(hwnd, ctypes.byref(rc)) or not to_screen(hwnd, ctypes.byref(pt)):
                return None
        except Exception:
            return None
        left, top = pt.x - outer.left, pt.y - outer.top
        box = (max(0, left), max(0, top), min(outer.width, left + rc.right), min(outer.height, top + rc.bottom))
        return box if box[2] > box[0] and box[3] > box[1] else None

    def _window_image(self, hwnd: int) -> Any:
        """The window's visible frame (DWM bounds) rendered by PrintWindow; NotSupported if blank."""
        outer = self._outer_rect(hwnd)
        frame = self._frame_rect(hwnd)
        w, h = outer.width, outer.height
        if w <= 0 or h <= 0 or w > _MAX_SIDE or h > _MAX_SIDE:
            raise NotSupported(f"Window {hwnd:#x} has no capturable size ({w}x{h})")
        full: Tuple[int, int, int, int] = (0, 0, w, h)
        box = full
        inner = frame.intersect(outer)
        if inner is not None and inner != outer:
            box = (inner.left - outer.left, inner.top - outer.top, inner.right - outer.left, inner.bottom - outer.top)
        outcomes: List[str] = []
        for flags in (PW_RENDERFULLCONTENT, 0):
            img = self._print_window_raw(hwnd, w, h, flags)
            if img is None:
                outcomes.append(f"PrintWindow(flags={flags}) failed")
                continue
            if flags == 0:
                scale = self._unaware_scale(hwnd)
                if scale > 1.0 + 1e-6:  # an unaware window paints at its logical size, top-left
                    from PIL import Image

                    lw, lh = max(1, int(round(w / scale))), max(1, int(round(h / scale)))
                    resample = getattr(Image, "Resampling", Image).LANCZOS
                    img = img.crop((0, 0, lw, lh)).resize((w, h), resample)
                client = self._client_box(hwnd, outer)
                if client is not None and _is_black(img.crop(client)):
                    # WM_PRINT of a window that draws its content through DirectX /
                    # DirectComposition: the frame paints, the client area stays black.
                    outcomes.append(f"PrintWindow(flags={flags}) returned a black client area")
                    continue
            if box != full:
                img = img.crop(box)
            if not is_blank_image(img):
                return img
            outcomes.append(f"PrintWindow(flags={flags}) returned a blank image")
        raise NotSupported(
            f"Cannot capture window {hwnd:#x} off-screen ({'; '.join(outcomes)}): it draws through a surface "
            "PrintWindow cannot read (some GPU / UWP / protected content). Bring it to the front and use "
            "mode='screen' instead."
        )

    def capture_window(self, handle: Any, *, restore_minimized: bool = False) -> Any:
        """The window's own content via ``PrintWindow`` (covered and off-screen parts included).

        See the module docstring, and :meth:`locum.backends.base.Backend.capture_window`
        for the contract.
        """
        self._check()
        hwnd = self._hwnd(handle)
        if not self._can_print_window():
            raise NotSupported("PrintWindow / CreateDIBSection are unavailable on this system")
        a = self._api
        with self._per_monitor_dpi():
            if not a.IsIconic(hwnd):
                return self._window_image(hwnd)
            if not _is_parked(self._outer_rect(hwnd)):
                try:
                    return self._window_image(hwnd)
                except NotSupported:
                    pass
            if not restore_minimized:
                raise NotSupported(
                    f"Window {hwnd:#x} is minimized: Windows keeps no image of a minimized window's content. "
                    "Pass restore_minimized=True to show it for a moment without activating it (no focus "
                    "change), or restore it first."
                )
            with self._lock:
                # SW_SHOWNOACTIVATE shows the window at its normal size and clears
                # WPF_RESTORETOMAXIMIZED: the placement is read first so that a window
                # minimized while maximized still comes back maximized for the user.
                placement = self._placement(hwnd)
                a.ShowWindow(hwnd, SW_SHOWNOACTIVATE)  # restores without activation or focus change
                try:
                    self._sleep(self.RESTORE_SETTLE)
                    return self._window_image(hwnd)
                finally:
                    if a.IsWindow(hwnd) and not a.IsIconic(hwnd):
                        a.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)  # minimize again, activating nothing
                    if placement is not None and placement.flags & WPF_RESTORETOMAXIMIZED and a.IsWindow(hwnd):
                        self._restore_placement(hwnd, placement)

    def _placement(self, hwnd: int) -> Optional[WINDOWPLACEMENT]:
        fn = getattr(self._api, "GetWindowPlacement", None)
        if fn is None:
            return None
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        try:
            return wp if fn(hwnd, ctypes.byref(wp)) else None
        except Exception:
            return None

    def _restore_placement(self, hwnd: int, placement: WINDOWPLACEMENT) -> None:
        """Put back the minimized window's original placement (its WPF_RESTORETOMAXIMIZED
        flag) without activating it."""
        fn = getattr(self._api, "SetWindowPlacement", None)
        if fn is None:
            return
        placement.length = ctypes.sizeof(WINDOWPLACEMENT)
        placement.showCmd = SW_SHOWMINNOACTIVE
        try:
            fn(hwnd, ctypes.byref(placement))
        except Exception:
            pass
