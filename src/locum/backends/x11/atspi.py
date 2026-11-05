"""AT-SPI support of the X11 backend.

Loading ``gi.repository.Atspi``, the AT-SPI -> portable role table and the
:class:`AtspiNode` wrapper exposing AT-SPI accessibles as
:class:`~locum.backends.base.AccessibleNode`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from ...types import Rect
from ..base import AccessibleNode


def load_atspi() -> Optional[Any]:
    """Return the ``gi.repository.Atspi`` module, or ``None`` if AT-SPI is unavailable.

    ``pyatspi`` is a thin wrapper over the same GObject-introspection module,
    so it is only used as a second way of getting it imported.
    """
    try:
        import gi  # type: ignore

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore

        return Atspi
    except Exception:
        pass
    try:
        import pyatspi  # type: ignore  # noqa: F401
        from gi.repository import Atspi  # type: ignore

        return Atspi
    except Exception:
        return None


# --------------------------------------------------------------------------- AT-SPI nodes
_ATSPI_ROLE_MAP: Dict[str, str] = {
    "desktop frame": "desktop",
    "application": "application",
    "frame": "window",
    "window": "window",
    "internal frame": "window",
    "dialog": "dialog",
    "alert": "dialog",
    "file chooser": "dialog",
    "color chooser": "dialog",
    "font chooser": "dialog",
    "panel": "pane",
    "filler": "pane",
    "scroll pane": "pane",
    "split pane": "pane",
    "viewport": "pane",
    "layered pane": "pane",
    "root pane": "pane",
    "glass pane": "pane",
    "option pane": "pane",
    "section": "group",
    "grouping": "group",
    "form": "group",
    "block quote": "group",
    "landmark": "group",
    "tool bar": "toolbar",
    "status bar": "statusbar",
    "title bar": "titlebar",
    "menu bar": "menubar",
    "menu": "menu",
    "popup menu": "menu",
    "menu item": "menuitem",
    "check menu item": "menuitem",
    "radio menu item": "menuitem",
    "tearoff menu item": "menuitem",
    "push button": "button",
    "button": "button",
    "push button menu": "button",
    "toggle button": "togglebutton",
    "check box": "checkbox",
    "radio button": "radiobutton",
    "combo box": "combobox",
    "entry": "edit",
    "password text": "edit",
    "editbar": "edit",
    "text": "text",  # becomes "edit" when the node is editable
    "label": "text",
    "static": "text",
    "paragraph": "text",
    "caption": "text",
    "accelerator label": "text",
    "document frame": "document",
    "document web": "document",
    "document text": "document",
    "document email": "document",
    "document spreadsheet": "document",
    "document presentation": "document",
    "html container": "document",
    "link": "link",
    "image": "image",
    "icon": "image",
    "list": "list",
    "list box": "list",
    "list item": "listitem",
    "tree": "tree",
    "tree table": "tree",
    "tree item": "treeitem",
    "table": "table",
    "table row": "row",
    "table cell": "cell",
    "column header": "columnheader",
    "table column header": "columnheader",
    "row header": "rowheader",
    "table row header": "rowheader",
    "page tab": "tab",
    "page tab list": "tablist",
    "slider": "slider",
    "spin button": "spinbutton",
    "progress bar": "progressbar",
    "level bar": "progressbar",
    "scroll bar": "scrollbar",
    "separator": "separator",
    "tool tip": "tooltip",
    "heading": "header",
    "header": "header",
    "unknown": "unknown",
    "invalid": "unknown",
}


_INVOKE_ACTIONS = ("click", "press", "activate", "jump", "open", "invoke")


_EXPAND_ACTIONS = ("expand or contract", "expand or collapse", "toggle expand", "expand")


_COLLAPSE_ACTIONS = ("expand or contract", "expand or collapse", "toggle expand", "collapse")


def map_atspi_role(role_name: str, editable: bool = False) -> str:
    """Map an AT-SPI role name (``"push button"``) to the portable vocabulary in :data:`ROLES`.

    Unknown roles are passed through lowercased with spaces removed.
    """
    rn = (role_name or "").strip().lower()
    mapped = _ATSPI_ROLE_MAP.get(rn)
    if mapped is None:
        mapped = rn.replace(" ", "") or "unknown"
    if mapped == "text" and editable:
        return "edit"
    return mapped


def _iface(acc: Any, name: str) -> Any:
    """Return an AT-SPI interface (``get_<name>_iface`` / ``get_<name>``) or None."""
    for attr in (f"get_{name}_iface", f"get_{name}"):
        fn = getattr(acc, attr, None)
        if fn is None:
            continue
        try:
            return fn()
        except Exception:
            return None
    return None


def _proc_comm(pid: Optional[int]) -> str:
    """Process name of ``pid`` from ``/proc/<pid>/comm`` (``""`` if unavailable)."""
    if not pid:
        return ""
    try:
        with open(f"/proc/{int(pid)}/comm", "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except (OSError, ValueError, TypeError):
        return ""


class AtspiNode(AccessibleNode):
    """An AT-SPI accessible wrapped as a portable :class:`AccessibleNode`.

    Every property swallows errors (stale objects, D-Bus timeouts) and returns
    empty values, as required by the :class:`AccessibleNode` contract.

    Args:
        acc: the ``Atspi.Accessible``.
        atspi: the ``Atspi`` module (for enums such as ``CoordType``).
        parent: the parent node if already known (saves a D-Bus round trip).
        handle_resolver: callable ``(node) -> Optional[int]`` returning the X
            window id of a top-level node; supplied by :class:`X11Backend`.
        max_children: safety cap on children returned for huge containers.
    """

    def __init__(
        self,
        acc: Any,
        atspi: Any,
        parent: Optional["AtspiNode"] = None,
        handle_resolver: Optional[Callable[["AtspiNode"], Any]] = None,
        max_children: int = 5000,
    ) -> None:
        self._acc = acc
        self._atspi = atspi
        self._parent = parent
        self._resolver = handle_resolver
        self._max_children = max_children

    @property
    def accessible(self) -> Any:
        """The underlying ``Atspi.Accessible``."""
        return self._acc

    def _call(self, method: str, *args: Any, default: Any = None) -> Any:
        fn = getattr(self._acc, method, None)
        if fn is None:
            return default
        try:
            result = fn(*args)
        except Exception:
            return default
        return default if result is None else result

    # ------------------------------------------------------------- identity
    @property
    def native_role(self) -> str:
        return str(self._call("get_role_name", default="") or "")

    @property
    def role(self) -> str:
        native = self.native_role
        editable = False
        if (native or "").lower() in ("text", "entry", "password text"):
            editable = "editable" in self._raw_states()
        return map_atspi_role(native, editable)

    @property
    def name(self) -> str:
        return str(self._call("get_name", default="") or "")

    @property
    def description(self) -> str:
        return str(self._call("get_description", default="") or "")

    @property
    def value(self) -> Optional[str]:
        role = self.role
        if role in ("slider", "spinbutton", "progressbar", "scrollbar"):
            v = _iface(self._acc, "value")
            if v is not None:
                try:
                    cur = v.get_current_value()
                    return ("%g" % cur) if isinstance(cur, float) else str(cur)
                except Exception:
                    pass
        t = _iface(self._acc, "text")
        if t is not None:
            try:
                return str(t.get_text(0, -1) or "")
            except Exception:
                return None
        v = _iface(self._acc, "value")
        if v is not None:
            try:
                cur = v.get_current_value()
                return ("%g" % cur) if isinstance(cur, float) else str(cur)
            except Exception:
                return None
        return None

    @property
    def automation_id(self) -> str:
        aid = self._call("get_accessible_id", default="")
        if aid:
            return str(aid)
        attrs = self._raw_attributes()
        return str(attrs.get("id", "") or attrs.get("xml-id", "") or "")

    @property
    def class_name(self) -> str:
        attrs = self._raw_attributes()
        return str(attrs.get("class", "") or attrs.get("tag", "") or "")

    @property
    def bounds(self) -> Optional[Rect]:
        coord = getattr(getattr(self._atspi, "CoordType", None), "SCREEN", 0)
        ext = self._call("get_extents", coord)
        if ext is None:
            comp = _iface(self._acc, "component")
            if comp is None:
                return None
            try:
                ext = comp.get_extents(coord)
            except Exception:
                return None
        try:
            x, y, w, h = int(ext.x), int(ext.y), int(ext.width), int(ext.height)
        except Exception:
            return None
        if w < 0 or h < 0 or x <= -(1 << 30) or y <= -(1 << 30):
            return None
        return Rect(x, y, w, h)

    def _raw_states(self) -> frozenset:
        ss = self._call("get_state_set")
        if ss is None:
            return frozenset()
        try:
            states = ss.get_states()
        except Exception:
            return frozenset()
        names = set()
        for st in states or ():
            nick = getattr(st, "value_nick", None)
            if nick is None:
                nick = getattr(st, "value_name", None) or str(st)
                nick = nick.rsplit("_STATE_", 1)[-1] if "_STATE_" in nick else nick.rsplit(".", 1)[-1]
            names.add(str(nick).lower().replace("_", "-"))
        return frozenset(names)

    @property
    def states(self) -> frozenset:
        raw = self._raw_states()
        if not raw:
            return frozenset()
        out = {s.replace("-", "_") for s in raw}
        if "read-only" in raw:
            out.discard("read_only")
            out.add("readonly")
        if "enabled" in raw or "sensitive" in raw:
            out.add("enabled")
        else:
            out.add("disabled")
        if "showing" in raw and "visible" in raw:
            out.add("visible")
            out.discard("offscreen")
        else:
            out.discard("visible")
            out.add("offscreen")
        if "checked" in raw or ("pressed" in raw and self.native_role.lower() == "toggle button"):
            out.add("checked")
        return frozenset(out)

    @property
    def pid(self) -> Optional[int]:
        pid = self._call("get_process_id")
        try:
            return int(pid) if pid else None
        except Exception:
            return None

    @property
    def handle(self) -> Any:
        if self._resolver is None:
            return None
        try:
            return self._resolver(self)
        except Exception:
            return None

    def _raw_attributes(self) -> Dict[str, Any]:
        attrs = self._call("get_attributes", default={})
        try:
            return dict(attrs)
        except Exception:
            return {}

    def attributes(self) -> Dict[str, Any]:
        out: Dict[str, Any] = dict(self._raw_attributes())
        toolkit = self._call("get_toolkit_name")
        if toolkit:
            out.setdefault("toolkit", str(toolkit))
        out.setdefault("native_role", self.native_role)
        app = self.app_name()
        if app:
            out.setdefault("app", app)
        return out

    def app_name(self) -> str:
        """Application name for selectors (``[app=gedit]``): the process name of
        :attr:`pid` (as in :meth:`X11Backend.list_windows`), else the name of the
        AT-SPI application node this accessible belongs to."""
        name = _proc_comm(self.pid)
        if name:
            return name
        node: Optional[AtspiNode] = self
        for _ in range(256):
            if node is None:
                break
            if node.native_role == "application":
                return node.name
            node = node.parent
        return ""

    # ------------------------------------------------------------------ tree
    def children(self) -> Sequence["AtspiNode"]:
        count = self._call("get_child_count", default=0)
        try:
            count = min(int(count), self._max_children)
        except Exception:
            return []
        out: List[AtspiNode] = []
        for i in range(max(0, count)):
            child = self._call("get_child_at_index", i)
            if child is not None:
                out.append(AtspiNode(child, self._atspi, self, None, self._max_children))
        return out

    @property
    def parent(self) -> Optional["AtspiNode"]:
        if self._parent is not None:
            return self._parent
        p = self._call("get_parent")
        if p is None:
            return None
        return AtspiNode(p, self._atspi, None, None, self._max_children)

    # --------------------------------------------------------------- actions
    def _action_names(self) -> List[str]:
        act = _iface(self._acc, "action")
        if act is None:
            return []
        try:
            n = int(act.get_n_actions())
        except Exception:
            return []
        names = []
        for i in range(n):
            try:
                names.append(str(act.get_action_name(i) or "").lower())
            except Exception:
                names.append("")
        return names

    def _do_action(self, candidates: Sequence[str], fallback_first: bool = False) -> bool:
        act = _iface(self._acc, "action")
        if act is None:
            return False
        names = self._action_names()
        idx = next((i for c in candidates for i, n in enumerate(names) if n == c), None)
        if idx is None and fallback_first and names:
            idx = 0
        if idx is None:
            return False
        try:
            ok = act.do_action(idx)
        except Exception:
            return False
        return ok is None or bool(ok)

    def actions(self) -> List[str]:
        names = self._action_names()
        out: List[str] = []
        if names:
            out.append("invoke")
        if "toggle" in names or ("click" in names and self.role in ("checkbox", "togglebutton", "radiobutton")):
            out.append("toggle")
        if any(n in _EXPAND_ACTIONS for n in names):
            out.extend(["expand", "collapse"])
        if _iface(self._acc, "editable_text") is not None or _iface(self._acc, "value") is not None:
            out.append("set_value")
        if _iface(self._acc, "component") is not None:
            out.extend(["focus", "scroll_into_view"])
        par = self._call("get_parent")
        if par is not None and _iface(par, "selection") is not None:
            out.append("select")
        seen = set()
        return [a for a in out if not (a in seen or seen.add(a))]

    def invoke(self) -> bool:
        return self._do_action(_INVOKE_ACTIONS, fallback_first=True)

    def set_value(self, text: str) -> bool:
        et = _iface(self._acc, "editable_text")
        if et is not None:
            try:
                ok = et.set_text_contents(text)
                return True if ok is None else bool(ok)
            except Exception:
                pass
        v = _iface(self._acc, "value")
        if v is not None:
            try:
                ok = v.set_current_value(float(text))
                return True if ok is None else bool(ok)
            except Exception:
                return False
        return False

    def focus(self) -> bool:
        comp = _iface(self._acc, "component")
        if comp is None:
            return False
        try:
            ok = comp.grab_focus()
            return True if ok is None else bool(ok)
        except Exception:
            return False

    def toggle(self) -> bool:
        if self._do_action(("toggle",)):
            return True
        if self.role in ("checkbox", "togglebutton", "radiobutton", "menuitem"):
            return self._do_action(("click", "press", "activate"))
        return False

    def expand(self) -> bool:
        if "expanded" in self._raw_states():
            return True
        return self._do_action(_EXPAND_ACTIONS)

    def collapse(self) -> bool:
        if "expandable" in self._raw_states() and "expanded" not in self._raw_states():
            return True
        return self._do_action(_COLLAPSE_ACTIONS)

    def select(self) -> bool:
        par = self._call("get_parent")
        sel = _iface(par, "selection") if par is not None else None
        if sel is None:
            return False
        idx = self._call("get_index_in_parent")
        if idx is None or int(idx) < 0:
            return False
        try:
            ok = sel.select_child(int(idx))
            return True if ok is None else bool(ok)
        except Exception:
            return False

    def scroll_into_view(self) -> bool:
        comp = _iface(self._acc, "component")
        if comp is None or not hasattr(comp, "scroll_to"):
            return False
        anywhere = getattr(getattr(self._atspi, "ScrollType", None), "ANYWHERE", 6)
        try:
            ok = comp.scroll_to(anywhere)
            return True if ok is None else bool(ok)
        except Exception:
            return False

    def child_at_point(self, x: int, y: int) -> Optional["AtspiNode"]:
        """Direct child under screen point ``(x, y)`` (AT-SPI ``Component.GetAccessibleAtPoint``)."""
        comp = _iface(self._acc, "component")
        if comp is None:
            return None
        coord = getattr(getattr(self._atspi, "CoordType", None), "SCREEN", 0)
        try:
            child = comp.get_accessible_at_point(int(x), int(y), coord)
        except Exception:
            return None
        if child is None or child == self._acc:
            return None
        return AtspiNode(child, self._atspi, self, None, self._max_children)

    def same_as(self, other: AccessibleNode) -> bool:
        if not isinstance(other, AtspiNode):
            return False
        if other._acc is self._acc:
            return True
        try:
            return bool(other._acc == self._acc)
        except Exception:
            return False
