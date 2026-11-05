"""Screen capture, pixels and visual waiting.

:class:`Screen` wraps a backend's ``screenshot``/``monitors`` primitives with
coordinate-safe helpers:

* :meth:`Screen.capture` always returns an ``RGB`` image whose size equals the
  requested region, even if the region hangs off the virtual desktop (the
  missing part is black), so ``image pixel + region origin == screen pixel``
  holds unconditionally.
* :meth:`Screen.wait_for_change` / :meth:`Screen.wait_until_stable` poll the
  screen with injectable ``sleep``/``clock`` (deterministic tests).
* Convenience wrappers :meth:`Screen.locate`, :meth:`Screen.locate_all`,
  :meth:`Screen.find_color` and :meth:`Screen.find_text` capture and return
  results in **screen coordinates**.
"""

from __future__ import annotations

import os
import pathlib
import time
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple, Union

from .types import Match, Monitor, Point, Rect, RectLike
from . import vision

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from .backends.base import Backend

__all__ = ["Screen"]

_METRICS = ("mean", "pixels")


class Screen:
    """Screen access on top of a :class:`~pypilot.backends.base.Backend`.

    Args:
        backend: the platform backend.
        sleep: sleep function used by the wait helpers (inject for tests).
        clock: monotonic clock used by the wait helpers (inject for tests).
    """

    def __init__(
        self,
        backend: "Backend",
        *,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.backend = backend
        self.sleep = sleep
        self.clock = clock

    def __repr__(self) -> str:
        return f"<Screen backend={getattr(self.backend, 'name', type(self.backend).__name__)!r}>"

    # ------------------------------------------------------------------ geometry
    def monitors(self) -> List[Monitor]:
        """All monitors (index 0 is the primary one)."""
        return list(self.backend.monitors())

    def primary_monitor(self) -> Monitor:
        mons = self.monitors()
        if not mons:
            raise RuntimeError("The backend reported no monitors")
        for m in mons:
            if m.is_primary:
                return m
        return mons[0]

    def size(self) -> Tuple[int, int]:
        """``(width, height)`` of the primary monitor."""
        r = self.primary_monitor().rect
        return (r.width, r.height)

    def virtual_rect(self) -> Rect:
        """Bounding rect of all monitors (may have a negative origin)."""
        return self.backend.virtual_screen()

    def monitor_rect(self, monitor: int) -> Rect:
        """Rect of monitor ``monitor`` (matched by :attr:`Monitor.index`, else list position)."""
        mons = self.monitors()
        for m in mons:
            if m.index == monitor:
                return m.rect
        if 0 <= monitor < len(mons):
            return mons[monitor].rect
        raise ValueError(f"No monitor {monitor}; available: {[m.index for m in mons]}")

    def on_screen(self, x: int, y: int) -> bool:
        """True if ``(x, y)`` lies on some monitor."""
        return any(m.rect.contains((x, y)) for m in self.monitors())

    def resolve_region(self, region: Optional[RectLike] = None, *, monitor: Optional[int] = None) -> Rect:
        """The absolute screen rect :meth:`capture` would grab for these arguments.

        ``region`` is absolute unless ``monitor`` is also given, in which case it
        is relative to that monitor's top-left corner.
        """
        if monitor is not None:
            mrect = self.monitor_rect(monitor)
            if region is None:
                return mrect
            return Rect.coerce(region).offset(mrect.left, mrect.top)
        if region is None:
            return self.virtual_rect()
        return Rect.coerce(region)

    # ------------------------------------------------------------------- capture
    def capture(self, region: Optional[RectLike] = None, *, monitor: Optional[int] = None) -> "Image":
        """Screenshot as an ``RGB`` PIL image.

        Args:
            region: absolute screen rect (``Rect``, ``(l, t, w, h)`` or dict);
                default = the whole virtual desktop. With ``monitor``, relative
                to that monitor.
            monitor: capture this monitor (index as in :meth:`monitors`).

        The returned image is exactly ``region.width x region.height``; parts of
        the region outside the virtual desktop are black.

        Raises:
            ValueError: empty region or unknown monitor.
        """
        from PIL import Image

        rect = self.resolve_region(region, monitor=monitor)
        if rect.is_empty:
            raise ValueError(f"Cannot capture an empty region: {rect}")
        vs = self.virtual_rect()
        visible = rect.intersect(vs)
        if visible is None:
            return Image.new("RGB", (rect.width, rect.height), (0, 0, 0))
        full = region is None and monitor is None and visible == vs
        img = self.backend.screenshot(None if full else visible)
        img = self._normalize(img, visible.width, visible.height)
        if visible == rect:
            return img
        canvas = Image.new("RGB", (rect.width, rect.height), (0, 0, 0))
        canvas.paste(img, (visible.left - rect.left, visible.top - rect.top))
        return canvas

    @staticmethod
    def _normalize(img: Any, width: int, height: int) -> "Image":
        """RGB, exactly ``width x height`` (backends on HiDPI may return 2x images)."""
        from PIL import Image

        if not isinstance(img, Image.Image):
            img = vision.load_image(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != (width, height):
            img = img.resize((width, height), getattr(Image, "Resampling", Image).BILINEAR)
        return img

    def grab(self, region: Optional[RectLike] = None, *, monitor: Optional[int] = None) -> Tuple["Image", Point]:
        """Like :meth:`capture` but also returns the screen position of pixel (0, 0)."""
        rect = self.resolve_region(region, monitor=monitor)
        return self.capture(region, monitor=monitor), Point(rect.left, rect.top)

    def save(self, path: Union[str, "os.PathLike[str]"], region: Optional[RectLike] = None, *, monitor: Optional[int] = None) -> str:
        """Capture and save to ``path`` (format from the extension, PNG if none).

        Parent directories are created. Returns the absolute path written.
        """
        p = pathlib.Path(path).expanduser()
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        img = self.capture(region, monitor=monitor)
        if p.suffix:
            img.save(str(p))
        else:
            img.save(str(p), format="PNG")
        return str(p.resolve())

    # -------------------------------------------------------------------- pixels
    def pixel(self, x: int, y: int) -> Tuple[int, int, int]:
        """RGB color at screen position ``(x, y)``.

        Raises:
            ValueError: the point is outside the virtual desktop.
        """
        x, y = int(x), int(y)
        if not self.virtual_rect().contains((x, y)):
            raise ValueError(f"Point ({x}, {y}) is outside the screen {self.virtual_rect()}")
        px = self.backend.pixel(x, y)
        if isinstance(px, int):  # single-channel backend
            return (px, px, px)
        return (int(px[0]), int(px[1]), int(px[2]))

    def pixel_matches(self, x: int, y: int, color: Any, tolerance: int = 0) -> bool:
        """True if every RGB channel at ``(x, y)`` is within ``tolerance`` of ``color``.

        ``color`` may be an ``(r, g, b)`` tuple, ``"#rrggbb"`` or a CSS color name.
        """
        target = vision.parse_color(color)
        actual = self.pixel(x, y)
        tol = max(0, int(tolerance))
        return all(abs(a - b) <= tol for a, b in zip(actual, target))

    # --------------------------------------------------------------- waiting
    def _difference(self, a: "Image", b: "Image", metric: str, pixel_tolerance: int) -> float:
        if metric == "pixels":
            return vision.changed_fraction(a, b, pixel_tolerance=pixel_tolerance)
        return vision.image_diff(a, b)

    @staticmethod
    def _check_metric(metric: str) -> None:
        if metric not in _METRICS:
            raise ValueError(f"metric must be one of {_METRICS}, got {metric!r}")

    def wait_for_change(
        self,
        region: Optional[RectLike] = None,
        *,
        timeout: float = 10,
        interval: float = 0.2,
        threshold: float = 0.01,
        metric: str = "mean",
        pixel_tolerance: int = 8,
        monitor: Optional[int] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> bool:
        """Wait until the region differs from how it looked when the call started.

        Args:
            region: area to watch (default: whole screen). Prefer a small region:
                with the default ``"mean"`` metric a tiny change on a full-screen
                capture may stay below ``threshold``.
            timeout: seconds to wait (``0`` = take a single comparison).
            interval: seconds between captures.
            threshold: change is detected when the difference is ``> threshold``.
            metric: ``"mean"`` = :func:`vision.image_diff` (normalized mean absolute
                difference); ``"pixels"`` = fraction of pixels that changed by more
                than ``pixel_tolerance`` (:func:`vision.changed_fraction`).
            sleep / clock: override the instance's sleep/clock for this call.

        Returns:
            ``True`` as soon as a change is seen, ``False`` on timeout.
        """
        self._check_metric(metric)
        sleep = sleep or self.sleep
        clock = clock or self.clock
        start = clock()
        baseline = self.capture(region, monitor=monitor)
        while True:
            elapsed = clock() - start
            if timeout > 0 and elapsed >= timeout - 1e-9:
                return False
            if timeout > 0:
                sleep(max(0.0, min(interval, timeout - elapsed)))
            current = self.capture(region, monitor=monitor)
            if self._difference(baseline, current, metric, pixel_tolerance) > threshold:
                return True
            if timeout <= 0:
                return False

    def wait_until_stable(
        self,
        region: Optional[RectLike] = None,
        *,
        stable_for: float = 0.5,
        timeout: float = 10,
        interval: float = 0.1,
        threshold: float = 0.001,
        metric: str = "mean",
        pixel_tolerance: int = 8,
        monitor: Optional[int] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> bool:
        """Wait until the region stops changing for ``stable_for`` seconds.

        Each capture is compared to the frame at which the current quiet period
        began (not just the previous frame), so slow continuous drift such as a
        fade-in is not mistaken for stability.

        Returns:
            ``True`` once stable, ``False`` if ``timeout`` elapsed first.
        """
        self._check_metric(metric)
        sleep = sleep or self.sleep
        clock = clock or self.clock
        start = clock()
        reference = self.capture(region, monitor=monitor)
        quiet_since = start
        eps = 1e-9  # tolerate float accumulation in clocks
        while True:
            now = clock()
            if now - quiet_since >= stable_for - eps:
                return True
            if now - start >= timeout - eps:
                return False
            sleep(max(0.0, min(interval, timeout - (now - start))))
            current = self.capture(region, monitor=monitor)
            now = clock()
            if self._difference(reference, current, metric, pixel_tolerance) > threshold:
                reference = current
                quiet_since = now

    # ------------------------------------------------------- vision shortcuts
    def locate(self, needle: Any, *, region: Optional[RectLike] = None, monitor: Optional[int] = None, **kw: Any) -> Optional[Match]:
        """Capture ``region`` and :func:`vision.locate` ``needle``; result in screen coords."""
        img, origin = self.grab(region, monitor=monitor)
        return vision.locate(needle, img, offset=origin, **kw)

    def locate_all(self, needle: Any, *, region: Optional[RectLike] = None, monitor: Optional[int] = None, **kw: Any) -> List[Match]:
        """Capture ``region`` and :func:`vision.locate_all` ``needle``; results in screen coords."""
        img, origin = self.grab(region, monitor=monitor)
        return vision.locate_all(needle, img, offset=origin, **kw)

    def find_color(
        self, color: Any, *, region: Optional[RectLike] = None, monitor: Optional[int] = None, tolerance: int = 0, **kw: Any
    ) -> List[Rect]:
        """Connected regions of ``color`` on screen (screen coords)."""
        img, origin = self.grab(region, monitor=monitor)
        return vision.find_color(img, color, tolerance=tolerance, offset=origin, **kw)

    def find_text(self, query: str, *, region: Optional[RectLike] = None, monitor: Optional[int] = None, **kw: Any) -> List[Match]:
        """OCR the region and return matches of ``query`` (screen coords). See :func:`ocr.find_text`."""
        from . import ocr

        img, origin = self.grab(region, monitor=monitor)
        return ocr.find_text(img, query, offset=origin, **kw)

    def read_text(self, region: Optional[RectLike] = None, *, monitor: Optional[int] = None, engine: Any = None) -> str:
        """OCR the region and return its text (lines joined by ``\\n``)."""
        from . import ocr

        return ocr.read_text(self.capture(region, monitor=monitor), engine=engine)
