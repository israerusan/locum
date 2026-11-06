"""UI element selectors.

Three interchangeable notations compile to the same :class:`Selector` object
and are matched against :class:`~locum.backends.base.AccessibleNode` trees.

CSS-like (canonical)
--------------------
::

    button                          role (``*`` = any)
    #okBtn  .Edit                   automation_id / class_name
    [name="OK"] [name=OK]           attribute, bare-word values allowed
    [name*="ok" i]                  ops: = != *= ^= $= ~= (regex) %= (glob); ``i`` = ignore case
    [name]                          attribute present and non-empty
    button "OK"                     shorthand for button[name="OK"]
    "OK"                            any element whose name or value is "OK"
    :enabled :disabled :visible :hidden :focused :checked :unchecked :selected
    :editable :readonly :expanded :collapsed :focusable :active :root
    :has-text("save")               name or value contains (case-insensitive)
    :has(button "OK")  :has(> edit) contains a matching descendant / child
    :not(:disabled)                 negation
    :nth(0) :nth(-1) :first :last   position among the matches *so far*
    window "Calc" > pane button     descendant (space) and child (``>``) combinators
    button "OK", button "Yes"       union

Window and dialog titles are compared ignoring decorations that change on
their own (:func:`locum._nodeinfo.stable_title`): ``window "doc.txt - Notepad"``
also matches ``*doc.txt - Notepad`` and ``window "Pypilot library"`` matches
``◐ Pypilot library`` (an animated terminal spinner). This applies to the
``=``, ``!=``, ``^=``, ``$=`` and ``*=`` tests of ``name`` / ``text`` on
window and dialog nodes; ``~=`` and ``%=`` patterns are matched as written.

UiPath XML
----------
::

    <wnd app='notepad.exe' title='*Notepad' /><ctrl name='Save' role='push button' idx='2' />

``wnd``/``html`` tags select top-level windows, any other tag (``ctrl``,
``webctrl``, ``uia``...) a descendant of the previous tag. Values containing
``*``/``?`` are globs; UiPath roles (``push button``, ``editable text``...) are
mapped to locum roles; ``idx`` is 1-based.

Dict
----
::

    {"role": "button", "name": "OK", "within": {"role": "window", "title": "Calc"}}

Keys are attribute names (``name__contains``, ``name__iregex``... select an
operator), ``within`` / ``child_of`` give an ancestor/parent selector, ``nth``
an index and boolean state keys (``"enabled": True``) map to pseudo-classes.

Matching is lazy and efficient: every :meth:`Selector.find_all` call builds a
cached snapshot of the visited tree so each node's ``children()`` and
properties are fetched at most once. Selectors without positional pseudo
classes are matched right-to-left during a single pre-order walk (which lets
``find_first`` stop at the first hit); positional selectors are evaluated
step by step, walking each tree once per compound step.
"""

from __future__ import annotations

import re
import threading
import time
import weakref
from typing import Any, Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from .backends.base import AccessibleNode
from ._cancel import check as _cancel_check
from ._nodeinfo import stable_title
from .errors import SelectorSyntaxError

__all__ = [
    "Selector",
    "parse",
    "selector_for",
    "tree_text",
    "describe_node",
    "quote",
    "ROLE_ALIASES",
    "UIPATH_ROLES",
    "INTERACTIVE_ROLES",
]

# --------------------------------------------------------------------------- tables

#: Friendly role spellings accepted in selectors -> canonical role.
ROLE_ALIASES: Dict[str, str] = {
    "textbox": "edit",
    "textfield": "edit",
    "input": "edit",
    "entry": "edit",
    "radio": "radiobutton",
    "combo": "combobox",
    "dropdown": "combobox",
    "label": "text",
    "statictext": "text",
    "static": "text",
    "img": "image",
    "hyperlink": "link",
    "push button": "button",
    "pushbutton": "button",
    "check": "checkbox",
    "menu item": "menuitem",
    "list item": "listitem",
    "tree item": "treeitem",
    "tab item": "tab",
    "tabitem": "tab",
}

#: UiPath / MSAA role names -> locum roles.
UIPATH_ROLES: Dict[str, str] = {
    "push button": "button",
    "button": "button",
    "editable text": "edit",
    "edit": "edit",
    "text": "text",
    "static text": "text",
    "check box": "checkbox",
    "radio button": "radiobutton",
    "combo box": "combobox",
    "drop down": "combobox",
    "drop down button": "button",
    "list": "list",
    "list item": "listitem",
    "menu item": "menuitem",
    "menu bar": "menubar",
    "popup menu": "menu",
    "page tab": "tab",
    "page tab list": "tablist",
    "property page": "tabpanel",
    "outline": "tree",
    "outline item": "treeitem",
    "client": "pane",
    "pane": "pane",
    "window": "window",
    "dialog": "dialog",
    "grouping": "group",
    "group": "group",
    "tool bar": "toolbar",
    "status bar": "statusbar",
    "title bar": "titlebar",
    "graphic": "image",
    "image": "image",
    "link": "link",
    "hyperlink": "link",
    "table": "table",
    "row": "row",
    "cell": "cell",
    "column header": "columnheader",
    "row header": "rowheader",
    "slider": "slider",
    "spin button": "spinbutton",
    "progress bar": "progressbar",
    "scroll bar": "scrollbar",
    "separator": "separator",
    "tool tip": "tooltip",
    "tooltip": "tooltip",
    "document": "document",
    "application": "application",
}

#: Roles considered interactive by ``tree_text(interactive_only=True)`` and the LLM layer.
INTERACTIVE_ROLES = frozenset(
    {
        "button", "togglebutton", "checkbox", "radiobutton", "combobox", "edit", "document", "link",
        "listitem", "menuitem", "tab", "treeitem", "slider", "spinbutton", "scrollbar", "cell",
        "columnheader", "rowheader",
    }
)

# canonical attribute keys
_ATTR_ALIASES: Dict[str, str] = {
    "name": "name",
    "title": "name",
    "aaname": "name",
    "id": "automation_id",
    "automation_id": "automation_id",
    "automationid": "automation_id",
    "class": "class_name",
    "cls": "class_name",
    "class_name": "class_name",
    "classname": "class_name",
    "text": "text",
    "app": "app",
    "process": "app",
    "pid": "pid",
    "role": "role",
    "value": "value",
    "description": "description",
    "desc": "description",
    "help": "description",
    "handle": "handle",
    "hwnd": "handle",
}

_OPS = ("=", "!=", "*=", "^=", "$=", "~=", "%=")
_STATE_PSEUDOS = frozenset(
    {
        "enabled", "disabled", "visible", "hidden", "focused", "checked", "unchecked", "selected",
        "editable", "readonly", "expanded", "collapsed", "focusable", "active", "root", "mixed",
    }
)
_PSEUDO_ALIASES = {
    "contains": "has-text", "offscreen": "hidden", "invisible": "hidden", "indeterminate": "mixed",
}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")
#: Operators whose name test also compares window titles without volatile decorations.
_CORE_OPS = frozenset({"=", "!=", "*=", "^=", "$="})
#: Roles whose names are titles that decorate themselves (spinners, dirty markers).
_TITLED_ROLES = frozenset({"window", "dialog"})
_GLOB_CHARS = re.compile(r"[*?]")


def _ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_-" or ord(ch) > 127


def quote(text: str) -> str:
    """Quote ``text`` as a selector string literal (double quotes, backslash escapes)."""
    out = ['"']
    for ch in str(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _glob_to_regex(pattern: str) -> str:
    parts = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def _norm_role(role: str) -> str:
    r = role.strip().lower()
    return ROLE_ALIASES.get(r, r)


def _app_base(app: str) -> str:
    return app.strip().replace("\\", "/").rsplit("/", 1)[-1]


def _norm_app(app: str) -> str:
    a = _app_base(app).lower()
    return a[:-4] if a.endswith(".exe") else a


# =========================================================================== snapshot


class _Entry:
    """A node in the per-query snapshot: caches children and properties."""

    __slots__ = ("node", "parent", "depth", "path", "_children", "_props")

    def __init__(self, node: AccessibleNode, parent: Optional["_Entry"], depth: int, path: Tuple[int, ...]):
        self.node = node
        self.parent = parent
        self.depth = depth
        self.path = path
        self._children: Optional[List["_Entry"]] = None
        self._props: Dict[str, Any] = {}

    def children(self) -> List["_Entry"]:
        if self._children is None:
            try:
                kids = list(self.node.children())
            except Exception:  # stale element / access denied
                kids = []
            self._children = [_Entry(k, self, self.depth + 1, self.path + (i,)) for i, k in enumerate(kids)]
        return self._children

    def prop(self, key: str) -> Any:
        try:
            return self._props[key]
        except KeyError:
            pass
        val = _read_prop(self.node, key)
        self._props[key] = val
        return val

    def is_within(self, other: "_Entry") -> bool:
        """True if ``self`` is ``other`` or one of its descendants."""
        n = len(other.path)
        return len(self.path) >= n and self.path[:n] == other.path


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _read_prop(node: AccessibleNode, key: str) -> Any:
    if key == "name":
        return _safe(lambda: node.name, "") or ""
    if key == "value":
        return _str_or_none(_safe(lambda: node.value))
    if key == "role":
        return (_safe(lambda: node.role, "") or "").lower()
    if key == "native_role":
        return (_safe(lambda: node.native_role, "") or "").lower()
    if key == "automation_id":
        return _safe(lambda: node.automation_id, "") or ""
    if key == "class_name":
        return _safe(lambda: node.class_name, "") or ""
    if key == "description":
        return _safe(lambda: node.description, "") or ""
    if key == "pid":
        return _str_or_none(_safe(lambda: node.pid))
    if key == "handle":
        return _str_or_none(_safe(lambda: node.handle))
    if key == "states":
        return _safe(lambda: frozenset(node.states), frozenset())
    if key == "bounds":
        return _safe(lambda: node.bounds)
    if key == "is_enabled":
        return bool(_safe(lambda: node.is_enabled, False))
    if key == "is_visible":
        return bool(_safe(lambda: node.is_visible, False))
    if key == "attributes":
        return _safe(lambda: dict(node.attributes()), {}) or {}
    raise KeyError(key)


def _extra_attr(entry: _Entry, key: str) -> Optional[str]:
    attrs = entry.prop("attributes")
    if key in attrs:
        return _str_or_none(attrs[key])
    lk = key.lower()
    for k, v in attrs.items():
        if str(k).lower() == lk:
            return _str_or_none(v)
    return None


def _attr_values(entry: _Entry, key: str) -> List[Optional[str]]:
    """Values an attribute key resolves to (``text`` resolves to name *and* value)."""
    if key == "text":
        return [entry.prop("name"), entry.prop("value")]
    if key in ("name", "value", "automation_id", "class_name", "description", "pid", "handle"):
        return [entry.prop(key)]
    if key == "role":
        return [entry.prop("role"), entry.prop("native_role")]
    if key == "app":
        v = _extra_attr(entry, "app")
        if v is None:
            v = _extra_attr(entry, "process")
        return [v]
    return [_extra_attr(entry, key)]


class _Snapshot:
    """Lazily materialised view of the trees under ``roots`` plus match memo tables."""

    def __init__(self, roots: Sequence[AccessibleNode], max_depth: int):
        self.max_depth = int(max_depth)
        self.roots = [_Entry(n, None, 0, (i,)) for i, n in enumerate(roots) if n is not None]
        self.memo: Dict[Tuple[int, int], bool] = {}

    @classmethod
    def over(cls, entries: Sequence[_Entry], max_depth: int) -> "_Snapshot":
        """A snapshot over already-materialised root entries (their cached children and
        properties are reused; the match memo is fresh)."""
        snap = cls((), max_depth)
        snap.roots = list(entries)
        return snap

    def walk(self, start: _Entry, include_self: bool = True) -> Iterator[_Entry]:
        """Pre-order traversal of ``start``'s subtree, honouring ``max_depth``."""
        md = self.max_depth
        if include_self:
            stack = [start]
        else:
            if start.depth >= md:
                return
            stack = list(reversed(start.children()))
        while stack:
            _cancel_check()
            e = stack.pop()
            yield e
            if e.depth < md:
                stack.extend(reversed(e.children()))

    def walk_all(self) -> Iterator[_Entry]:
        for r in self.roots:
            yield from self.walk(r)


# =========================================================================== AST


class _Cond:
    """A simple (non-positional) condition inside a compound."""

    positional = False

    def test(self, entry: _Entry, snap: _Snapshot) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def render(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class _AttrCond(_Cond):
    """``[key op value i]`` (also produced by ``#id``, ``.cls`` and quoted shorthands)."""

    def __init__(self, key: str, op: Optional[str], value: Optional[str] = None, ci: bool = False, form: str = "attr"):
        self.key = key
        self.op = op  # None = presence test
        self.value = value
        self.ci = ci
        self.form = form  # attr | id | class | shorthand | bare
        self._rx: Optional["re.Pattern[str]"] = None
        # Process names are matched case-insensitively whatever the operator
        # (Windows file names are; ``[app="EXCEL.EXE"]`` == ``[app=excel]``).
        self._fold = ci or key == "app"
        if op == "~=":
            self._rx = re.compile(value or "", re.I if self._fold else 0)
        elif op == "%=":
            v = value or ""
            if key == "app":
                v = _norm_app(v)
            self._rx = re.compile(_glob_to_regex(v), re.S | (re.I if self._fold else 0))
        cmp = value if value is not None else ""
        if key == "app" and op in ("=", "!="):
            cmp = _norm_app(cmp)
        self._cmp = cmp.casefold() if self._fold else cmp
        # Window titles: the value without volatile decorations (spinner glyphs, dirty markers).
        self._core: Optional[str] = None
        if key in ("name", "text") and op in _CORE_OPS and value is not None:
            core = stable_title(value)
            self._core = core.casefold() if self._fold else core

    def _one(self, actual: Optional[str]) -> bool:
        op = self.op
        if actual is None:
            return False
        if op is None:
            return actual != ""
        if self.key == "app":
            norm = _norm_app(actual)
            if op in ("=", "!=", "%="):
                return self._cmp_one(norm)
            # Substring/prefix/suffix/regex tests see both the real file name
            # ("saplogon.exe", so $=".exe" works) and the normalised one ("saplogon").
            return self._cmp_one(_app_base(actual)) or self._cmp_one(norm)
        return self._cmp_one(actual)

    def _cmp_one(self, actual: str) -> bool:
        op = self.op
        if self._rx is not None:
            if op == "~=":
                return self._rx.search(actual) is not None
            return self._rx.fullmatch(actual) is not None
        a = actual.casefold() if self._fold else actual
        v = self._cmp
        if op == "=" or op == "!=":
            return a == v
        if op == "*=":
            return v in a
        if op == "^=":
            return a.startswith(v)
        if op == "$=":
            return a.endswith(v)
        raise AssertionError(op)  # pragma: no cover

    def test(self, entry: _Entry, snap: _Snapshot) -> bool:
        vals = _attr_values(entry, self.key)
        hit = any(self._one(v) for v in vals)
        if not hit and self._core is not None and entry.prop("role") in _TITLED_ROLES:
            hit = self._core_hit(entry.prop("name"))
        return not hit if self.op == "!=" else hit

    def _core_hit(self, actual: str) -> bool:
        """The test on the stable parts of a window title (see :func:`stable_title`)."""
        core = stable_title(actual)
        if core == actual and self._core == self._cmp:
            return False  # neither side is decorated: the plain test already answered
        a = core.casefold() if self._fold else core
        v = self._core or ""
        op = self.op
        if op == "=" or op == "!=":
            return a == v
        if op == "*=":
            return v in a
        if op == "^=":
            return a.startswith(v)
        return a.endswith(v)  # $=

    def render(self) -> str:
        v = self.value or ""
        if self.form == "id" and _IDENT_RE.match(v):
            return "#" + v
        if self.form == "class" and _IDENT_RE.match(v):
            return "." + v
        if self.form == "bare":
            return quote(v)
        if self.form == "shorthand":
            return " " + quote(v)
        key = self.key if _IDENT_RE.match(self.key) else quote(self.key)
        if self.op is None:
            return f"[{key}]"
        return f"[{key}{self.op}{quote(v)}{' i' if self.ci else ''}]"


class _StateCond(_Cond):
    def __init__(self, name: str):
        self.name = name

    def test(self, entry: _Entry, snap: _Snapshot) -> bool:
        n = self.name
        if n == "enabled":
            return entry.prop("is_enabled")
        if n == "disabled":
            return not entry.prop("is_enabled")
        if n == "visible":
            return entry.prop("is_visible")
        if n == "hidden":
            return not entry.prop("is_visible")
        if n == "root":
            return entry.parent is None
        st = entry.prop("states")
        if n == "unchecked":
            # A tri-state box in the indeterminate state is neither checked nor unchecked.
            return "checked" not in st and "mixed" not in st and "indeterminate" not in st
        if n == "mixed":
            return "mixed" in st or "indeterminate" in st
        if n == "editable":
            return "editable" in st and "readonly" not in st
        if n == "collapsed":
            return "collapsed" in st or ("expandable" in st and "expanded" not in st)
        return n in st

    def render(self) -> str:
        return ":" + self.name


class _HasTextCond(_Cond):
    def __init__(self, text: str):
        self.text = text
        self._needle = text.casefold()

    def test(self, entry: _Entry, snap: _Snapshot) -> bool:
        for v in (entry.prop("name"), entry.prop("value")):
            if v and self._needle in v.casefold():
                return True
        return False

    def render(self) -> str:
        return f":has-text({quote(self.text)})"


class _HasCond(_Cond):
    def __init__(self, selector: "Selector"):
        self.selector = selector

    def test(self, entry: _Entry, snap: _Snapshot) -> bool:
        if entry.depth >= snap.max_depth:
            return False
        return bool(self.selector._search(snap, entry.children(), boundary=entry, limit=1))

    def render(self) -> str:
        return f":has({self.selector})"


class _NotCond(_Cond):
    def __init__(self, selector: "Selector"):
        self.selector = selector

    def test(self, entry: _Entry, snap: _Snapshot) -> bool:
        return not self.selector._matches_entry(entry, snap, boundary=None)

    def render(self) -> str:
        return f":not({self.selector})"


class _NthCond(_Cond):
    positional = True

    def __init__(self, index: int, label: Optional[str] = None):
        self.index = int(index)
        self.label = label  # "first" / "last" for rendering

    def pick(self, entries: List[_Entry]) -> List[_Entry]:
        i = self.index
        if -len(entries) <= i < len(entries):
            return [entries[i]]
        return []

    def test(self, entry: _Entry, snap: _Snapshot) -> bool:  # positional: never used per node
        return True

    def render(self) -> str:
        if self.label:
            return ":" + self.label
        return f":nth({self.index})"


class _Compound:
    """``role`` plus an ordered list of conditions."""

    def __init__(self, role: Optional[str] = None, conds: Optional[List[_Cond]] = None, star: bool = False):
        self.role = role
        self.conds: List[_Cond] = conds or []
        self.star = star

    @property
    def positional(self) -> bool:
        return any(c.positional for c in self.conds)

    def role_ok(self, entry: _Entry) -> bool:
        if self.role is None:
            return True
        if entry.prop("role") == self.role:
            return True
        native = entry.prop("native_role")
        return native == self.role or native.replace(" ", "") == self.role

    def test(self, entry: _Entry, snap: _Snapshot) -> bool:
        """Match ignoring positional conditions (memoised per snapshot)."""
        key = (id(self), id(entry))
        hit = snap.memo.get(key)
        if hit is None:
            hit = self.role_ok(entry) and all(c.test(entry, snap) for c in self.conds if not c.positional)
            snap.memo[key] = hit
        return hit

    def apply(self, entries: List[_Entry], snap: _Snapshot) -> List[_Entry]:
        """Filter an ordered candidate list, applying positional picks in sequence."""
        cur = [e for e in entries if self.role_ok(e)]
        for c in self.conds:
            if c.positional:
                cur = c.pick(cur)  # type: ignore[attr-defined]
            else:
                cur = [e for e in cur if c.test(e, snap)]
            if not cur:
                break
        return cur

    def render(self) -> str:
        head = self.role or ""
        if head and not _IDENT_RE.match(head):
            head = f"*[role={quote(head)}]"
        if self.star and not head:
            head = "*"
        body = "".join(c.render() for c in self.conds)
        if not head and body.startswith(" "):
            head = "*"
        out = head + body
        return out or "*"


class _Branch:
    """A complex selector: compounds joined by combinators (``" "`` or ``">"``)."""

    def __init__(self, steps: List[Tuple[Optional[str], _Compound]], relative: Optional[str] = None):
        self.steps = steps  # first combinator is None
        self.relative = relative  # ">" for ``:has(> x)``
        self.positional = any(c.positional for _, c in steps)

    def render(self) -> str:
        out = "> " if self.relative == ">" else ""
        for i, (comb, comp) in enumerate(self.steps):
            if i:
                out += " > " if comb == ">" else " "
            out += comp.render()
        return out

    # -- right-to-left matching (no positional pseudos) --------------------------
    def match_rtl(self, entry: _Entry, snap: _Snapshot, boundary: Optional[_Entry], i: Optional[int] = None) -> bool:
        if i is None:
            i = len(self.steps) - 1
        comb, comp = self.steps[i]
        if not comp.test(entry, snap):
            return False
        if i == 0:
            if self.relative == ">":
                return entry.parent is boundary
            return True
        if comb == ">":
            p = entry.parent
            if p is None or p is boundary:
                return False
            return self._match_memo(p, snap, boundary, i - 1)
        p = entry.parent
        while p is not None and p is not boundary:
            if self._match_memo(p, snap, boundary, i - 1):
                return True
            p = p.parent
        return False

    def _match_memo(self, entry: _Entry, snap: _Snapshot, boundary: Optional[_Entry], i: int) -> bool:
        key = (id(self), id(entry), id(boundary), i)
        hit = snap.memo.get(key)  # type: ignore[arg-type]
        if hit is None:
            hit = self.match_rtl(entry, snap, boundary, i)
            snap.memo[key] = hit  # type: ignore[index]
        return hit

    # -- search ----------------------------------------------------------------
    def search(
        self,
        snap: _Snapshot,
        roots: List[_Entry],
        boundary: Optional[_Entry],
        include_roots: bool,
        limit: Optional[int],
    ) -> List[_Entry]:
        if not self.positional:
            out: List[_Entry] = []
            if self.relative == ">" and len(self.steps) == 1:
                # ``:has(> x)``: only the boundary's direct children can match.
                candidates: Iterable[_Entry] = roots
            else:
                # ``:has(> a b)`` / ``:has(> a > b)``: the last compound can be any
                # descendant; match_rtl anchors step 0 to the boundary's children.
                candidates = (e for r in roots for e in snap.walk(r))
            root_ids = None if include_roots else {id(r) for r in roots}
            for e in candidates:
                if root_ids is not None and id(e) in root_ids:
                    continue
                if self.match_rtl(e, snap, boundary):
                    out.append(e)
                    if limit is not None and len(out) >= limit:
                        break
            return out
        return self._search_stepwise(snap, roots, include_roots, limit)

    def _search_stepwise(self, snap: _Snapshot, roots: List[_Entry], include_roots: bool, limit: Optional[int]) -> List[_Entry]:
        last = len(self.steps) - 1
        current: List[_Entry] = []
        for i, (comb, comp) in enumerate(self.steps):
            if i == 0:
                if self.relative == ">":
                    cands = list(roots)
                else:
                    cands = [e for r in roots for e in snap.walk(r)]
                if i == last and not include_roots:
                    root_ids = {id(r) for r in roots}
                    cands = [e for e in cands if id(e) not in root_ids]
            elif comb == ">":
                seen = set()
                cands = []
                for p in current:
                    if p.depth >= snap.max_depth:
                        continue
                    for c in p.children():
                        if id(c) not in seen:
                            seen.add(id(c))
                            cands.append(c)
                cands.sort(key=lambda e: e.path)
            else:
                cands = []
                prev: Optional[_Entry] = None
                for p in sorted(current, key=lambda e: e.path):
                    if prev is not None and p.is_within(prev):
                        continue  # already covered by the ancestor's walk
                    prev = p
                    cands.extend(snap.walk(p, include_self=False))
            current = comp.apply(cands, snap)
            if not current:
                return []
        return current[:limit] if limit is not None else current


# =========================================================================== Selector


class Selector:
    """A compiled selector (a union of complex selectors).

    Build with :func:`parse`. ``str(selector)`` gives the canonical CSS form,
    which parses back to an equivalent selector.
    """

    def __init__(self, branches: List[_Branch], source: Any = None):
        if not branches:
            raise ValueError("A selector needs at least one branch")
        self.branches = branches
        self.source = source
        self._text: Optional[str] = None

    # -- public API ------------------------------------------------------------
    def find_all(
        self,
        roots: Union[AccessibleNode, Sequence[AccessibleNode]],
        *,
        max_depth: int = 64,
        limit: Optional[int] = None,
        include_roots: bool = True,
    ) -> List[AccessibleNode]:
        """All matching nodes under ``roots`` in document (pre-order) order.

        Args:
            roots: Root node(s) to search (typically ``backend.accessibility_roots()``).
            max_depth: Maximum depth below a root (roots are depth 0).
            limit: Stop after this many results (``None`` = all).
            include_roots: Whether the roots themselves may be returned (they can
                always satisfy *ancestor* steps of the selector).
        """
        if limit is not None and limit <= 0:
            return []
        snap = _Snapshot(_as_roots(roots), max_depth)
        return [e.node for e in self._search(snap, snap.roots, None, limit, include_roots)]

    def find_first(
        self,
        roots: Union[AccessibleNode, Sequence[AccessibleNode]],
        max_depth: int = 64,
        *,
        include_roots: bool = True,
    ) -> Optional[AccessibleNode]:
        """The first match in document order, or ``None``."""
        found = self.find_all(roots, max_depth=max_depth, limit=1, include_roots=include_roots)
        return found[0] if found else None

    def matches(self, node: AccessibleNode) -> bool:
        """Whether ``node`` satisfies the last compound of any branch.

        Ancestry (combinators) and positional pseudo-classes are ignored.
        """
        snap = _Snapshot([node], 64)
        entry = snap.roots[0]
        return any(b.steps[-1][1].test(entry, snap) for b in self.branches)

    @property
    def is_positional(self) -> bool:
        return any(b.positional for b in self.branches)

    # -- internals ---------------------------------------------------------------
    def _search(
        self,
        snap: _Snapshot,
        roots: List[_Entry],
        boundary: Optional[_Entry],
        limit: Optional[int],
        include_roots: bool = True,
    ) -> List[_Entry]:
        if len(self.branches) == 1:
            return self.branches[0].search(snap, roots, boundary, include_roots, limit)
        if not any(b.positional or b.relative for b in self.branches):
            # One pre-order walk testing every branch right-to-left: document order
            # for free, no duplicates and early exit once ``limit`` is reached.
            out: List[_Entry] = []
            root_ids = None if include_roots else {id(r) for r in roots}
            for r in roots:
                for e in snap.walk(r):
                    if root_ids is not None and id(e) in root_ids:
                        continue
                    if any(b.match_rtl(e, snap, boundary) for b in self.branches):
                        out.append(e)
                        if limit is not None and len(out) >= limit:
                            return out
            return out
        merged: Dict[int, _Entry] = {}
        for b in self.branches:
            # With several branches the document order of the union is only known
            # after every branch ran, so the limit is applied at the end.
            for e in b.search(snap, roots, boundary, include_roots, None):
                merged.setdefault(id(e), e)
        out = sorted(merged.values(), key=lambda e: e.path)
        return out[:limit] if limit is not None else out

    def _matches_entry(self, entry: _Entry, snap: _Snapshot, boundary: Optional[_Entry]) -> bool:
        return any(b.match_rtl(entry, snap, boundary) for b in self.branches)

    # -- dunder ------------------------------------------------------------------
    def __str__(self) -> str:
        if self._text is None:
            self._text = ", ".join(b.render() for b in self.branches)
        return self._text

    def __repr__(self) -> str:
        return f"Selector({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Selector):
            return str(self) == str(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(str(self))


def _as_roots(roots: Union[AccessibleNode, Sequence[AccessibleNode], None]) -> List[AccessibleNode]:
    if roots is None:
        return []
    if isinstance(roots, AccessibleNode):
        return [roots]
    if not isinstance(roots, (list, tuple)) and hasattr(roots, "node"):
        return [roots.node]  # a single Element-like wrapper
    out = []
    for r in roots:
        # Unwrap Element-like wrappers (Element, ObservedElement), never a node itself:
        # an AccessibleNode subclass may well have an unrelated ``node`` attribute.
        out.append(r if isinstance(r, AccessibleNode) else getattr(r, "node", r))
    return out


# =========================================================================== tokenizer

_T_IDENT = "IDENT"
_T_STRING = "STRING"
_T_HASH = "#"
_T_DOT = "."
_T_LBRACK = "["
_T_RBRACK = "]"
_T_LPAREN = "("
_T_RPAREN = ")"
_T_COLON = ":"
_T_COMMA = ","
_T_GT = ">"
_T_STAR = "*"
_T_OP = "OP"
_T_EOF = "EOF"


class _Tok:
    __slots__ = ("kind", "value", "pos", "ws")

    def __init__(self, kind: str, value: str, pos: int, ws: bool):
        self.kind = kind
        self.value = value
        self.pos = pos
        self.ws = ws  # preceded by whitespace

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.kind} {self.value!r} @{self.pos}{' ws' if self.ws else ''}>"


def _syntax_error(text: str, pos: int, msg: str) -> SelectorSyntaxError:
    pos = max(0, min(pos, len(text)))
    line = text.replace("\n", " ")
    err = SelectorSyntaxError(f"{msg} at position {pos}:\n  {line}\n  {' ' * pos}^")
    err.selector = text  # type: ignore[attr-defined]
    err.position = pos  # type: ignore[attr-defined]
    err.reason = msg  # type: ignore[attr-defined]
    return err


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}


class _Lexer:
    """Context-aware tokenizer for the CSS-like language.

    Two contexts need raw scanning: a bare attribute value after an operator
    (``[app=notepad.exe]``) and the unquoted argument of ``:has-text(...)``.
    The parser requests those explicitly through :meth:`raw_value`.
    """

    def __init__(self, text: str):
        self.text = text
        self.i = 0
        self._peeked: Optional[_Tok] = None

    def error(self, pos: int, msg: str) -> SelectorSyntaxError:
        return _syntax_error(self.text, pos, msg)

    def _skip_ws(self) -> bool:
        start = self.i
        t = self.text
        while self.i < len(t) and t[self.i].isspace():
            self.i += 1
        return self.i > start

    def peek(self) -> _Tok:
        if self._peeked is None:
            self._peeked = self._next()
        return self._peeked

    def next(self) -> _Tok:
        tok = self.peek()
        self._peeked = None
        return tok

    def _read_string(self) -> str:
        t = self.text
        q = t[self.i]
        start = self.i
        self.i += 1
        out = []
        while self.i < len(t):
            ch = t[self.i]
            if ch == "\\":
                if self.i + 1 >= len(t):
                    raise self.error(self.i, "Dangling escape in string")
                nxt = t[self.i + 1]
                if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", t[self.i + 2 : self.i + 6]):
                    out.append(chr(int(t[self.i + 2 : self.i + 6], 16)))
                    self.i += 6
                    continue
                if nxt in _ESCAPES:
                    out.append(_ESCAPES[nxt])
                else:  # unknown escapes stay literal so regexes like "\d+" work unquoted
                    out.append("\\" + nxt)
                self.i += 2
                continue
            if ch == q:
                self.i += 1
                return "".join(out)
            out.append(ch)
            self.i += 1
        raise self.error(start, "Unterminated string")

    def _read_ident(self) -> str:
        t = self.text
        out = []
        while self.i < len(t):
            ch = t[self.i]
            if ch == "\\" and self.i + 1 < len(t):
                out.append(t[self.i + 1])
                self.i += 2
            elif _ident_char(ch):
                out.append(ch)
                self.i += 1
            else:
                break
        return "".join(out)

    def _next(self) -> _Tok:
        ws = self._skip_ws()
        t = self.text
        if self.i >= len(t):
            return _Tok(_T_EOF, "", self.i, ws)
        pos = self.i
        ch = t[pos]
        if ch in "\"'":
            return _Tok(_T_STRING, self._read_string(), pos, ws)
        two = t[pos : pos + 2]
        if two in ("!=", "*=", "^=", "$=", "~=", "%="):
            self.i += 2
            return _Tok(_T_OP, two, pos, ws)
        if ch == "=":
            self.i += 1
            return _Tok(_T_OP, "=", pos, ws)
        if ch in "#.[]():,>*":
            self.i += 1
            return _Tok(ch, ch, pos, ws)
        if _ident_char(ch) or ch == "\\":
            return _Tok(_T_IDENT, self._read_ident(), pos, ws)
        if ch in "+~":
            raise self.error(pos, f"Sibling combinator {ch!r} is not supported")
        raise self.error(pos, f"Unexpected character {ch!r}")

    def raw_value(self, stop: str) -> Tuple[str, int]:
        """Read an unquoted value up to whitespace or any char in ``stop`` (no peeked token allowed)."""
        assert self._peeked is None
        self._skip_ws()
        t = self.text
        start = self.i
        while self.i < len(t) and not t[self.i].isspace() and t[self.i] not in stop:
            self.i += 1
        return t[start : self.i], start

    def raw_until_paren(self) -> Tuple[str, int]:
        """Read raw text up to the matching ``)`` (exclusive), honouring nested parentheses."""
        assert self._peeked is None
        t = self.text
        start = self.i
        depth = 0
        while self.i < len(t):
            ch = t[self.i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    return t[start : self.i].strip(), start
                depth -= 1
            self.i += 1
        raise self.error(start, "Missing ')'")

    def peek_char(self) -> str:
        """Next non-space raw character (only valid when no token is peeked)."""
        assert self._peeked is None
        j = self.i
        while j < len(self.text) and self.text[j].isspace():
            j += 1
        return self.text[j] if j < len(self.text) else ""


# =========================================================================== CSS parser


class _CssParser:
    def __init__(self, text: str):
        self.text = text
        self.lx = _Lexer(text)

    def error(self, pos: int, msg: str) -> SelectorSyntaxError:
        return _syntax_error(self.text, pos, msg)

    def expect(self, kind: str, what: Optional[str] = None) -> _Tok:
        tok = self.lx.next()
        if tok.kind != kind:
            found = "end of selector" if tok.kind == _T_EOF else repr(tok.value)
            raise self.error(tok.pos, f"Expected {what or repr(kind)} but found {found}")
        return tok

    def parse(self) -> Selector:
        if not self.text.strip():
            raise self.error(0, "Empty selector")
        sel = self.parse_list(allow_relative=False, closing=None)
        tok = self.lx.peek()
        if tok.kind != _T_EOF:
            raise self.error(tok.pos, f"Unexpected {tok.value!r}")
        return sel

    def parse_list(self, allow_relative: bool, closing: Optional[str]) -> Selector:
        branches = [self.parse_complex(allow_relative, closing)]
        while self.lx.peek().kind == _T_COMMA:
            self.lx.next()
            branches.append(self.parse_complex(allow_relative, closing))
        return Selector(branches)

    def _ends_complex(self, tok: _Tok, closing: Optional[str]) -> bool:
        return tok.kind in (_T_EOF, _T_COMMA) or (closing is not None and tok.kind == closing)

    def parse_complex(self, allow_relative: bool, closing: Optional[str]) -> _Branch:
        relative = None
        tok = self.lx.peek()
        if tok.kind == _T_GT:
            if not allow_relative:
                raise self.error(tok.pos, "Selector cannot start with '>'")
            self.lx.next()
            relative = ">"
        tok = self.lx.peek()
        if self._ends_complex(tok, closing):
            raise self.error(tok.pos, "Expected a selector")
        steps: List[Tuple[Optional[str], _Compound]] = [(None, self.parse_compound())]
        while True:
            tok = self.lx.peek()
            if self._ends_complex(tok, closing):
                break
            if tok.kind == _T_GT:
                self.lx.next()
                nxt = self.lx.peek()
                if self._ends_complex(nxt, closing) or nxt.kind == _T_GT:
                    raise self.error(nxt.pos, "Expected a selector after '>'")
                steps.append((">", self.parse_compound()))
            elif tok.ws:
                steps.append((" ", self.parse_compound()))
            else:
                raise self.error(tok.pos, f"Unexpected {tok.value!r}")
        return _Branch(steps, relative)

    def parse_compound(self) -> _Compound:
        lx = self.lx
        tok = lx.peek()
        comp = _Compound()
        typed = False
        shorthand_used = False
        if tok.kind == _T_IDENT:
            lx.next()
            comp.role = _norm_role(tok.value)
            typed = True
        elif tok.kind == _T_STAR:
            lx.next()
            comp.star = True
            typed = True
        elif tok.kind == _T_STRING:
            lx.next()
            comp.conds.append(_AttrCond("text", "=", tok.value, form="bare"))
            shorthand_used = True
        elif tok.kind not in (_T_HASH, _T_DOT, _T_LBRACK, _T_COLON):
            found = "end of selector" if tok.kind == _T_EOF else repr(tok.value)
            raise self.error(tok.pos, f"Expected a role, '*', '#', '.', '[', ':' or a string but found {found}")
        started = typed or shorthand_used  # whitespace ends the compound once something was consumed
        while True:
            tok = lx.peek()
            if started and tok.ws and not (tok.kind == _T_STRING and typed and not shorthand_used):
                break
            started = True
            if tok.kind == _T_STRING:
                if not typed or shorthand_used:
                    raise self.error(tok.pos, "Unexpected string (put a space before it to select a descendant)")
                lx.next()
                comp.conds.append(_AttrCond("name", "=", tok.value, form="shorthand"))
                shorthand_used = True
            elif tok.kind == _T_HASH:
                lx.next()
                v = lx.next()
                if v.kind not in (_T_IDENT, _T_STRING) or v.ws:
                    raise self.error(v.pos, "Expected an id after '#'")
                comp.conds.append(_AttrCond("automation_id", "=", v.value, form="id"))
            elif tok.kind == _T_DOT:
                lx.next()
                v = lx.next()
                if v.kind not in (_T_IDENT, _T_STRING) or v.ws:
                    raise self.error(v.pos, "Expected a class name after '.'")
                comp.conds.append(_AttrCond("class_name", "=", v.value, form="class"))
            elif tok.kind == _T_LBRACK:
                lx.next()
                comp.conds.append(self.parse_attr(tok.pos))
            elif tok.kind == _T_COLON:
                lx.next()
                comp.conds.append(self.parse_pseudo())
            else:
                break
        return comp

    def parse_attr(self, open_pos: int) -> _AttrCond:
        lx = self.lx
        name_tok = lx.next()
        if name_tok.kind not in (_T_IDENT, _T_STRING) or name_tok.value == "":
            raise self.error(name_tok.pos, "Expected an attribute name after '['")
        key = _ATTR_ALIASES.get(name_tok.value.lower(), name_tok.value)
        tok = lx.next()
        if tok.kind == _T_RBRACK:
            return _AttrCond(key, None)
        if tok.kind != _T_OP:
            raise self.error(tok.pos, f"Expected an operator ({' '.join(_OPS)}) or ']'")
        op = tok.value
        # value: quoted string or bare word
        if lx.peek_char() in ("'", '"'):
            vtok = lx.next()
            value, vpos = vtok.value, vtok.pos
        else:
            value, vpos = lx.raw_value("]")
            if value == "":
                raise self.error(vpos, "Expected a value")
        ci = False
        tok = lx.next()
        if tok.kind == _T_IDENT and tok.value.lower() in ("i", "s"):
            ci = tok.value.lower() == "i"
            tok = lx.next()
        if tok.kind != _T_RBRACK:
            raise self.error(tok.pos, "Expected ']'" if tok.kind != _T_EOF else f"Unclosed '[' (opened at {open_pos})")
        if key == "role" and op in ("=", "!="):
            value = _norm_role(value)
        try:
            return _AttrCond(key, op, value, ci)
        except re.error as exc:
            raise self.error(vpos, f"Invalid regular expression: {exc}") from None

    def parse_pseudo(self) -> _Cond:
        lx = self.lx
        tok = lx.next()
        if tok.kind != _T_IDENT or tok.ws:
            raise self.error(tok.pos, "Expected a pseudo-class name after ':'")
        name = tok.value.lower()
        name = _PSEUDO_ALIASES.get(name, name)
        if name in _STATE_PSEUDOS:
            return _StateCond(name)
        if name in ("first", "last"):
            return _NthCond(0 if name == "first" else -1, label=name)
        if name not in ("has-text", "has", "not", "nth"):
            raise self.error(tok.pos, f"Unknown pseudo-class ':{tok.value}'")
        nxt = lx.peek()
        if nxt.kind != _T_LPAREN or nxt.ws:
            raise self.error(nxt.pos, f"':{name}' needs an argument in parentheses")
        lx.next()
        if name == "has-text":
            if lx.peek_char() in ("'", '"'):
                arg = lx.next().value
                self.expect(_T_RPAREN, "')'")
            else:
                arg, apos = lx.raw_until_paren()
                if not arg:
                    raise self.error(apos, "':has-text' needs a non-empty argument")
                self.expect(_T_RPAREN, "')'")
            return _HasTextCond(arg)
        if name == "nth":
            t = lx.next()
            if t.kind == _T_IDENT and re.fullmatch(r"-?\d+", t.value):
                n = int(t.value)
            else:
                raise self.error(t.pos, "':nth' needs an integer argument")
            self.expect(_T_RPAREN, "')'")
            return _NthCond(n)
        inner = self.parse_list(allow_relative=(name == "has"), closing=_T_RPAREN)
        self.expect(_T_RPAREN, "')'")
        if name == "has":
            return _HasCond(inner)
        if inner.is_positional:
            raise self.error(tok.pos, "Positional pseudo-classes (:nth/:first/:last) are not allowed inside :not()")
        return _NotCond(inner)


# =========================================================================== UiPath XML

_XML_TAG_RE = re.compile(r"\s*<\s*([A-Za-z_][\w\-]*)")
_XML_ATTR_RE = re.compile(r"\s*([A-Za-z_][\w\-:]*)\s*=\s*(?:'([^']*)'|\"([^\"]*)\")")
_UIPATH_ATTRS = {"ctrlname": "name", "ctrlid": "automation_id", "aaname": "name", "title": "name", "cls": "class_name"}
_XML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}


def _xml_unescape(s: str) -> str:
    return re.sub(r"&(amp|lt|gt|quot|apos);", lambda m: _XML_ENTITIES[m.group(0)], s)


def _parse_uipath(text: str) -> Selector:
    pos = 0
    steps: List[Tuple[Optional[str], _Compound]] = []
    n = len(text)
    while True:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        m = _XML_TAG_RE.match(text, pos)
        if not m:
            raise _syntax_error(text, pos, "Expected '<tag'")
        tag = m.group(1).lower()
        pos = m.end()
        attrs: List[Tuple[str, str, int]] = []
        while True:
            while pos < n and text[pos].isspace():
                pos += 1
            if text.startswith("/>", pos):
                pos += 2
                break
            if text.startswith(">", pos):
                pos += 1
                close = re.compile(r"\s*</\s*" + re.escape(m.group(1)) + r"\s*>", re.I).match(text, pos)
                if close:
                    pos = close.end()
                break
            am = _XML_ATTR_RE.match(text, pos)
            if not am:
                if pos >= n:
                    raise _syntax_error(text, pos, f"Unterminated <{tag}> tag")
                raise _syntax_error(text, pos, "Expected attribute name='value' or '/>'")
            val = am.group(2) if am.group(2) is not None else am.group(3)
            attrs.append((am.group(1).lower(), _xml_unescape(val), am.start(1)))
            pos = am.end()
        comp = _Compound()
        # Only the first <wnd>/<html>/<app> is a top-level window; a later
        # <wnd> is a child window (child HWND) of the previous one.
        top = tag in ("wnd", "html", "app") and not steps
        if top:
            comp.conds.append(_StateCond("root"))
        nth: Optional[int] = None
        for key, val, apos in attrs:
            if key == "idx":
                if not re.fullmatch(r"\d+", val.strip()) or int(val) < 1:
                    raise _syntax_error(text, apos, "idx must be a positive integer")
                nth = int(val) - 1
                continue
            glob = bool(_GLOB_CHARS.search(val))
            if key == "role" and not glob and comp.role is None:
                low = val.strip().lower()
                comp.role = UIPATH_ROLES.get(low) or _norm_role(low.replace(" ", ""))
                continue
            ckey = _UIPATH_ATTRS.get(key) or _ATTR_ALIASES.get(key, key)
            op = "%=" if glob else "="
            if ckey == "role" and op == "=":
                val = _norm_role(val)
            comp.conds.append(_AttrCond(ckey, op, val))
        if nth is not None:
            comp.conds.append(_NthCond(nth))
        steps.append((None if not steps else " ", comp))
    if not steps:
        raise _syntax_error(text, 0, "Empty UiPath selector")
    return Selector([_Branch(steps)], source=text)


# =========================================================================== dict

_DICT_OPS = {
    "": "=",
    "exact": "=",
    "eq": "=",
    "ne": "!=",
    "not": "!=",
    "contains": "*=",
    "startswith": "^=",
    "endswith": "$=",
    "regex": "~=",
    "glob": "%=",
}
_DICT_STATE_KEYS = {
    "enabled": ("enabled", "disabled"),
    "disabled": ("disabled", "enabled"),
    "visible": ("visible", "hidden"),
    "hidden": ("hidden", "visible"),
    "focused": ("focused", None),
    "checked": ("checked", "unchecked"),
    "selected": ("selected", None),
    "editable": ("editable", None),
    "expanded": ("expanded", "collapsed"),
    "mixed": ("mixed", None),
}
_DICT_BOOL_STRINGS = {"true": True, "yes": True, "on": True, "1": True, "false": False, "no": False, "off": False, "0": False}


def _dict_bool(key: str, val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int) and val in (0, 1):
        return bool(val)
    if isinstance(val, str) and val.strip().lower() in _DICT_BOOL_STRINGS:
        return _DICT_BOOL_STRINGS[val.strip().lower()]
    raise SelectorSyntaxError(f"Dict selector state key {key!r} needs true/false, got {val!r}")


def _dict_compound(d: Dict[str, Any]) -> _Compound:
    comp = _Compound()
    nth: Optional[int] = None
    for raw_key, val in d.items():
        key = str(raw_key)
        if key in ("within", "child_of", "parent", "ancestor"):
            continue
        if key in ("nth", "index"):
            try:
                nth = int(val)
            except (TypeError, ValueError):
                raise SelectorSyntaxError(f"Dict selector key {key!r} needs an integer, got {val!r}") from None
            continue
        if key in ("has_text", "contains_text"):
            comp.conds.append(_HasTextCond(str(val)))
            continue
        if key in ("has",):
            comp.conds.append(_HasCond(parse(val)))
            continue
        if key in ("not",):
            inner = parse(val)
            if inner.is_positional:
                raise SelectorSyntaxError(
                    'Positional pseudo-classes (:nth/:first/:last) are not allowed inside "not"'
                )
            comp.conds.append(_NotCond(inner))
            continue
        if key in ("states", "state"):
            names = [val] if isinstance(val, str) else list(val)
            for s in names:
                s = str(s).lower()
                if s not in _STATE_PSEUDOS:
                    raise SelectorSyntaxError(f"Unknown state {s!r} in dict selector")
                comp.conds.append(_StateCond(s))
            continue
        if key in _DICT_STATE_KEYS:
            pos_name, neg_name = _DICT_STATE_KEYS[key]
            if _dict_bool(key, val):
                comp.conds.append(_StateCond(pos_name))
            elif neg_name is not None:
                comp.conds.append(_StateCond(neg_name))
            else:
                comp.conds.append(_NotCond(Selector([_Branch([(None, _Compound(conds=[_StateCond(pos_name)]))])])))
            continue
        base, _, suffix = key.partition("__")
        suffix = suffix.lower()
        ci = False
        if suffix.startswith("i") and suffix[1:] in _DICT_OPS and suffix not in _DICT_OPS:
            ci, suffix = True, suffix[1:]
        if suffix not in _DICT_OPS:
            raise SelectorSyntaxError(f"Unknown operator suffix {'__' + suffix!r} in dict selector key {key!r}")
        op = _DICT_OPS[suffix]
        ckey = _ATTR_ALIASES.get(base.lower(), base)
        if isinstance(val, re.Pattern):
            op = "~="
            ci = ci or bool(val.flags & re.I)
            # Keep the other flags as inline flags (re.U is the default for str patterns).
            inline = "".join(c for f, c in ((re.A, "a"), (re.M, "m"), (re.S, "s"), (re.X, "x")) if val.flags & f)
            val = (f"(?{inline})" if inline else "") + val.pattern
        if ckey == "role" and op == "=" and comp.role is None and not ci and val is not None:
            comp.role = _norm_role(str(val))
            continue
        sval = _str_or_none(val)
        if sval is None:
            raise SelectorSyntaxError(f"Dict selector key {key!r} has value None")
        if ckey == "role" and op in ("=", "!="):
            sval = _norm_role(sval)  # like CSS [role!="push button"]
        try:
            comp.conds.append(_AttrCond(ckey, op, sval, ci))
        except re.error as exc:
            raise SelectorSyntaxError(f"Invalid regular expression for {key!r}: {exc}") from None
    if nth is not None:
        comp.conds.append(_NthCond(nth))
    return comp


def _dict_steps(d: Dict[str, Any]) -> List[Tuple[Optional[str], _Compound]]:
    if not isinstance(d, dict):
        raise SelectorSyntaxError(f"Expected a dict selector, got {type(d).__name__}")
    if "selector" in d and len(d) == 1:
        sel = parse(d["selector"])
        if len(sel.branches) != 1:
            raise SelectorSyntaxError("A nested 'selector' must not be a union")
        return list(sel.branches[0].steps)
    prefix: List[Tuple[Optional[str], _Compound]] = []
    comb = None
    for key, c in (("within", " "), ("ancestor", " "), ("child_of", ">"), ("parent", ">")):
        if key in d:
            if prefix:
                raise SelectorSyntaxError("Use only one of within/ancestor/child_of/parent")
            inner = d[key]
            if isinstance(inner, (str, Selector)):
                s = parse(inner)
                if len(s.branches) != 1:
                    raise SelectorSyntaxError(f"'{key}' must not be a union selector")
                prefix = list(s.branches[0].steps)
            else:
                prefix = _dict_steps(inner)
            comb = c
    comp = _dict_compound(d)
    if prefix:
        return prefix + [(comb, comp)]
    return [(None, comp)]


def _parse_dict(d: Dict[str, Any]) -> Selector:
    if not d:
        raise SelectorSyntaxError("Empty dict selector")
    if "any" in d and len(d) == 1:
        options = d["any"]
        if not isinstance(options, (list, tuple)):
            raise SelectorSyntaxError(f"'any' needs a list of selectors, got {type(options).__name__}")
        branches: List[_Branch] = []
        for o in options:
            branches.extend(parse(o).branches)
        if not branches:
            raise SelectorSyntaxError("'any' needs at least one selector")
        return Selector(branches, source=d)
    return Selector([_Branch(_dict_steps(d))], source=d)


# =========================================================================== parse()

_CACHE: Dict[str, Selector] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 1024


def parse(selector: Union[str, Dict[str, Any], Selector]) -> Selector:
    """Compile a CSS-like string, a UiPath XML string, a dict or a :class:`Selector`.

    String results are cached. Raises :class:`~locum.errors.SelectorSyntaxError`
    (with ``.position`` and ``.selector`` attributes for strings) on invalid input.
    """
    if isinstance(selector, Selector):
        return selector
    if isinstance(selector, dict):
        return _parse_dict(selector)
    if not isinstance(selector, str):
        raise SelectorSyntaxError(f"Unsupported selector type {type(selector).__name__}")
    with _CACHE_LOCK:
        hit = _CACHE.get(selector)
    if hit is not None:
        return hit
    stripped = selector.strip()
    if stripped.startswith("<"):
        sel = _parse_uipath(selector)
    else:
        sel = _CssParser(selector).parse()
        sel.source = selector
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[selector] = sel
    return sel


# =========================================================================== selector_for

_LONG_NAME = 80


def _role_head(role: str) -> str:
    """Selector head matching the (lower-cased) ``role`` exactly.

    ``parse`` rewrites role aliases (``label`` -> ``text``, ``input`` -> ``edit``),
    also in ``[role=...]``; a node whose role *is* such a spelling gets a test
    that is not normalised (a glob without wildcards, or an anchored regex).
    """
    if not role:
        return "*"
    if _norm_role(role) != role:
        if "*" in role or "?" in role:
            return f"*[role~={quote('^' + re.escape(role) + '$')}]"
        return f"*[role%={quote(role)}]"
    return role if _IDENT_RE.match(role) else f"*[role={quote(role)}]"


def _compound_for(node: AccessibleNode, *, use_id: bool = True, use_class: bool = False) -> str:
    role = (_safe(lambda: node.role, "") or "").lower()
    head = _role_head(role)
    aid = _safe(lambda: node.automation_id, "") or ""
    name = _safe(lambda: node.name, "") or ""
    if role in _TITLED_ROLES:
        # "◐ Pypilot library" -> window "Pypilot library": the spinner animates and a
        # dirty marker comes and goes; window titles are matched without them anyway.
        name = stable_title(name)
    out = head
    if use_id and aid:
        out += "#" + aid if _IDENT_RE.match(aid) else f"[automation_id={quote(aid)}]"
        return out
    if use_class:
        cls = _safe(lambda: node.class_name, "") or ""
        if cls:
            out += "." + cls if _IDENT_RE.match(cls) else f"[class_name={quote(cls)}]"
    if name:
        if len(name) > _LONG_NAME or "\n" in name:
            prefix = name.split("\n", 1)[0][:60]
            out += f"[name^={quote(prefix)}]"
        else:
            out += " " + quote(name) if _IDENT_RE.match(head) or head == "*" else f"[name={quote(name)}]"
    return out


def _same(a: AccessibleNode, b: AccessibleNode) -> bool:
    if a is b:
        return True
    try:
        return bool(a.same_as(b))
    except Exception:
        return False


#: Ancestors above the search roots: AT-SPI and AX parent a window to its
#: application (and AT-SPI the application to the desktop), but backends never
#: return these from ``accessibility_roots()``, so a selector must not start there.
_ABOVE_ROOT_ROLES = frozenset({"application", "desktop"})


def _chain(
    node: AccessibleNode,
    root: Optional[AccessibleNode],
    roots: Optional[Sequence[AccessibleNode]] = None,
) -> List[AccessibleNode]:
    """``[top, ..., node]`` following ``parent`` links.

    Stops at ``root``, or else at the first ancestor that is one of ``roots``.
    Without an explicit ``root``, when no ancestor is one of ``roots``, leading
    application/desktop ancestors are dropped so the chain starts where a
    search over the backend's roots does.
    """
    chain = [node]
    seen = 0
    cur = node
    hit = False
    while seen < 256:
        if root is not None and _same(cur, root):
            hit = True
            break
        if root is None and roots is not None and any(_same(cur, r) for r in roots):
            hit = True
            break
        p = _safe(lambda: cur.parent)
        if p is None:
            break
        chain.append(p)
        cur = p
        seen += 1
    chain.reverse()
    if not hit and root is None:
        while len(chain) > 1 and (_safe(lambda: chain[0].role, "") or "").lower() in _ABOVE_ROOT_ROLES:
            chain.pop(0)
    return chain


def selector_for(
    node: AccessibleNode,
    *,
    root: Optional[AccessibleNode] = None,
    roots: Optional[Sequence[AccessibleNode]] = None,
    prefix_cache: Optional[List[Any]] = None,
) -> str:
    """Generate a robust CSS selector that finds ``node`` again.

    ``prefix_cache`` (a list owned by the caller, e.g. one per observation)
    remembers each top-level window's prefix and whether it is unique among
    ``roots``: proving that searches every window, so selectors for many
    elements of the same window share the cost. Reuse a cache only while the
    windows have not changed.

    Preference order for the target: ``role#automation_id``, then
    ``role "name"``, then role + class + name; the top-level window ancestor
    is prepended (``window "Title" button "OK"``) and, when the result is still
    ambiguous, a meaningful intermediate ancestor and finally ``:nth(k)`` are
    added. The result satisfies ``parse(s).find_first(roots)`` being ``node``
    (by :meth:`AccessibleNode.same_as`).

    Args:
        node: The node (must expose ``parent`` links for an ancestor prefix).
        root: Scope the selector is relative to (default: the top-most ancestor,
            or the ancestor that is one of ``roots``; application/desktop
            ancestors above the windows are never used).
        roots: All search roots (e.g. ``desktop.roots()`` /
            ``backend.accessibility_roots()``); used to disambiguate windows
            sharing a title. Pass them whenever the selector will be used with
            ``Desktop.find``: without ``roots`` uniqueness is only checked inside
            the node's own top-level window, so two windows with the same title
            (e.g. two SAP sessions) can yield a selector that finds the other one.
    """
    chain = _chain(node, root, roots)
    top = chain[0]
    scope = list(roots) if roots is not None else [top]

    depth = max(64, len(chain) + 1)
    # With a prefix cache, the trees of ``roots`` are read once (per few seconds) and
    # shared by every selector built with that cache: each uniqueness test below is
    # then a pure-Python search instead of a walk of the live tree (one cross-process
    # call per node and property, ~0.5-5 s per test on a busy desktop).
    tree = _tree_cache(prefix_cache) if prefix_cache is not None and roots is not None else None
    if tree is not None:
        tree.seed(roots or ())
    state: Dict[str, Any] = {"tree": tree, "refreshed": False}

    def find(sel_text: str, where: Sequence[AccessibleNode], max_depth: int = 64,
             limit: Optional[int] = None) -> List[AccessibleNode]:
        sel = parse(sel_text)
        t = state["tree"]
        if t is None:
            return sel.find_all(where, max_depth=max_depth, limit=limit)
        snap = _Snapshot.over(t.entries_for(where), max_depth)
        out = [e.node for e in sel._search(snap, snap.roots, None, limit, True)]
        if t.building:
            t.born = time.monotonic()  # its lifetime counts from the end of the read
        return out

    def find_with(sel_text: str, where: Sequence[AccessibleNode], max_depth: int = 64,
                  live_limit: Optional[int] = None) -> List[AccessibleNode]:
        """Matches (live: up to ``live_limit``); on the shared tree all of them, and the tree
        is re-read once when ``node`` is not among them (it changed since it was read:
        ``node`` is new, or was renamed)."""
        if state["tree"] is None:
            return find(sel_text, where, max_depth, live_limit)
        found = find(sel_text, where, max_depth)
        if not any(_same(f, node) for f in found) and not state["refreshed"]:
            state["refreshed"] = True
            state["tree"] = _tree_cache(prefix_cache, fresh=True)
            state["tree"].seed(roots or ())
            found = find(sel_text, where, max_depth)
        return found

    def unique(sel_text: str) -> bool:
        """Whether ``sel_text`` matches exactly ``node`` within ``scope``."""
        try:
            found = find_with(sel_text, scope, depth, live_limit=2)
        except Exception:
            return False
        return len(found) == 1 and _same(found[0], node)

    def count(sel_text: str) -> int:
        try:
            return len(find(sel_text, scope, depth))
        except Exception:
            return 0

    def index_among(sel_text: str, where: Sequence[AccessibleNode], target: AccessibleNode) -> Optional[int]:
        try:
            found = find_with(sel_text, where) if target is node else find(sel_text, where)
        except Exception:
            return None
        return next((i for i, f in enumerate(found) if _same(f, target)), None)

    try:
        # Prefix identifying the top-level ancestor.
        cached = None
        if prefix_cache is not None and roots is not None:
            cached = next((entry for entry in prefix_cache
                           if isinstance(entry, (tuple, list)) and len(entry) >= 3 and _same(entry[0], top)), None)
        if cached is not None:
            top_sel, top_unique = cached[1], cached[2]
        else:
            top_sel, top_unique = _top_prefix(top, roots, scope, count, index_among=index_among)
            if prefix_cache is not None and roots is not None:
                prefix_cache.append((top, top_sel, top_unique))
        if len(chain) == 1:
            return top_sel
        if top_unique:
            # The prefix matches exactly one node anywhere in ``roots`` (``top``), so every
            # match of "<prefix> <rest>" lies inside ``top``: checking the rest against that
            # one window gives the same answer as searching every window again (which cost
            # seconds per element on a desktop with thousands of accessible elements).
            scope = [top]
        return _selector_below(node, chain, top_sel, scope, unique, index_among=index_among)
    finally:
        if state["tree"] is not None:
            state["tree"].building = False


# --------------------------------------------------------------------------- shared tree

#: Seconds a tree read for :func:`selector_for` is reused by later calls sharing its
#: ``prefix_cache``, counted from the end of the call that read it: long enough for one
#: listing (``find_elements``, an observation's selectors, a recorder burst), short
#: enough that a selector built later is proven unique against a fresh tree.
_TREE_TTL = 2.0
_TREES_PER_THREAD = 4


class _TreeCache:
    """Read-once :class:`_Entry` trees of the search roots, shared through a ``prefix_cache``.

    Kept beside the cache (not in it: callers inspect its ``(top, prefix, unique)``
    entries), keyed by the cache list's identity, so a caller that starts a new
    prefix cache because the windows changed also starts a new tree. Root entries
    get the position of their root in ``roots`` so document order across roots is
    the same as a fresh search's.
    """

    __slots__ = ("owner", "born", "building", "nodes", "entries", "_alias", "_stamps", "_pids")

    def __init__(self, owner: List[Any], born: float):
        self.owner = owner
        self.born = born
        self.building = True  # until the selector_for call that created it returns
        self.nodes: List[AccessibleNode] = []
        self.entries: List[_Entry] = []
        # Other objects for the same roots (a chain's top, a re-read root): id -> (object, index).
        self._alias: Dict[int, Tuple[AccessibleNode, int]] = {}
        # Change sources (UI Automation events) and their stamp when this tree was read.
        self._stamps: List[Tuple[Any, int]] = [(src, src.stamp()) for src in _change_sources()]
        self._pids: Dict[int, Optional[FrozenSet[int]]] = {}  # root index -> processes drawing it

    def revalidate(self) -> bool:
        """Forget the subtrees of the roots that changed since the tree was read (an element
        added, removed or renamed there: a selector proven unique against the old subtree
        could match two elements now). ``False`` when that cannot be told (the event history
        is gone or names no process): the whole tree must be read again."""
        stamps = []
        for src, stamp in self._stamps:
            if not src.alive:
                return False
            now = src.stamp()
            if now != stamp:
                dirty = src.dirty_since(stamp)
                if dirty is None:
                    return False
                for j, node in enumerate(self.nodes):
                    if j not in self._pids:
                        self._pids[j] = src.pids_of(node)
                    pids = self._pids[j]
                    if pids is None or pids & dirty:
                        self.entries[j] = _Entry(node, None, 0, (j,))
            stamps.append((src, now))
        self._stamps = stamps
        return True

    def seed(self, roots: Sequence[AccessibleNode]) -> None:
        if not self.nodes:
            self.entries_for(roots)

    def entries_for(self, where: Sequence[AccessibleNode]) -> List[_Entry]:
        return [self._entry(n, i) for i, n in enumerate(where) if n is not None]

    def _entry(self, node: AccessibleNode, hint: int) -> _Entry:
        nodes = self.nodes
        if hint < len(nodes) and nodes[hint] is node:
            return self.entries[hint]
        for j, n in enumerate(nodes):
            if n is node:
                return self.entries[j]
        alias = self._alias.get(id(node))
        if alias is not None and alias[0] is node:
            return self.entries[alias[1]]
        order = ([hint] if hint < len(nodes) else []) + [j for j in range(len(nodes)) if j != hint]
        for j in order:
            if _same(nodes[j], node):
                self._alias[id(node)] = (node, j)
                return self.entries[j]
        entry = _Entry(node, None, 0, (len(nodes),))
        nodes.append(node)
        self.entries.append(entry)
        return entry


_TREES = threading.local()  # per thread: COM nodes are used and released by the thread that read them
_SOURCES: "weakref.WeakSet[Any]" = weakref.WeakSet()
_SOURCES_LOCK = threading.Lock()


def register_change_source(source: Any) -> None:
    """Tell the shared selector trees which windows changed (backends with change events).

    ``source`` has ``alive``, ``stamp() -> int``, ``dirty_since(stamp) -> set of pids or
    None`` and ``pids_of(node) -> frozenset of pids or None``. Held weakly: keep a
    reference for as long as it should be used.
    """
    with _SOURCES_LOCK:
        _SOURCES.add(source)


def _change_sources() -> List[Any]:
    with _SOURCES_LOCK:
        return [s for s in _SOURCES if getattr(s, "alive", False)]


def release_trees() -> None:
    """Free this thread's shared selector trees (they hold accessibility nodes -- COM
    objects on Windows). Long-lived threads (an MCP worker) call it after each request;
    the trees would otherwise stay referenced until the thread builds the next selector."""
    _TREES.trees = []


def _tree_cache(prefix_cache: List[Any], fresh: bool = False) -> _TreeCache:
    """The shared tree of ``prefix_cache`` (a new one when absent, expired or ``fresh``)."""
    trees: List[_TreeCache] = getattr(_TREES, "trees", None) or []
    now = time.monotonic()
    trees = [t for t in trees if now - t.born <= _TREE_TTL]  # expired trees are dropped
    tree = None if fresh else next((t for t in trees if t.owner is prefix_cache), None)
    if tree is not None and not tree.revalidate():
        tree = None
    if tree is None:
        trees = [t for t in trees if t.owner is not prefix_cache]
        tree = _TreeCache(prefix_cache, now)
        trees.append(tree)
        del trees[:-_TREES_PER_THREAD]
    _TREES.trees = trees
    return tree


def _top_prefix(top: AccessibleNode, roots: Optional[Sequence[AccessibleNode]], scope: Sequence[AccessibleNode],
                count: Callable[[str], int], *,
                index_among: Optional[Callable[[str, Sequence[AccessibleNode], AccessibleNode], Optional[int]]] = None,
                ) -> Tuple[str, bool]:
    """``(prefix, unique)`` for the top-level ancestor: unique means it matches only ``top``."""
    index_among = index_among or _index_among
    top_sel = _compound_for(top, use_id=True)
    top_unique = roots is None  # without roots the scope is the node's own window already
    if roots is not None:
        n = count(top_sel)
        top_unique = n == 1
        if n > 1:
            app = _extra_app(top)
            if app:
                top_sel += f"[app={quote(app)}]"
            n = count(top_sel)
            top_unique = n == 1
            if n > 1:
                idx = index_among(top_sel, scope, top)
                if idx is not None:
                    top_sel += f":nth({idx})"
    return top_sel, top_unique


def _selector_below(node: AccessibleNode, chain: Sequence[AccessibleNode], top_sel: str,
                    scope: Sequence[AccessibleNode], unique: Callable[[str], bool], *,
                    index_among: Optional[Callable[[str, Sequence[AccessibleNode], AccessibleNode], Optional[int]]] = None,
                    ) -> str:
    """The part of :func:`selector_for` below the top-level prefix (``unique`` searches ``scope``)."""
    index_among = index_among or _index_among
    candidates = [
        _compound_for(node, use_id=True),
        _compound_for(node, use_id=False),
        _compound_for(node, use_id=False, use_class=True),
    ]
    tried = []
    for cand in dict.fromkeys(candidates):
        text = f"{top_sel} {cand}"
        tried.append(text)
        if unique(text):
            return text
    # Add the nearest identifiable intermediate ancestor.
    base = tried[0]
    for anc in reversed(chain[1:-1]):
        aid = _safe(lambda: anc.automation_id, "") or ""
        aname = _safe(lambda: anc.name, "") or ""
        if not aid and not aname:
            continue
        text = f"{top_sel} {_compound_for(anc)} {candidates[0]}"
        if unique(text):
            return text
        base = text
        break
    # Positional fallback on the most specific form.
    for text in [base] + tried:
        idx = index_among(text, scope, node)
        if idx is not None:
            return f"{text}:nth({idx})"
    # Last resort: an explicit child path from the top.
    parts = [top_sel]
    for i in range(1, len(chain)):
        parent_e, child = chain[i - 1], chain[i]
        kids = _safe(lambda: list(parent_e.children()), []) or []
        role = (_safe(lambda: child.role, "") or "").lower() or "*"
        same_role = [k for k in kids if (_safe(lambda: k.role, "") or "").lower() == role]
        pos = next((j for j, k in enumerate(same_role) if _same(k, child)), 0)
        parts.append(f"> {_role_head('' if role == '*' else role)}:nth({pos})")
    return " ".join(parts)


def _extra_app(node: AccessibleNode) -> str:
    attrs = _safe(lambda: dict(node.attributes()), {}) or {}
    v = attrs.get("app") or attrs.get("process") or ""
    return str(v)


def _index_among(sel_text: str, scope: Sequence[AccessibleNode], node: AccessibleNode) -> Optional[int]:
    try:
        found = parse(sel_text).find_all(scope)
    except Exception:
        return None
    for i, f in enumerate(found):
        if _same(f, node):
            return i
    return None


# =========================================================================== tree_text

_SHOWN_STATES = ("focused", "disabled", "checked", "mixed", "selected", "expanded", "collapsed", "readonly", "offscreen", "invisible", "active")


def _short(text: str, limit: int = 80) -> str:
    text = text.replace("\r", "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_node(node: AccessibleNode, *, include_bounds: bool = True) -> str:
    """One-line description: ``button "OK" #okBtn (120,340 80x30) [focused]``."""
    role = (_safe(lambda: node.role, "") or "unknown").lower()
    parts = [role]
    name = _safe(lambda: node.name, "") or ""
    if name:
        parts.append(quote(_short(name)))
    aid = _safe(lambda: node.automation_id, "") or ""
    if aid:
        parts.append("#" + aid if _IDENT_RE.match(aid) else f"[automation_id={quote(aid)}]")
    value = _safe(lambda: node.value)
    if value not in (None, "") and str(value) != name:
        parts.append("value=" + quote(_short(str(value), 60)))
    if include_bounds:
        b = _safe(lambda: node.bounds)
        if b is not None:
            parts.append(f"({b.left},{b.top} {b.width}x{b.height})")
    states = _safe(lambda: node.states, frozenset()) or frozenset()
    shown = [s for s in _SHOWN_STATES if s in states]
    if shown:
        parts.append("[" + ", ".join(shown) + "]")
    return " ".join(parts)


def _is_interactive(node: AccessibleNode) -> bool:
    role = (_safe(lambda: node.role, "") or "").lower()
    if role in INTERACTIVE_ROLES:
        return True
    acts = _safe(lambda: list(node.actions()), []) or []
    if any(a in ("invoke", "toggle", "set_value", "select", "expand") for a in acts):
        return True
    st = _safe(lambda: node.states, frozenset()) or frozenset()
    return "editable" in st


def tree_text(
    roots: Union[AccessibleNode, Sequence[AccessibleNode]],
    *,
    max_depth: int = 8,
    interactive_only: bool = False,
    max_nodes: int = 2000,
    include_bounds: bool = True,
) -> str:
    """Indented, human/LLM readable dump of accessibility trees.

    Each line looks like ``button "OK" #okBtn (120,340 80x30) [focused]``.
    With ``interactive_only`` only top-level nodes and visible interactive
    elements are listed (non-interactive containers are skipped and their
    interactive children are promoted). Output stops after ``max_nodes`` lines
    with a truncation note.
    """
    lines: List[str] = []
    truncated = False
    for root in _as_roots(roots):
        stack: List[Tuple[AccessibleNode, int, int]] = [(root, 0, 0)]  # node, depth, indent
        while stack:
            node, depth, indent = stack.pop()
            show = True
            if interactive_only and depth > 0:
                show = _is_interactive(node) and bool(_safe(lambda: node.is_visible, False))
            if show:
                if len(lines) >= max_nodes:
                    truncated = True
                    break
                lines.append("  " * indent + describe_node(node, include_bounds=include_bounds))
            if depth < max_depth:
                kids = _safe(lambda: list(node.children()), []) or []
                child_indent = indent + 1 if show else indent
                stack.extend((k, depth + 1, child_indent) for k in reversed(kids))
        if truncated:
            break
    if truncated:
        lines.append(f"... (truncated after {max_nodes} nodes)")
    return "\n".join(lines)
