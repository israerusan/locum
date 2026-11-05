"""Screen support for the Windows backend: process DPI awareness
(:func:`enable_dpi_awareness`), the per-thread mss grabber holder and the
:class:`_ScreenMixin` (virtual screen, monitors, screenshots).
"""

from __future__ import annotations

import ctypes
import threading
import weakref
from typing import Any, List, Optional, Tuple

from ...types import Monitor, Rect
from ._win32api import (
    BITMAPINFO,
    BITMAPINFOHEADER,
    BI_RGB,
    CAPTUREBLT,
    DIB_RGB_COLORS,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    ERROR_ACCESS_DENIED,
    E_ACCESSDENIED,
    LOGPIXELSX,
    MDT_EFFECTIVE_DPI,
    MONITORINFOEXW,
    MONITORINFOF_PRIMARY,
    PROCESS_PER_MONITOR_DPI_AWARE,
    SRCCOPY,
    UINT,
    _win32,
)


# =========================================================================== DPI awareness
_dpi_lock = threading.Lock()
_dpi_mode: Optional[str] = None


def enable_dpi_awareness(api: Any = None) -> str:
    """Make the process per-monitor DPI aware (idempotent). Returns the mode obtained.

    Tries, in order: ``SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)``
    (Windows 10 1703+), ``...(PER_MONITOR_AWARE)``,
    ``shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)`` (8.1+),
    ``SetProcessDPIAware`` (Vista+). If the awareness was already set (by a
    manifest or an earlier call, which makes later calls fail with
    access-denied) the calling *thread* is switched with
    ``SetThreadDpiAwarenessContext`` when possible.

    Returns one of ``"per_monitor_v2"``, ``"per_monitor"``, ``"system"``,
    ``"thread_per_monitor_v2"``, ``"preset"`` (already set elsewhere) or
    ``"unaware"``.
    """
    global _dpi_mode
    if _dpi_mode is not None and api is None:
        return _dpi_mode
    with _dpi_lock:
        if _dpi_mode is not None and api is None:
            return _dpi_mode
        a = api or _win32()
        mode = "unaware"
        denied = False
        fn = getattr(a, "SetProcessDpiAwarenessContext", None)
        if fn is not None:
            for ctx, label in (
                (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, "per_monitor_v2"),
                (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE, "per_monitor"),
            ):
                if fn(ctypes.c_void_p(ctx)):
                    mode = label
                    break
                if a.get_last_error() == ERROR_ACCESS_DENIED:
                    denied = True
                    break
        if mode == "unaware" and not denied:
            fn2 = getattr(a, "SetProcessDpiAwareness", None)
            if fn2 is not None:
                hr = int(fn2(PROCESS_PER_MONITOR_DPI_AWARE))
                if hr == 0:
                    mode = "per_monitor"
                elif hr == E_ACCESSDENIED:
                    denied = True
        if mode == "unaware" and not denied:
            fn3 = getattr(a, "SetProcessDPIAware", None)
            if fn3 is not None and fn3():
                mode = "system"
        if mode == "unaware" and denied:
            thread_fn = getattr(a, "SetThreadDpiAwarenessContext", None)
            if thread_fn is not None and thread_fn(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
                mode = "thread_per_monitor_v2"
            else:
                mode = "preset"
        if api is None:
            _dpi_mode = mode
        return mode


class _MssHolder:
    """Thread-local owner of one mss grabber; its finalizer closes the grabber."""

    __slots__ = ("sct", "__weakref__")

    def __init__(self, sct: Any) -> None:
        self.sct = sct


def _close_quietly(obj: Any) -> None:
    try:
        obj.close()
    except Exception:
        pass


class _ScreenMixin:
    """Virtual screen, monitors and screenshots of :class:`WindowsBackend`."""

    # =================================================================== screen
    def virtual_screen(self) -> Rect:
        self._check()
        return self._virtual_metrics()

    def _monitor_scale(self, hmon: Any) -> float:
        a = self._api
        fn = getattr(a, "GetDpiForMonitor", None)
        if fn is not None:
            dx, dy = UINT(0), UINT(0)
            try:
                if int(fn(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy))) == 0 and dx.value:
                    return round(dx.value / 96.0, 4)
            except Exception:
                pass
        hdc = a.GetDC(None)
        if hdc:
            try:
                dpi = int(a.GetDeviceCaps(hdc, LOGPIXELSX))
                if dpi > 0:
                    return round(dpi / 96.0, 4)
            finally:
                a.ReleaseDC(None, hdc)
        return 1.0

    def monitors(self) -> List[Monitor]:
        """All monitors in physical pixels; the primary first, others ordered left-to-right."""
        self._check()
        a = self._api
        handles: List[Any] = []

        def cb(hmon: Any, hdc: Any, lprect: Any, lparam: Any) -> int:
            handles.append(hmon)
            return 1

        proc = a.MONITORENUMPROC(cb)
        if not a.EnumDisplayMonitors(None, None, proc, 0):
            raise self._error("EnumDisplayMonitors")
        found: List[Tuple[Rect, bool, str, float]] = []
        for h in handles:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if not a.GetMonitorInfoW(h, ctypes.byref(info)):
                continue
            found.append((info.rcMonitor.to_rect(), bool(info.dwFlags & MONITORINFOF_PRIMARY),
                          str(info.szDevice), self._monitor_scale(h)))
        if not found:
            vs = self._virtual_metrics()
            return [Monitor(0, vs, 1.0, True, "")]
        found.sort(key=lambda m: (not m[1], m[0].left, m[0].top))
        return [Monitor(i, r, s, prim, name) for i, (r, prim, name, s) in enumerate(found)]

    def _mss(self) -> Any:
        """This thread's mss grabber (it holds GDI device contexts and a bitmap).

        It lives in a thread-local slot whose finalizer closes it as soon as the
        thread exits, so thread-per-job services never pile up GDI handles (capped
        per process); :meth:`close` closes the ones of threads still running.
        """
        holder = getattr(self._mss_local, "holder", None)
        if holder is None:
            import mss

            factory = getattr(mss, "MSS", None) or mss.mss
            sct = factory()
            holder = _MssHolder(sct)
            with self._mss_lock:
                self._mss_closers = [c for c in self._mss_closers if c.alive]
                self._mss_closers.append(weakref.finalize(holder, _close_quietly, sct))
            self._mss_local.holder = holder
        return holder.sct

    def _grab_mss(self, r: Rect) -> Any:
        from PIL import Image

        shot = self._mss().grab({"left": r.left, "top": r.top, "width": r.width, "height": r.height})
        return Image.frombuffer("RGB", (shot.width, shot.height), bytes(shot.bgra), "raw", "BGRX", 0, 1)

    def _grab_gdi(self, r: Rect) -> Any:
        from PIL import Image

        a = self._api
        hdc_screen = a.GetDC(None)
        if not hdc_screen:
            raise self._error("GetDC")
        hdc_mem = hbm = None
        try:
            hdc_mem = a.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                raise self._error("CreateCompatibleDC")
            hbm = a.CreateCompatibleBitmap(hdc_screen, r.width, r.height)
            if not hbm:
                raise self._error("CreateCompatibleBitmap")
            old = a.SelectObject(hdc_mem, hbm)
            try:
                if not a.BitBlt(hdc_mem, 0, 0, r.width, r.height, hdc_screen, r.left, r.top, SRCCOPY | CAPTUREBLT):
                    raise self._error("BitBlt")
            finally:
                a.SelectObject(hdc_mem, old)  # GetDIBits needs the bitmap deselected
            bmi = BITMAPINFO()
            hdr = bmi.bmiHeader
            hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            hdr.biWidth = r.width
            hdr.biHeight = -r.height  # negative = top-down rows
            hdr.biPlanes = 1
            hdr.biBitCount = 32
            hdr.biCompression = BI_RGB
            buf = ctypes.create_string_buffer(r.width * r.height * 4)
            lines = a.GetDIBits(hdc_mem, hbm, 0, r.height, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
            if int(lines) != r.height:
                raise self._error("GetDIBits")
            return Image.frombuffer("RGB", (r.width, r.height), buf.raw, "raw", "BGRX", 0, 1)
        finally:
            if hbm:
                a.DeleteObject(hbm)
            if hdc_mem:
                a.DeleteDC(hdc_mem)
            a.ReleaseDC(None, hdc_screen)

    def _grab(self, r: Rect) -> Any:
        if not self._mss_broken:
            try:
                return self._grab_mss(r)
            except Exception:
                if self._screenshot_method == "mss":
                    raise
                self._mss_broken = True
        return self._grab_gdi(r)

    def screenshot(self, region: Optional[Rect] = None) -> Any:
        """RGB screenshot of ``region`` (default: whole virtual screen); off-screen parts are black."""
        from PIL import Image

        self._check()
        vs = self._virtual_metrics()
        r = vs if region is None else Rect.coerce(region)
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Screenshot region must have a positive size, got {r}")
        inter = r.intersect(vs)
        if inter is None:
            return Image.new("RGB", (r.width, r.height), (0, 0, 0))
        img = self._grab(inter)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if inter == r:
            return img
        canvas = Image.new("RGB", (r.width, r.height), (0, 0, 0))
        canvas.paste(img, (inter.left - r.left, inter.top - r.top))
        return canvas
