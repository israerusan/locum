"""Top-level windows.

:class:`Window` wraps a :class:`~pypilot.types.WindowInfo` snapshot plus the
desktop it belongs to, and offers window management (activate, minimize,
move, close...) and element lookup scoped to the window's accessibility tree.

The ``desktop`` argument only needs the attributes of
:class:`pypilot.desktop.Desktop` that are used here: ``backend``,
``default_timeout`` and (optionally) ``sleep``.
"""

from __future__ import annotations

import fnmatch
import re
import time
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Union

from .errors import ActionTimeout, ElementNotFound, NotSupported, WindowNotFound
from .types import Rect, RectLike, WindowInfo
from .wait import wait_until

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from .desktop import Desktop
    from .element import Element

__all__ = ["Window", "match_window"]

TitleQuery = Union[str, "re.Pattern[str]", None]

_GLOB = re.compile(r"[*?\[]")


def _norm_app(app: str) -> str:
    a = str(app).strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    return a[:-4] if a.endswith(".exe") else a


def _match_text(actual: str, query: Any, *, substring: bool) -> bool:
    if isinstance(query, re.Pattern):
        return query.search(actual) is not None
    if callable(query):
        return bool(query(actual))
    q = str(query)
    if actual == q:
        return True
    low, ql = actual.lower(), q.lower()
    if _GLOB.search(q) and fnmatch.fnmatchcase(low, ql):
        return True
    return substring and ql in low


def match_window(
    info: WindowInfo,
    title: TitleQuery = None,
    app: Union[str, "re.Pattern[str]", None] = None,
    class_name: Union[str, "re.Pattern[str]", None] = None,
    pid: Optional[int] = None,
) -> bool:
    """Whether ``info`` satisfies every given criterion (``None`` = don't care).

    * ``title``: exact match; a string with glob characters (``* ? [``) is a
      case-insensitive glob; a compiled ``re.Pattern`` is searched; any plain
      string also matches as a case-insensitive substring.
    * ``app``: case-insensitive process name, ``.exe`` and directories optional
      (``"excel"`` matches ``C:\\...\\EXCEL.EXE``); globs and patterns allowed.
    * ``class_name``: exact, glob or pattern.
    * ``pid``: exact process id.
    """
    if title is not None and not _match_text(info.title or "", title, substring=True):
        return False
    if app is not None:
        if isinstance(app, re.Pattern):
            if app.search(info.app or "") is None:
                return False
        else:
            actual, want = _norm_app(info.app or ""), _norm_app(app)
            if actual != want and not (_GLOB.search(want) and fnmatch.fnmatchcase(actual, want)):
                return False
    if class_name is not None and not _match_text(info.class_name or "", class_name, substring=False):
        return False
    if pid is not None and info.pid != int(pid):
        return False
    return True


def _desktop_sleep(desktop: Any) -> Callable[[float], Any]:
    fn = getattr(desktop, "sleep", None)
    return fn if callable(fn) else time.sleep


def _desktop_clock(desktop: Any) -> Callable[[], float]:
    """The desktop's injected clock (``Desktop(clock=...)``), else :func:`time.monotonic`."""
    fn = getattr(desktop, "_clock", None)
    return fn if callable(fn) else time.monotonic


def _desktop_interval(desktop: Any, default: float = 0.1) -> float:
    """The desktop's ``poll_interval`` (seconds between polls)."""
    try:
        v = float(getattr(desktop, "poll_interval", default))
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


def _default_timeout(desktop: Any, timeout: Optional[float]) -> float:
    if timeout is None:
        timeout = getattr(desktop, "default_timeout", 10.0)
    return float(timeout)


class Window:
    """A top-level window.

    Properties reflect the last snapshot; every action refreshes it, and
    :meth:`refresh` can be called explicitly.
    """

    def __init__(self, info: WindowInfo, desktop: "Desktop"):
        if not isinstance(info, WindowInfo):
            raise TypeError(f"Window expects a WindowInfo, got {type(info).__name__}")
        self._info = info
        self.desktop = desktop

    # ------------------------------------------------------------------ props
    @property
    def backend(self) -> Any:
        return self.desktop.backend

    @property
    def info(self) -> WindowInfo:
        return self._info

    @property
    def handle(self) -> Any:
        return self._info.handle

    @property
    def title(self) -> str:
        return self._info.title

    @property
    def rect(self) -> Rect:
        return self._info.rect

    @property
    def app(self) -> str:
        return self._info.app

    @property
    def pid(self) -> Optional[int]:
        return self._info.pid

    @property
    def class_name(self) -> str:
        return self._info.class_name

    @property
    def is_active(self) -> bool:
        return self._info.is_active

    @property
    def is_minimized(self) -> bool:
        return self._info.is_minimized

    @property
    def is_maximized(self) -> bool:
        return self._info.is_maximized

    @property
    def is_visible(self) -> bool:
        return self._info.is_visible

    @property
    def center(self):
        return self._info.rect.center

    # ---------------------------------------------------------------- state
    def _lookup(self) -> Optional[WindowInfo]:
        for w in self.backend.list_windows():
            if w.handle == self.handle:
                return w
        return None

    @property
    def exists(self) -> bool:
        """Whether the window is still listed by the backend."""
        try:
            return self._lookup() is not None
        except Exception:
            return False

    def refresh(self) -> "Window":
        """Re-read the window's state from the backend (raises :class:`WindowNotFound` if it is gone)."""
        info = self._lookup()
        if info is None:
            raise WindowNotFound(f"Window {self.title!r} (handle {self.handle!r}) no longer exists")
        self._info = info
        return self

    def _after(self) -> "Window":
        try:
            self.refresh()
        except WindowNotFound:
            pass
        return self

    # -------------------------------------------------------------- actions
    def _guard(self) -> None:
        """Honour the fail-safe (global abort flag + screen corners) before changing the window.

        Window operations go straight to the backend (no mouse/keyboard input),
        so the input-level fail-safe checks never see them.
        """
        fs = getattr(self.desktop, "failsafe", None)
        check = getattr(fs, "check", None)
        if callable(check):
            check()
            return
        from . import failsafe as _fs
        from .errors import FailSafeTriggered

        if _fs.is_aborted():
            reason = _fs.abort_reason()
            raise FailSafeTriggered("Automation aborted" + (f": {reason}" if reason else ""))

    def activate(self) -> "Window":
        """Bring the window to the foreground (restoring it if minimized)."""
        self._guard()
        self.backend.activate_window(self.handle)
        return self._after()

    def minimize(self) -> "Window":
        self._guard()
        self.backend.set_window_state(self.handle, "minimized")
        return self._after()

    def maximize(self) -> "Window":
        self._guard()
        self.backend.set_window_state(self.handle, "maximized")
        return self._after()

    def restore(self) -> "Window":
        self._guard()
        self.backend.set_window_state(self.handle, "normal")
        return self._after()

    def close(self) -> "Window":
        """Ask the window to close (it may show a confirmation dialog; see :meth:`wait_closed`)."""
        self._guard()
        self.backend.close_window(self.handle)
        return self._after()

    def set_rect(self, rect: RectLike) -> "Window":
        r = Rect.coerce(rect)
        self._guard()
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Window size must be positive, got {r.width}x{r.height}")
        self.backend.move_window(self.handle, r)
        return self._after()

    def move(self, x: int, y: int) -> "Window":
        r = self.rect
        return self.set_rect(Rect(int(x), int(y), r.width, r.height))

    def resize(self, width: int, height: int) -> "Window":
        r = self.rect
        return self.set_rect(Rect(r.left, r.top, int(width), int(height)))

    def screenshot(self) -> "Image":
        """Capture the window's rectangle (clipped to the virtual screen)."""
        self._after()
        if self.is_minimized:
            raise NotSupported(f"Window {self.title!r} is minimized; restore it before taking a screenshot")
        r = self.rect
        try:
            vs = self.backend.virtual_screen()
            clipped = r.intersect(vs)
        except Exception:
            clipped = r
        if clipped is None:
            raise NotSupported(f"Window {self.title!r} is entirely off-screen")
        return self.backend.screenshot(clipped)

    def wait_closed(self, timeout: Optional[float] = 10) -> bool:
        """Wait until the window disappears. Returns True; raises :class:`ActionTimeout` otherwise."""
        wait_until(
            lambda: not self.exists,
            _default_timeout(self.desktop, timeout),
            _desktop_interval(self.desktop),
            message=f"Window {self.title!r} still open after {timeout}s",
            ignore_exceptions="transient",
            sleep=_desktop_sleep(self.desktop),
            clock=_desktop_clock(self.desktop),
        )
        return True

    # -------------------------------------------------------------- elements
    def _root_node(self) -> Any:
        roots = list(self.backend.accessibility_roots())
        for r in roots:
            try:
                if r.handle is not None and r.handle == self.handle:
                    return r
            except Exception:
                continue
        for r in roots:
            try:
                if r.name == self.title:
                    return r
            except Exception:
                continue
        return None

    @property
    def element(self) -> "Element":
        """The window's accessibility root (matched by handle, then by title)."""
        from .element import Element

        node = self._root_node()
        if node is None:
            raise ElementNotFound(f"No accessibility root for window {self.title!r} (handle {self.handle!r})")
        return Element(node, self.desktop, scope=self)

    def find(self, selector: Any, timeout: Optional[float] = None) -> "Element":
        """Find an element inside this window (polling until ``timeout``; ``None`` = desktop default).

        The window root itself can satisfy the first step, so both ``"button"``
        and ``'window "Title" > button'`` work.
        """
        from .element import Element, _poll_find
        from .selectors import parse

        sel = parse(selector)
        timeout = _default_timeout(self.desktop, timeout)

        def probe() -> Any:
            root = self._root_node()
            return None if root is None else sel.find_first([root])

        try:
            # NotSupported (no accessibility API), fail-safe and policy errors propagate at once.
            node = _poll_find(self.desktop, probe, timeout)
        except ActionTimeout as exc:
            raise ElementNotFound(
                f"No element matching {_selector_text(selector, sel)!r} in window {self.title!r} within {timeout:g}s"
            ) from exc
        return Element(node, self.desktop, selector=_selector_text(selector, sel), scope=self)

    def find_all(self, selector: Any) -> List["Element"]:
        """All elements matching ``selector`` inside this window (no waiting)."""
        from .element import Element
        from .selectors import parse

        sel = parse(selector)
        root = self._root_node()
        if root is None:
            return []
        text = _selector_text(selector, sel)
        return [Element(n, self.desktop, selector=text, scope=self, index=i) for i, n in enumerate(sel.find_all([root]))]

    def exists_element(self, selector: Any, timeout: float = 0.0) -> bool:
        try:
            self.find(selector, timeout=timeout)
            return True
        except ElementNotFound:
            return False

    # ------------------------------------------------------------------ misc
    def to_dict(self) -> dict:
        return self._info.to_dict()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Window):
            return self.handle == other.handle
        return NotImplemented

    def __hash__(self) -> int:
        try:
            return hash(("window", self.handle))
        except TypeError:
            return hash(("window", str(self.handle)))

    def __repr__(self) -> str:
        r = self.rect
        flags = [f for f, on in (("active", self.is_active), ("minimized", self.is_minimized), ("maximized", self.is_maximized)) if on]
        extra = f" [{', '.join(flags)}]" if flags else ""
        return f"<Window {self.title!r} app={self.app!r} handle={self.handle!r} ({r.left},{r.top} {r.width}x{r.height}){extra}>"


def _selector_text(original: Any, parsed: Any) -> Any:
    """Keep string selectors verbatim; dicts / Selector objects become canonical CSS strings."""
    if isinstance(original, str):
        return original
    return str(parsed)
