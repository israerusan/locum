"""Accessibility audit of desktop windows (``locum a11y-audit``).

:func:`audit` walks the accessibility tree of one window (or of every window)
**read-only** -- it never clicks, types or moves the mouse -- and reports
problems that make an application hard to use with a screen reader, the
keyboard alone, or imprecise pointing:

==================  ========  ==================================================
rule                severity  what it flags
==================  ========  ==================================================
unnamed-control     error     button, toggle button, checkbox, radio button,
                              link, menu item, tab, slider, actionable list/tree
                              item... with no accessible name (``warning`` when
                              only a child text names it)
unlabeled-input     error     edit / combobox / spin button with no accessible
                              name (a nearby label that is not associated with
                              it is mentioned)
unnamed-image       info      image with no name (fine if decorative)
unnamed-window      warning   top-level window without a title
not-focusable       warning   enabled control the keyboard cannot focus (only
                              when the backend reports focusability at all)
small-target        warning   enabled control smaller than 24x24 px whose
                              24 px circle overlaps another target (WCAG 2.5.8)
duplicate-name      warning   sibling controls of the same role sharing a name
offscreen-visible   info      control reported offscreen although its bounds
                              lie inside its (on-screen) parent and window
==================  ========  ==================================================

Every finding carries the rule, severity, a one-line element description
(:func:`locum.selectors.describe_node`), a selector that finds the element
again (:func:`locum.selectors.selector_for`) and its bounds. See
``docs/A11Y.md``.

::

    from locum import Desktop
    from locum.a11y import audit

    report = audit(Desktop(), "Paint")
    print(report.format_text())
    if report.failed("error"):
        ...
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .backends.base import AccessibleNode
from .types import Rect

__all__ = ["audit", "AuditReport", "Finding", "RULES", "SEVERITIES", "DEFAULT_MIN_TARGET_SIZE"]

#: Severities, most severe first.
SEVERITIES = ("error", "warning", "info")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

#: WCAG 2.2 SC 2.5.8 "Target Size (Minimum)": 24 by 24 CSS pixels.
DEFAULT_MIN_TARGET_SIZE = 24

#: Rule id -> (default severity, summary).
RULES: Dict[str, Tuple[str, str]] = {
    "unnamed-control": ("error", "interactive control has no accessible name"),
    "unlabeled-input": ("error", "text input has no accessible name or label"),
    "unnamed-image": ("info", "image has no accessible name"),
    "unnamed-window": ("warning", "window has no title"),
    "not-focusable": ("warning", "interactive control cannot receive keyboard focus"),
    "small-target": ("warning", "click target is smaller than the minimum size (WCAG 2.5.8)"),
    "duplicate-name": ("warning", "sibling controls of the same role share a name"),
    "offscreen-visible": ("info", "control is reported offscreen but lies inside its visible parent"),
}

# Controls a user operates directly: they need a name (what a screen reader announces).
_NAMED_ROLES = frozenset({
    "button", "togglebutton", "checkbox", "radiobutton", "link", "menuitem", "tab", "slider",
    "splitbutton", "hyperlink",
})
# Items are interactive only when actionable (a static list of text rows is not).
_ITEM_ROLES = frozenset({"listitem", "treeitem"})
_INPUT_ROLES = frozenset({"edit", "combobox", "spinbutton"})
# Controls the keyboard must be able to reach on their own.
_FOCUS_ROLES = frozenset({"button", "togglebutton", "checkbox", "radiobutton", "link", "edit", "combobox",
                          "slider", "spinbutton", "splitbutton", "hyperlink"})
# Composite widgets that move focus between their children with the arrow keys
# (roving focus): their children are legitimately not focusable one by one.
_ROVING_CONTAINERS = frozenset({"toolbar", "menubar", "menu", "tablist", "list", "tree", "table", "grid",
                                "radiogroup", "statusbar", "titlebar", "scrollbar", "combobox", "spinbutton",
                                "tabpanel", "ribbon", "header"})
# Targets subject to WCAG 2.5.8 (links are excluded: inline links are exempt).
_TARGET_ROLES = frozenset({"button", "togglebutton", "checkbox", "radiobutton", "combobox", "spinbutton",
                           "slider", "tab", "splitbutton"})
# Siblings of these roles may legitimately repeat names (data rows, menu entries).
_DUP_ROLES = frozenset({"button", "togglebutton", "checkbox", "radiobutton", "link", "tab", "edit",
                        "combobox", "slider", "splitbutton"})
_ACTIONS = ("invoke", "toggle", "select", "expand")


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


# =========================================================================== snapshot


class _Snap(AccessibleNode):
    """A read-once view of an accessibility node.

    Real trees answer every property with a cross-process call (UI Automation,
    AT-SPI, AX). The audit reads each node's properties and children once, and
    :func:`~locum.selectors.selector_for` then searches this cached tree in
    pure Python instead of re-querying the application for every finding.
    """

    __slots__ = ("_raw", "_parent", "_props", "_kids")

    def __init__(self, node: AccessibleNode, parent: Optional["_Snap"] = None):
        self._raw = node
        self._parent = parent
        self._props: Dict[str, Any] = {}
        self._kids: Optional[List["_Snap"]] = None

    def _get(self, key: str, fn: Callable[[], Any], default: Any) -> Any:
        try:
            return self._props[key]
        except KeyError:
            v = _safe(fn, default)
            if v is None:
                v = default
            self._props[key] = v
            return v

    @property
    def role(self) -> str:
        return self._get("role", lambda: (self._raw.role or "").lower(), "")

    @property
    def native_role(self) -> str:
        return self._get("native_role", lambda: self._raw.native_role, "")

    @property
    def name(self) -> str:
        return self._get("name", lambda: self._raw.name, "")

    @property
    def value(self) -> Optional[str]:
        return self._get("value", lambda: self._raw.value, None)

    @property
    def automation_id(self) -> str:
        return self._get("automation_id", lambda: self._raw.automation_id, "")

    @property
    def class_name(self) -> str:
        return self._get("class_name", lambda: self._raw.class_name, "")

    @property
    def description(self) -> str:
        return self._get("description", lambda: self._raw.description, "")

    @property
    def bounds(self) -> Optional[Rect]:
        return self._get("bounds", lambda: self._raw.bounds, None)

    @property
    def states(self) -> frozenset:
        return self._get("states", lambda: frozenset(self._raw.states), frozenset())

    @property
    def pid(self) -> Optional[int]:
        return self._get("pid", lambda: self._raw.pid, None)

    @property
    def handle(self) -> Any:
        return self._get("handle", lambda: self._raw.handle, None)

    def attributes(self) -> Dict[str, Any]:
        return self._get("attributes", lambda: dict(self._raw.attributes()), {})

    def actions(self) -> List[str]:
        return self._get("actions", lambda: list(self._raw.actions()), [])

    def children(self) -> List["_Snap"]:
        if self._kids is None:
            kids = _safe(lambda: list(self._raw.children()), []) or []
            self._kids = [_Snap(k, self) for k in kids if k is not None]
        return self._kids

    @property
    def parent(self) -> Optional["_Snap"]:
        return self._parent

    def same_as(self, other: AccessibleNode) -> bool:
        if other is self:
            return True
        o = other._raw if isinstance(other, _Snap) else other
        if o is self._raw:
            return True
        return bool(_safe(lambda: self._raw.same_as(o), False))

    def __repr__(self) -> str:
        return f"<_Snap {self.role!r} {self.name!r}>"


# =========================================================================== report


@dataclass
class Finding:
    """One accessibility problem."""

    rule: str
    severity: str
    message: str
    element: str
    selector: Optional[str] = None
    bounds: Optional[Rect] = None
    role: str = ""
    name: str = ""
    window: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "element": self.element,
            "selector": self.selector,
            "bounds": self.bounds.to_dict() if self.bounds is not None else None,
            "role": self.role,
            "name": self.name,
            "window": self.window,
        }


@dataclass
class AuditReport:
    """Result of :func:`audit`."""

    findings: List[Finding] = field(default_factory=list)
    #: Titles of the audited windows.
    windows: List[str] = field(default_factory=list)
    #: Accessibility nodes examined.
    nodes: int = 0
    elapsed: float = 0.0
    #: The walk stopped early (``time_budget`` / ``max_nodes``); findings are partial.
    truncated: bool = False
    notes: List[str] = field(default_factory=list)
    min_target_size: int = DEFAULT_MIN_TARGET_SIZE

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def at_least(self, severity: str) -> List[Finding]:
        """Findings at ``severity`` or more severe."""
        if severity not in _RANK:
            raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}, got {severity!r}")
        return [f for f in self.findings if _RANK.get(f.severity, 99) <= _RANK[severity]]

    def failed(self, fail_on: str = "error") -> bool:
        """Whether findings at/above ``fail_on`` exist (``"never"`` never fails)."""
        if fail_on == "never":
            return False
        return bool(self.at_least(fail_on))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "windows": list(self.windows),
            "nodes": self.nodes,
            "elapsed": round(self.elapsed, 3),
            "truncated": self.truncated,
            "min_target_size": self.min_target_size,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        c = self.counts()
        n = len(self.findings)
        parts = ", ".join(f"{c[s]} {s}{'s' if c[s] != 1 and s != 'info' else ''}" for s in SEVERITIES)
        wins = len(self.windows)
        return (f"{n} finding{'s' if n != 1 else ''} ({parts}) in {self.nodes} nodes of "
                f"{wins} window{'s' if wins != 1 else ''}, {self.elapsed:.1f}s"
                + (" (truncated)" if self.truncated else ""))

    def format_text(self) -> str:
        """Human-readable report: findings grouped by window, then a summary line."""
        lines: List[str] = []
        current = None
        for f in self.findings:
            if f.window != current:
                current = f.window
                lines.append(f"window {current!r}:" if current else "(no window):")
            lines.append(f"  {f.severity:<7} {f.rule:<17} {f.element}")
            lines.append(f"          {f.message}" + (f"  ->  {f.selector}" if f.selector else ""))
        for note in self.notes:
            lines.append(f"note: {note}")
        lines.append(self.summary())
        return "\n".join(lines)


# =========================================================================== rules


def _visible(n: _Snap) -> bool:
    st = n.states
    if "offscreen" in st or "invisible" in st:
        return False
    b = n.bounds
    return b is not None and not b.is_empty


def _enabled(n: _Snap) -> bool:
    st = n.states
    return "disabled" not in st


def _actionable(n: _Snap) -> bool:
    acts = n.actions()
    if any(a in _ACTIONS for a in acts):
        return True
    return bool({"selectable", "checkable"} & n.states)


def _is_control(n: _Snap) -> bool:
    role = n.role
    if role in _NAMED_ROLES:
        return True
    if role in _ITEM_ROLES:
        return _actionable(n)
    if role == "image":
        return "invoke" in n.actions()  # a clickable image is a button in disguise
    return False


def _in_control(n: _Snap, levels: int = 3) -> bool:
    """Whether ``n`` sits inside a control (an icon inside a button is the button's, not an image)."""
    p, i = n.parent, 0
    while p is not None and i < levels:
        if _is_control(p) or p.role in _INPUT_ROLES:
            return True
        p, i = p.parent, i + 1
    return False


# Window-chrome buttons: reached through the system menu (Alt+Space), not Tab.
_CAPTION_IDS = frozenset({"minimize", "maximize", "restore", "close", "minimizebutton", "maximizebutton",
                          "restorebutton", "closebutton", "help"})


def _is_chrome(n: _Snap, levels: int = 4) -> bool:
    if n.automation_id.casefold() in _CAPTION_IDS:
        return True
    p, i = n.parent, 0
    while p is not None and i < levels:
        if p.role == "titlebar" or "titlebar" in p.automation_id.casefold() or "titlebar" in p.class_name.casefold():
            return True
        p, i = p.parent, i + 1
    return False


def _clipped(n: _Snap, b: Rect, min_size: int, levels: int = 4) -> bool:
    """Whether ``b`` looks cut off by an ancestor's edge (a gallery row scrolled half out of view):
    the visible part is small, the real target is not."""
    p, i = n.parent, 0
    while p is not None and i < levels:
        pb = p.bounds
        if pb is not None:
            if b.height < min_size and (abs(b.top - pb.top) <= 1 or abs(b.bottom - pb.bottom) <= 1):
                return True
            if b.width < min_size and (abs(b.left - pb.left) <= 1 or abs(b.right - pb.right) <= 1):
                return True
        p, i = p.parent, i + 1
    return False


def _child_text(n: _Snap, depth: int = 2) -> str:
    """Name of a descendant text/image that visually labels ``n`` (within ``depth`` levels)."""
    stack = [(k, 1) for k in n.children()]
    while stack:
        k, d = stack.pop(0)
        if k.role in ("text", "image") and k.name.strip():
            return k.name.strip()
        if d < depth:
            stack.extend((c, d + 1) for c in k.children())
    return ""


def _nearby_label(n: _Snap) -> str:
    """Text of the closest preceding sibling ``text`` node (a visual, unassociated label)."""
    p = n.parent
    if p is None:
        return ""
    sibs = p.children()
    idx = next((i for i, s in enumerate(sibs) if s is n), -1)
    for s in reversed(sibs[:idx] if idx > 0 else []):
        if s.role == "text" and s.name.strip():
            return s.name.strip()
        if s.role in _INPUT_ROLES or s.role in _NAMED_ROLES:
            break
    return ""


def _in_roving_container(n: _Snap, levels: int = 4) -> bool:
    p, i = n.parent, 0
    while p is not None and i < levels:
        if p.role in _ROVING_CONTAINERS:
            return True
        p, i = p.parent, i + 1
    return False


def _dist_point_rect(x: float, y: float, r: Rect) -> float:
    dx = max(r.left - x, 0.0, x - (r.left + r.width))
    dy = max(r.top - y, 0.0, y - (r.top + r.height))
    return math.hypot(dx, dy)


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# =========================================================================== audit


def _resolve_roots(desktop: Any, window: Any) -> List[AccessibleNode]:
    if window is None:
        return list(desktop.roots())
    from .element import Element
    from .window import Window

    if isinstance(window, AccessibleNode):
        return [window]
    if isinstance(window, Element):
        return [window.node]
    w = window if isinstance(window, Window) else desktop.window(window, timeout=0)
    return [w.element.node]


def audit(
    desktop: Any = None,
    window: Any = None,
    *,
    max_depth: int = 32,
    time_budget: Optional[float] = 30.0,
    min_target_size: int = DEFAULT_MIN_TARGET_SIZE,
    max_nodes: int = 20000,
    selectors: bool = True,
    rules: Optional[Iterable[str]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> AuditReport:
    """Audit the accessibility of ``window`` (or of every window) without touching the UI.

    Args:
        desktop: A :class:`~locum.Desktop` (default: a new one).
        window: Title query (like ``locum tree --window``), a :class:`~locum.window.Window`,
            :class:`~locum.element.Element` or accessibility node; ``None`` audits every window.
        max_depth: Maximum tree depth below each window.
        time_budget: Seconds after which the walk stops (the report is then marked
            ``truncated``); ``None`` = no limit. Selector generation stops at the
            budget too (later findings get no selector).
        min_target_size: Minimum target size in (physical) pixels for ``small-target``.
        max_nodes: Stop after examining this many nodes.
        selectors: Build a selector for every finding.
        rules: Only run these rule ids (default: all of :data:`RULES`).

    Returns:
        An :class:`AuditReport`; findings are in window, then document order.
    """
    if desktop is None:
        from .desktop import Desktop

        desktop = Desktop()
    if max_depth < 0:
        raise ValueError("max_depth must be >= 0")
    if min_target_size < 0:
        raise ValueError("min_target_size must be >= 0")
    enabled_rules = set(RULES) if rules is None else set(rules)
    unknown = enabled_rules - set(RULES)
    if unknown:
        raise ValueError(f"unknown rule(s): {', '.join(sorted(unknown))}; known: {', '.join(RULES)}")

    t0 = clock()
    deadline = None if time_budget is None else t0 + max(0.0, float(time_budget))
    report = AuditReport(min_target_size=int(min_target_size))
    targets = [_Snap(r) for r in _resolve_roots(desktop, window) if r is not None]

    # Selector search roots: every window (so a selector is unique desktop-wide), with
    # the audited windows' snapshots substituted so their cached trees are reused.
    sel_roots: Optional[List[AccessibleNode]] = None
    if selectors:
        try:
            live = list(desktop.roots())
        except Exception:
            live = []
        sel_roots = []
        for r in live:
            mine = next((t for t in targets if t.same_as(r)), None)
            sel_roots.append(mine if mine is not None else _Snap(r))
        for t in targets:
            if not any(s is t for s in sel_roots):
                sel_roots.append(t)
    prefix_cache: List[Any] = []
    selectors_cut = False

    def over_budget() -> bool:
        return deadline is not None and clock() > deadline

    for root in targets:
        title = root.name
        report.windows.append(title)
        nodes: List[Tuple[_Snap, int]] = []
        stack: List[Tuple[_Snap, int]] = [(root, 0)]
        while stack:
            if report.nodes >= max_nodes or over_budget():
                report.truncated = True
                why = "max_nodes" if report.nodes >= max_nodes else "time budget"
                report.notes.append(f"{why} reached while walking {title!r}: findings are partial")
                break
            n, depth = stack.pop()
            nodes.append((n, depth))
            report.nodes += 1
            if depth < max_depth:
                stack.extend((k, depth + 1) for k in reversed(n.children()))
        raw = _check_window(root, nodes, enabled_rules, int(min_target_size), report)
        for node, rule, severity, message in raw:
            sel = None
            if selectors and not over_budget():
                sel = _selector(node, sel_roots, prefix_cache)
            elif selectors and not selectors_cut:
                selectors_cut = True
                report.notes.append("time budget reached: later findings have no selector")
            report.findings.append(Finding(
                rule=rule, severity=severity, message=message, element=_describe(node), selector=sel,
                bounds=node.bounds, role=node.role, name=node.name, window=title,
            ))
        if report.truncated:
            break
    report.elapsed = clock() - t0
    return report


def _describe(node: _Snap) -> str:
    from .selectors import describe_node

    return _safe(lambda: describe_node(node), node.role or "unknown") or "unknown"


def _selector(node: _Snap, roots: Optional[List[AccessibleNode]], cache: List[Any]) -> Optional[str]:
    from .selectors import selector_for

    return _safe(lambda: selector_for(node, roots=roots, prefix_cache=cache))


def _check_window(
    root: _Snap,
    nodes: Sequence[Tuple[_Snap, int]],
    rules: set,
    min_size: int,
    report: AuditReport,
) -> List[Tuple[_Snap, str, str, str]]:
    """``(node, rule, severity, message)`` for one window, in document order."""
    out: List[Tuple[int, _Snap, str, str, str]] = []
    order = {id(n): i for i, (n, _d) in enumerate(nodes)}

    def add(n: _Snap, rule: str, message: str, severity: Optional[str] = None) -> None:
        if rule in rules:
            out.append((order.get(id(n), 0), n, rule, severity or RULES[rule][0], message))

    if not root.name.strip() and root.role in ("window", "dialog", ""):
        add(root, "unnamed-window", "window has no title: users cannot tell it apart from others")

    # Only judge focusability when the backend reports it for this window at all
    # (otherwise every control would look unfocusable).
    reports_focus = any("focusable" in n.states or "focused" in n.states for n, _d in nodes)
    if "not-focusable" in rules and not reports_focus:
        report.notes.append(f"{root.name!r}: the backend reports no keyboard focusability; not-focusable skipped")

    root_b = root.bounds
    targets: List[_Snap] = []
    for n, _depth in nodes:
        if n is root:
            continue
        role = n.role
        vis = _visible(n)
        if role in _TARGET_ROLES and vis and _enabled(n):
            targets.append(n)
        if not vis:
            if "offscreen" in n.states and _enabled(n) and (role in _NAMED_ROLES or role in _INPUT_ROLES):
                p = n.parent
                b, pb = n.bounds, (p.bounds if p is not None else None)
                if (b is not None and not b.is_empty and pb is not None and root_b is not None
                        and p is not None and _visible(p) and _visible(root)
                        and _inside(b, pb) and _inside(b, root_b)):
                    add(n, "offscreen-visible",
                        f"{role} is reported offscreen, yet lies inside its visible parent: assistive "
                        "tools may skip it")
            continue
        name = n.name.strip()
        if _is_control(n) and not name:
            label = _child_text(n)
            if label:
                add(n, "unnamed-control",
                    f"{role} has no accessible name; only its child text {_short(label)!r} labels it "
                    "(set the control's name)", "warning")
            else:
                add(n, "unnamed-control", f"{role} has no accessible name: a screen reader announces only "
                                          f"\"{role}\"")
        elif role in _INPUT_ROLES and not name:
            label = _nearby_label(n)
            hint = (f"; the nearby text {_short(label)!r} is not associated as its label" if label
                    else "")
            add(n, "unlabeled-input", f"{role} has no accessible name or label{hint}")
        elif role == "image" and not name and not _in_control(n):
            add(n, "unnamed-image", "image has no name (fine if purely decorative; otherwise give it alt text)")
        if (reports_focus and role in _FOCUS_ROLES and _enabled(n) and "focusable" not in n.states
                and "focused" not in n.states and not _in_roving_container(n) and not _is_chrome(n)):
            add(n, "not-focusable", f"{role} cannot receive keyboard focus: keyboard-only users cannot reach it")

    # duplicate-name: siblings of the same role sharing a name.
    if "duplicate-name" in rules:
        seen_parents = set()
        for n, _d in nodes:
            p = n.parent
            if p is None or id(p) in seen_parents or n is root:
                continue
            seen_parents.add(id(p))
            groups: Dict[Tuple[str, str], List[_Snap]] = {}
            for s in p.children():
                if s.role in _DUP_ROLES and s.name.strip() and _visible(s) and id(s) in order:
                    groups.setdefault((s.role, s.name.strip().casefold()), []).append(s)
            for (role, _name), members in groups.items():
                if len(members) > 1:
                    for m in members[1:]:
                        add(m, "duplicate-name",
                            f"{len(members)} sibling {role}s are named {_short(m.name)!r}: users cannot "
                            "tell them apart")

    # small-target (WCAG 2.5.8 with its spacing exception).
    if "small-target" in rules and min_size > 0:
        radius = min_size / 2.0
        rects = [(t, t.bounds) for t in targets if t.bounds is not None]
        small = [(t, b) for t, b in rects
                 if (b.width < min_size or b.height < min_size) and not _clipped(t, b, min_size)]
        for t, b in small:
            cx, cy = b.left + b.width / 2.0, b.top + b.height / 2.0
            crowded = False
            for u, ub in rects:
                if u is t or _contains_or_contained(ub, b):
                    continue
                if _dist_point_rect(cx, cy, ub) < radius:
                    crowded = True
                    break
                if (ub.width < min_size or ub.height < min_size) and math.hypot(
                        cx - (ub.left + ub.width / 2.0), cy - (ub.top + ub.height / 2.0)) < min_size:
                    crowded = True
                    break
            if crowded:
                add(t, "small-target",
                    f"{t.role} is {b.width}x{b.height} px (< {min_size}x{min_size}) and too close to another "
                    "target: hard to hit precisely")

    out.sort(key=lambda r: r[0])
    return [(n, rule, sev, msg) for _i, n, rule, sev, msg in out]


def _inside(inner: Rect, outer: Rect) -> bool:
    return (inner.left >= outer.left and inner.top >= outer.top
            and inner.left + inner.width <= outer.left + outer.width
            and inner.top + inner.height <= outer.top + outer.height)


def _contains_or_contained(a: Rect, b: Rect) -> bool:
    """Nested targets (a split button's arrow inside the button) are one target, not neighbours."""
    return _inside(a, b) or _inside(b, a)
