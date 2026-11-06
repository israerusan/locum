"""Turn the desktop into something an LLM can read: a *set-of-marks* observation.

:func:`observe` captures, in one consistent snapshot:

* a screenshot (downscaled so it fits a vision model's image limits),
* the visible **interactive elements** from the accessibility tree, each with a
  small integer ``id``, its role/name/value/states, bounds and a robust
  selector,
* the same screenshot with numbered boxes drawn on every element (the
  "set-of-marks" prompting technique: the model can say *click element 7*
  instead of guessing pixel coordinates),
* the window list, the active window and the cursor position.

All coordinates shown to the model are in the **image space** of the returned
screenshot; :meth:`Observation.to_screen` maps them back to physical screen
pixels (and :meth:`Observation.to_image` the other way round).
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..backends.base import AccessibleNode
from ..errors import ElementNotFound, NotSupported
from ..types import Point, Rect, WindowInfo
from .._nodeinfo import is_password

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from ..desktop import Desktop

#: Image-size limits of current Claude vision models (no server-side downscaling for
#: computer use): the long edge and the number of 28x28 "visual tokens".
MAX_LONG_EDGE = 2576
MAX_VISUAL_TOKENS = 4784
PATCH_SIZE = 28

#: States worth showing to a model, in display order.
_SHOWN_STATES = ("focused", "disabled", "checked", "selected", "expanded", "collapsed", "readonly", "occluded")


def visual_tokens(width: int, height: int, patch: int = PATCH_SIZE) -> int:
    """Number of ``patch x patch`` tiles a ``width x height`` image costs."""
    return int(math.ceil(width / float(patch)) * math.ceil(height / float(patch)))


def fit_image_size(
    width: int,
    height: int,
    max_long_edge: Optional[int] = MAX_LONG_EDGE,
    max_visual_tokens: Optional[int] = MAX_VISUAL_TOKENS,
    patch: int = PATCH_SIZE,
) -> Tuple[int, int]:
    """Largest ``(w, h)`` with the aspect ratio of ``width x height`` (never upscaled) such that
    ``max(w, h) <= max_long_edge`` and ``visual_tokens(w, h) <= max_visual_tokens``.

    >>> fit_image_size(2560, 1440)
    (2560, 1440)
    >>> fit_image_size(3840, 2160)
    (2576, 1449)
    """
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError(f"Image size must be positive, got {width}x{height}")
    long_edge = max(width, height)
    s = 1.0
    if max_long_edge:
        if max_long_edge <= 0:
            raise ValueError("max_long_edge must be positive")
        s = min(1.0, float(max_long_edge) / long_edge)

    def dims(scale: float) -> Tuple[int, int]:
        w = max(1, int(round(width * scale)))
        h = max(1, int(round(height * scale)))
        if max_long_edge:  # rounding must never break the long-edge limit
            w, h = min(w, int(max_long_edge)), min(h, int(max_long_edge))
        return min(w, width), min(h, height)

    w, h = dims(s)
    if max_visual_tokens:
        if max_visual_tokens <= 0:
            raise ValueError("max_visual_tokens must be positive")
        if visual_tokens(w, h, patch) > max_visual_tokens:
            s = min(s, math.sqrt(max_visual_tokens * patch * patch / float(width * height)))
            w, h = dims(s)
            while visual_tokens(w, h, patch) > max_visual_tokens and (w > 1 or h > 1):
                s *= 0.995
                w, h = dims(s)
    return w, h


def _safe(fn: Any, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _node_value(node: Any) -> Optional[str]:
    """``node.value``, masked (``"***"``) for password fields: the model and the audit log never see passwords."""
    value = _safe(lambda: node.value)
    if value not in (None, "") and is_password(node):
        return "***"
    return value


def _quote(text: str, limit: int = 80) -> str:
    text = text.replace("\r", "").replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _LazySelector(str):
    """Marker: pass ``selector=LAZY_SELECTOR`` plus ``_selector_source`` to build it on demand."""


LAZY_SELECTOR = _LazySelector("")


@dataclass
class ObservedElement:
    """One numbered element of an :class:`Observation`.

    ``bounds`` are physical **screen** coordinates; use
    :meth:`Observation.to_image` to express them in screenshot pixels.
    """

    id: int
    role: str
    name: str
    value: Optional[str]
    bounds: Rect
    states: frozenset
    #: A selector that finds the element again. Computed on first access when the
    #: element was created lazily (see :data:`LAZY_SELECTOR`): building one proves it
    #: unique by searching the accessibility trees, which costs seconds per element on
    #: a desktop with thousands of elements, and most observed elements are never acted on.
    selector: str = field(repr=False, compare=False)
    node: AccessibleNode = field(repr=False, compare=False)
    window_title: str = ""
    app: str = ""
    _selector_source: Optional[Callable[[], str]] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.selector is LAZY_SELECTOR:
            del self.__dict__["selector"]  # computed by __getattr__ on first access

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not set on the instance: the lazy selector.
        if name != "selector":
            raise AttributeError(name)
        source = self.__dict__.get("_selector_source")
        try:
            value = source() if source is not None else ""
        except Exception:
            value = ""
        self.__dict__["selector"] = value
        return value

    @property
    def selector_ready(self) -> bool:
        """Whether :attr:`selector` is already known (reading it then costs nothing)."""
        return "selector" in self.__dict__

    @property
    def center(self) -> Point:
        return self.bounds.center

    @property
    def occluded(self) -> bool:
        """Whether another window covers the element's center (clicks use the native action)."""
        return "occluded" in self.states

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "bounds": self.bounds.to_dict(),
            "states": sorted(self.states),
            "selector": self.selector,
            "window": self.window_title,
            "app": self.app,
        }


@dataclass
class Observation:
    """A consistent snapshot of the desktop for an LLM (see module docstring).

    Attributes:
        screenshot: The (downscaled) screenshot, or ``None`` when not captured.
        elements: Numbered interactive elements (ids start at 1).
        windows: Top-level windows, front-most first.
        active_window: The foreground window, if known.
        cursor: Cursor position in **screen** coordinates.
        screen_size: ``(width, height)`` of the captured region in screen pixels.
        scale: ``image_px = (screen_px - region.left) * scale`` (``<= 1``).
        annotated: The screenshot with numbered boxes, or ``None``.
        region: The captured screen rectangle (virtual screen or one window).
        notes: Remarks for the model (e.g. windows a ``time_budget`` cut short).
    """

    screenshot: Optional["Image"]
    elements: List[ObservedElement]
    windows: List[WindowInfo]
    active_window: Optional[WindowInfo]
    cursor: Point
    screen_size: Tuple[int, int]
    scale: float
    annotated: Optional["Image"]
    region: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    #: Remarks for the model, e.g. which windows a ``time_budget`` cut short.
    notes: List[str] = field(default_factory=list)
    # Live desktop roots + prefix cache from observe(): selectors built later (lazily,
    # add_element, refresh) are proven unique among all windows, not only inside their own.
    _ctx: Optional["_SelectorContext"] = field(default=None, init=False, repr=False, compare=False)

    # Older spelling (an Observation built by hand, e.g. by the toolkit before any observe):
    # setting the roots gives a context that cannot re-read them from a backend.
    @property
    def _roots(self) -> Optional[List[AccessibleNode]]:
        return self._ctx.roots if self._ctx is not None else None

    @_roots.setter
    def _roots(self, roots: Optional[List[AccessibleNode]]) -> None:
        self._ctx = _SelectorContext(None, list(roots) if roots else None)

    @property
    def _prefix_cache(self) -> Optional[List[Any]]:
        return self._ctx.cache if self._ctx is not None else None

    @_prefix_cache.setter
    def _prefix_cache(self, cache: Optional[List[Any]]) -> None:
        if self._ctx is not None and cache is not None:
            self._ctx.cache = cache

    # ----------------------------------------------------------------- mapping
    @property
    def image_size(self) -> Tuple[int, int]:
        """``(width, height)`` of the image coordinate space."""
        img = self.screenshot or self.annotated
        if img is not None:
            return img.size
        return (max(1, int(round(self.region.width * self.scale))), max(1, int(round(self.region.height * self.scale))))

    def to_screen(self, x: float, y: float) -> Point:
        """Map image coordinates (what the model sees) to physical screen coordinates.

        Points are clamped into the captured region so a model's off-by-one at
        the image border never lands outside the screen.
        """
        s = self.scale if self.scale > 0 else 1.0
        sx = self.region.left + float(x) / s
        sy = self.region.top + float(y) / s
        r = self.region
        if r.width > 0 and r.height > 0:
            sx = min(max(sx, r.left), r.right - 1)
            sy = min(max(sy, r.top), r.bottom - 1)
        return Point(int(round(sx)), int(round(sy)))

    def to_image(self, x: float, y: float) -> Point:
        """Map physical screen coordinates to image coordinates."""
        s = self.scale if self.scale > 0 else 1.0
        return Point(int(round((float(x) - self.region.left) * s)), int(round((float(y) - self.region.top) * s)))

    def rect_to_image(self, rect: Rect) -> Rect:
        """Map a screen :class:`Rect` to image space."""
        tl = self.to_image(rect.left, rect.top)
        br = self.to_image(rect.right, rect.bottom)
        return Rect.from_ltrb(tl.x, tl.y, max(br.x, tl.x + 1), max(br.y, tl.y + 1))

    # ------------------------------------------------------------------ lookup
    def element(self, id: int) -> ObservedElement:
        """The element with this id; raises :class:`ElementNotFound` (a ``LookupError``)."""
        try:
            key = int(id)
        except (TypeError, ValueError):
            raise ElementNotFound(f"Invalid element id {id!r}") from None
        for el in self.elements:
            if el.id == key:
                return el
        hi = max((e.id for e in self.elements), default=0)
        raise ElementNotFound(
            f"No element with id {key} in the last observation (valid ids: 1..{hi}). "
            "Call observe again if the screen changed."
        )

    def add_element(self, node: AccessibleNode, *, selector: str = "") -> ObservedElement:
        """Append ``node`` (if not already present) with a fresh id; returns its entry.

        An element already in the observation keeps its id but is refreshed from the
        live node: the entry was a snapshot from ``observe`` time, and a later
        ``find_elements`` / ``wait_for`` must not describe what it *used* to show
        (a calculator display still reading "0" after the result came in)."""
        for el in self.elements:
            if _same(el.node, node):
                _refresh(el, node, self._ctx)
                return el
        b = _safe(lambda: node.bounds)
        next_id = max((e.id for e in self.elements), default=0) + 1
        entry = ObservedElement(
            id=next_id,
            role=(_safe(lambda: node.role, "") or "unknown").lower(),
            name=_safe(lambda: node.name, "") or "",
            value=_node_value(node),
            bounds=b if b is not None else Rect(0, 0, 0, 0),
            states=frozenset(_safe(lambda: node.states, frozenset()) or ()),
            selector=selector or LAZY_SELECTOR,
            node=node,
            _selector_source=None if selector else (
                lambda n=node, c=self._ctx: _selector_in(n, c)),
        )
        self.elements.append(entry)
        return entry

    # ------------------------------------------------------------------ output
    def describe_element(self, el: ObservedElement) -> str:
        """``[3] button "OK" at (412, 230) size 80x30 [focused]`` in image coordinates."""
        parts = [f"[{el.id}]", el.role]
        if el.name:
            parts.append(_quote(el.name))
        if el.value not in (None, "") and str(el.value) != el.name:
            parts.append("value=" + _quote(str(el.value), 60))
        if not el.bounds.is_empty:
            c = self.to_image(*el.bounds.center)
            parts.append(f"at ({c.x}, {c.y})")
            w = max(1, int(round(el.bounds.width * self.scale)))
            h = max(1, int(round(el.bounds.height * self.scale)))
            parts.append(f"size {w}x{h}")
        shown = [s for s in _SHOWN_STATES if s in el.states]
        if shown:
            parts.append("[" + ", ".join(shown) + "]")
        return " ".join(parts)

    def text(self, max_elements: int = 300) -> str:
        """A compact text rendering for the model (all coordinates in image space)."""
        lines: List[str] = []
        iw, ih = self.image_size
        sw, sh = self.screen_size
        if self.screenshot is not None:
            lines.append(
                f"Screen region {sw}x{sh} px shown as a {iw}x{ih} screenshot (scale {self.scale:.4g}). "
                "All coordinates are in screenshot pixels."
            )
        else:
            lines.append(f"Screen region {sw}x{sh} px (no screenshot). Coordinates are in this region's pixels (scale {self.scale:.4g}).")
        cur = self.to_image(*self.cursor)
        lines.append(f"Cursor: ({cur.x}, {cur.y})")
        if self.active_window is not None:
            aw = self.active_window
            lines.append(f"Active window: {_quote(aw.title)}" + (f" (app {aw.app})" if aw.app else ""))
        if self.windows:
            lines.append("Windows (front to back):")
            for w in self.windows[:30]:
                flags = []
                if w.is_active:
                    flags.append("active")
                if w.is_minimized:
                    flags.append("minimized")
                r = self.rect_to_image(w.rect)
                lines.append(
                    f"  - {_quote(w.title)}" + (f" app={w.app}" if w.app else "")
                    + f" at ({r.left}, {r.top}) size {r.width}x{r.height}"
                    + (f" [{', '.join(flags)}]" if flags else "")
                )
            if len(self.windows) > 30:
                lines.append(f"  ... and {len(self.windows) - 30} more")
        if self.elements:
            lines.append("Elements (use the [id] with tools; 'occluded' = covered by another window):")
            current = None
            for el in self.elements[: max(0, int(max_elements))]:
                if el.window_title != current:
                    current = el.window_title
                    lines.append(f"  in window {_quote(current)}:" if current else "  (no window):")
                lines.append("    " + self.describe_element(el))
            if len(self.elements) > max_elements:
                lines.append(f"  ... {len(self.elements) - max_elements} more elements not shown")
        else:
            lines.append("No accessible interactive elements were found (use coordinates from the screenshot).")
        lines.extend(f"Note: {n}" for n in self.notes)
        return "\n".join(lines)

    def image_base64(self, annotated: bool = True) -> str:
        """Base64 PNG of the (annotated) screenshot; raises ``ValueError`` if none was captured."""
        from ..vision import to_base64_png

        img = self.annotated if annotated and self.annotated is not None else self.screenshot
        if img is None:
            raise ValueError("This observation has no screenshot")
        return to_base64_png(img)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly summary (no images)."""
        return {
            "region": self.region.to_dict(),
            "screen_size": list(self.screen_size),
            "image_size": list(self.image_size),
            "scale": self.scale,
            "cursor": self.cursor.to_dict(),
            "active_window": self.active_window.to_dict() if self.active_window else None,
            "windows": [w.to_dict() for w in self.windows],
            "elements": [e.to_dict() for e in self.elements],
            "notes": list(self.notes),
        }


def _same(a: AccessibleNode, b: AccessibleNode) -> bool:
    if a is b:
        return True
    try:
        return bool(a.same_as(b))
    except Exception:
        return False


def _refresh(el: ObservedElement, node: AccessibleNode, ctx: Optional["_SelectorContext"] = None) -> None:
    """Update ``el``'s description fields from the live ``node`` (id unchanged).

    A field that can no longer be read keeps its old value. When the name changes, a
    selector already built from the old name is dropped and rebuilt on next use."""
    missing = object()
    role = _safe(lambda: node.role, missing)
    if role is not missing and role:
        el.role = str(role).lower()
    name = _safe(lambda: node.name, missing)
    if name is not missing:
        name = name or ""
        # An empty selector without a source was asked for (observe(selectors=False)): keep it.
        if name != el.name and el.selector_ready and (el.selector or el._selector_source is not None):
            del el.__dict__["selector"]
            if el._selector_source is None:
                el._selector_source = lambda n=node, c=ctx: _selector_in(n, c)
        el.name = name
    raw = _safe(lambda: node.value, missing)
    if raw is not missing:
        el.value = "***" if raw not in (None, "") and is_password(node) else raw
    b = _safe(lambda: node.bounds)
    if b is not None:
        el.bounds = b
    states = _safe(lambda: node.states, missing)
    if states is not missing:
        # "occluded" is computed by observe (window stacking), not read from the node.
        keep = {"occluded"} & el.states
        el.states = frozenset(states or ()) | keep


def _selector(node: AccessibleNode, roots: Optional[Sequence[AccessibleNode]] = None,
              prefix_cache: Optional[List[Any]] = None) -> str:
    from ..selectors import describe_node, selector_for

    try:
        return selector_for(node, roots=roots, prefix_cache=prefix_cache)
    except Exception:
        return _safe(lambda: describe_node(node, include_bounds=False), "") or ""


def _root_key(root: AccessibleNode) -> Tuple[Any, ...]:
    return (_safe(lambda: root.handle), _safe(lambda: root.role), _safe(lambda: root.name),
            _safe(lambda: root.automation_id))


class _SelectorContext:
    """Search roots and prefix cache for an observation's (lazily built) selectors.

    Selectors are built after ``observe`` returns (on first ``.selector`` access, or by
    ``add_element`` from ``find_elements`` / ``wait_for``), when windows may have opened,
    closed or been retitled. Each build therefore proves uniqueness against the *live*
    roots, and the shared prefix cache (valid only while the windows have not changed)
    is dropped whenever the roots differ from the ones it was filled for.
    """

    def __init__(self, backend: Any, roots: Optional[List[AccessibleNode]]) -> None:
        self.backend = backend
        self.roots = roots
        self.sig = self._sig(roots)
        self.cache: List[Any] = []

    @staticmethod
    def _sig(roots: Optional[Sequence[AccessibleNode]]) -> Optional[List[Tuple[Any, ...]]]:
        return None if roots is None else [_root_key(r) for r in roots]

    def current(self) -> Tuple[Optional[List[AccessibleNode]], List[Any]]:
        if self.backend is None:
            return self.roots, self.cache
        try:
            live: Optional[List[AccessibleNode]] = list(self.backend.accessibility_roots()) or None
        except Exception:
            live = self.roots  # cannot re-read: keep the observe-time roots
        else:
            sig = self._sig(live)
            changed = (sig != self.sig or live is None or self.roots is None or len(live) != len(self.roots)
                       or not all(_same(a, b) for a, b in zip(live, self.roots)))
            if changed:
                self.roots, self.sig, self.cache = live, sig, []
            else:
                self.roots = live
        return self.roots, self.cache


def _selector_in(node: AccessibleNode, ctx: Optional[_SelectorContext]) -> str:
    if ctx is None:
        return _selector(node)
    roots, cache = ctx.current()
    return _selector(node, roots, cache)


def _is_interactive(node: AccessibleNode) -> bool:
    from ..selectors import INTERACTIVE_ROLES

    role = (_safe(lambda: node.role, "") or "").lower()
    if role in INTERACTIVE_ROLES:
        return True
    acts = _safe(lambda: list(node.actions()), []) or []
    if any(a in ("invoke", "toggle", "set_value", "select", "expand") for a in acts):
        return True
    return "editable" in (_safe(lambda: node.states, frozenset()) or frozenset())


def _resolve_window(desktop: "Desktop", window: Any) -> Any:
    """``window`` -> :class:`~locum.window.Window` (accepts Window, WindowInfo or a title query)."""
    from ..window import Window

    if isinstance(window, Window):
        return window
    if isinstance(window, WindowInfo):
        return Window(window, desktop)
    return desktop.window(window, timeout=0)


def observe(
    desktop: "Desktop",
    *,
    screenshot: bool = True,
    annotate: bool = True,
    max_long_edge: int = 1568,
    interactive_only: bool = True,
    window: Any = None,
    max_elements: int = 300,
    max_visual_tokens: Optional[int] = MAX_VISUAL_TOKENS,
    selectors: bool = True,
    max_depth: int = 32,
    time_budget: Optional[float] = None,
) -> Observation:
    """Snapshot the desktop for an LLM (set-of-marks).

    Args:
        desktop: The :class:`~locum.desktop.Desktop` to observe.
        screenshot: Capture a screenshot (otherwise only the tree is read).
        annotate: Also produce the screenshot with numbered element boxes.
        max_long_edge: Downscale the screenshot so its long edge is at most this.
        interactive_only: Only list interactive elements (buttons, edits, ...);
            otherwise every visible named element (labels too).
        window: Restrict to one window (:class:`Window`, :class:`WindowInfo` or a
            title query); the screenshot is then cropped to that window.
        max_elements: Cap on listed elements (front-most windows first).
        max_visual_tokens: Visual-token budget for the screenshot (``None`` = no limit).
        selectors: Compute a robust selector for every element (slower on huge trees).
        max_depth: Maximum accessibility-tree depth walked.
        time_budget: Seconds the element walk may take (``None`` = no limit). Windows
            are walked front-most first and none may use more than its share of what is
            left, so one huge or hung application (a browser with thousands of nodes)
            cannot stall the observation; windows cut short are named in
            :attr:`Observation.notes` and the partial result is returned.

    Elements are numbered from 1 in window Z-order (front-most first), then
    document order. Elements whose center is covered by a window in front are
    kept (their native action still works) but flagged ``occluded`` and not
    drawn on the screenshot.
    """
    backend = desktop.backend
    vr = desktop.virtual_rect()
    try:
        all_windows = list(backend.list_windows())
    except NotSupported:
        all_windows = []
    try:
        active = backend.active_window()
    except NotSupported:
        active = None

    target_window = None
    if window is not None:
        target_window = _resolve_window(desktop, window)
        region = target_window.rect.intersect(vr) or target_window.rect
    else:
        region = vr

    # ------------------------------------------------------------------ elements
    try:
        roots: List[AccessibleNode] = list(backend.accessibility_roots())
    except NotSupported:
        roots = []
    # Selectors are always proven unique among *all* windows, even when only one is observed.
    all_roots: Optional[List[AccessibleNode]] = list(roots) or None
    if target_window is not None:
        try:
            roots = [target_window.element.node]
        except Exception:
            roots = []

    # Front-to-back window rects for occlusion tests (minimized windows don't cover anything).
    front: List[Tuple[Any, Rect]] = [(w.handle, w.rect) for w in all_windows if w.is_visible and not w.is_minimized]
    z_index = {h: i for i, (h, _) in enumerate(front)}
    titles = {w.handle: w for w in all_windows}
    # Walk in window Z-order (front-most first) whatever order the backend lists roots in
    # (AT-SPI: application order). Roots of unlisted windows (e.g. pop-up menus) keep their slots.
    zs = [_safe(lambda: z_index.get(_safe(lambda: r.handle))) for r in roots]
    slots = [i for i, z in enumerate(zs) if z is not None]
    if slots:
        ordered = sorted(slots, key=lambda k: zs[k])
        picked = [roots[k] for k in ordered]
        roots = list(roots)
        for i, r in zip(slots, picked):
            roots[i] = r

    elements: List[ObservedElement] = []
    # One per observation: elements of the same window share the (expensive) proof that
    # the window's selector prefix is unique on the desktop (while the windows are unchanged).
    ctx = _SelectorContext(backend, all_roots)
    limit = max(0, int(max_elements))
    budget_end = None if time_budget is None else _time.monotonic() + max(0.0, float(time_budget))
    partial: List[str] = []
    unscanned: List[str] = []
    for idx, root in enumerate(roots):
        if len(elements) >= limit:
            break
        handle = _safe(lambda: root.handle)
        info = titles.get(handle)
        win_title = info.title if info is not None else (_safe(lambda: root.name, "") or "")
        app = info.app if info is not None else ""
        zi = z_index.get(handle)
        win_end = None
        if budget_end is not None:
            now = _time.monotonic()
            left = budget_end - now
            if left <= 0:
                unscanned.append(win_title)
                continue
            # Front windows may use half of what is left; later ones at least a fair share.
            win_end = now + max(left / (len(roots) - idx), left * 0.5)
        for node in root.walk(max_depth=max_depth):
            if len(elements) >= limit:
                break
            if win_end is not None and _time.monotonic() >= win_end:
                partial.append(win_title)
                break
            if node is root and _safe(lambda: node.role, "") == "window":
                continue  # windows are listed separately
            b = _safe(lambda: node.bounds)
            if b is None or b.is_empty or not _safe(lambda: node.is_visible, False):
                continue
            if b.intersect(region) is None:
                continue
            if interactive_only:
                if not _is_interactive(node):
                    continue
            else:
                if not (_safe(lambda: node.name, "") or _safe(lambda: node.value) or _is_interactive(node)):
                    continue
            states = set(_safe(lambda: node.states, frozenset()) or ())
            if zi is not None:
                c = b.center
                if any(r.contains(c) for _, r in front[:zi]):
                    states.add("occluded")
            elements.append(
                ObservedElement(
                    id=len(elements) + 1,
                    role=(_safe(lambda: node.role, "") or "unknown").lower(),
                    name=_safe(lambda: node.name, "") or "",
                    value=_node_value(node),
                    bounds=b,
                    states=frozenset(states),
                    selector=LAZY_SELECTOR if selectors else "",
                    node=node,
                    window_title=win_title,
                    app=app,
                    _selector_source=(lambda n=node: _selector_in(n, ctx))
                    if selectors else None,
                )
            )

    # ---------------------------------------------------------------- screenshot
    shot = None
    annotated_img = None
    scale = 1.0
    if screenshot:
        tw, th = fit_image_size(region.width, region.height, max_long_edge, max_visual_tokens)
        scale = tw / float(region.width) if region.width else 1.0
        raw = desktop.screenshot(region)
        if raw.size != (tw, th):
            from PIL import Image as _PILImage

            resample = getattr(_PILImage, "Resampling", _PILImage).LANCZOS
            shot = raw.resize((tw, th), resample)
        else:
            shot = raw
        if annotate:
            from ..vision import annotate as _annotate

            boxes = [(el.bounds, str(el.id)) for el in elements if not el.occluded]
            annotated_img = _annotate(shot, boxes, color=None, offset=(region.left, region.top), scale=scale) if boxes else shot.copy()
    obs = Observation(
        screenshot=shot,
        elements=elements,
        windows=all_windows,
        active_window=active,
        cursor=desktop.position(),
        screen_size=(region.width, region.height),
        scale=scale,
        annotated=annotated_img,
        region=region,
    )
    if partial or unscanned:
        def _names(ts: List[str]) -> str:
            shown = ", ".join(_quote(t) if t else "(untitled)" for t in ts[:8])
            return shown + (f" and {len(ts) - 8} more" if len(ts) > 8 else "")

        msg = f"Time budget of {float(time_budget):g}s ran out, so this observation is partial:"
        if partial:
            msg += f" only part of {_names(partial)} was scanned;"
        if unscanned:
            msg += f" {_names(unscanned)} not scanned;"
        obs.notes.append(msg.rstrip(";") + ". Observe one window (window=...) to see the rest.")
    obs._ctx = ctx
    return obs


__all__ = [
    "MAX_LONG_EDGE",
    "MAX_VISUAL_TOKENS",
    "Observation",
    "ObservedElement",
    "fit_image_size",
    "observe",
    "visual_tokens",
]
