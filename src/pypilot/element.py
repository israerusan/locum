"""UI elements: an accessibility node bound to a desktop.

:class:`Element` is what ``desktop.find(...)`` returns. It exposes the node's
properties, tree navigation, scoped lookups and *robust* actions:

* ``click(native="auto")`` clicks the element's center with the mouse when it
  is visible and actually hit-testable, and falls back to the native
  accessibility ``invoke`` when it is off-screen, minimized or covered by
  another window -- so automations keep working where coordinate-only tools
  (PyAutoGUI) silently click the wrong thing.
* ``set_text`` sets values natively (UIA ValuePattern, AT-SPI EditableText...)
  when possible, verifies the result and falls back to focus + select-all +
  typing.

The ``desktop`` argument only needs the parts of
:class:`pypilot.desktop.Desktop` used here: ``backend``, ``mouse``,
``keyboard``, ``default_timeout`` and optionally ``sleep``, ``typing_mode`` and
``find``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

from .backends.base import AccessibleNode
from .errors import ActionTimeout, ElementNotFound, NotSupported
from .types import Point, Rect
from .wait import wait_until

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from .desktop import Desktop
    from .window import Window

__all__ = ["Element", "ELEMENT_STATES", "NATIVE_ACTIONS", "add_action_listener", "remove_action_listener"]

#: Native (accessibility-API) actions reported to action listeners.
NATIVE_ACTIONS = ("invoke", "set_text", "toggle", "select", "expand", "collapse")

ActionListener = Callable[[str, "Element", Dict[str, Any]], Any]
# id(backend) -> (backend, listeners). Keyed by identity: backends need not be hashable.
_LISTENERS: Dict[int, Tuple[Any, List[ActionListener]]] = {}


def add_action_listener(backend: Any, listener: ActionListener) -> None:
    """Call ``listener(kind, element, data)`` after every successful *native* element
    action on ``backend`` (``kind`` in :data:`NATIVE_ACTIONS`).

    Native actions (UIA patterns, AT-SPI actions...) produce no mouse/keyboard
    input, so input hooks cannot see them; the recorder uses this to capture
    ``Element.set_text``/``invoke``/... too. ``data`` holds action details,
    e.g. ``{"text": ...}`` for ``set_text``. Listener errors are logged, never raised.
    """
    entry = _LISTENERS.get(id(backend))
    if entry is None or entry[0] is not backend:
        entry = (backend, [])
        _LISTENERS[id(backend)] = entry
    entry[1].append(listener)


def remove_action_listener(backend: Any, listener: ActionListener) -> None:
    """Undo :func:`add_action_listener` (no error if it was not registered)."""
    entry = _LISTENERS.get(id(backend))
    if entry is None or entry[0] is not backend:
        return
    try:
        entry[1].remove(listener)
    except ValueError:
        return
    if not entry[1]:
        del _LISTENERS[id(backend)]


def _notify(element: "Element", kind: str, **data: Any) -> None:
    entry = _LISTENERS.get(id(element.backend))
    if entry is None or entry[0] is not element.backend:
        return
    for fn in list(entry[1]):
        try:
            fn(kind, element, data)
        except Exception as exc:  # a listener must never break the automation
            import logging

            logging.getLogger("pypilot.element").warning("action listener %r failed: %s", fn, exc)

NativeMode = Union[bool, str]

#: States accepted by :meth:`Element.wait_until`.
ELEMENT_STATES = ("visible", "hidden", "enabled", "disabled", "focused", "checked", "unchecked", "selected", "gone", "exists")


def _native_mode(native: NativeMode) -> Union[bool, str]:
    if native is True or native is False:
        return native
    v = str(native).strip().lower()
    if v == "auto":
        return "auto"
    if v in ("true", "native", "invoke", "1", "yes"):
        return True
    if v in ("false", "mouse", "keyboard", "typing", "0", "no"):
        return False
    raise ValueError(f"native must be True, False or 'auto', got {native!r}")


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _same(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        return bool(a.same_as(b))
    except Exception:
        return False


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


class Element:
    """A UI element (accessibility node) bound to a desktop.

    Args:
        node: The backend's :class:`AccessibleNode`.
        desktop: The owning :class:`~pypilot.desktop.Desktop`.
        selector: The selector that found the element (enables :meth:`refresh`).
        scope: The :class:`Element`/:class:`~pypilot.window.Window` the selector
            is relative to (``None`` = the whole desktop).
        index: Position of the element among the selector's matches when it
            came from ``find_all()``/``wait()`` (``None`` = the first match, as
            for ``find()``). :meth:`refresh` and the ``"gone"``/``"exists"``
            waits use it so the element never silently turns into another match.
    """

    def __init__(
        self,
        node: AccessibleNode,
        desktop: "Desktop",
        selector: Optional[Any] = None,
        *,
        scope: Any = None,
        index: Optional[int] = None,
    ):
        if node is None:
            raise ValueError("Element needs a node")
        if index is not None and int(index) < 0:
            raise ValueError("index must be >= 0")
        self._node = node
        self.desktop = desktop
        self.selector = selector
        self._scope = scope
        self._index = None if index is None else int(index)

    # ------------------------------------------------------------ properties
    @property
    def node(self) -> AccessibleNode:
        return self._node

    @property
    def backend(self) -> Any:
        return self.desktop.backend

    @property
    def role(self) -> str:
        return _safe(lambda: self._node.role, "") or ""

    @property
    def name(self) -> str:
        return _safe(lambda: self._node.name, "") or ""

    @property
    def value(self) -> Optional[str]:
        return _safe(lambda: self._node.value)

    @property
    def text(self) -> str:
        """``value`` if the element has one, else ``name``."""
        v = self.value
        return v if v not in (None, "") else self.name

    @property
    def automation_id(self) -> str:
        return _safe(lambda: self._node.automation_id, "") or ""

    @property
    def class_name(self) -> str:
        return _safe(lambda: self._node.class_name, "") or ""

    @property
    def description(self) -> str:
        return _safe(lambda: self._node.description, "") or ""

    @property
    def bounds(self) -> Optional[Rect]:
        return _safe(lambda: self._node.bounds)

    @property
    def center(self) -> Point:
        b = self.bounds
        if b is None:
            raise ElementNotFound(f"{self!r} has no screen bounds (hidden, minimized or stale)")
        return b.center

    @property
    def states(self) -> frozenset:
        return _safe(lambda: frozenset(self._node.states), frozenset()) or frozenset()

    @property
    def is_enabled(self) -> bool:
        return bool(_safe(lambda: self._node.is_enabled, False))

    @property
    def is_visible(self) -> bool:
        return bool(_safe(lambda: self._node.is_visible, False))

    @property
    def is_focused(self) -> bool:
        return "focused" in self.states

    @property
    def is_checked(self) -> bool:
        return "checked" in self.states

    @property
    def is_selected(self) -> bool:
        return "selected" in self.states

    @property
    def actions(self) -> List[str]:
        return list(_safe(lambda: self._node.actions(), []) or [])

    @property
    def window(self) -> Optional["Window"]:
        """The top-level :class:`~pypilot.window.Window` owning this element, if it can be determined."""
        from .window import Window

        from ._nodeinfo import owning_window

        info = owning_window(self.backend, self._node, guess=False)
        return Window(info, self.desktop) if info is not None else None

    # ------------------------------------------------------------------ tree
    def _wrap(self, node: AccessibleNode, selector: Any = None, scope: Any = None) -> "Element":
        return Element(node, self.desktop, selector, scope=scope if scope is not None else self._scope)

    @property
    def parent(self) -> Optional["Element"]:
        p = _safe(lambda: self._node.parent)
        return None if p is None else self._wrap(p)

    def children(self) -> List["Element"]:
        kids = _safe(lambda: list(self._node.children()), []) or []
        return [self._wrap(k) for k in kids]

    def find(self, selector: Any, timeout: Optional[float] = None) -> "Element":
        """First descendant matching ``selector``, polling up to ``timeout`` seconds
        (``None`` = ``desktop.default_timeout``). Raises :class:`ElementNotFound`."""
        from .selectors import parse

        sel = parse(selector)
        if timeout is None:
            timeout = float(getattr(self.desktop, "default_timeout", 10.0))
        try:
            node = wait_until(
                lambda: sel.find_first([self._node], include_roots=False),
                timeout,
                _desktop_interval(self.desktop),
                sleep=_desktop_sleep(self.desktop),
                clock=_desktop_clock(self.desktop),
            )
        except ActionTimeout as exc:
            raise ElementNotFound(f"No element matching {str(sel)!r} inside {self!r} within {timeout:g}s") from exc
        return Element(node, self.desktop, selector if isinstance(selector, str) else str(sel), scope=self)

    def find_all(self, selector: Any) -> List["Element"]:
        """All descendants matching ``selector`` (no waiting), in document order."""
        from .selectors import parse

        sel = parse(selector)
        text = selector if isinstance(selector, str) else str(sel)
        return [
            Element(n, self.desktop, text, scope=self, index=i)
            for i, n in enumerate(sel.find_all([self._node], include_roots=False))
        ]

    def exists(self, selector: Any, timeout: float = 0) -> bool:
        """Whether a descendant matching ``selector`` appears within ``timeout`` seconds."""
        try:
            self.find(selector, timeout=timeout)
            return True
        except ElementNotFound:
            return False

    # ----------------------------------------------------------- resolution
    @property
    def index(self) -> Optional[int]:
        """Position among the selector's matches (``None`` = first match / not from ``find_all``)."""
        return self._index

    def _matches(self) -> List[AccessibleNode]:
        """Current matches of the selector in its scope (``[]`` if the scope is gone)."""
        from .errors import WindowNotFound

        src = self._scope if self._scope is not None else self.desktop
        try:
            found = src.find_all(self.selector)
        except (ElementNotFound, WindowNotFound):
            return []
        return [getattr(f, "node", f) for f in found]

    def _pick(self, strict: bool = False) -> Optional[AccessibleNode]:
        """Choose this element among the selector's current matches.

        The element's own node wins if it still matches (so an element never
        jumps to another match while it exists). Otherwise -- e.g. a stale node
        that the toolkit re-created -- the match at :attr:`index` (the first
        match for ``find()`` results) is used, unless ``strict`` is set.
        """
        nodes = self._matches()
        for n in nodes:
            if _same(n, self._node):
                return n
        if strict:
            return None
        i = self._index or 0
        return nodes[i] if i < len(nodes) else None

    def _resolve(self, timeout: float = 0.0, strict: bool = False) -> Optional[AccessibleNode]:
        """Re-run the selector in its scope; ``None`` if it no longer matches this element."""
        if self.selector is None:
            return None
        if timeout <= 0:
            return self._pick(strict)
        try:
            hit = wait_until(
                lambda: (lambda n: (n,) if n is not None else None)(self._pick(strict)),
                timeout,
                _desktop_interval(self.desktop),
                sleep=_desktop_sleep(self.desktop),
                clock=_desktop_clock(self.desktop),
            )
        except ActionTimeout:
            return None
        return hit[0]

    def refresh(self, timeout: float = 0.0) -> "Element":
        """Re-resolve the element through its selector (in place) and return ``self``.

        The current node is kept while it still matches; otherwise the match at
        the element's :attr:`index` is used (the first match for ``find()``
        results). Elements without a selector keep their (live) node. Raises
        :class:`ElementNotFound` if the selector no longer matches.
        """
        if self.selector is None:
            return self
        node = self._resolve(timeout)
        if node is None:
            raise ElementNotFound(f"Element {self.selector!r} no longer exists")
        self._node = node
        return self

    # --------------------------------------------------------------- helpers
    def _ancestors(self, node: AccessibleNode, limit: int = 128) -> List[AccessibleNode]:
        out = []
        cur = _safe(lambda: node.parent)
        while cur is not None and len(out) < limit:
            out.append(cur)
            nxt = cur
            cur = _safe(lambda: nxt.parent)
        return out

    def _occluded(self, point: Point) -> bool:
        """True if hit-testing ``point`` lands on an unrelated element (e.g. a covering window)."""
        try:
            hit = self.backend.element_at(int(point.x), int(point.y))
        except Exception:
            return False
        if hit is None:
            return False
        if _same(hit, self._node):
            return False
        if any(_same(a, self._node) for a in self._ancestors(hit)):
            return False  # hit a child of ours
        if any(_same(a, hit) for a in self._ancestors(self._node)):
            return False  # hit-testing is coarser than our element
        return True

    def _guard(self) -> None:
        """Honour the fail-safe (global abort flag + screen corners) before a native action.

        Native accessibility actions produce no mouse/keyboard input, so the
        checks in :class:`~pypilot.mouse.Mouse`/:class:`~pypilot.keyboard.Keyboard`
        never see them; every state-changing element action calls this first.
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

    def _on_screen(self, b: Rect) -> Optional[Rect]:
        """The largest part of ``b`` lying on a monitor (``None`` if entirely off-screen)."""
        screens: List[Rect] = []
        try:
            screens = [m.rect for m in self.backend.monitors()]
        except Exception:
            screens = []
        if not screens:
            try:
                screens = [self.backend.virtual_screen()]
            except Exception:
                return b  # cannot tell; assume visible
        best: Optional[Rect] = None
        for r in screens:
            part = b.intersect(r)
            if part is not None and (best is None or part.area > best.area):
                best = part
        return best

    def _on_any_screen(self, p: Point) -> bool:
        try:
            screens = [m.rect for m in self.backend.monitors()] or [self.backend.virtual_screen()]
        except Exception:
            return True
        return any(r.contains(p) for r in screens)

    def _click_point(self, offset: Tuple[int, int], *, require: bool) -> Optional[Point]:
        b = self.bounds
        if b is None or not self.is_visible:
            if not require:
                return None
            _safe(lambda: self._node.scroll_into_view(), False)
            b = self.bounds
            if b is None or b.is_empty:
                raise ElementNotFound(f"{self!r} is not visible on screen; cannot click it with the mouse")
        dx, dy = int(offset[0]), int(offset[1])
        part = self._on_screen(b)
        if part is None and require:
            # Entirely off-screen: ask the toolkit to scroll it into view once.
            _safe(lambda: self._node.scroll_into_view(), False)
            nb = self.bounds
            part = self._on_screen(nb) if nb is not None and not nb.is_empty else None
            if part is not None:
                b = nb  # type: ignore[assignment]
        if part is None:
            if not require:
                return None
            raise ElementNotFound(f"{self!r} is off-screen at {b}; cannot click it with the mouse")
        if (dx, dy) == (0, 0):
            # Center of the visible part: the OS would clamp an off-screen point to the
            # screen edge and the click would land on a neighbouring control.
            return part.center
        point = Point(b.center.x + dx, b.center.y + dy)
        if not self._on_any_screen(point):
            if not require:
                return None
            raise ElementNotFound(f"Click point ({point.x}, {point.y}) of {self!r} is off-screen")
        return point

    def _try_invoke(self) -> bool:
        self._guard()
        ok = bool(_safe(lambda: self._node.invoke(), False))
        if ok:
            _notify(self, "invoke")
        return ok

    def _activate_window(self) -> bool:
        """Bring the element's top-level window to the front; ``True`` if that was attempted."""
        from ._nodeinfo import owning_window

        info = owning_window(self.backend, self._node, guess=False)
        if info is None:
            return False
        try:
            self.backend.activate_window(info.handle)
        except Exception:
            return False
        return True

    def _mouse_click(self, point: Point, button: str, clicks: int) -> None:
        self.desktop.mouse.click(point, button=button, clicks=clicks)

    # --------------------------------------------------------------- actions
    def click(self, button: str = "left", clicks: int = 1, *, offset: Tuple[int, int] = (0, 0), native: NativeMode = "auto") -> "Element":
        """Click the element.

        Args:
            button: ``"left"``, ``"right"``, ``"middle"``...
            clicks: Number of clicks (2 = double click).
            offset: Pixel offset from the center.
            native: ``"auto"`` -- mouse click at the center when the element is
                visible and not covered, otherwise the native ``invoke`` action
                (if the element is covered and cannot be invoked, its window is
                activated first); ``True`` -- native invoke only (raises
                :class:`NotSupported`); ``False`` -- mouse only.
        """
        mode = _native_mode(native)
        self._guard()
        simple = str(button).lower() == "left" and int(clicks) == 1 and tuple(offset) == (0, 0)
        if mode is True:
            if not simple:
                raise ValueError("native=True only supports a single left click without offset")
            return self.invoke()
        if mode == "auto":
            point = self._click_point(offset, require=False)
            if point is None:
                if simple and self._try_invoke():
                    return self
                point = self._click_point(offset, require=True)
            elif self._occluded(point):
                if simple and self._try_invoke():
                    return self
                activated = self._activate_window()
                point = self._click_point(offset, require=True)
                if activated:
                    p = point
                    try:  # give the window manager a moment to raise the window
                        wait_until(lambda: not self._occluded(p), 1.0, _desktop_interval(self.desktop) or 0.05,
                                   sleep=_desktop_sleep(self.desktop), clock=_desktop_clock(self.desktop))
                    except ActionTimeout:
                        pass
                if self._occluded(point):
                    # Clicking now would hit whatever covers the element (another window).
                    raise ElementNotFound(
                        f"{self!r} is covered by another element at ({point.x}, {point.y}) and could not be "
                        "invoked natively or brought to the front; refusing to click the wrong thing"
                    )
        else:
            point = self._click_point(offset, require=True)
        assert point is not None
        self._mouse_click(point, button, int(clicks))
        return self

    def double_click(self, button: str = "left", *, offset: Tuple[int, int] = (0, 0)) -> "Element":
        return self.click(button, 2, offset=offset, native=False)

    def right_click(self, *, offset: Tuple[int, int] = (0, 0)) -> "Element":
        return self.click("right", 1, offset=offset, native=False)

    def hover(self, *, offset: Tuple[int, int] = (0, 0)) -> "Element":
        point = self._click_point(offset, require=True)
        self.desktop.mouse.move(point)
        return self

    def invoke(self) -> "Element":
        """Perform the default action natively (no mouse). Raises :class:`NotSupported` if unavailable."""
        if not self._try_invoke():
            raise NotSupported(f"{self!r} does not support native invoke")
        return self

    def focus(self) -> "Element":
        """Give keyboard focus natively; editable elements fall back to a mouse click."""
        self._guard()
        if _safe(lambda: self._node.focus(), False):
            return self
        if self._is_text_input():
            return self.click(native=False)
        raise NotSupported(f"{self!r} cannot be focused natively")

    def _is_text_input(self) -> bool:
        st = self.states
        return self.role in ("edit", "combobox", "document", "spinbutton") or "editable" in st

    def _ensure_focus(self) -> None:
        self._guard()
        if self.is_focused:
            return
        if _safe(lambda: self._node.focus(), False) and (self.is_focused or not self.states):
            return
        self.click(native=False)

    def _typing_mode(self, mode: Optional[str]) -> str:
        return mode or getattr(self.desktop, "typing_mode", None) or "auto"

    def set_text(self, text: str, *, native: NativeMode = "auto", clear: bool = True) -> "Element":
        """Replace (``clear=True``) or extend the element's text.

        ``native="auto"`` uses the accessibility set-value action and verifies
        the result, falling back to focus + ``mod+a`` + typing; ``True`` only
        tries the native path (raises :class:`NotSupported`); ``False`` always types.
        """
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        mode = _native_mode(native)
        self._guard()
        target = text if clear else (self.value or "") + text
        if mode is not False:
            ok = bool(_safe(lambda: self._node.set_value(target), False))
            if ok:
                current = self.value
                if mode is True or current is None or current == target:
                    _notify(self, "set_text", text=target)
                    return self
            elif mode is True:
                raise NotSupported(f"{self!r} does not support setting its value natively")
        self._ensure_focus()
        kb = self.desktop.keyboard
        if clear:
            kb.hotkey("mod+a")
            kb.press("backspace")
        if text:
            kb.type(text, mode=self._typing_mode(None))
        return self

    def type(self, text: str, **kw: Any) -> "Element":
        """Focus the element (if needed) and type ``text`` (keyword args go to ``Keyboard.type``)."""
        self._ensure_focus()
        kw["mode"] = self._typing_mode(kw.get("mode"))
        self.desktop.keyboard.type(text, **kw)
        return self

    def clear(self) -> "Element":
        return self.set_text("", clear=True)

    def press(self, *keys: Any, **kw: Any) -> "Element":
        """Focus the element (if needed) and press keys: ``el.press("ctrl+a", "delete")``."""
        self._ensure_focus()
        self.desktop.keyboard.press(*keys, **kw)
        return self

    def toggle(self) -> "Element":
        """Toggle a checkbox / toggle button (native first, then click)."""
        self._guard()
        if _safe(lambda: self._node.toggle(), False):
            _notify(self, "toggle")
            return self
        return self.click()

    def check(self) -> "Element":
        """Ensure the element is checked."""
        if not self.is_checked:
            self.toggle()
        return self

    def uncheck(self) -> "Element":
        """Ensure the element is not checked."""
        if self.is_checked:
            self.toggle()
        return self

    def select(self) -> "Element":
        """Select a list/tree/tab item (native select, then invoke, then click)."""
        self._guard()
        if _safe(lambda: self._node.select(), False):
            _notify(self, "select")
            return self
        return self.click()

    def expand(self) -> "Element":
        """Expand a tree item / combo box (native first, then the keyboard)."""
        self._guard()
        if "expanded" in self.states:
            return self
        if _safe(lambda: self._node.expand(), False):
            _notify(self, "expand")
            return self
        role = self.role
        if role == "treeitem":
            return self.press("right")
        if role == "combobox":
            return self.press("alt+down")
        raise NotSupported(f"{self!r} cannot be expanded")

    def collapse(self) -> "Element":
        """Collapse a tree item / combo box (native first, then the keyboard)."""
        self._guard()
        if "collapsed" in self.states:
            return self
        if _safe(lambda: self._node.collapse(), False):
            _notify(self, "collapse")
            return self
        role = self.role
        if role == "treeitem":
            return self.press("left")
        if role == "combobox":
            return self.press("escape")
        raise NotSupported(f"{self!r} cannot be collapsed")

    def scroll_into_view(self) -> "Element":
        """Ask the backend to scroll the element into view (best effort)."""
        self._guard()
        _safe(lambda: self._node.scroll_into_view(), False)
        return self

    def scroll(self, dy: int, dx: int = 0) -> "Element":
        """Scroll ``dy`` wheel notches (``>0`` = up) with the cursor over the element."""
        self.desktop.mouse.scroll(dy, dx, at=self._click_point((0, 0), require=True))
        return self

    def screenshot(self) -> "Image":
        """Capture the element's on-screen rectangle."""
        b = self.bounds
        if b is None or b.is_empty:
            raise ElementNotFound(f"{self!r} has no screen bounds")
        try:
            clipped = b.intersect(self.backend.virtual_screen())
        except Exception:
            clipped = b
        if clipped is None:
            raise ElementNotFound(f"{self!r} is entirely off-screen")
        return self.backend.screenshot(clipped)

    def highlight(self, duration: float = 1.0) -> "Element":
        """Flash a rectangle around the element if the backend can draw overlays (otherwise a no-op)."""
        b = self.bounds
        fn = getattr(self.backend, "highlight", None)
        if b is not None and callable(fn):
            try:
                fn(b, duration)
            except NotSupported:
                pass
        return self

    # ------------------------------------------------------------------ waits
    def _state_predicate(self, state: Any) -> Callable[[], bool]:
        if callable(state):
            return lambda: bool(state(self))
        s = str(state).lower()
        # Elements from find_all()/wait() (index set) are tracked by identity: another
        # node matching the same selector does not keep them "existing".
        strict = self._index is not None
        if s == "gone":
            if self.selector is not None:
                return lambda: self._resolve(0.0, strict) is None
            return lambda: not self.is_visible
        if s == "exists":
            if self.selector is not None:
                return lambda: self._resolve(0.0, strict) is not None
            return lambda: self.bounds is not None
        table: Dict[str, Callable[[], bool]] = {
            "visible": lambda: self.is_visible,
            "hidden": lambda: not self.is_visible,
            "enabled": lambda: self.is_enabled,
            "disabled": lambda: not self.is_enabled,
            "focused": lambda: self.is_focused,
            "checked": lambda: self.is_checked,
            "unchecked": lambda: not self.is_checked,
            "selected": lambda: self.is_selected,
        }
        if s not in table:
            raise ValueError(f"Unknown state {state!r}; expected one of {', '.join(ELEMENT_STATES)} or a callable")
        return table[s]

    def wait_until(self, state: Any = "visible", timeout: Optional[float] = None, interval: Optional[float] = None) -> "Element":
        """Wait until the element reaches ``state`` (a name from :data:`ELEMENT_STATES`
        or ``callable(element) -> bool``). Returns ``self``; raises :class:`ActionTimeout`.

        ``interval`` defaults to the desktop's ``poll_interval``; the desktop's
        injected ``sleep``/``clock`` are used for waiting."""
        if interval is None:
            interval = _desktop_interval(self.desktop)
        pred = self._state_predicate(state)
        if timeout is None:
            timeout = float(getattr(self.desktop, "default_timeout", 10.0))
        label = getattr(state, "__name__", None) or str(state)
        wait_until(
            pred,
            timeout,
            interval,
            message=f"{self!r} did not become {label!r} within {timeout:g}s",
            sleep=_desktop_sleep(self.desktop),
            clock=_desktop_clock(self.desktop),
        )
        return self

    # ------------------------------------------------------------------- misc
    def to_dict(self) -> dict:
        b = self.bounds
        return {
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "bounds": b.to_dict() if b is not None else None,
            "states": sorted(self.states),
            "selector": self.selector if self.selector is None or isinstance(self.selector, str) else str(self.selector),
        }

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Element):
            return _same(self._node, other._node)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("element", self.role, self.automation_id))

    def __repr__(self) -> str:
        from .selectors import describe_node

        try:
            desc = describe_node(self._node)
        except Exception:  # pragma: no cover - defensive
            desc = self.role
        return f"<Element {desc}>"
