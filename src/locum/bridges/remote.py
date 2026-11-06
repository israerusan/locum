"""An image-only session scoped to one remote-desktop window (Citrix, RDP, Horizon).

A Citrix Workspace (``wfica32.exe``), Remote Desktop (``mstsc.exe``) or VMware
Horizon window is a single bitmap to every accessibility API on the client:
there is no tree to select from. What works is what a human does -- look at
the pixels and send input to that window. locum already has the pieces
(:meth:`Desktop.locate <locum.desktop.Desktop.locate>`, ``click_image``,
``find_text``/``click_text`` OCR, ``type``/``press``); :class:`ImageSession`
only binds them to the remote window:

* every search is confined to the window's current rectangle, so a matching
  icon or word in another application is never clicked;
* every click and keystroke runs inside an input-target binding
  (:mod:`locum._target`, strict): if another window takes the foreground or
  covers the point, :class:`~locum.errors.TargetLost` is raised and nothing is
  sent;
* with a :class:`~locum.llm.safety.GuardedDesktop` (or a guarded window from
  one) every action is policy-checked as usual (OCR text clicks are judged by
  their text).

::

    from locum import Desktop
    from locum.bridges.remote import ImageSession

    d = Desktop()
    citrix = ImageSession(d.window(app="wfica32"))
    citrix.click_text("Purchase Orders", timeout=20)
    citrix.wait_image("icons/new.png").center
    citrix.click_image("icons/new.png")
    citrix.type("4500012345\\n")
    citrix.read_text((0, 0, 400, 60))               # window-relative region
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator, List, Optional, Tuple

from .. import _target
from ..types import Match, Point, Rect

__all__ = ["ImageSession"]


class ImageSession:
    """Image/OCR automation confined to one window, with input bound to it.

    Args:
        window: The remote session's :class:`~locum.window.Window` (or a guarded one).
        desktop: The desktop to act through (default: the window's; a
            :class:`~locum.llm.safety.GuardedDesktop` puts actions behind its policy).
        activate: Bring the window to the front before input when it is not.
        default_timeout: Default wait for ``wait_image`` / ``click_image`` / ``click_text``
            (remote screens repaint slowly; ``None`` = the desktop's ``default_timeout``).
    """

    def __init__(self, window: Any, desktop: Any = None, *, activate: bool = True,
                 default_timeout: Optional[float] = None) -> None:
        if desktop is None:
            try:
                desktop = object.__getattribute__(window, "_g_guard")  # a GuardedWindow's GuardedDesktop
            except AttributeError:
                desktop = window.desktop
        self.window = window
        self.desktop = desktop
        self.activate = bool(activate)
        self.default_timeout = default_timeout

    # ------------------------------------------------------------------ geometry
    @property
    def rect(self) -> Rect:
        """The window's current screen rectangle."""
        return self.window.refresh().rect

    def to_screen(self, x: int, y: int) -> Point:
        """Window-relative point -> screen point."""
        r = self.rect
        return Point(r.left + int(x), r.top + int(y))

    def _region(self, region: Any = None) -> Rect:
        r = self.rect
        if region is None:
            return r
        sub = Rect.coerce(region).offset(r.left, r.top)
        inside = sub.intersect(r)
        if inside is None:
            raise ValueError(f"Region {region!r} lies outside the window ({r.width}x{r.height})")
        return inside

    def _timeout(self, timeout: Optional[float]) -> Optional[float]:
        return self.default_timeout if timeout is None else timeout

    # ------------------------------------------------------------------ binding
    @contextlib.contextmanager
    def bound(self) -> Iterator[None]:
        """Input inside the block must reach this window (strict :class:`~locum._target.InputTarget`)."""
        if self.activate:
            backend = self.window.backend
            try:
                front = backend.foreground_handle()
            except Exception:  # noqa: BLE001
                front = None
            if front != self.window.handle:
                self.window.activate()
        target = _target.for_window(self.window, strict=True)
        if target is None:
            raise ValueError(f"{self.window!r} has no native handle; input cannot be bound to it")
        with _target.bind(target):
            yield

    # ------------------------------------------------------------------ looking
    def screenshot(self) -> Any:
        return self.desktop.screenshot(self.rect)

    def locate(self, image: Any, *, region: Any = None, **kw: Any) -> Optional[Match]:
        """Template match inside the window (``region``: window-relative rect); screen coordinates."""
        return self.desktop.locate(image, region=self._region(region), **kw)

    def locate_all(self, image: Any, *, region: Any = None, **kw: Any) -> List[Match]:
        return self.desktop.locate_all(image, region=self._region(region), **kw)

    def wait_image(self, image: Any, *, timeout: Optional[float] = None, region: Any = None, **kw: Any) -> Match:
        return self.desktop.wait_image(image, timeout=self._timeout(timeout), region=self._region(region), **kw)

    def find_text(self, text: str, *, region: Any = None, timeout: float = 0.0, **kw: Any) -> Optional[Match]:
        """OCR inside the window for ``text`` (``engine``, ``regex``, ``fuzzy``... as in ``Desktop.find_text``)."""
        return self.desktop.find_text(text, region=self._region(region), timeout=timeout, **kw)

    def read_text(self, region: Any = None, **kw: Any) -> str:
        return self.desktop.read_text(self._region(region), **kw)

    # ------------------------------------------------------------------ acting
    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> Point:
        """Click a window-relative point."""
        point = self.to_screen(x, y)
        with self.bound():
            self.desktop.click(point, button=button, clicks=clicks)
        return point

    def click_image(self, image: Any, *, timeout: Optional[float] = None, region: Any = None,
                    offset: Tuple[int, int] = (0, 0), **kw: Any) -> Match:
        with self.bound():
            return self.desktop.click_image(image, timeout=self._timeout(timeout), region=self._region(region),
                                            offset=offset, **kw)

    def click_text(self, text: str, *, timeout: Optional[float] = None, region: Any = None,
                   offset: Tuple[int, int] = (0, 0), **kw: Any) -> Match:
        with self.bound():
            return self.desktop.click_text(text, timeout=self._timeout(timeout), region=self._region(region),
                                           offset=offset, **kw)

    def type(self, text: str, *, interval: float = 0.0, mode: Optional[str] = None) -> None:
        """Type into the remote session. Remote sessions see key events, not Unicode injection
        reliably: ``mode="keys"`` is often the safest choice (``"unicode"`` can drop characters)."""
        with self.bound():
            self.desktop.type(text, interval=interval, mode=mode)

    def press(self, *keys: Any, **kw: Any) -> None:
        with self.bound():
            self.desktop.press(*keys, **kw)

    def hotkey(self, *keys: Any, **kw: Any) -> None:
        with self.bound():
            self.desktop.hotkey(*keys, **kw)

    def __repr__(self) -> str:
        return f"<ImageSession {getattr(self.window, 'title', '')!r}>"
