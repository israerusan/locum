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

Opt-in keyboard walk (``tab_walk=True`` / ``--tab-walk``): presses **Tab only**
in one window, bound to it (:mod:`locum._target`) under the desktop input lock,
records the focus order and adds the ``tab-*`` rules (``tab-trap``,
``tab-escape``, ``tab-focus-lost``, ``tab-focus-invisible``,
``tab-focus-unnamed``, ``tab-order-backwards``, ``tab-unreachable``,
``tab-order-asymmetric``). It is the only part of the audit that sends input;
see :func:`tab_walk`.

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

__all__ = ["audit", "tab_walk", "AuditReport", "Finding", "TabStop", "TabWalk", "RULES", "TAB_RULES",
           "SEVERITIES", "DEFAULT_MIN_TARGET_SIZE", "DEFAULT_MAX_TABS"]

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
    # Keyboard walk (tab_walk=True / --tab-walk only).
    "tab-trap": ("error", "Tab stops moving the focus, or loops among a subset of the controls"),
    "tab-escape": ("error", "Tab moved the keyboard focus out of the window"),
    "tab-focus-lost": ("warning", "after Tab no element has the keyboard focus"),
    "tab-focus-invisible": ("warning", "Tab focuses an element that is not visible"),
    "tab-focus-unnamed": ("warning", "Tab focuses an element without an accessible name"),
    "tab-order-backwards": ("info", "Tab order jumps backwards against the reading order"),
    "tab-unreachable": ("warning", "focusable control that Tab never reaches"),
    "tab-order-asymmetric": ("warning", "Shift+Tab does not retrace the Tab order"),
}

#: Rules produced only by the keyboard walk.
TAB_RULES = frozenset(r for r in RULES if r.startswith("tab-"))

#: Default maximum number of Tab presses of a keyboard walk.
DEFAULT_MAX_TABS = 200

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
    #: The keyboard walk's result (``tab_walk=True`` only).
    tab_walk: Optional["TabWalk"] = None

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
            **({"tab_walk": self.tab_walk.to_dict()} if self.tab_walk is not None else {}),
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
        if self.tab_walk is not None:
            lines.extend(self.tab_walk.format_lines())
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
    tab_walk: bool = False,
    max_tabs: int = DEFAULT_MAX_TABS,
    reverse_check: bool = False,
    policy: Any = None,
    confirm: Any = None,
    settle: float = 0.05,
) -> AuditReport:
    """Audit the accessibility of ``window`` (or of every window) without touching the UI.

    With ``tab_walk=True`` the audit is no longer read-only: after the static
    rules it runs :func:`tab_walk` on ``window`` (required; one window), which
    presses Tab (and with ``reverse_check`` Shift+Tab) in it, and adds the
    ``tab-*`` findings and the recorded focus order (:attr:`AuditReport.tab_walk`).
    ``max_tabs``, ``reverse_check``, ``policy``, ``confirm`` and ``settle`` are
    passed to :func:`tab_walk`.

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
    if tab_walk:
        _check_walk_window(window)
        if int(max_tabs) < 1:
            raise ValueError("max_tabs must be >= 1")

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

    walked: Dict[int, List[Tuple[_Snap, int]]] = {}
    for root in targets:
        title = root.name
        report.windows.append(title)
        nodes: List[Tuple[_Snap, int]] = []
        walked[id(root)] = nodes
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
    if tab_walk and targets:
        root = targets[0]
        snapshot = None if report.truncated else [n for n, _d in walked.get(id(root), [])]
        walk = _run_tab_walk(desktop, window, max_tabs=int(max_tabs), reverse_check=reverse_check,
                             policy=policy, confirm=confirm, settle=settle, selectors=selectors,
                             sel_roots=sel_roots, prefix_cache=prefix_cache, snapshot=snapshot,
                             rules=enabled_rules, title=root.name)
        report.tab_walk = walk
        report.findings.extend(walk.findings)
        report.notes.extend(walk.notes)
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


# =========================================================================== keyboard walk
#
# The only part of the audit that sends input. Every key it sends is Tab (and
# Shift+Tab with ``reverse_check``), bound to the audited window: the presses go
# through ``desktop.press`` (a GuardedDesktop judges them against its policy) while
# the desktop input lock is held and ``locum._target`` refuses any press once
# another window is in the foreground.

_EDIT_ROLES = frozenset({"edit", "document"})


@dataclass
class TabStop:
    """One element the keyboard focus reached during :func:`tab_walk`."""

    #: 1-based number of the press that focused it.
    index: int
    role: str
    name: str
    element: str
    selector: Optional[str] = None
    bounds: Optional[Rect] = None
    visible: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "role": self.role,
            "name": self.name,
            "element": self.element,
            "selector": self.selector,
            "bounds": self.bounds.to_dict() if self.bounds is not None else None,
            "visible": self.visible,
        }


@dataclass
class TabWalk:
    """Result of :func:`tab_walk`: the focus order and the ``tab-*`` findings."""

    window: str = ""
    #: Distinct elements in the order Tab reached them.
    stops: List[TabStop] = field(default_factory=list)
    #: Tab presses sent (forward walk).
    presses: int = 0
    #: Shift+Tab presses sent (``reverse_check``).
    reverse_presses: int = 0
    #: The focus came back to the first stop: the whole cycle was seen.
    complete: bool = False
    #: Why the walk ended: ``cycle``, ``max_tabs``, ``trap``, ``escape``, ``focus-lost``,
    #: ``interrupted`` (another window took the focus), ``refused`` (the policy),
    #: ``dry-run`` (a dry-run policy: nothing was pressed) or ``no-focus`` (nothing was
    #: focused before the first press, and Tab did not focus anything either).
    stopped: str = ""
    #: Shift+Tab retraced the whole cycle (``None``: not checked).
    reverse_ok: Optional[bool] = None
    #: The element focused before the walk has the focus again (``None``: nothing was focused).
    restored_focus: Optional[bool] = None
    findings: List[Finding] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window": self.window,
            "presses": self.presses,
            "reverse_presses": self.reverse_presses,
            "complete": self.complete,
            "stopped": self.stopped,
            "reverse_ok": self.reverse_ok,
            "restored_focus": self.restored_focus,
            "stops": [s.to_dict() for s in self.stops],
        }

    def format_lines(self) -> List[str]:
        state = "cycle complete" if self.complete else f"stopped: {self.stopped or 'unknown'}"
        n, p = len(self.stops), self.presses
        lines = [f"tab order of {self.window!r} ({n} stop{'s' if n != 1 else ''}, "
                 f"{p} Tab press{'es' if p != 1 else ''}, {state}):"]
        for s in self.stops:
            flag = "" if s.visible else "  (not visible)"
            lines.append(f"  {s.index:>3}. {s.element}{flag}")
        return lines


def _check_walk_window(window: Any) -> None:
    from .window import Window

    if window is None or (isinstance(window, str) and not window.strip()):
        raise ValueError("the keyboard walk (tab_walk) needs one target window: it presses Tab, which must "
                         "never reach whatever window happens to have the focus")
    if not isinstance(window, (str, Window)) and not hasattr(window, "activate"):
        raise ValueError("the keyboard walk (tab_walk) needs a window title or a Window, not an element or node")


def _snapshot(root: AccessibleNode, max_depth: int = 32, max_nodes: int = 20000) -> List[_Snap]:
    out: List[_Snap] = []
    stack: List[Tuple[_Snap, int]] = [(_Snap(root), 0)]
    while stack and len(out) < max_nodes:
        n, depth = stack.pop()
        out.append(n)
        if depth < max_depth:
            stack.extend((k, depth + 1) for k in reversed(n.children()))
    return out


def tab_walk(
    desktop: Any = None,
    window: Any = None,
    *,
    max_tabs: int = DEFAULT_MAX_TABS,
    reverse_check: bool = False,
    policy: Any = None,
    confirm: Any = None,
    settle: float = 0.05,
    selectors: bool = True,
    rules: Optional[Iterable[str]] = None,
) -> TabWalk:
    """Walk ``window``'s keyboard focus order by pressing Tab (this **sends input**).

    The window is activated and must then be in the foreground. Every press is
    bound to it (:mod:`locum._target`, strict): if another window takes the
    focus, the next press is not sent and the walk stops (``stopped="interrupted"``).
    The whole walk holds the desktop input lock (:mod:`locum._inputlock`), so no
    other locum worker interleaves input. Nothing but Tab is pressed -- and
    Shift+Tab with ``reverse_check`` -- and the element focused before the walk
    is focused again afterwards through the accessibility API (no key, no click).

    The walk records the element focused after each press and stops when the
    focus returns to the first stop (a complete cycle), after ``max_tabs``
    presses, or at the first trap / escape / lost focus. Findings (``tab-*``
    rules, see :data:`RULES`):

    * ``tab-trap`` (error): Tab leaves the focus where it was (a field that takes
      Tab as text: the walk stops right away), or loops among a subset of controls.
    * ``tab-escape`` (error): the focus left the window.
    * ``tab-focus-lost`` (warning): no element has the focus after a press.
    * ``tab-focus-invisible`` / ``tab-focus-unnamed`` (warning): the focused
      element is offscreen/invisible / has no accessible name.
    * ``tab-order-backwards`` (info): the next stop lies before the previous
      one in reading order (above it and not to its right, or left of it on the
      same row) -- a heuristic.
    * ``tab-unreachable`` (warning, complete cycle only): an enabled, visible,
      focusable control (button, edit, checkbox...) that Tab never reached.
    * ``tab-order-asymmetric`` (warning, ``reverse_check``): Shift+Tab does not
      retrace the cycle backwards.

    Args:
        desktop: A :class:`~locum.Desktop` or a
            :class:`~locum.llm.safety.GuardedDesktop` (its policy judges every press).
        window: Title query or :class:`~locum.window.Window` (required).
        max_tabs: Maximum Tab presses.
        reverse_check: After a complete cycle, press Shift+Tab once per stop.
        policy: A :class:`~locum.llm.safety.Policy`: the presses go through a
            ``GuardedDesktop`` with it (``confirm``: its confirmation callback).
        settle: Seconds to wait after each press before reading the focus.
        selectors: Compute a selector for every stop.
        rules: Only report these rule ids (default: every ``tab-*`` rule).
    """
    if desktop is None:
        from .desktop import Desktop

        desktop = Desktop()
    _check_walk_window(window)
    if int(max_tabs) < 1:
        raise ValueError("max_tabs must be >= 1")
    enabled = set(TAB_RULES) if rules is None else set(rules)
    unknown = enabled - set(RULES)
    if unknown:
        raise ValueError(f"unknown rule(s): {', '.join(sorted(unknown))}; known: {', '.join(RULES)}")
    return _run_tab_walk(desktop, window, max_tabs=int(max_tabs), reverse_check=reverse_check, policy=policy,
                         confirm=confirm, settle=settle, selectors=selectors, sel_roots=None, prefix_cache=None,
                         snapshot="auto", rules=enabled, title=None)


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if a is b:
        return True
    return bool(_safe(lambda: a.same_as(b), False))


def _index_of(seen: Sequence[Any], node: Any) -> Optional[int]:
    for i, s in enumerate(seen):
        if _same(s, node):
            return i
    return None


def _editable_value(node: Any) -> Optional[str]:
    """The text of a (non-password) edit/document, to notice a Tab typed into it as text."""
    if node is None:
        return None
    from ._nodeinfo import is_password

    role = (_safe(lambda: node.role, "") or "").lower()
    if role not in _EDIT_ROLES or _safe(lambda: is_password(node), True):
        return None
    v = _safe(lambda: node.value)
    return v if isinstance(v, str) else None


def _in_window(backend: Any, handle: Any, node: Any, root: Any) -> Optional[bool]:
    """Whether keyboard focus is still in the window: the foreground window belongs to it
    and (when the backend gives the focused node a parent chain) the node lies in its tree."""
    from ._nodeinfo import ancestors

    try:
        fg = backend.foreground_handle()
    except Exception:
        return None
    if fg is None:
        return False  # no foreground window at all
    if not (fg == handle or bool(_safe(lambda: backend.window_belongs_to(fg, handle), False))):
        return False
    if node is not None and root is not None and not _same(node, root):
        chain = _safe(lambda: ancestors(node), []) or []
        if chain and not any(_same(a, root) for a in chain):
            # The chain may end at a pop-up the window owns (a menu): accept that.
            top = _safe(lambda: chain[-1].handle)
            if top is None or not (top == handle or _safe(lambda: backend.window_belongs_to(top, handle), False)):
                return False
    return True


def _backwards(prev: Rect, cur: Rect, tol: int = 4) -> Optional[str]:
    """How ``cur`` lies before ``prev`` in (left-to-right, top-to-bottom) reading order, if it does."""
    if cur.bottom <= prev.top - tol and cur.left < prev.right - tol:
        return "above"
    overlap = min(cur.bottom, prev.bottom) - max(cur.top, prev.top)
    if overlap > min(cur.height, prev.height) / 2 and cur.right <= prev.left - tol:
        return "left of"
    return None


def _run_tab_walk(desktop: Any, window: Any, *, max_tabs: int, reverse_check: bool, policy: Any, confirm: Any,
                  settle: float, selectors: bool, sel_roots: Optional[List[AccessibleNode]],
                  prefix_cache: Optional[List[Any]], snapshot: Any, rules: set,
                  title: Optional[str]) -> TabWalk:
    from . import _inputlock, _target
    from .errors import NotSupported, PolicyViolation, TargetLost

    d = desktop
    if policy is not None:
        from .llm.safety import GuardedDesktop

        d = GuardedDesktop(desktop, policy, confirm=confirm)
    raw_desktop = getattr(d, "unwrapped", d)
    backend = raw_desktop.backend
    w = d.window(window, timeout=0) if isinstance(window, str) else window
    w.activate()
    rw = getattr(w, "unwrapped", w)
    target = _target.for_window(rw, strict=True)
    if target is None:
        raise NotSupported("the keyboard walk needs a window handle to bind the Tab presses to; "
                           "this backend gives none (nothing was sent)")
    handle = target.handle
    fg = _safe(backend.foreground_handle)
    if fg is None or not (fg == handle or _safe(lambda: backend.window_belongs_to(fg, handle), False)):
        raise TargetLost(f"could not bring {rw.title!r} to the foreground (another window keeps the focus); "
                         "no Tab was sent")
    root = _safe(lambda: rw.element.node)
    walk = TabWalk(window=title if title is not None else (_safe(lambda: rw.title, "") or ""))
    if snapshot == "auto":
        snapshot = _snapshot(root) if root is not None else None
    cache: List[Any] = prefix_cache if prefix_cache is not None else []
    seen: List[Any] = []  # raw focused nodes, in stop order

    def add(node: Any, rule: str, message: str, severity: Optional[str] = None) -> None:
        if rule not in rules:
            return
        snap = node if isinstance(node, _Snap) else (_Snap(node) if node is not None else None)
        if snap is None and root is not None:
            snap = _Snap(root)
        sel = _selector(snap, sel_roots, cache) if selectors and snap is not None else None
        walk.findings.append(Finding(
            rule=rule, severity=severity or RULES[rule][0], message=message,
            element=_describe(snap) if snap is not None else "window", selector=sel,
            bounds=snap.bounds if snap is not None else None, role=snap.role if snap is not None else "",
            name=snap.name if snap is not None else "", window=walk.window,
        ))

    def pause() -> None:
        if settle and settle > 0:
            sleeper = getattr(raw_desktop, "sleep", None)
            if callable(sleeper):
                sleeper(settle)
            else:  # pragma: no cover - every Desktop has sleep()
                time.sleep(settle)

    def label(node: Any) -> str:
        return _describe(_Snap(node)) if node is not None else "nothing"

    def press(key: str, n: int) -> bool:
        try:
            d.press(key)
        except TargetLost as exc:
            walk.stopped = "interrupted"
            walk.notes.append(f"tab walk stopped before {key} press {n}: {exc}")
            return False
        except PolicyViolation as exc:
            walk.stopped = "refused"
            walk.notes.append(f"tab walk stopped: the policy refused {key} press {n}: {exc}")
            return False
        pause()
        return True

    if getattr(getattr(d, "policy", None), "dry_run", False):
        walk.stopped = "dry-run"
        walk.notes.append("the safety policy is in dry-run mode: no Tab was sent, so there is no focus order")
        return walk
    initial = _safe(backend.focused_element)
    try:
        with _inputlock.desktop_input(raw_desktop), _target.bind(target):
            _forward(walk, seen, max_tabs, backend, handle, root, press, add, label, selectors, sel_roots, cache)
            _backward_jumps(walk, seen, add)
            if walk.complete and snapshot is not None:
                for n in snapshot:
                    if root is not None and n.same_as(root):
                        continue
                    if (n.role in _FOCUS_ROLES and _visible(n) and _enabled(n) and "focusable" in n.states
                            and not _in_roving_container(n) and not _is_chrome(n)
                            and _index_of(seen, n._raw) is None):
                        add(n, "tab-unreachable", f"{n.role} is focusable but Tab never reaches it: "
                                                  "keyboard-only users cannot get to it")
            elif not walk.complete:
                walk.notes.append("tab-unreachable not checked: the tab walk did not see a whole cycle")
            if reverse_check and walk.complete and seen:
                walk.reverse_ok = True
                for k, want in enumerate(reversed(seen), 1):
                    if not press("shift+tab", k):
                        walk.reverse_ok = None
                        break
                    walk.reverse_presses += 1
                    got = _safe(backend.focused_element)
                    if not _same(got, want):
                        walk.reverse_ok = False
                        add(got if got is not None else want, "tab-order-asymmetric",
                            f"Shift+Tab press {k} focused {label(got)}, expected {label(want)} (the Tab order "
                            "backwards): the reverse order differs from the forward order")
                        break
            elif reverse_check:
                walk.notes.append("reverse (Shift+Tab) check skipped: the tab walk did not see a whole cycle")
    finally:
        walk.restored_focus = _restore_focus(backend, initial)
        if walk.restored_focus is False:
            walk.notes.append(f"could not give the focus back to {label(initial)} (focused before the tab walk)")
    return walk


def _forward(walk: TabWalk, seen: List[Any], max_tabs: int, backend: Any, handle: Any, root: Any,
             press: Callable[[str, int], bool], add: Callable[..., None], label: Callable[[Any], str],
             selectors: bool, sel_roots: Optional[List[AccessibleNode]], cache: List[Any]) -> None:
    """The forward Tab walk: fills ``walk.stops`` / ``seen`` and sets ``complete`` / ``stopped``."""
    rb =_safe(lambda: root.bounds) if root is not None else None
    for i in range(1, max_tabs + 1):
        before = _safe(backend.focused_element)
        before_value = _editable_value(before)
        if not press("tab", i):
            return
        walk.presses += 1
        node = _safe(backend.focused_element)
        if _in_window(backend, handle, node, root) is False:
            add(node, "tab-escape", f"Tab press {i} moved the keyboard focus out of {walk.window!r} "
                                    f"(from {label(before)} to {label(node)}); the walk stopped")
            walk.stopped = "escape"
            return
        if node is None and before is None and not seen:
            walk.stopped = "no-focus"
            walk.notes.append(f"nothing in {walk.window!r} has the keyboard focus and Tab did not focus anything: "
                              "focus a control of the window (click it) and run the walk again")
            return
        if node is None:
            add(before, "tab-focus-lost", f"after Tab press {i} (from {label(before)}) no element has the "
                                          "keyboard focus; the walk stopped")
            walk.stopped = "focus-lost"
            return
        if before is not None and _same(node, before):
            after_value = _editable_value(node)
            if before_value is not None and after_value is not None and after_value != before_value:
                add(node, "tab-trap", f"{label(node)} takes Tab as text (its text changed): keyboard users "
                                      "cannot Tab out of it; the walk stopped (the inserted Tab was not undone)")
                walk.stopped = "trap"
                return
        idx = _index_of(seen, node)
        if idx == 0:
            walk.complete = True
            walk.stopped = "cycle"
            return
        if idx is not None:
            if idx == len(seen) - 1:
                msg = f"Tab press {i} left the focus on {label(node)}: keyboard users are trapped there"
            else:
                msg = (f"Tab press {i} went back to stop {idx + 1} ({label(node)}) instead of the first stop: "
                       f"the focus loops among stops {idx + 1}-{len(seen)}")
            add(node, "tab-trap", msg + "; the walk stopped")
            walk.stopped = "trap"
            return
        snap = _Snap(node)
        vis = _visible(snap)
        b = snap.bounds
        if vis and b is not None and rb is not None and not rb.is_empty and b.intersect(rb) is None:
            vis = False  # entirely outside its window
        stop = TabStop(index=i, role=snap.role, name=snap.name, element=_describe(snap),
                       selector=_selector(snap, sel_roots, cache) if selectors else None, bounds=b, visible=vis)
        seen.append(node)
        walk.stops.append(stop)
        if not vis:
            add(snap, "tab-focus-invisible", f"Tab press {i} focused {stop.element}, which is not visible: "
                                             "sighted keyboard users lose track of the focus")
        if not snap.name.strip() and snap.role not in ("window", "dialog"):
            add(snap, "tab-focus-unnamed", f"Tab press {i} focused a {snap.role or 'element'} with no accessible "
                                           "name: a screen reader announces only its role")
    walk.stopped = "max_tabs"
    walk.notes.append(f"the focus did not return to the first stop within {max_tabs} Tab presses: "
                      "the tab walk is partial (raise max_tabs / --max-tabs)")


def _backward_jumps(walk: TabWalk, seen: Sequence[Any], add: Callable[..., None]) -> None:
    """``tab-order-backwards`` for the steps of the walk (and the closing step of a complete cycle).

    The walk starts wherever the focus was, so one wrap-around -- the step to the
    stop that comes first in reading order -- is part of every cycle and is not reported.
    """
    stops = walk.stops
    shown = [i for i, s in enumerate(stops) if s.visible and s.bounds is not None]
    if len(shown) < 2:
        return
    first = min(shown, key=lambda i: (stops[i].bounds.top, stops[i].bounds.left))  # type: ignore[union-attr]
    edges = [(i, i + 1) for i in range(len(stops) - 1)]
    if walk.complete and len(stops) > 1:
        edges.append((len(stops) - 1, 0))
    for a, b in edges:
        prev, cur = stops[a], stops[b]
        if b == first or not (prev.visible and cur.visible) or prev.bounds is None or cur.bounds is None:
            continue
        how = _backwards(prev.bounds, cur.bounds)
        if how:
            add(seen[b], "tab-order-backwards", f"Tab goes from {prev.element} to {cur.element}, which lies "
                                                f"{how} it: the focus order may not follow the reading order")


def _restore_focus(backend: Any, initial: Any) -> Optional[bool]:
    """Focus ``initial`` again through the accessibility API (no key press, no click)."""
    if initial is None:
        return None
    if _same(_safe(backend.focused_element), initial):
        return True
    _safe(lambda: initial.focus(), False)
    return _same(_safe(backend.focused_element), initial)
