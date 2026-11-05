"""``MacOSBackend`` displays (monitors, point/pixel conversion) and screenshots.
"""

from __future__ import annotations

import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

from ...errors import BackendError
from ...types import Monitor, Point, Rect
from .constants import (
    SCREEN_RECORDING_HELP,
    kCGNullWindowID,
    kCGWindowImageDefault,
    kCGWindowListOptionOnScreenOnly,
)
from .displays import DisplayInfo, DisplayLayout, capture_plan


class _ScreenMixin:
    # -------------------------------------------------------------------- displays
    def _query_displays(self) -> List[DisplayInfo]:
        q = self._q
        err, ids, count = q.CGGetActiveDisplayList(32, None, None)
        if err:
            raise BackendError(f"CGGetActiveDisplayList failed (error {err})")
        ids = list(ids or [])[: int(count or 0)]
        main = int(q.CGMainDisplayID())
        screens: Dict[int, Tuple[float, str, Optional[Tuple[float, float, float, float]]]] = {}
        if self._appkit is not None:
            try:
                ns_screens = list(self._appkit.NSScreen.screens() or [])
                primary_h = float(ns_screens[0].frame().size.height) if ns_screens else 0.0
                for s in ns_screens:
                    did = int(s.deviceDescription()["NSScreenNumber"])
                    name = str(s.localizedName()) if hasattr(s, "localizedName") else ""
                    vf = s.visibleFrame()
                    # Cocoa: bottom-left origin, y up -> Quartz: top-left origin, y down
                    work = (float(vf.origin.x), primary_h - float(vf.origin.y) - float(vf.size.height),
                            float(vf.size.width), float(vf.size.height))
                    screens[did] = (float(s.backingScaleFactor()), name, work)
            except Exception:
                screens = {}
        out: List[DisplayInfo] = []
        for did in ids:
            did = int(did)
            try:
                if q.CGDisplayMirrorsDisplay(did) not in (0, None):
                    continue  # mirror of another display: same pixels
            except Exception:
                pass
            b = q.CGDisplayBounds(did)
            x, y = float(b.origin.x), float(b.origin.y)
            w, h = float(b.size.width), float(b.size.height)
            scale, name, work = screens.get(did, (0.0, "", None))
            if not scale:
                scale = 1.0
                try:
                    mode = q.CGDisplayCopyDisplayMode(did)
                    pw = float(q.CGDisplayModeGetPixelWidth(mode))
                    if w > 0 and pw > 0:
                        scale = pw / w
                except Exception:
                    pass
            out.append(DisplayInfo(did, x, y, w, h, scale=scale, is_primary=(did == main),
                                   name=name or f"Display {did}", work_area=work))
        if not out:
            raise BackendError("No active displays")
        return out

    def _get_layout(self) -> DisplayLayout:
        now = time.monotonic()
        if self._layout is None or (self._layout_ttl is not None and now - self._layout_time > self._layout_ttl):
            self._layout = DisplayLayout(self._query_displays())
            self._layout_time = now
        return self._layout

    def _points_rect_to_pixels(self, x: float, y: float, w: float, h: float) -> Rect:
        return self._get_layout().points_rect_to_pixels(x, y, w, h)

    def _rect_on_screen(self, rect: Rect) -> bool:
        return any(d.pixel_rect.intersect(rect) is not None for d in self._get_layout().displays)

    def monitors(self) -> List[Monitor]:
        return self._get_layout().monitors()

    def to_points(self, x: float, y: float) -> Tuple[float, float]:
        """Convert backend pixel coordinates to Quartz global points."""
        return self._get_layout().pixel_to_point(x, y)

    def to_pixels(self, x: float, y: float) -> Point:
        """Convert Quartz global points to backend pixel coordinates."""
        return self._get_layout().point_to_pixel(x, y)

    # ---------------------------------------------------------------------- screen
    def _mss(self) -> Any:
        sct = getattr(self._tls, "mss", None)
        if sct is None:
            import mss  # hard dependency, but keep the import lazy

            sct = mss.mss()
            self._tls.mss = sct
        return sct

    def _grab_points(self, x: int, y: int, w: int, h: int) -> Any:
        """Capture a region given in points, at the display's full pixel resolution.

        Quartz comes first: mss on macOS captures with
        ``kCGWindowImageNominalResolution`` (one pixel per point), so on a Retina
        display its image would have to be upscaled. mss is the fallback."""
        from PIL import Image

        try:
            return self._grab_points_cg(x, y, w, h)
        except Exception as cg_exc:
            try:
                shot = self._mss().grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            except Exception as mss_exc:
                raise BackendError(f"Screen capture failed (Quartz: {cg_exc}; mss: {mss_exc})") from mss_exc

    def _grab_points_cg(self, x: int, y: int, w: int, h: int) -> Any:
        from PIL import Image

        q = self._q
        img = q.CGWindowListCreateImage(q.CGRectMake(x, y, w, h), kCGWindowListOptionOnScreenOnly,
                                        kCGNullWindowID, kCGWindowImageDefault)
        if img is None:
            raise BackendError("CGWindowListCreateImage returned no image. " + SCREEN_RECORDING_HELP)
        width, height = int(q.CGImageGetWidth(img)), int(q.CGImageGetHeight(img))
        bpr = int(q.CGImageGetBytesPerRow(img))
        data = bytes(q.CGDataProviderCopyData(q.CGImageGetDataProvider(img)))
        return Image.frombuffer("RGB", (width, height), data, "raw", "BGRX", bpr, 1)

    def _warn_screen_recording(self) -> None:
        if self._warned_capture:
            return
        self._warned_capture = True
        if self.permissions().get("screen_recording") is False:
            warnings.warn(SCREEN_RECORDING_HELP, RuntimeWarning, stacklevel=3)

    def screenshot(self, region: Optional[Rect] = None) -> Any:
        """RGB screenshot of ``region`` (pixels; default: the whole virtual screen)."""
        from PIL import Image

        self._warn_screen_recording()
        layout = self._get_layout()
        region = layout.virtual_pixel_rect if region is None else Rect.coerce(region)
        if region.is_empty:
            raise ValueError(f"Empty screenshot region: {region}")
        out = Image.new("RGB", (region.width, region.height), (0, 0, 0))
        for d in layout.displays:
            part = d.pixel_rect.intersect(region)
            if part is None:
                continue
            pts, expected, crop = capture_plan(d, part)
            img = self._grab_points(*pts)
            if img.size != expected:
                img = img.resize(expected, Image.BILINEAR)
            out.paste(img.crop(crop), (part.left - region.left, part.top - region.top))
        return out
