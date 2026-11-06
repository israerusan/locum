"""The :class:`Desktop` facade: one object that drives the whole desktop.

``Desktop`` wires the lower layers together -- :class:`~locum.mouse.Mouse`,
:class:`~locum.keyboard.Keyboard`, :class:`~locum.screen.Screen`,
:class:`~locum.failsafe.FailSafe`, the selector engine, elements and
windows -- on top of a single :class:`~locum.backends.base.Backend`::

    from locum import Desktop

    with Desktop() as d:
        d.window("Untitled - Notepad").activate()
        d.find('edit').set_text("hello")
        d.hotkey("ctrl+s")
        d.click('button "Save"')            # selector -> element -> robust click
        d.click("icons/ok.png")              # image file -> template match on screen
        d.click((640, 400))                  # plain coordinates

Every "where" argument (``target``) goes through :meth:`Desktop.resolve_point`,
so coordinates, rects, vision matches, elements, windows, selector strings and
template images are interchangeable. Every "wait" goes through
:mod:`locum.wait` with the desktop's injectable ``sleep``/``clock``, which
keeps tests instant and deterministic.

Waits fail fast on errors that waiting cannot fix. A probe error is retried
only when :func:`locum.errors.is_transient` calls it transient ("not there
yet": :class:`~locum.errors.ElementNotFound`/``WindowNotFound``/``ImageNotFound``,
an inner :class:`~locum.errors.ActionTimeout`, a :class:`~locum.errors.BackendError`,
an ``OSError``, a UI Automation ``COMError`` or another accessibility-bridge
error); the final :class:`~locum.errors.ActionTimeout` names the last such
error. Everything else -- a ``TypeError``/``AttributeError``/``ValueError`` in a
predicate, a ``PermissionError`` or :class:`~locum.errors.PermissionDenied`,
:class:`~locum.errors.BackendUnavailable`, :class:`~locum.errors.NotSupported`,
a fail-safe stop, a policy refusal, an invalid selector, and any exception type
not in the transient set (``RuntimeError``, ``KeyError``...) -- is raised
unchanged on the first probe. This applies to :meth:`Desktop.find`,
:meth:`~Desktop.wait`, :meth:`~Desktop.window`, :meth:`~Desktop.launch`,
:meth:`~Desktop.locate` / :meth:`~Desktop.wait_image`, :meth:`~Desktop.find_text`
and :meth:`Desktop.wait_until`.Earlier development builds retried every exception
until the timeout, so a typo or a missing permission looked like a slow "not
found"; pass ``ignore_exceptions=True`` to :meth:`Desktop.wait_until` for the
old swallow-everything behaviour there.

Creating a ``Desktop`` never touches the display until a method needs it,
except that ``backend=None`` resolves the process-wide default backend.
"""

from __future__ import annotations

import contextlib
import difflib
import math
import os
import pathlib
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Iterator, List, Optional, Sequence, Tuple, Union

from . import failsafe as _failsafe_mod
from .backends.base import CAP_ACCESSIBILITY, AccessibleNode, Backend
from .element import ELEMENT_STATES, Element
from .errors import (
    FATAL_ERRORS,
    ActionTimeout,
    BackendError,
    ElementNotFound,
    FailSafeTriggered,
    ImageNotFound,
    WindowNotFound,
    is_transient,
)
from .failsafe import FailSafe
from .keyboard import DEFAULT_CLIPBOARD_SETTLE, DEFAULT_CLIPBOARD_TIMEOUT, TYPING_MODES, Keyboard
from .motion import Humanize
from .mouse import Mouse
from .screen import Screen
from .types import Match, Monitor, Point, Rect, WindowInfo, coerce_point
from .wait import wait_until as _wait_until
from .window import Window, match_window

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

__all__ = ["Desktop", "IMAGE_EXTENSIONS"]

#: File extensions that make a ``str`` target be treated as a template image path.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")

_XY_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")

# Errors that must never be swallowed by a polling loop (kept for backward
# compatibility; the full rule, including unknown exception types, is
# :func:`locum.errors.is_transient`).
_FATAL = FATAL_ERRORS

# Quoted strings and bare ``[name=Value]`` values inside a selector (for hints).
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'')
_BARE_ATTR_RE = re.compile(r"\[\s*(?:name|title|text|aaname|value)\s*[!*^$~%]?=\s*([^\]\s\"']+)", re.I)
_LEAD_ROLE_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)")


def _describe_image(image: Any) -> str:
    if isinstance(image, (str, pathlib.PurePath)):
        return repr(str(image))
    size = getattr(image, "size", None)
    if isinstance(size, tuple) and len(size) == 2:
        return f"<image {size[0]}x{size[1]}>"
    shape = getattr(image, "shape", None)
    if shape is not None:
        return f"<array {'x'.join(str(s) for s in shape)}>"
    return f"<{type(image).__name__}>"


class Desktop:
    """High-level desktop automation facade.

    Args:
        backend: A :class:`Backend` instance, a registered backend name
            (``"mock"``, ``"x11"``, ``"windows"``, ``"macos"``) or ``None`` for
            the process-wide default backend.
        failsafe: Enable the screen-corner fail-safe (the global abort flag is
            always honoured). A :class:`FailSafe` instance may be passed instead.
        pause: Seconds to sleep after every mouse/keyboard action (PyAutoGUI's ``PAUSE``).
        humanize: ``True`` (default human-like motion), a :class:`Humanize`
            configuration, or ``False`` for mechanical/instant moves.
        default_timeout: Seconds that :meth:`find`, :meth:`window`,
            :meth:`wait_image`... wait when no ``timeout`` is given.
        typing_mode: Default :meth:`Keyboard.type` mode (``auto``, ``unicode``, ``keys``, ``clipboard``).
        sleep: Sleep function used for every wait/pause (inject ``lambda s: None`` in tests).
        clock: Monotonic clock used by the polling helpers.
        poll_interval: Seconds between polls in :meth:`find`, :meth:`window`, :meth:`locate`...
        clipboard_settle: Seconds clipboard-mode typing waits after a paste before
            restoring the clipboard, when the backend cannot tell whether the target
            app has read it (raise it for slow apps or remote sessions); see :class:`Keyboard`.
        clipboard_timeout: Seconds to wait for the target app to read a paste on
            backends that can tell (X11 native clipboard); see :class:`Keyboard`.
        heal_selectors: Self-repairing selectors (see :mod:`locum.heal`): when
            :meth:`find` / :meth:`wait` match nothing within the timeout, use the
            clear best look-alike (similar name, same role, same automation id,
            near the previous position) instead of failing, and log what changed
            on the ``locum.heal`` logger. ``False`` (the default: acting on the
            wrong element is dangerous), ``True``, or a :class:`~locum.heal.HealOptions`.
            Per call, ``find(..., heal=True/False)`` overrides it.

    Attributes:
        backend, mouse, keyboard, screen, failsafe, default_timeout, humanize, typing_mode, heal_selectors.
    """

    def __init__(
        self,
        backend: Union[Backend, str, None] = None,
        *,
        failsafe: Union[bool, FailSafe] = True,
        pause: float = 0.0,
        humanize: Union[bool, Humanize, None] = False,
        default_timeout: float = 10.0,
        typing_mode: str = "auto",
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.1,
        clipboard_settle: float = DEFAULT_CLIPBOARD_SETTLE,
        clipboard_timeout: float = DEFAULT_CLIPBOARD_TIMEOUT,
        heal_selectors: Any = False,
    ):
        from .backends import create_backend, get_backend

        if isinstance(backend, str):
            self.backend: Backend = create_backend(backend)
            self._owns_backend = True
        elif backend is None:
            self.backend = get_backend()
            self._owns_backend = False
        else:
            self.backend = backend
            self._owns_backend = False

        try:
            if default_timeout is None or not math.isfinite(float(default_timeout)) or float(default_timeout) < 0:
                raise ValueError(f"default_timeout must be a finite number >= 0, got {default_timeout!r}")
            mode = (typing_mode or "auto").lower()
            if mode not in TYPING_MODES:
                raise ValueError(f"typing_mode must be one of {', '.join(TYPING_MODES)}, got {typing_mode!r}")
            if poll_interval is None or not math.isfinite(float(poll_interval)) or float(poll_interval) < 0:
                raise ValueError(f"poll_interval must be a finite number >= 0, got {poll_interval!r}")

            from .heal import HealOptions

            if not isinstance(heal_selectors, (bool, HealOptions)):
                raise TypeError(f"heal_selectors must be a bool or a HealOptions, not {type(heal_selectors).__name__}")
            #: Heal non-matching selectors in :meth:`find`/:meth:`wait` (``False``, ``True`` or a HealOptions).
            self.heal_selectors = heal_selectors
            self.default_timeout = float(default_timeout)
            self.typing_mode = mode
            self.poll_interval = float(poll_interval)
            self._sleep_fn = sleep
            self._clock = clock
            self._closed = False

            if humanize is True:
                self.humanize: Optional[Humanize] = Humanize()
            elif humanize is False or humanize is None:
                self.humanize = None
            elif isinstance(humanize, Humanize):
                self.humanize = humanize
            else:
                raise TypeError(f"humanize must be a bool or a Humanize, not {type(humanize).__name__}")

            if isinstance(failsafe, FailSafe):
                self.failsafe = failsafe
            else:
                self.failsafe = FailSafe(self.backend, enabled=bool(failsafe))

            self._pause = max(0.0, float(pause))
            self.mouse = Mouse(self.backend, self.failsafe, self._pause, self.humanize, sleep=sleep, clock=clock)
            self.keyboard = Keyboard(self.backend, self.failsafe, self._pause, sleep=sleep,
                                     clipboard_settle=clipboard_settle, clipboard_timeout=clipboard_timeout)
            self.screen = Screen(self.backend, sleep=sleep, clock=clock)
        except BaseException:
            # A bad option must not leak the backend this constructor created (and owns).
            if self._owns_backend:
                try:
                    self.backend.close()
                except Exception:
                    pass
            raise

    # ================================================================ plumbing
    @property
    def pause(self) -> float:
        """Seconds slept after every mouse/keyboard action."""
        return self._pause

    @pause.setter
    def pause(self, value: float) -> None:
        self._pause = max(0.0, float(value))
        self.mouse.pause = self._pause
        self.keyboard.pause = self._pause

    @property
    def capabilities(self) -> frozenset:
        """The backend's capability flags (see :mod:`locum.backends.base`)."""
        return self.backend.capabilities

    def has(self, capability: str) -> bool:
        """Whether the backend supports ``capability``."""
        return self.backend.has(capability)

    def sleep(self, seconds: float) -> None:
        """Sleep ``seconds`` (via the injected sleep function), honouring the global abort flag.

        An :func:`~locum.failsafe.abort` (kill hotkey) during the sleep raises
        :class:`FailSafeTriggered` at once (within ~0.1 s), not after the whole wait.
        """
        self._check_abort()
        seconds = float(seconds)
        if seconds > 0:
            _failsafe_mod.abortable_sleep(seconds, self._sleep_fn)
        self._check_abort()

    @staticmethod
    def _check_abort() -> None:
        if _failsafe_mod.is_aborted():
            reason = _failsafe_mod.abort_reason()
            raise FailSafeTriggered("Automation aborted" + (f": {reason}" if reason else ""))

    def _timeout(self, timeout: Optional[float]) -> float:
        t = self.default_timeout if timeout is None else float(timeout)
        if not math.isfinite(t) or t < 0:
            raise ValueError(f"timeout must be a finite number >= 0, got {timeout!r}")
        return t

    def wait_until(self, predicate: Callable[[], Any], timeout: Optional[float] = None, interval: float = 0.1, **kw: Any) -> Any:
        """Poll ``predicate`` until truthy (see :func:`locum.wait.wait_until`); ``timeout=None`` = :attr:`default_timeout`.

        Returns the first truthy value; raises :class:`ActionTimeout`. Errors raised
        by ``predicate`` are retried only when transient (``ignore_exceptions``
        defaults to ``"transient"``, see :func:`locum.errors.is_transient`); pass
        ``ignore_exceptions=True`` to swallow every :class:`Exception`.
        """
        kw.setdefault("ignore_exceptions", "transient")
        kw.setdefault("sleep", self.sleep)
        kw.setdefault("clock", self._clock)
        # interval=0 would busy-spin (and never advance an injected clock), as for poll_interval=0.
        return _wait_until(predicate, self._timeout(timeout), interval or 0.05, **kw)

    #: Fallback poll interval of event-driven waits: accessibility change events wake
    #: them at once, so the tree is re-searched slowly while nothing happens.
    EVENT_POLL_INTERVAL = 0.5

    def _event_wait(self, predicate: Callable[[], Any], interval: float) -> Tuple[Callable[[], Any], Callable[[float], Any], float]:
        """``(predicate, sleep, interval)`` for an accessibility-tree wait.

        When the backend has change events (UI Automation on Windows) and this desktop
        uses the real clock, the wait sleeps until the next change event (or the slow
        fallback interval) instead of re-searching the tree every ``interval``. Under an
        event storm it never re-checks more often than ``interval``. Otherwise the
        arguments come back unchanged.
        """
        if self._sleep_fn is not time.sleep or self._clock is not time.monotonic:
            return predicate, self.sleep, interval  # injected clock/sleep (tests): plain polling
        try:
            tracker = self.backend.change_tracker()
        except Exception:
            tracker = None
        if tracker is None:
            return predicate, self.sleep, interval
        seen = [tracker.generation]

        def checked() -> Any:
            seen[0] = tracker.generation
            return predicate()

        def sleep(seconds: float) -> None:
            start = time.monotonic()
            end = start + max(0.0, float(seconds))
            while True:
                self._check_abort()
                now = time.monotonic()
                if now >= end:
                    break
                if tracker.wait(seen[0], min(0.1, end - now)) > seen[0]:
                    # Something changed: re-check, but not more often than the normal interval.
                    rest = start + min(float(interval), float(seconds)) - time.monotonic()
                    if rest > 0:
                        _failsafe_mod.abortable_sleep(rest, self._sleep_fn)
                    break
            self._check_abort()

        return checked, sleep, max(float(interval), self.EVENT_POLL_INTERVAL)

    def _poll(
        self,
        probe: Callable[[], Any],
        timeout: float,
        message: str,
        fatal: Tuple[type, ...] = (),
        *,
        events: bool = False,
    ) -> Any:
        """Poll ``probe`` until truthy; transient errors count as "not yet", fatal ones propagate.

        An error is retried only if :func:`locum.errors.is_transient` says so;
        any other error is raised unchanged on the first probe. ``fatal`` adds
        exception types that must be raised at once for this probe even though
        they would otherwise be transient.
        """
        last: List[BaseException] = []
        fatal_extra = tuple(fatal)

        def guarded() -> Any:
            try:
                return probe()
            except Exception as exc:
                if isinstance(exc, fatal_extra) or not is_transient(exc):
                    raise
                last[:] = [exc]  # transient backend / stale tree errors
                return None

        # poll_interval=0 would busy-spin (and never advance an injected clock).
        pred, sleep, interval = guarded, self.sleep, self.poll_interval or 0.05
        if events:  # an accessibility-tree probe: wake on change events where available
            pred, sleep, interval = self._event_wait(guarded, interval)
        try:
            return _wait_until(pred, timeout, interval, message=message, ignore_exceptions=False,
                               sleep=sleep, clock=self._clock)
        except ActionTimeout:
            if last:
                raise ActionTimeout(f"{message}; last error: {type(last[0]).__name__}: {last[0]}") from last[0]
            raise

    # ============================================================ resolution
    def _is_image(self, target: Any) -> bool:
        if isinstance(target, (pathlib.PurePath, bytes, bytearray, memoryview)):
            return True
        if isinstance(target, str):
            low = target.strip().lower()
            if low.startswith("data:image/"):
                return True
            return low.endswith(IMAGE_EXTENSIONS) and os.path.isfile(os.path.expanduser(target.strip()))
        mod = type(target).__module__ or ""
        if mod.startswith("PIL."):
            return True
        shape = getattr(target, "shape", None)
        return mod.startswith("numpy") and shape is not None and len(shape) >= 2

    @staticmethod
    def _check_image_path(target: str) -> None:
        """Raise :class:`FileNotFoundError` for a string that names a missing image file.

        Strings ending in an image extension are meant as image paths; without
        this check a typo'd or cwd-relative path would be parsed as a selector
        (``ok.png`` = role ``ok`` with class ``png``) and fail confusingly.
        """
        text = target.strip()
        if text.lower().endswith(IMAGE_EXTENSIONS) and not text.lower().startswith("data:image/"):
            path = os.path.abspath(os.path.expanduser(text))
            if not os.path.isfile(path):
                raise FileNotFoundError(f"Image file {text!r} not found (resolved to {path!r}, cwd {os.getcwd()!r})")

    def resolve_point(self, target: Any = None, y: Optional[float] = None) -> Point:
        """Turn any "where" into a screen :class:`Point`.

        Accepted targets:

        * ``None`` -- the current cursor position (``y`` given: current x, that y);
        * numbers ``(x, y)`` -- ``resolve_point(10, 20)``; a lone ``x`` keeps the current y;
        * point-likes -- ``Point``, ``(x, y)``, ``{"x":..,"y":..}``, ``"x,y"`` strings;
        * :class:`Rect` / :class:`Match` / :class:`Window` -- their center;
        * :class:`Element` -- its center (raises :class:`ElementNotFound` if it has no bounds);
        * other ``str`` -- a **selector** (:meth:`find` with the default timeout), or a template
          image path if it names an existing image file;
        * ``pathlib.Path`` / PIL image / numpy array / bytes / data URL -- template-matched on
          screen (waits up to :attr:`default_timeout`, raises :class:`ImageNotFound`).
        """
        if target is None:
            cur = self.mouse.position()
            return cur if y is None else Point(cur.x, int(round(y)))
        if isinstance(target, bool):
            raise TypeError("A bool is not a screen position")
        if isinstance(target, (int, float)):
            yy = self.mouse.position().y if y is None else int(round(y))
            return Point(int(round(target)), yy)
        if y is not None:
            raise TypeError(f"y must be None when target is a {type(target).__name__}")
        if isinstance(target, Element):
            return target.center
        if isinstance(target, AccessibleNode):
            return Element(target, self).center
        if isinstance(target, Window):
            # Current rect, not the snapshot (raises WindowNotFound if it closed).
            return target.refresh().center
        if isinstance(target, (Point, Rect, Match, dict)):
            return coerce_point(target)
        if isinstance(target, str):
            m = _XY_RE.match(target)
            if m:
                return Point(int(round(float(m.group(1)))), int(round(float(m.group(2)))))
            if self._is_image(target):
                return self.wait_image(target).center
            self._check_image_path(target)
            return self.find(target).center
        if self._is_image(target):
            return self.wait_image(target).center
        return coerce_point(target)

    def _resolve_element(self, target: Any) -> Optional[Element]:
        """The :class:`Element` a click target designates, if any (selector strings are looked up)."""
        if isinstance(target, Element):
            return target
        if isinstance(target, AccessibleNode):
            return Element(target, self)
        if isinstance(target, str) and not _XY_RE.match(target) and not self._is_image(target):
            self._check_image_path(target)
            return self.find(target)
        return None

    def _check_on_screen(self, point: Point) -> None:
        """Raise ``ValueError`` if ``point`` lies on no monitor.

        The OS would clamp it to the nearest screen edge and the action would
        land on whatever is there (a scrollbar, a close button), e.g. a script
        recorded on two monitors replayed over a one-monitor RDP session.
        """
        try:
            rects = [m.rect for m in self.screen.monitors()] or [self.screen.virtual_rect()]
        except Exception:
            return  # cannot tell; let the backend decide
        if not any(r.contains(point) for r in rects):
            layout = ", ".join(f"({r.left}, {r.top}, {r.width}x{r.height})" for r in rects)
            raise ValueError(f"Point ({point.x}, {point.y}) is off-screen (monitors: {layout}); "
                             "pass allow_offscreen=True to send it anyway")

    def _action_point(self, target: Any, y: Optional[float] = None, offset: Tuple[int, int] = (0, 0),
                      *, allow_offscreen: bool = False) -> Point:
        """Where a mouse action on ``target`` must go.

        Elements (and selectors) are hit-tested like :meth:`Element.click`: a
        covered element's window is brought to the front, and
        :class:`ElementNotFound` is raised if it stays covered, hidden or
        off-screen. Other targets resolve through :meth:`resolve_point` and must
        lie on a monitor unless ``allow_offscreen``.
        """
        dx, dy = int(offset[0]), int(offset[1])
        if y is None:
            el = self._resolve_element(target)
            if el is not None:
                el._guard()
                return el._mouse_point((dx, dy))
        point = self.resolve_point(target, y).offset(dx, dy)
        if not allow_offscreen:
            self._check_on_screen(point)
        return point

    def _image_target(self, target: Any, use: str) -> bool:
        """Whether ``target`` (given to an element lookup) is a template image.

        A string ending in an image extension whose file is missing raises
        :class:`FileNotFoundError`; with ``use`` empty, an image raises
        ``TypeError`` (the lookup returns elements only).
        """
        if self._is_image(target):
            if not use:
                raise TypeError(f"{_describe_image(target)} is an image, not a selector; "
                                "use locate()/wait_image()/locate_all() to find images")
            return True
        if isinstance(target, str):
            self._check_image_path(target)
        return False

    def _region(self, region: Any) -> Optional[Rect]:
        """Coerce a region argument: Rect-like, :class:`Element` (bounds), :class:`Window` or :class:`Match`."""
        if region is None:
            return None
        if isinstance(region, Element):
            b = region.bounds
            if b is None:
                raise ElementNotFound(f"{region!r} has no screen bounds to use as a region")
            return b
        if isinstance(region, Window):
            # Current rect, not the snapshot (raises WindowNotFound if it closed),
            # matching Window.screenshot().
            return region.refresh().rect
        if isinstance(region, Match):
            return region.rect
        if isinstance(region, WindowInfo):
            return region.rect
        return Rect.coerce(region)

    # ================================================================== mouse
    def position(self) -> Point:
        """Current cursor position."""
        return self.mouse.position()

    def move(
        self,
        target: Any,
        y: Optional[float] = None,
        *,
        duration: Optional[float] = None,
        humanize: Union[bool, Humanize, None] = None,
        tween: Any = "ease_in_out_quad",
        offset: Tuple[int, int] = (0, 0),
        allow_offscreen: bool = False,
    ) -> Point:
        """Move the cursor to ``target`` (see :meth:`resolve_point`) + ``offset``. Returns the destination.

        Elements and selectors are hit-tested as in :meth:`click` (never hover
        whatever covers them); a point on no monitor raises ``ValueError``
        unless ``allow_offscreen``.
        """
        point = self._action_point(target, y, offset, allow_offscreen=allow_offscreen)
        return self.mouse.move(point, duration=duration, tween=tween, humanize=humanize)

    def move_rel(self, dx: float, dy: float, **kw: Any) -> Point:
        """Move relative to the current position (keyword arguments as in :meth:`move`)."""
        cur = self.position()
        return self.move(Point(int(round(cur.x + dx)), int(round(cur.y + dy))), **kw)

    def click(
        self,
        target: Any = None,
        y: Optional[float] = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.0,
        offset: Tuple[int, int] = (0, 0),
        duration: Optional[float] = None,
        humanize: Union[bool, Humanize, None] = None,
        allow_offscreen: bool = False,
    ) -> Point:
        """Click ``target`` (see :meth:`resolve_point`; ``None`` = where the cursor is).

        When the target is an element (or a selector string) and no timing
        options are given, :meth:`Element.click` is used, which falls back to the
        native accessibility *invoke* when the element is covered or off-screen.
        A point on no monitor raises ``ValueError`` (the OS would click the
        clamped screen edge) unless ``allow_offscreen``.

        Returns the clicked point (the element's center for native invokes).
        """
        dx, dy = int(offset[0]), int(offset[1])
        if y is None:
            el = self._resolve_element(target)
            if el is not None:
                if duration is None and not interval and humanize is None:
                    el.click(button, clicks, offset=(dx, dy), native="auto")
                    clicked = getattr(el, "_last_click_point", None)
                    if clicked is not None:
                        return clicked  # the visible-part center, for a partly off-screen element
                    b = el.bounds
                    return b.center.offset(dx, dy) if b is not None else self.position()
                # Same hit-test as Element.click: never click whatever covers the element.
                el._guard()
                el._wait_enabled()
                point = el._mouse_point((dx, dy))
                return self.mouse.click(point, button=button, clicks=clicks, interval=interval,
                                        duration=duration, humanize=humanize)
        if target is None and y is None and (dx, dy) == (0, 0):
            return self.mouse.click(None, button=button, clicks=clicks, interval=interval, humanize=humanize)
        point = self.resolve_point(target, y).offset(dx, dy)
        if not allow_offscreen:
            self._check_on_screen(point)
        return self.mouse.click(point, button=button, clicks=clicks, interval=interval, duration=duration, humanize=humanize)

    def double_click(self, target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
        """Double-click ``target`` (same options as :meth:`click`)."""
        kw["clicks"] = 2
        return self.click(target, y, **kw)

    def triple_click(self, target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
        """Triple-click ``target`` (selects a line/paragraph in most apps)."""
        kw["clicks"] = 3
        return self.click(target, y, **kw)

    def right_click(self, target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
        """Right-click ``target``."""
        kw["button"] = "right"
        return self.click(target, y, **kw)

    def middle_click(self, target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
        """Middle-click ``target``."""
        kw["button"] = "middle"
        return self.click(target, y, **kw)

    def mouse_down(self, button: str = "left") -> None:
        """Press and hold a mouse button at the current position."""
        self.mouse.down(button)

    def mouse_up(self, button: str = "left") -> None:
        """Release a mouse button."""
        self.mouse.up(button)

    def drag(
        self,
        start: Any,
        end: Any,
        *,
        duration: float = 0.5,
        button: str = "left",
        humanize: Union[bool, Humanize, None] = None,
        hold: float = 0.05,
        allow_offscreen: bool = False,
    ) -> Point:
        """Drag from ``start`` (``None`` = current position) to ``end``; both accept any target.

        Element/selector ends are hit-tested as in :meth:`click`: the press and
        the drop never land on a window that covers them (:class:`ElementNotFound`
        instead). Points on no monitor raise ``ValueError`` unless ``allow_offscreen``.
        """
        start_el = None if start is None else self._resolve_element(start)
        s = None if start is None else self._action_point(start_el or start, allow_offscreen=allow_offscreen)
        e = self._action_point(end, allow_offscreen=allow_offscreen)
        # Bringing the drop target's window forward may have covered the start.
        if start_el is not None and s is not None and start_el._occluded(s):
            raise ElementNotFound(f"The drag start {start_el!r} is covered once the drop target is in front; "
                                  "refusing to drag from the wrong thing")
        return self.mouse.drag(s, e, button=button, duration=duration, humanize=humanize, hold=hold)

    def scroll(self, dy: int = 0, dx: int = 0, *, at: Any = None, allow_offscreen: bool = False) -> None:
        """Scroll wheel notches (``dy > 0`` = up, ``dx > 0`` = right), optionally over ``at``.

        An element/selector ``at`` is hit-tested as in :meth:`click` (never scroll
        a window that covers it); a point on no monitor raises ``ValueError``
        unless ``allow_offscreen``.
        """
        point = None if at is None else self._action_point(at, allow_offscreen=allow_offscreen)
        self.mouse.scroll(dy, dx, at=point)

    # =============================================================== keyboard
    def type(self, text: str, *, interval: float = 0.0, mode: Optional[str] = None, verify: bool = True) -> None:
        """Type ``text`` into the focused control (``mode=None`` = :attr:`typing_mode`).

        With ``verify=True`` (default) and an accessibility backend, when the
        control with keyboard focus is a readable, writable text field it is
        read before and after typing, exactly like :meth:`Element.type`: some
        applications drop or garble synthesized keystrokes while busy (seen with
        Windows 11 Notepad right after launch), and an unambiguous result is
        repaired. Anything else (no focused element, not a text field,
        password fields, text with Tab/Enter key presses) is typed unverified,
        as before. ``verify=False`` always types raw keystrokes.
        """
        mode = mode or self.typing_mode
        el: Optional[Element] = None
        before: Optional[str] = None
        if verify and text and self.has(CAP_ACCESSIBILITY):
            try:
                el = self.focused()
                before = el._verify_snapshot(text, mode) if el is not None else None
            except Exception:
                el, before = None, None
        self.keyboard.type(text, interval=interval, mode=mode)
        if el is not None and before is not None:
            el._verify_typed(before, text)

    def press(self, *keys: Any, presses: int = 1, interval: float = 0.0) -> None:
        """Tap keys in sequence: ``press("tab", "tab", "enter")``; combos like ``"ctrl+a"`` act as hotkeys."""
        self.keyboard.press(*keys, presses=presses, interval=interval)

    def hotkey(self, *keys: Any, interval: float = 0.0) -> None:
        """Press a key combination: ``hotkey("ctrl+s")`` / ``hotkey("ctrl", "s")`` (``mod`` = ctrl/cmd)."""
        self.keyboard.hotkey(*keys, interval=interval)

    def key_down(self, key: str) -> None:
        """Press and hold ``key``."""
        self.keyboard.down(key)

    def key_up(self, key: str) -> None:
        """Release ``key``."""
        self.keyboard.up(key)

    @contextlib.contextmanager
    def hold(self, *keys: Any) -> Iterator[List[str]]:
        """Hold keys for the duration of a ``with`` block: ``with d.hold("shift"): d.click(...)``."""
        with self.keyboard.hold(*keys) as held:
            yield held

    # ========================================================= screen/vision
    def screenshot(self, region: Any = None, *, monitor: Optional[int] = None, path: Optional[Union[str, "os.PathLike[str]"]] = None) -> "Image":
        """Capture the screen (or ``region``: rect-like, Element, Window or Match), optionally saving it to ``path``."""
        img = self.screen.capture(self._region(region), monitor=monitor)
        if path is not None:
            p = pathlib.Path(path).expanduser()
            if not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix:
                img.save(str(p))
            else:
                img.save(str(p), format="PNG")
        return img

    def monitors(self) -> List[Monitor]:
        """All monitors (index 0 is the primary)."""
        return self.screen.monitors()

    def size(self) -> Tuple[int, int]:
        """``(width, height)`` of the primary monitor."""
        return self.screen.size()

    def virtual_rect(self) -> Rect:
        """Bounding rect of every monitor."""
        return self.screen.virtual_rect()

    def on_screen(self, x: Any, y: Optional[float] = None) -> bool:
        """Whether a point lies on some monitor."""
        p = coerce_point(x) if y is None else Point(int(round(x)), int(round(y)))
        return self.screen.on_screen(p.x, p.y)

    def pixel(self, x: Any, y: Optional[float] = None) -> Tuple[int, int, int]:
        """RGB color at a screen point."""
        p = coerce_point(x) if y is None else Point(int(round(x)), int(round(y)))
        return self.screen.pixel(p.x, p.y)

    def pixel_matches(self, x: int, y: int, color: Any, tolerance: int = 0) -> bool:
        """Whether the pixel at ``(x, y)`` is within ``tolerance`` of ``color``."""
        return self.screen.pixel_matches(x, y, color, tolerance)

    @staticmethod
    def _load_needle(image: Any) -> Any:
        from . import vision

        if isinstance(image, str):
            image = os.path.expanduser(image)
        return vision.load_image(image)

    def locate(
        self,
        image: Any,
        *,
        region: Any = None,
        confidence: float = 0.9,
        grayscale: bool = False,
        scales: Sequence[float] = (1.0,),
        timeout: float = 0.0,
    ) -> Optional[Match]:
        """Find ``image`` on screen (template matching; alpha = mask). Polls up to ``timeout`` seconds.

        Returns the best :class:`Match` in screen coordinates or ``None``.

        A fatal capture error (:class:`~locum.errors.PermissionDenied`, e.g. macOS
        Screen Recording not granted; :class:`~locum.errors.BackendUnavailable`;
        :class:`~locum.errors.NotSupported`; invalid arguments) is raised on the
        first attempt. A transient one (:class:`~locum.errors.BackendError`,
        ``OSError``: a locked workstation, a disconnected RDP session) is retried
        and, if the screen still cannot be captured when ``timeout`` expires, raised
        instead of returning ``None``.
        """
        needle = self._load_needle(image)
        rect = self._region(region)
        t = float(timeout)

        failed: List[BaseException] = []  # error of the latest capture, if it failed

        def probe() -> Optional[Match]:
            try:
                m = self.screen.locate(needle, region=rect, confidence=confidence, grayscale=grayscale, scales=tuple(scales))
            except Exception as exc:
                failed[:] = [exc]
                raise
            failed.clear()
            return m

        try:
            # Fatal errors (argument validation: transparent needle, scales <= 0, bad
            # confidence...; missing permission; no backend) escape _poll at once.
            return self._poll(probe, t, f"Image {_describe_image(image)} not found")
        except ActionTimeout:
            if failed:
                # The screen could not be captured (locked workstation, RDP session
                # disconnected...): that is not "not found", so report the real cause.
                raise failed[0] from None
            return None

    def locate_all(self, image: Any, **kw: Any) -> List[Match]:
        """All occurrences of ``image`` on screen (``region``, ``confidence``, ``grayscale``,
        ``scales``, ``limit``, ``overlap`` as in :func:`locum.vision.locate_all`), best first."""
        needle = self._load_needle(image)
        if "region" in kw:
            kw["region"] = self._region(kw["region"])
        return self.screen.locate_all(needle, **kw)

    def wait_image(self, image: Any, *, timeout: Optional[float] = None, **kw: Any) -> Match:
        """Wait until ``image`` appears (``timeout=None`` = :attr:`default_timeout`). Raises :class:`ImageNotFound`."""
        t = self._timeout(timeout)
        m = self.locate(image, timeout=t, **kw)
        if m is None:
            conf = kw.get("confidence", 0.9)
            raise ImageNotFound(f"Image {_describe_image(image)} not found on screen within {t:g}s (confidence >= {conf})")
        return m

    def click_image(
        self,
        image: Any,
        *,
        timeout: Optional[float] = None,
        button: str = "left",
        clicks: int = 1,
        offset: Tuple[int, int] = (0, 0),
        **kw: Any,
    ) -> Match:
        """Wait for ``image`` and click its center (+``offset``). Returns the :class:`Match`."""
        m = self.wait_image(image, timeout=timeout, **kw)
        self.mouse.click(m.center.offset(int(offset[0]), int(offset[1])), button=button, clicks=clicks)
        return m

    def find_text(self, text: str, *, region: Any = None, timeout: float = 0.0, **kw: Any) -> Optional[Match]:
        """OCR the screen (or ``region``) for ``text``; polls up to ``timeout``. Returns the first match or ``None``.

        Keyword arguments (``engine``, ``regex``, ``case_sensitive``, ``fuzzy``) go to
        :func:`locum.ocr.find_text`. Raises :class:`NotSupported` if no OCR engine is installed.
        """
        rect = self._region(region)

        def probe() -> Optional[Match]:
            found = self.screen.find_text(text, region=rect, **kw)
            return found[0] if found else None

        try:
            return self._poll(probe, float(timeout), f"Text {text!r} not found")
        except ActionTimeout:
            return None

    def click_text(
        self,
        text: str,
        *,
        timeout: Optional[float] = None,
        button: str = "left",
        clicks: int = 1,
        offset: Tuple[int, int] = (0, 0),
        **kw: Any,
    ) -> Match:
        """Wait for ``text`` to appear on screen (OCR) and click it. Raises :class:`ImageNotFound`."""
        t = self._timeout(timeout)
        m = self.find_text(text, timeout=t, **kw)
        if m is None:
            raise ImageNotFound(f"Text {text!r} not found on screen within {t:g}s")
        self.mouse.click(m.center.offset(int(offset[0]), int(offset[1])), button=button, clicks=clicks)
        return m

    def read_text(self, region: Any = None, *, engine: Any = None) -> str:
        """OCR the screen (or ``region``) and return its text."""
        return self.screen.read_text(self._region(region), engine=engine)

    def wait_for_change(self, region: Any = None, **kw: Any) -> bool:
        """Wait until ``region`` changes (see :meth:`Screen.wait_for_change`)."""
        kw.setdefault("sleep", self.sleep)
        kw.setdefault("clock", self._clock)
        return self.screen.wait_for_change(self._region(region), **kw)

    def wait_until_stable(self, region: Any = None, **kw: Any) -> bool:
        """Wait until ``region`` stops changing (see :meth:`Screen.wait_until_stable`)."""
        kw.setdefault("sleep", self.sleep)
        kw.setdefault("clock", self._clock)
        return self.screen.wait_until_stable(self._region(region), **kw)

    # ================================================================ elements
    def roots(self) -> List[AccessibleNode]:
        """Top-level accessibility nodes (one per window/application)."""
        return list(self.backend.accessibility_roots())

    @staticmethod
    def _selector_text(selector: Any, parsed: Any) -> str:
        return selector if isinstance(selector, str) else str(parsed)

    def _scope(self, root: Any) -> Any:
        if root is None or isinstance(root, (Element, Window)):
            return root
        if isinstance(root, AccessibleNode):
            return Element(root, self)
        raise TypeError(f"root must be an Element, Window or AccessibleNode, not {type(root).__name__}")

    def _hint(self, selector_text: str, limit: int = 3, max_nodes: int = 4000) -> str:
        """A cheap "did you mean" hint built from names/roles present in the tree."""
        from .selectors import describe_node

        try:
            roots = self.roots()
        except Exception:
            return ""
        if not roots:
            return " (the accessibility tree is empty: no windows are open)"
        by_name: dict = {}
        roles: dict = {}
        count = 0
        for r in roots:
            for node in r.walk(max_depth=32):
                count += 1
                if count > max_nodes:
                    break
                try:
                    name, role = node.name or "", node.role or ""
                except Exception:
                    continue
                if name and name.lower() not in by_name:
                    by_name[name.lower()] = node
                if role and role not in roles:
                    roles[role] = node
            if count > max_nodes:
                break
        names = list(by_name)

        def named(words: List[str]) -> List[str]:
            out: List[str] = []
            for w in words:
                wl = w.lower()
                close = difflib.get_close_matches(wl, names, n=limit, cutoff=0.6)
                close += [n for n in names if wl and wl in n and n not in close]
                for n in close:
                    try:
                        desc = describe_node(by_name[n], include_bounds=False)
                    except Exception:
                        desc = repr(n)
                    if desc not in out:
                        out.append(desc)
            return out

        wanted = [a or b for a, b in _QUOTED_RE.findall(selector_text) if (a or b)]
        wanted += _BARE_ATTR_RE.findall(selector_text)
        suggestions: List[str] = []
        tip = ""
        if wanted:
            suggestions = named(wanted)
        else:
            m = _LEAD_ROLE_RE.match(selector_text)
            lead = m.group(1).lower() if m else ""
            if lead and lead not in roles:
                from .backends.base import ROLES
                from .selectors import ROLE_ALIASES

                if lead not in ROLES and lead not in ROLE_ALIASES:
                    # A bare word is read as a role: click("Save") looks for role "save".
                    bare = selector_text.strip()
                    suggestions = named([bare] + ([m.group(1)] if m.group(1) != bare else []))
                    if suggestions:
                        tip = (f" (a bare word is a role; write a name in quotes: '\"{m.group(1)}\"' "
                               f"or 'button \"{m.group(1)}\"')")
                if not suggestions:
                    suggestions = [f"role {r!r}" for r in difflib.get_close_matches(lead, list(roles), n=limit, cutoff=0.5)]
        if not suggestions:
            return ""
        return "; did you mean: " + ", ".join(suggestions[:limit]) + tip

    def _heal_options(self) -> Any:
        from .heal import HealOptions

        opts = getattr(self, "heal_selectors", False)
        return opts if isinstance(opts, HealOptions) else None

    def _heal(self, sel: Any, text: str, *, fingerprint: Any, scope: Any = None) -> Any:
        """One heal scan after a lookup timed out (:func:`locum.heal.heal`); returns its outcome."""
        from .heal import HealOutcome, heal

        opts = self._heal_options()
        kw = {"log": False}  # logged by the caller once the element is accepted
        if scope is None:
            roots = self.roots()
            return heal(sel, roots, fingerprint=fingerprint, options=opts, selector_text=text, selector_roots=roots, **kw)
        if isinstance(scope, Element):
            node = scope.node
            return heal(sel, [node], fingerprint=fingerprint, options=opts, selector_text=text,
                        include_roots=False, selector_root=node, **kw)
        node = getattr(scope, "_root_node", lambda: None)()
        if node is None:
            return HealOutcome(reason="the window has no accessibility root")
        return heal(sel, [node], fingerprint=fingerprint, options=opts, selector_text=text, selector_root=node, **kw)

    def find(self, selector: Any, *, timeout: Optional[float] = None, root: Any = None,
             heal: Optional[bool] = None, fingerprint: Any = None) -> Element:
        """Find the first element matching ``selector`` (CSS-like, UiPath XML or dict).

        Polls up to ``timeout`` seconds (``None`` = :attr:`default_timeout`, ``0`` =
        a single check). ``root`` scopes the search to an :class:`Element` or
        :class:`Window`. Raises :class:`ElementNotFound` (with close-match hints)
        or :class:`SelectorSyntaxError` immediately for invalid selectors.

        ``heal`` (default: :attr:`heal_selectors`, off unless enabled) turns on
        self-repair when nothing matches within the timeout: the clear best
        look-alike is returned with ``element.healed`` describing the change
        (see :mod:`locum.heal`). ``fingerprint`` -- a
        :class:`~locum.heal.Fingerprint` or its dict -- is what the element
        looked like before (role, name, automation id, bounds, window).
        """
        from .heal import enabled as _heal_enabled, log_heal
        from .selectors import parse

        self._image_target(selector, "")
        scope = self._scope(root)
        healing = _heal_enabled(heal, getattr(self, "heal_selectors", False))
        if scope is not None:
            if not healing:
                return scope.find(selector, timeout=timeout)
            if isinstance(scope, Element):
                return scope.find(selector, timeout=timeout, heal=True, fingerprint=fingerprint)
            try:
                return scope.find(selector, timeout=timeout)
            except ElementNotFound as exc:
                sel = parse(selector)
                outcome = self._heal(sel, self._selector_text(selector, sel), fingerprint=fingerprint, scope=scope)
                if not outcome:
                    raise ElementNotFound(f"{exc} (self-healing: {outcome.reason})") from exc.__cause__
                el = Element(outcome.node, self, outcome.info.new_selector, scope=scope)
                el.healed = outcome.info
                log_heal(outcome.info)
                return el
        sel = parse(selector)
        t = self._timeout(timeout)
        text = self._selector_text(selector, sel)
        try:
            node = self._poll(lambda: sel.find_first(self.roots()), t, f"No element matching {text!r}", events=True)
        except ActionTimeout as exc:
            if not healing:
                raise ElementNotFound(f"No element matching {text!r} within {t:g}s{self._hint(text)}") from exc
            outcome = self._heal(sel, text, fingerprint=fingerprint)
            if not outcome:
                raise ElementNotFound(f"No element matching {text!r} within {t:g}s{self._hint(text)}"
                                      f" (self-healing: {outcome.reason})") from exc
            el = Element(outcome.node, self, outcome.info.new_selector)
            el.healed = outcome.info
            log_heal(outcome.info)
            return el
        return Element(node, self, text)

    def find_all(self, selector: Any, *, root: Any = None) -> List[Element]:
        """All elements matching ``selector`` right now (no waiting), in document order."""
        from .selectors import parse

        self._image_target(selector, "")
        scope = self._scope(root)
        if scope is not None:
            return scope.find_all(selector)
        sel = parse(selector)
        text = self._selector_text(selector, sel)
        return [Element(n, self, text, index=i) for i, n in enumerate(sel.find_all(self.roots()))]

    def exists(self, selector: Any, *, timeout: float = 0.0, root: Any = None) -> bool:
        """Whether an element matching ``selector`` appears within ``timeout`` seconds.

        ``selector`` may also be a template image (an existing image file, a
        ``pathlib.Path``, bytes, PIL/numpy image or data URL): it is then looked
        for on screen (inside ``root``'s bounds if given), as with :meth:`locate`.
        """
        if self._image_target(selector, "exists"):
            return self.locate(selector, region=root, timeout=timeout) is not None
        try:
            # An existence check never heals: "is the old element still there?" must not
            # turn true because a look-alike exists.
            self.find(selector, timeout=timeout, root=root, heal=False)
            return True
        except ElementNotFound:
            return False

    def wait(self, selector: Any, *, state: Any = "visible", timeout: Optional[float] = None, root: Any = None,
             heal: Optional[bool] = None, fingerprint: Any = None) -> Any:
        """Wait for an element matching ``selector`` to reach ``state``.

        ``state`` is one of :data:`locum.element.ELEMENT_STATES` or a callable
        ``(Element) -> bool``. ``"gone"`` waits until nothing matches and returns
        ``None``. Raises :class:`ElementNotFound` if nothing ever matched, or
        :class:`ActionTimeout` if matches never reached the state.

        For a template image (see :meth:`exists`), ``"visible"``/``"exists"``
        return its :class:`Match` (as :meth:`wait_image`, raising
        :class:`ImageNotFound`) and ``"gone"`` waits until it is no longer on
        screen (:class:`ActionTimeout` otherwise); other states are rejected.

        ``heal``/``fingerprint`` work as in :meth:`find` and apply only when
        nothing ever matched (never to ``"gone"``): the healed element is
        returned if it is in ``state``.
        """
        from .heal import enabled as _heal_enabled, log_heal
        from .selectors import parse

        if self._image_target(selector, "wait"):
            mode = state.lower() if isinstance(state, str) else None
            if mode in ("visible", "exists"):
                return self.wait_image(selector, timeout=timeout, region=root)
            if mode != "gone":
                raise ValueError(f"state {state!r} does not apply to an image; use 'visible', 'exists' or 'gone'")
            t = self._timeout(timeout)
            rect = self._region(root)
            needle = self._load_needle(selector)
            self._poll(lambda: self.screen.locate(needle, region=rect) is None, t,
                       f"Image {_describe_image(selector)} still on screen after {t:g}s")
            return None
        sel = parse(selector)
        t = self._timeout(timeout)
        text = self._selector_text(selector, sel)
        scope = self._scope(root)
        if isinstance(state, str) and state.lower() not in ELEMENT_STATES:
            raise ValueError(f"Unknown state {state!r}; expected one of {', '.join(ELEMENT_STATES)} or a callable")

        def candidates() -> List[Element]:
            if scope is None:
                return self.find_all(sel)
            return scope.find_all(sel)

        label = getattr(state, "__name__", None) or str(state)
        if isinstance(state, str) and state.lower() == "gone":
            self._poll(lambda: not candidates(), t, f"Element {text!r} still present after {t:g}s", events=True)
            return None

        seen = [False]

        def probe() -> Optional[Element]:
            for i, el in enumerate(candidates()):
                seen[0] = True
                if el._state_predicate(state)():
                    # Keep the match's position so refresh()/waits on the result
                    # track *this* element, not the selector's first match.
                    return Element(el.node, self, text, scope=scope, index=i)
            return None

        try:
            return self._poll(probe, t, f"Element {text!r} did not become {label!r} within {t:g}s", events=True)
        except ActionTimeout as exc:
            if not seen[0]:
                msg = f"No element matching {text!r} within {t:g}s{self._hint(text)}"
                if not _heal_enabled(heal, getattr(self, "heal_selectors", False)):
                    raise ElementNotFound(msg) from exc
                outcome = self._heal(sel, text, fingerprint=fingerprint, scope=scope)
                if not outcome:
                    raise ElementNotFound(f"{msg} (self-healing: {outcome.reason})") from exc
                el = Element(outcome.node, self, outcome.info.new_selector, scope=scope)
                if not el._state_predicate(state)():
                    raise ElementNotFound(
                        f"{msg} (self-healing: {outcome.info.new_selector!r} is not {label!r})") from exc
                el.healed = outcome.info
                log_heal(outcome.info)
                return el
            raise

    def element_at(self, x: Any, y: Optional[float] = None) -> Optional[Element]:
        """The deepest element at a screen point, or ``None``."""
        p = coerce_point(x) if y is None else Point(int(round(x)), int(round(y)))
        node = self.backend.element_at(p.x, p.y)
        return None if node is None else Element(node, self)

    def focused(self) -> Optional[Element]:
        """The element with keyboard focus, or ``None``."""
        node = self.backend.focused_element()
        return None if node is None else Element(node, self)

    def tree(self, *, window: Any = None, max_depth: int = 8, interactive_only: bool = False, max_nodes: int = 2000) -> str:
        """Readable dump of the accessibility tree (whole desktop, or one window / element)."""
        from .selectors import tree_text

        if window is None:
            roots: List[AccessibleNode] = self.roots()
        elif isinstance(window, Element):
            roots = [window.node]
        else:
            w = window if isinstance(window, Window) else self.window(window, timeout=0)
            roots = [w.element.node]
        return tree_text(roots, max_depth=max_depth, interactive_only=interactive_only, max_nodes=max_nodes)

    # ================================================================= windows
    def windows(self, title: Any = None, *, app: Any = None, class_name: Any = None, pid: Optional[int] = None) -> List[Window]:
        """Top-level windows (front-most first) matching every given criterion (see :func:`match_window`)."""
        return [
            Window(info, self)
            for info in self.backend.list_windows()
            if match_window(info, title, app, class_name, pid)
        ]

    @staticmethod
    def _rank(window: Window, title: Any) -> int:
        if isinstance(title, str):
            if window.title == title:
                return 0
            if window.title.lower() == title.lower():
                return 1
        return 2

    def window(
        self,
        title: Any = None,
        *,
        app: Any = None,
        class_name: Any = None,
        pid: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Window:
        """The best window matching the query, waiting up to ``timeout`` (``None`` = :attr:`default_timeout`).

        Exact title matches win over case-insensitive/substring/glob matches;
        ties keep the Z-order (front-most first). Without criteria returns the
        active window (or the front-most one). Raises :class:`WindowNotFound`.
        """
        t = self._timeout(timeout)
        no_criteria = title is None and app is None and class_name is None and pid is None

        def probe() -> Optional[Window]:
            found = self.windows(title, app=app, class_name=class_name, pid=pid)
            if not found:
                return None
            if no_criteria:
                active = [w for w in found if w.is_active]
                return active[0] if active else found[0]
            return sorted(found, key=lambda w: self._rank(w, title))[0]

        try:
            return self._poll(probe, t, "window not found", events=True)
        except ActionTimeout as exc:
            crit = ", ".join(
                f"{k}={v!r}" for k, v in (("title", title), ("app", app), ("class_name", class_name), ("pid", pid)) if v is not None
            ) or "any"
            try:
                titles = [w.title for w in self.backend.list_windows()]
            except Exception:
                titles = []
            listing = ", ".join(repr(x) for x in titles[:15]) if titles else "none"
            raise WindowNotFound(f"No window matching {crit} within {t:g}s. Open windows: {listing}") from exc

    def active_window(self) -> Optional[Window]:
        """The foreground window, or ``None``."""
        info = self.backend.active_window()
        return None if info is None else Window(info, self)

    def launch(
        self,
        command: Any,
        *,
        wait_window: Any = None,
        timeout: Optional[float] = None,
        new_window: bool = True,
        **kw: Any,
    ) -> Any:
        """Start an application (``kw`` go to the backend, e.g. ``subprocess.Popen`` options).

        With ``wait_window`` (a title query as in :meth:`window`), waits for that window
        and returns it as a :class:`Window`; otherwise returns the process handle.

        By default (``new_window=True``) only a window that appeared after the launch
        counts, so an already-open window with a matching title (another Excel
        workbook...) is never returned instead of the new one; a window of the
        launched process wins. If no new window appears within ``timeout`` and the
        launched process has exited (it handed over to an already-running instance
        that just showed its existing window), the best pre-existing match is
        returned. ``new_window=False`` accepts any matching window at once.
        """
        if wait_window is not None:
            self._timeout(timeout)  # reject a bad timeout before starting anything
        before: Optional[List[Any]] = None
        if wait_window is not None and new_window:
            try:
                before = [w.handle for w in self.backend.list_windows()]
            except Exception:
                before = None  # cannot tell old from new windows: accept any match
        proc = self.backend.launch(command, **kw)
        if wait_window is None:
            return proc
        if before is None:
            return self.window(wait_window, timeout=timeout)
        old = before
        t = self._timeout(timeout)
        pid = getattr(proc, "pid", None)

        def probe() -> Optional[Window]:
            fresh = [w for w in self.windows(wait_window) if w.handle not in old]
            if not fresh:
                return None
            if pid is not None:
                fresh = [w for w in fresh if w.pid == pid] or fresh
            return sorted(fresh, key=lambda w: self._rank(w, wait_window))[0]

        try:
            return self._poll(probe, t, "window not found", events=True)
        except ActionTimeout:
            try:  # subprocess.Popen.poll(): None while the process runs
                running = callable(getattr(proc, "poll", None)) and proc.poll() is None
            except Exception:
                running = False
            if running:
                raise WindowNotFound(
                    f"No new window matching {wait_window!r} appeared within {t:g}s after launching {command!r} "
                    "(windows that were already open are ignored; pass new_window=False to accept them)"
                ) from None
            return self.window(wait_window, timeout=0)

    # =============================================================== clipboard
    def clipboard_get(self) -> str:
        """Text currently on the clipboard."""
        return self.backend.get_clipboard()

    def clipboard_set(self, text: str) -> None:
        """Put ``text`` on the clipboard."""
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        self.backend.set_clipboard(text)

    # ================================================================== lifecycle
    def close(self) -> None:
        """Release every key and mouse button this desktop (and its backend) holds and close the backend if this desktop created it."""
        if self._closed:
            return
        self._closed = True
        for release in (self.keyboard.release_all, self.mouse.release_all, getattr(self.backend, "release_all", None)):
            # The backend's own bookkeeping also covers a press an interrupt cut off
            # before Keyboard/Mouse could record it.
            if release is None:
                continue
            try:
                release()
            except Exception:
                pass
        if self._owns_backend:
            try:
                self.backend.close()
            except BackendError:
                pass

    def __enter__(self) -> "Desktop":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<Desktop backend={getattr(self.backend, 'name', type(self.backend).__name__)!r} "
            f"failsafe={self.failsafe.enabled} humanize={self.humanize is not None} "
            f"default_timeout={self.default_timeout:g}>"
        )
