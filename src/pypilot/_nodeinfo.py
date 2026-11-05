"""Backend-independent helpers that relate accessibility nodes to top-level windows.

Backends disagree on what :attr:`AccessibleNode.handle` means for a node that
is *not* itself a top-level window: the mock backend returns the owning
window's handle, UI Automation returns the control's own (child) ``HWND`` or
nothing (WPF/UWP), and AT-SPI only resolves handles for window/dialog roles.
Code that needs "which window/application does this node belong to" (policy
checks, window activation before a click, ``Element.window``) must therefore
not rely on ``node.handle`` alone. :func:`owning_window` resolves it robustly:

1. the node's (or any ancestor's) handle matching a listed window;
2. the top-most ancestor matched by process id and title / bounds;
3. a single window of the node's process.

:func:`window_at` is the pure-geometry fallback (front-most window whose
rectangle contains a point).
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional, Sequence

from .types import Point, Rect, WindowInfo

__all__ = ["ancestors", "owning_window", "node_app", "window_at", "list_windows_safe", "target_label"]

_MAX_DEPTH = 256


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def list_windows_safe(backend: Any) -> List[WindowInfo]:
    """``backend.list_windows()`` or ``[]`` when unsupported/failing."""
    return list(_safe(lambda: backend.list_windows(), []) or [])


def ancestors(node: Any, limit: int = _MAX_DEPTH) -> List[Any]:
    """Parents of ``node``, nearest first (cycles and runaway trees are cut at ``limit``)."""
    out: List[Any] = []
    seen = {id(node)}
    cur = _safe(lambda: node.parent)
    while cur is not None and len(out) < limit and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        nxt = cur
        cur = _safe(lambda: nxt.parent)
    return out


def _iou(a: Rect, b: Rect) -> float:
    try:
        return float(a.iou(b))
    except Exception:
        return 0.0


def owning_window(
    backend: Any,
    node: Any,
    windows: Optional[Sequence[WindowInfo]] = None,
    *,
    guess: bool = True,
) -> Optional[WindowInfo]:
    """The top-level window that contains ``node``, or ``None`` if it cannot be determined.

    Args:
        backend: The backend (only ``list_windows`` is used, and only when
            ``windows`` is not given).
        node: An :class:`~pypilot.backends.base.AccessibleNode`.
        windows: Pre-fetched ``list_windows()`` result (front-to-back order).
        guess: When the node's process owns several windows and none can be
            matched exactly, return the front-most of them (good enough to
            learn the *application*; pass ``False`` when the exact window
            matters, e.g. before activating it).
    """
    if node is None:
        return None
    wins = list(windows) if windows is not None else list_windows_safe(backend)
    if not wins:
        return None
    by_handle = {}
    for w in wins:
        try:
            by_handle.setdefault(w.handle, w)
        except TypeError:  # unhashable handle
            continue
    chain = [node] + ancestors(node)
    for n in chain:
        h = _safe(lambda: n.handle)
        if h is None:
            continue
        try:
            hit = by_handle.get(h)
        except TypeError:
            hit = None
        if hit is not None:
            return hit

    root = chain[-1]
    # Ancestors right below an application/desktop node are the real top-level windows.
    tops = [n for n in chain if (_safe(lambda: n.role, "") or "") in ("window", "dialog")]
    top = tops[-1] if tops else root
    pid = _safe(lambda: node.pid) or _safe(lambda: top.pid) or _safe(lambda: root.pid)
    title = _safe(lambda: top.name, "") or ""
    bounds = _safe(lambda: top.bounds)
    pool = [w for w in wins if pid and w.pid == pid]
    if title:
        for w in pool or wins:
            if w.title == title:
                return w
    if bounds is not None:
        best = max(pool or wins, key=lambda w: _iou(w.rect, bounds), default=None)
        if best is not None and _iou(best.rect, bounds) > 0.5:
            return best
    if len(pool) == 1:
        return pool[0]
    if pool and guess:
        nb = _safe(lambda: node.bounds)
        if nb is not None:
            c = nb.center
            for w in pool:
                if w.rect.contains(c):
                    return w
        return pool[0]
    return None


def node_app(backend: Any, node: Any, windows: Optional[Sequence[WindowInfo]] = None) -> str:
    """Application (process) name of ``node``: via its window, else its ``app``/``process`` attribute."""
    if node is None:
        return ""
    w = owning_window(backend, node, windows)
    if w is not None and w.app:
        return str(w.app)
    attrs = _safe(lambda: dict(node.attributes()), {}) or {}
    for key in ("app", "process"):
        if attrs.get(key):
            return str(attrs[key])
    return ""


def window_at(backend: Any, point: Point, windows: Optional[Iterable[WindowInfo]] = None) -> Optional[WindowInfo]:
    """Front-most visible, non-minimized window whose rectangle contains ``point``."""
    wins = list(windows) if windows is not None else list_windows_safe(backend)
    for w in wins:
        if not w.is_visible or w.is_minimized:
            continue
        if w.rect.contains(point):
            return w
    return None


#: Roles that end the ancestor walk of :func:`target_label` (top-level containers).
_TOP_ROLES = frozenset({"window", "dialog", "application", "desktop", "frame"})


def _is_actionable(node: Any) -> bool:
    from .selectors import INTERACTIVE_ROLES

    role = (_safe(lambda: node.role, "") or "").lower()
    if role in INTERACTIVE_ROLES:
        return True
    acts = _safe(lambda: list(node.actions()), []) or []
    return any(a in ("invoke", "toggle", "press", "click", "select") for a in acts)


def _label_of(node: Any) -> str:
    name = _safe(lambda: node.name, "") or ""
    if not name:
        value = _safe(lambda: node.value)
        name = value if isinstance(value, str) else ""
    return str(name)


def target_label(node: Any, limit: int = 16) -> str:
    """What activating ``node`` really acts on, as text for policy checks.

    Hit tests and focus often land on an unnamed or decorative child (an icon,
    an image, a text run) inside the control that actually receives the
    click/Enter. This returns the node's own name plus the names of its
    ancestors up to and including the nearest actionable one (button, link,
    menu item...), stopping at the top-level window. Ancestors above the first
    actionable control are not included, so a click on an "Open" button inside
    a "Payments" group does not count as a click on "Payments".

    Names are joined with ``" < "`` (innermost first); empty when nothing has a name.
    """
    if node is None:
        return ""
    names: List[str] = []

    def add(n: Any) -> None:
        label = _label_of(n).strip()
        if label and label not in names:
            names.append(label)

    add(node)
    if _is_actionable(node):
        return " < ".join(names)
    for anc in ancestors(node, limit):
        role = (_safe(lambda: anc.role, "") or "").lower()
        if role in _TOP_ROLES:
            break
        add(anc)
        if _is_actionable(anc):
            return " < ".join(names)
    # No actionable ancestor: only the node itself is the target.
    own = _label_of(node).strip()
    return own
