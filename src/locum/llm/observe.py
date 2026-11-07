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
from dataclasses import dataclass, field, replace as _dc_replace
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..backends.base import AccessibleNode
from ..errors import ElementNotFound, NotSupported
from ..types import Point, Rect, WindowInfo
from .._nodeinfo import is_password
from .._secret import MODEL_MIN_SCRUB, mask_value, scrub_data, scrub_for_model

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from ..desktop import Desktop

#: Image-size limits of current Claude vision models (no server-side downscaling for
#: computer use): the long edge and the number of 28x28 "visual tokens".
MAX_LONG_EDGE = 2576
MAX_VISUAL_TOKENS = 4784
PATCH_SIZE = 28
#: Oldest observation (seconds) whose unchanged windows ``observe(reuse=...)`` reuses;
#: bounds a missed change event to a short window of staleness.
REUSE_MAX_AGE = 20.0
#: Processes whose top-level windows only frame content drawn by *another* process
#: (Store/UWP apps such as Calculator and Settings: the window belongs to
#: ApplicationFrameHost.exe, its content and its change events to the app). Such a
#: window is reused only when the backend names the hosted processes
#: (``window_content_pids``); otherwise it is always walked again.
HOST_PROCESSES = frozenset({"applicationframehost.exe"})


def _window_pids(backend: Any, info: WindowInfo) -> Optional[frozenset]:
    """Processes whose change events can alter ``info``'s content: its own plus any
    process with a child window inside it (the UWP CoreWindow hosted by
    ApplicationFrameHost, a WebView2 or other cross-process child). ``None`` when that
    cannot be told (a host-process window without hosted-pid information)."""
    own = {int(info.pid)} if info.pid else set()
    lookup = getattr(backend, "window_content_pids", None)
    extra = _safe(lambda: lookup(info.handle)) if callable(lookup) else None
    is_host = (info.app or "").lower() in HOST_PROCESSES
    if extra is None:
        return None if is_host or not own else frozenset(own)
    pids = own | {int(p) for p in extra if p}
    if is_host and not (pids - own):
        return None  # the hosted app's window is detached: its events cannot be attributed
    return frozenset(pids) if pids else None

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
    # A secret typed into an ordinary field: masked like a password field. Only a value that
    # *is* a secret, or holds an unambiguous (long) one: a PIN must not turn 1234.50 into ***.50.
    return mask_value(value)


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
        return _for_model(self._dict())

    def _dict(self) -> Dict[str, Any]:
        """:meth:`to_dict` before scrubbing (the caller scrubs once)."""
        value = mask_value(self.value)  # a secret registered after the observation
        selector = self.selector
        if selector and scrub_for_model(selector) != selector:
            # A name holding a secret: a selector with *** in it would find nothing (or
            # something else). The model acts on the id instead.
            selector = ""
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "value": value,
            "bounds": self.bounds.to_dict(),
            "states": sorted(self.states),
            "selector": selector,
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
    # Highest element id ever handed out in this chain of observations (obsdiff.compare
    # carries it forward). Ids reported "gone" stay retired: a new element never gets one.
    _max_id: int = field(default=0, init=False, repr=False, compare=False)

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
        ids = sorted(e.id for e in self.elements)
        valid = f"1..{ids[-1]}" if ids == list(range(1, len(ids) + 1)) and ids else (
            ", ".join(map(str, ids[:20])) + (" ..." if len(ids) > 20 else "") if ids else "none")
        raise ElementNotFound(
            f"No element with id {key} in the last observation (valid ids: {valid}). "
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
        next_id = self.next_id()
        win = self._window_of(node)
        entry = ObservedElement(
            id=next_id,
            role=(_safe(lambda: node.role, "") or "unknown").lower(),
            name=_safe(lambda: node.name, "") or "",
            value=_node_value(node),
            bounds=b if b is not None else Rect(0, 0, 0, 0),
            states=frozenset(_safe(lambda: node.states, frozenset()) or ()),
            selector=selector or LAZY_SELECTOR,
            node=node,
            window_title=win.title if win is not None else "",
            app=win.app if win is not None else "",
            _selector_source=None if selector else (
                lambda n=node, c=self._ctx: _selector_in(n, c)),
        )
        self.elements.append(entry)
        self._max_id = next_id
        return entry

    def _window_of(self, node: AccessibleNode) -> Optional[WindowInfo]:
        """The listed top-level window containing ``node``, so an element added later
        carries the same ``window_title`` as one found by ``observe`` (the diff of the
        next ``changes_only`` matches on it when the backend has no runtime ids)."""
        by_handle = {w.handle: w for w in self.windows if w.handle is not None}
        if not by_handle:
            return None
        cur: Optional[AccessibleNode] = node
        # With runtime ids (UI Automation) the diff matches without the title: skip the
        # cross-process walk up the tree and only try the node's own handle.
        depth = 0 if _safe(lambda: node.runtime_id) else 64
        for _ in range(depth + 1):
            if cur is None:
                break
            w = by_handle.get(_safe(lambda: cur.handle))
            if w is not None:
                return w
            cur = _safe(lambda: cur.parent())
        return None

    def next_id(self) -> int:
        """The id the next new element gets: above every id used in this chain of
        observations, including ids already reported gone (they are never reused)."""
        return max(max((e.id for e in self.elements), default=0), int(getattr(self, "_max_id", 0) or 0)) + 1

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
        # Values were masked when observed; names/titles holding a long secret are masked here.
        return scrub_for_model("\n".join(lines)) or ""

    def image_base64(self, annotated: bool = True) -> str:
        """Base64 PNG of the (annotated) screenshot; raises ``ValueError`` if none was captured."""
        from ..vision import to_base64_png

        img = self.annotated if annotated and self.annotated is not None else self.screenshot
        if img is None:
            raise ValueError("This observation has no screenshot")
        return to_base64_png(img)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly summary (no images)."""
        return _for_model({
            "region": self.region.to_dict(),
            "screen_size": list(self.screen_size),
            "image_size": list(self.image_size),
            "scale": self.scale,
            "cursor": self.cursor.to_dict(),
            "active_window": self.active_window.to_dict() if self.active_window else None,
            "windows": [w.to_dict() for w in self.windows],
            "elements": [e._dict() for e in self.elements],
            "notes": list(self.notes),
        })


def _for_model(data: Dict[str, Any]) -> Dict[str, Any]:
    """``data`` with the unambiguous (long) secrets scrubbed out of its strings: what the model
    reads must stay correct, so short secrets (PINs) never alter names, titles or numbers."""
    return scrub_data(data, min_len=MODEL_MIN_SCRUB)


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
        el.value = "***" if raw not in (None, "") and is_password(node) else mask_value(raw)
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
    # The stable part of the title: a spinner or dirty marker animating in a window title
    # (selectors already tolerate it) must not reset the prefix cache on every selector.
    from .._nodeinfo import stable_title

    return (_safe(lambda: root.handle), _safe(lambda: root.role), stable_title(_safe(lambda: root.name, "") or ""),
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


def _walk_without_documents(root: AccessibleNode, max_depth: int) -> Any:
    """Pre-order walk of ``root`` that skips every ``document`` subtree (web page content)."""
    from .._cancel import check

    stack = [(root, 0)]
    while stack:
        check()
        node, depth = stack.pop()
        if node is not root and (_safe(lambda: node.role, "") or "").lower() == "document":
            continue
        yield node
        if depth < max_depth:
            try:
                kids = list(node.children())
            except Exception:
                kids = []
            stack.extend((k, depth + 1) for k in reversed(kids))


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
    reuse: Optional[Observation] = None,
    prune_documents: Optional[Sequence[int]] = None,
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
        reuse: A previous observation (same options) whose elements may be reused for
            windows that did not change: only with a backend that reports
            accessibility change events (UI Automation on Windows), only for windows
            whose process -- and every process whose content the window hosts (a
            Store/UWP app inside ApplicationFrameHost, see :data:`HOST_PROCESSES`) --
            raised no event since then and whose rectangle and title are
            unchanged, and only within :data:`REUSE_MAX_AGE` seconds. Occlusion is
            recomputed. Without events this does nothing.
        prune_documents: Process ids (browsers connected over DevTools) whose windows are
            walked without their ``document`` subtrees -- the page content, read far
            more cheaply with ``browser_observe``. Their tabs and toolbars are kept.

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
    taken = _time.monotonic()
    budget_end = None if time_budget is None else taken + max(0.0, float(time_budget))
    partial: List[str] = []
    unscanned: List[str] = []
    pruned: List[str] = []

    # Reuse of unchanged windows (change events say which processes changed since `reuse`).
    try:
        tracker = backend.change_tracker()
    except Exception:
        tracker = None
    gen0 = tracker.generation if tracker is not None else None  # read before walking
    opts = (bool(interactive_only), int(max_depth), region, bool(selectors), tuple(sorted(set(prune_documents or ()))))
    dirty = None
    if (reuse is not None and tracker is not None and getattr(reuse, "_gen", None) is not None
            and getattr(reuse, "_opts", None) == opts and taken - getattr(reuse, "_taken", 0.0) <= REUSE_MAX_AGE):
        dirty = tracker.dirty_since(reuse._gen)
    prev_groups: Dict[Any, Dict[str, Any]] = getattr(reuse, "_groups", {}) if dirty is not None else {}
    groups: Dict[Any, Dict[str, Any]] = {}
    reused = 0

    for idx, root in enumerate(roots):
        if len(elements) >= limit:
            break
        handle = _safe(lambda: root.handle)
        info = titles.get(handle)
        win_title = info.title if info is not None else (_safe(lambda: root.name, "") or "")
        app = info.app if info is not None else ""
        zi = z_index.get(handle)
        start_len = len(elements)
        g = prev_groups.get(handle) if handle is not None else None
        # Events are attributed to the *sending* process, which for a Store/UWP window is
        # not the window's owner (ApplicationFrameHost): check every process it hosts, now
        # and at the previous observe (a suspended app's CoreWindow detaches).
        pids = _window_pids(backend, info) if tracker is not None and info is not None else None
        if (g is not None and info is not None and pids is not None and g.get("pids") is not None
                and dirty is not None and not ((pids | g["pids"]) & dirty)
                and g["complete"] and g["rect"] == info.rect and g["title"] == win_title):
            for old in g["elements"]:
                if len(elements) >= limit:
                    break
                st = set(old.states) - {"occluded"}
                if zi is not None and any(r.contains(old.bounds.center) for _, r in front[:zi]):
                    st.add("occluded")
                elements.append(_dc_replace(
                    old, id=len(elements) + 1, states=frozenset(st),
                    selector=LAZY_SELECTOR if selectors else "",
                    _selector_source=(lambda n=old.node: _selector_in(n, ctx)) if selectors else None,
                ))
            groups[handle] = {"rect": info.rect, "title": win_title, "elements": elements[start_len:],
                              "complete": len(elements) - start_len == len(g["elements"]), "pids": pids}
            reused += 1
            continue
        complete = True
        win_end = None
        if budget_end is not None:
            now = _time.monotonic()
            left = budget_end - now
            if left <= 0:
                unscanned.append(win_title)
                continue
            # Front windows may use half of what is left; later ones at least a fair share.
            win_end = now + max(left / (len(roots) - idx), left * 0.5)
        prune = bool(prune_documents) and info is not None and info.pid in set(prune_documents)
        if prune:
            pruned.append(win_title)
        for node in (_walk_without_documents(root, max_depth) if prune else root.walk(max_depth=max_depth)):
            if len(elements) >= limit:
                complete = False
                break
            if win_end is not None and _time.monotonic() >= win_end:
                partial.append(win_title)
                complete = False
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
        if handle is not None and info is not None:
            groups[handle] = {"rect": info.rect, "title": win_title, "elements": elements[start_len:],
                              "complete": complete, "pids": pids}

    # ---------------------------------------------------------------- screenshot
    shot = None
    annotated_img = None
    scale = 1.0
    blanked = False
    window_captured = False
    if screenshot:
        tw, th = fit_image_size(region.width, region.height, max_long_edge, max_visual_tokens)
        scale = tw / float(region.width) if region.width else 1.0
        raw = None
        if target_window is not None:
            raw = _capture_covered_window(target_window, region)
            window_captured = raw is not None
        if raw is None:
            raw = desktop.screenshot(region)
            if target_window is not None:
                raw, blanked = _blank_other_windows(raw, backend, target_window, region)
        if raw.size != (tw, th):
            from PIL import Image as _PILImage

            resample = getattr(_PILImage, "Resampling", _PILImage).LANCZOS
            shot = raw.resize((tw, th), resample)
        else:
            shot = raw
        if annotate:
            from ..vision import annotate as _annotate

            # A window captured off-screen shows its covered elements too, so they get boxes.
            boxes = [(el.bounds, str(el.id)) for el in elements if window_captured or not el.occluded]
            annotated_img = _annotate(shot, boxes, color=None, offset=(region.left, region.top), scale=scale) if boxes else shot.copy()
    shown_windows, shown_active = all_windows, active
    if target_window is not None:
        # Observing one window shows that window only: the titles of the other windows
        # (and which one has the focus) are not the model's business.
        th_ = target_window.handle
        shown_windows = [w for w in all_windows if w.handle == th_] or [_safe(lambda: target_window.info)]
        shown_windows = [w for w in shown_windows if w is not None]
        shown_active = active if active is not None and active.handle == th_ else None
    obs = Observation(
        screenshot=shot,
        elements=elements,
        windows=shown_windows,
        active_window=shown_active,
        cursor=desktop.position(),
        screen_size=(region.width, region.height),
        scale=scale,
        annotated=annotated_img,
        region=region,
    )
    obs.capped = limit > 0 and len(elements) >= limit
    obs._max_id = len(elements)
    obs._groups, obs._gen, obs._opts, obs._taken = groups, gen0, opts, taken
    obs.reused_windows = reused
    if pruned:
        shown = ", ".join(_quote(t) for t in pruned[:5]) + (f" and {len(pruned) - 5} more" if len(pruned) > 5 else "")
        obs.notes.append(f"Page content of the browser window(s) {shown} is not listed here: read and act on it "
                         "with browser_observe / browser_click / browser_type (faster and more precise).")
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
    if target_window is not None and (active is None or active.handle != target_window.handle):
        obs.notes.append("This window is not in the foreground (another window has the keyboard focus): "
                         "keyboard input goes to that other window until this one is focused.")
    if blanked:
        obs.notes.append("Parts of this window covered by other windows are blanked out in the screenshot.")
    if window_captured:
        obs.notes.append("Other windows cover part of this window: the screenshot shows the window's own content "
                         "(captured off-screen). A click at screenshot coordinates in a covered part would reach "
                         "the window in front; act on element ids (they use the elements' own actions).")
    obs._ctx = ctx
    return obs


def _capture_covered_window(window: Any, region: Rect) -> Any:
    """The part ``region`` of ``window`` captured off-screen (its own content, not what covers it),
    or ``None`` when the window is in plain view, the backend cannot capture windows, or the
    capture fails / comes back blank / no longer matches ``region`` (the caller then grabs
    the screen and blanks the covering windows out)."""
    try:
        if window.is_minimized or not window.is_covered():
            return None
        img, how = window._capture("auto")
    except Exception:
        return None
    if how != "window":
        return None
    r = window.rect
    if img.size != (r.width, r.height) or r.intersect(region) != region:
        return None
    x0, y0 = region.left - r.left, region.top - r.top
    return img.crop((x0, y0, x0 + region.width, y0 + region.height))


def _blank_other_windows(img: Any, backend: Any, window: Any, region: Rect) -> Tuple[Any, bool]:
    """``img`` (a capture of ``region``) with every window in front of ``window`` painted over:
    a window-scoped observation must not show the content of other applications."""
    try:
        covers = list(backend.windows_above(window.handle))
    except Exception:
        covers = []
    hits = [r.intersect(region) for r in covers]
    hits = [r for r in hits if r is not None and r.width > 0 and r.height > 0]
    if not hits:
        return img, False
    from PIL import ImageDraw

    out = img.copy()
    draw = ImageDraw.Draw(out)
    for r in hits:
        x0, y0 = r.left - region.left, r.top - region.top
        draw.rectangle([x0, y0, x0 + r.width - 1, y0 + r.height - 1], fill=(96, 96, 96))
    return out, True


__all__ = [
    "MAX_LONG_EDGE",
    "MAX_VISUAL_TOKENS",
    "Observation",
    "ObservedElement",
    "fit_image_size",
    "observe",
    "visual_tokens",
]
