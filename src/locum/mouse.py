"""High-level mouse control: timed/humanized moves, clicks, drags and scrolling.

:class:`Mouse` wraps a :class:`~locum.backends.base.Backend` (whose primitives
are instant) and adds everything that makes automation robust and natural:

* smooth tweened or humanized (Bézier + overshoot + tremor) motion, timed with
  Fitts's law when no duration is given;
* fail-safe checks before every action **and between every waypoint** of a
  move, so slamming the mouse into a screen corner stops even a long drag;
* drags that always release the button (``finally``), even when aborted;
* multi-clicks on a single spot (no cursor movement between clicks).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional, Union

from . import _inputlock
from ._cancel import check as _cancel_check
from ._target import check_input as _target_check
from .errors import NotSupported
from .failsafe import FailSafe
from .motion import Humanize, fitts_duration, plan_path
from .types import MouseButton, Point, coerce_point

HumanizeArg = Union[Humanize, bool, None]


class Mouse:
    """Mouse controller bound to a backend.

    Args:
        backend: The platform backend.
        failsafe: Optional :class:`FailSafe` checked before/during actions.
        pause: Seconds to sleep after every public action (PyAutoGUI's ``PAUSE``).
        humanize: Default humanization for moves (``None`` = mechanical/instant).
        sleep: Sleep function (inject ``lambda s: None`` in tests).
        clock: Monotonic clock used to schedule waypoints (inject in tests).
        rate: Motion sampling rate in Hz for timed moves.
    """

    def __init__(
        self,
        backend: Any,
        failsafe: Optional[FailSafe] = None,
        pause: float = 0.0,
        humanize: Optional[Humanize] = None,
        sleep: Callable[[float], Any] = time.sleep,
        *,
        clock: Callable[[], float] = time.monotonic,
        rate: int = 120,
    ):
        self.backend = backend
        self.failsafe = failsafe
        self.pause = float(pause)
        self.humanize = humanize
        self.rate = int(rate)
        self._sleep = sleep
        self._clock = clock
        self._pressed: list = []  # buttons held down by down() (press order)

    # ------------------------------------------------------------------ helpers
    def _check(self, ignore: Optional[Point] = None) -> None:
        """Gate run right before every move/press/scroll reaches the backend (never before a release).

        Besides the fail-safe it honours the running call's cancel scope, so a
        call the MCP server already reported as timed out / stopped sends no
        further input.
        """
        _cancel_check()
        if self.failsafe is not None:
            self.failsafe.check(ignore=ignore)

    def _pause(self) -> None:
        if self.pause > 0:
            self._sleep(self.pause)

    def _resolve_humanize(self, humanize: HumanizeArg) -> Optional[Humanize]:
        if humanize is None:
            return self.humanize
        if humanize is True:
            return self.humanize if self.humanize is not None else Humanize()
        if humanize is False:
            return None
        if isinstance(humanize, Humanize):
            return humanize
        raise TypeError(f"humanize must be a Humanize, bool or None, not {type(humanize).__name__}")

    @staticmethod
    def _point(x: Any, y: Any = None) -> Point:
        if y is None:
            return coerce_point(x)
        return Point(int(round(x)), int(round(y)))

    def _bounds(self):
        try:
            return self.backend.virtual_screen()
        except Exception:
            return None

    def _travel(
        self, target: Point, duration: Optional[float], tween: Any, humanize: HumanizeArg,
        ignore: Optional[Point] = None,
    ) -> Point:
        """Move to ``target`` without the trailing pause (shared by move/click/drag).

        ``ignore`` is a point the cursor was just moved to by this same action
        (drag's start), so a corner there does not self-trigger the fail-safe.
        """
        self._check(ignore=ignore)
        h = self._resolve_humanize(humanize)
        try:
            start = self.backend.mouse_position()
        except NotSupported:
            # The backend cannot read the pointer (Wayland before locum first moved it):
            # there is no start for a path, so jump straight to the target.
            self.backend.mouse_move(target.x, target.y)
            return target
        if duration is None:
            duration = fitts_duration(Point(*start).distance(target), speed=h.speed) if h is not None else 0.0
        duration = max(0.0, float(duration))
        if duration <= 0.0 and h is None:
            if tuple(start) != tuple(target):
                self.backend.mouse_move(target.x, target.y)
            return target
        path = plan_path(start, target, duration=duration, tween=tween, humanize=h, rate=self.rate, bounds=self._bounds())
        t0 = self._clock()
        last: Optional[Point] = Point(*start)
        for point, offset in path:
            # Skip the corner test only if the cursor is where *we* put it; a user
            # moving the mouse (into a corner) during the motion still trips it.
            wait = t0 + offset - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._check(ignore=last)
            if point != last:
                self.backend.mouse_move(point.x, point.y)
                last = point
        return target

    # --------------------------------------------------------------- public API
    def position(self) -> Point:
        """Current cursor position."""
        p = self.backend.mouse_position()
        return Point(int(p[0]), int(p[1]))

    @_inputlock.locked
    def move(
        self,
        x: Any,
        y: Optional[float] = None,
        *,
        duration: Optional[float] = None,
        tween: Any = "ease_in_out_quad",
        humanize: HumanizeArg = None,
    ) -> Point:
        """Move the cursor to ``(x, y)`` (or a point-like ``x`` with ``y=None``).

        ``duration=None`` moves instantly unless humanization is active, in which
        case the duration follows Fitts's law. Returns the target point.
        """
        target = self._point(x, y)
        self._travel(target, duration, tween, humanize)
        self._pause()
        return target

    def move_rel(self, dx: float, dy: float, **kw: Any) -> Point:
        """Move relative to the current position. Accepts :meth:`move` keyword arguments."""
        cur = self.position()
        return self.move(Point(int(round(cur.x + dx)), int(round(cur.y + dy))), **kw)

    @_inputlock.locked
    def down(self, button: Union[str, MouseButton] = "left") -> None:
        """Press (and hold) a mouse button at the current position."""
        btn = MouseButton.coerce(button)
        self._check()
        # Recorded before the backend call, so an interrupt inside it (after the press
        # reached the OS) still leaves release_all() knowing about the button.
        _target_check("button")
        added = btn not in self._pressed
        if added:
            self._pressed.append(btn)
        try:
            self.backend.mouse_down(btn)
        except Exception:
            if added:  # an ordinary error: the press was not made
                self._pressed.remove(btn)
            raise
        self._pause()

    def up(self, button: Union[str, MouseButton] = "left") -> None:
        """Release a mouse button. Never blocked by the fail-safe (releasing is always safe)."""
        btn = MouseButton.coerce(button)
        self.backend.mouse_up(btn)
        if btn in self._pressed:
            self._pressed.remove(btn)
        self._pause()

    def _safe_up(self, btn: MouseButton) -> None:
        """Release ``btn`` on an error path without masking the original exception."""
        try:
            self.backend.mouse_up(btn)
        except Exception:
            pass

    @property
    def pressed(self) -> list:
        """Buttons currently held down by :meth:`down` on this instance (press order)."""
        return list(self._pressed)

    def release_all(self) -> list:
        """Release every button held down by :meth:`down` (e.g. after an error mid-selection).

        Keeps going on errors and raises the first one at the end; returns the released buttons.
        """
        released: list = []
        first: Optional[BaseException] = None
        for btn in reversed(list(self._pressed)):
            try:
                self.backend.mouse_up(btn)
            except Exception as exc:
                if first is None:
                    first = exc
                continue
            self._pressed.remove(btn)
            released.append(btn)
        if first is not None:
            raise first
        return released

    @_inputlock.locked
    def click(
        self,
        x: Any = None,
        y: Optional[float] = None,
        *,
        button: Union[str, MouseButton] = "left",
        clicks: int = 1,
        interval: float = 0.0,
        duration: Optional[float] = None,
        humanize: HumanizeArg = None,
    ) -> Point:
        """Click ``clicks`` times at a point (``x=None`` = current position).

        The cursor is moved once and every click happens on that same spot, so
        the OS recognises double/triple clicks. Returns the clicked point.
        """
        btn = MouseButton.coerce(button)
        clicks = int(clicks)
        if clicks < 0:
            raise ValueError("clicks must be >= 0")
        h = self._resolve_humanize(humanize)
        if x is None:
            target = self.position()
            self._check()
        else:
            target = self._point(x, y)
            self._travel(target, duration, "ease_in_out_quad", h if h is not None else False)
        for i in range(clicks):
            if i and interval > 0:
                self._sleep(interval)
            self._check(ignore=target)
            _target_check("button", point=(target.x, target.y))
            down = False
            try:
                self.backend.mouse_down(btn)
                down = True
                if h is not None:
                    self._sleep(h.rng.uniform(0.03, 0.08) / h.speed)
            except BaseException as exc:
                if down or not isinstance(exc, Exception):  # an interrupt may land after the press was sent
                    self._safe_up(btn)
                raise
            self.backend.mouse_up(btn)
        self._pause()
        return target

    def double_click(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
        """Two clicks on the same spot."""
        kw.setdefault("clicks", 2)
        return self.click(x, y, **kw)

    def triple_click(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
        """Three clicks on the same spot (selects a line/paragraph in most apps)."""
        kw.setdefault("clicks", 3)
        return self.click(x, y, **kw)

    def right_click(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
        """Click with the right button."""
        kw["button"] = "right"
        return self.click(x, y, **kw)

    def middle_click(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
        """Click with the middle button."""
        kw["button"] = "middle"
        return self.click(x, y, **kw)

    @_inputlock.locked
    def drag(
        self,
        start: Any,
        end: Any,
        *,
        button: Union[str, MouseButton] = "left",
        duration: float = 0.5,
        humanize: HumanizeArg = None,
        hold: float = 0.05,
        tween: Any = "ease_in_out_quad",
    ) -> Point:
        """Press ``button`` at ``start`` (``None`` = current position), move to ``end`` and release.

        The button is released in a ``finally`` block, so it never stays stuck
        even if the fail-safe fires mid-drag. ``hold`` seconds are waited after
        pressing and before releasing, which many toolkits need to start/accept
        a drag. Returns the end point.
        """
        btn = MouseButton.coerce(button)
        h = self._resolve_humanize(humanize)
        at: Optional[Point] = None
        if start is not None:
            at = self._travel(self._point(start), None if h is not None else 0.0, tween, h if h is not None else False)
        else:
            self._check()
        target = self._point(end)
        _cancel_check()  # the move to ``start`` may have outlived the call's deadline
        _target_check("button")
        down = False
        try:
            self.backend.mouse_down(btn)
            down = True
            if hold > 0:
                self._sleep(hold)
            # The cursor sits where *we* put it (a corner start must not self-trigger).
            self._travel(target, duration, tween, h if h is not None else False, ignore=at)
            if hold > 0:
                self._sleep(hold)
        except BaseException as exc:
            if down or not isinstance(exc, Exception):  # an interrupt may land after the press was sent
                self._safe_up(btn)
            raise
        self.backend.mouse_up(btn)
        self._pause()
        return target

    @_inputlock.locked
    def scroll(self, dy: int = 0, dx: int = 0, *, at: Any = None) -> None:
        """Scroll by wheel notches (``dy > 0`` = up, ``dx > 0`` = right), optionally after moving to ``at``."""
        target: Optional[Point] = None
        if at is not None:
            target = self._travel(self._point(at), None, "ease_in_out_quad", None)
        self._check(ignore=target)
        dy, dx = int(dy), int(dx)
        if dy or dx:
            _target_check("scroll", point=(target.x, target.y) if target is not None else None)
            self.backend.scroll(dx=dx, dy=dy)
        self._pause()

    def __repr__(self) -> str:
        return f"<Mouse backend={getattr(self.backend, 'name', '?')} humanize={self.humanize is not None}>"


__all__ = ["Mouse"]
