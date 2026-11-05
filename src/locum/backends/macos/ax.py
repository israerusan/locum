"""Accessibility: the AX role tables (``map_ax_role``), the guarded AXUIElement wrapper
(``_AXApi``) and the :class:`~locum.backends.base.AccessibleNode` implementation (``AXNode``).
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Sequence, Set

from ...types import Rect
from ..base import AccessibleNode
from .constants import (
    kAXErrorSuccess,
    kAXValueAXErrorType,
    kAXValueCFRangeType,
    kAXValueCGPointType,
    kAXValueCGRectType,
    kAXValueCGSizeType,
)


# =============================================================================
# Accessibility role mapping
# =============================================================================

_AX_ROLE_MAP: Dict[str, str] = {
    "AXApplication": "application", "AXSystemWide": "desktop", "AXWindow": "window",
    "AXSheet": "dialog", "AXPopover": "dialog", "AXDrawer": "pane",
    "AXGroup": "group", "AXSplitGroup": "group", "AXRadioGroup": "group", "AXLayoutItem": "group",
    "AXMatte": "group", "AXRuler": "group", "AXColumn": "group",
    "AXLayoutArea": "pane", "AXScrollArea": "pane",
    "AXToolbar": "toolbar", "AXMenuBar": "menubar", "AXMenuBarItem": "menuitem",
    "AXMenu": "menu", "AXMenuItem": "menuitem",
    "AXButton": "button", "AXMenuButton": "button", "AXDisclosureTriangle": "button",
    "AXColorWell": "button", "AXDockItem": "button",
    "AXCheckBox": "checkbox", "AXRadioButton": "radiobutton",
    "AXPopUpButton": "combobox", "AXComboBox": "combobox",
    "AXTextField": "edit", "AXTextArea": "edit", "AXDateField": "edit", "AXTimeField": "edit",
    "AXStaticText": "text", "AXHeading": "header", "AXLink": "link", "AXImage": "image",
    "AXList": "list", "AXOutline": "tree", "AXBrowser": "tree",
    "AXTable": "table", "AXGrid": "table", "AXRow": "row", "AXCell": "cell",
    "AXTabGroup": "tablist", "AXSlider": "slider", "AXValueIndicator": "slider",
    "AXIncrementor": "spinbutton", "AXStepper": "spinbutton",
    "AXProgressIndicator": "progressbar", "AXBusyIndicator": "progressbar",
    "AXLevelIndicator": "progressbar", "AXRelevanceIndicator": "progressbar",
    "AXScrollBar": "scrollbar", "AXSplitter": "separator", "AXHelpTag": "tooltip",
    "AXWebArea": "document", "AXGrowArea": "unknown", "AXUnknown": "unknown", "AXHandle": "unknown",
}

_AX_SUBROLE_MAP: Dict[str, str] = {
    "AXDialog": "dialog", "AXSystemDialog": "dialog", "AXApplicationDialog": "dialog",
    "AXApplicationAlertDialog": "dialog",
    "AXToggle": "togglebutton", "AXSwitch": "checkbox", "AXTabButton": "tab",
    "AXOutlineRow": "treeitem", "AXTableRow": "row",
    "AXSecureTextField": "edit", "AXSearchField": "edit",
    "AXContentSeparator": "separator", "AXApplicationStatus": "statusbar",
    "AXDocument": "document", "AXDocumentArticle": "document", "AXDescriptionList": "list",
}

_CHECKABLE_ROLES = frozenset({"AXCheckBox", "AXRadioButton"})
_EDIT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXDateField", "AXTimeField"})


def map_ax_role(ax_role: Optional[str], subrole: Optional[str] = "") -> str:
    """Map an ``AXRole`` (+ optional ``AXSubrole``) to locum's portable role names.

    Unknown roles are passed through lowercased without the ``AX`` prefix
    (``"AXFancyThing"`` -> ``"fancything"``).
    """
    if subrole and subrole in _AX_SUBROLE_MAP:
        mapped = _AX_SUBROLE_MAP[subrole]
        # a "dialog" subrole only makes sense on windows; a toggle subrole on buttons/checkboxes
        if mapped != "dialog" or ax_role in ("AXWindow", "AXSheet", "AXGroup", None, ""):
            return mapped
    if not ax_role:
        return "unknown"
    if ax_role in _AX_ROLE_MAP:
        return _AX_ROLE_MAP[ax_role]
    name = ax_role[2:] if ax_role.startswith("AX") else ax_role
    return name.lower() or "unknown"


# =============================================================================
# Thin wrapper over the HIServices accessibility API
# =============================================================================

#: Identifiers Interface Builder assigns automatically ("_NS:136"); they change between
#: builds and launches, so they are not used as automation ids.
_GENERATED_ID_RE = re.compile(r"^_NS:\d+$")
_AX_POINT_RE = re.compile(r"x:\s*(-?[\d.]+)\s+y:\s*(-?[\d.]+)")
_AX_SIZE_RE = re.compile(r"w:\s*(-?[\d.]+)\s+h:\s*(-?[\d.]+)")


class _AXMissing:
    """Marker for an attribute whose value is an AXError (absent/unsupported)."""


_MISSING = _AXMissing()


class _AXApi:
    """Every accessibility call goes through this object (makes the node logic testable)."""

    def __init__(self, mod: Any, quartz: Any = None):
        self.m = mod
        self.q = quartz
        self.system_wide = mod.AXUIElementCreateSystemWide()
        try:  # global timeout: a hung app must not hang the automation
            mod.AXUIElementSetMessagingTimeout(self.system_wide, 1.5)
        except Exception:
            pass

    # -- trust ------------------------------------------------------------------------
    def is_trusted(self) -> bool:
        try:
            return bool(self.m.AXIsProcessTrusted())
        except Exception:
            return False

    def prompt_trust(self) -> bool:
        try:
            key = getattr(self.m, "kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt")
            return bool(self.m.AXIsProcessTrustedWithOptions({key: True}))
        except Exception:
            return self.is_trusted()

    # -- elements -----------------------------------------------------------------------
    def application(self, pid: int) -> Any:
        return self.m.AXUIElementCreateApplication(int(pid))

    def element_at(self, x: float, y: float) -> Any:
        try:
            err, el = self.m.AXUIElementCopyElementAtPosition(self.system_wide, float(x), float(y), None)
        except Exception:
            return None
        return el if err == kAXErrorSuccess else None

    def pid(self, el: Any) -> Optional[int]:
        try:
            err, pid = self.m.AXUIElementGetPid(el, None)
        except Exception:
            return None
        return int(pid) if err == kAXErrorSuccess else None

    def same(self, a: Any, b: Any) -> bool:
        try:
            return bool(a == b)
        except Exception:
            return a is b

    def window_id(self, el: Any) -> Optional[int]:
        """The exact ``CGWindowID`` of an AX window (private but long-stable
        ``_AXUIElementGetWindow``), or ``None`` when unavailable."""
        fn = self._get_window
        if fn is None:
            return None
        try:
            import ctypes
            import objc  # type: ignore

            out = ctypes.c_uint32(0)
            if fn(ctypes.c_void_p(objc.pyobjc_id(el)), ctypes.byref(out)) != kAXErrorSuccess:
                return None
            return int(out.value) or None
        except Exception:
            return None

    @property
    def _get_window(self) -> Any:
        if not hasattr(self, "_get_window_fn"):
            self._get_window_fn = None
            try:
                import ctypes
                import ctypes.util

                path = ctypes.util.find_library("ApplicationServices")
                fn = getattr(ctypes.cdll.LoadLibrary(path), "_AXUIElementGetWindow") if path else None
                if fn is not None:
                    fn.restype = ctypes.c_int32
                    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
                    self._get_window_fn = fn
            except Exception:
                self._get_window_fn = None
        return self._get_window_fn

    # -- attributes ------------------------------------------------------------------------
    def _convert(self, val: Any) -> Any:
        if val is None:
            return None
        tname = type(val).__name__
        if "AXValue" in tname:
            try:
                typ = self.m.AXValueGetType(val)
            except Exception:
                typ = None
            if typ == kAXValueAXErrorType:
                return _MISSING
            if typ is not None:
                try:
                    ok, out = self.m.AXValueGetValue(val, typ, None)
                except Exception:
                    ok, out = False, None
                if ok:
                    if typ == kAXValueCGPointType:
                        return (float(out.x), float(out.y))
                    if typ == kAXValueCGSizeType:
                        return (float(out.width), float(out.height))
                    if typ == kAXValueCGRectType:
                        return (float(out.origin.x), float(out.origin.y),
                                float(out.size.width), float(out.size.height))
                    if typ == kAXValueCFRangeType:  # pyobjc returns CFRange as a plain tuple
                        loc, length = (out.location, out.length) if hasattr(out, "location") else out
                        return (int(loc), int(length))
            text = str(val)
            m = _AX_POINT_RE.search(text)
            if m:
                return (float(m.group(1)), float(m.group(2)))
            m = _AX_SIZE_RE.search(text)
            if m:
                return (float(m.group(1)), float(m.group(2)))
            return None
        if isinstance(val, str):
            return str(val)
        return val

    def attr(self, el: Any, name: str, default: Any = None) -> Any:
        try:
            err, val = self.m.AXUIElementCopyAttributeValue(el, name, None)
        except Exception:
            return default
        if err != kAXErrorSuccess:
            return default
        out = self._convert(val)
        return default if out is None or out is _MISSING else out

    def attrs(self, el: Any, names: Sequence[str]) -> Dict[str, Any]:
        """Fetch several attributes in one IPC round-trip (missing ones are omitted)."""
        out: Dict[str, Any] = {}
        try:
            err, values = self.m.AXUIElementCopyMultipleAttributeValues(el, list(names), 0, None)
        except Exception:
            err, values = -1, None
        if err == kAXErrorSuccess and values is not None and len(values) == len(names):
            for name, val in zip(names, values):
                v = self._convert(val)
                if v is not None and v is not _MISSING:
                    out[name] = v
            return out
        for name in names:
            v = self.attr(el, name, _MISSING)
            if v is not _MISSING:
                out[name] = v
        return out

    def attr_names(self, el: Any) -> List[str]:
        try:
            err, names = self.m.AXUIElementCopyAttributeNames(el, None)
        except Exception:
            return []
        return [str(n) for n in names] if err == kAXErrorSuccess and names else []

    def settable(self, el: Any, name: str) -> bool:
        try:
            err, ok = self.m.AXUIElementIsAttributeSettable(el, name, None)
        except Exception:
            return False
        return err == kAXErrorSuccess and bool(ok)

    def set_attr(self, el: Any, name: str, value: Any) -> bool:
        try:
            return self.m.AXUIElementSetAttributeValue(el, name, value) == kAXErrorSuccess
        except Exception:
            return False

    def set_point(self, el: Any, name: str, x: float, y: float) -> bool:
        try:
            pt = self.q.CGPointMake(float(x), float(y)) if self.q is not None else (float(x), float(y))
            return self.set_attr(el, name, self.m.AXValueCreate(kAXValueCGPointType, pt))
        except Exception:
            return False

    def set_size(self, el: Any, name: str, w: float, h: float) -> bool:
        try:
            sz = self.q.CGSizeMake(float(w), float(h)) if self.q is not None else (float(w), float(h))
            return self.set_attr(el, name, self.m.AXValueCreate(kAXValueCGSizeType, sz))
        except Exception:
            return False

    # -- actions --------------------------------------------------------------------------
    def action_names(self, el: Any) -> List[str]:
        try:
            err, names = self.m.AXUIElementCopyActionNames(el, None)
        except Exception:
            return []
        return [str(n) for n in names] if err == kAXErrorSuccess and names else []

    def perform(self, el: Any, action: str) -> bool:
        try:
            return self.m.AXUIElementPerformAction(el, action) == kAXErrorSuccess
        except Exception:
            return False


# =============================================================================
# Accessible node
# =============================================================================

_UNSET: Any = object()

_BASIC_ATTRS = (
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXIdentifier",
    "AXPosition", "AXSize", "AXEnabled", "AXFocused", "AXSelected", "AXExpanded",
    "AXDisclosing", "AXMinimized", "AXHelp", "AXRoleDescription", "AXModal", "AXPlaceholderValue",
)

_ACTION_NAMES = {
    "AXPress": "invoke", "AXConfirm": "invoke", "AXPick": "select", "AXOpen": "invoke",
    "AXShowMenu": "show_menu", "AXIncrement": "increment", "AXDecrement": "decrement",
    "AXRaise": "raise", "AXScrollToVisible": "scroll_into_view", "AXCancel": "cancel",
    "AXShowAlternateUI": "show_alternate_ui", "AXShowDefaultUI": "show_default_ui",
}


def _value_to_str(v: Any) -> Optional[str]:
    if v is None or v is _MISSING:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(int(v))
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(float(v))
    if isinstance(v, str):
        return str(v)
    if isinstance(v, (tuple, list)):
        return None
    tname = type(v).__name__
    if "AXUIElement" in tname:
        return None
    try:
        return str(v)
    except Exception:
        return None


class AXNode(AccessibleNode):
    """An ``AXUIElementRef`` exposed through the portable :class:`AccessibleNode` API.

    Attribute values are fetched in one batched IPC call and cached for
    :attr:`CACHE_TTL` seconds; :meth:`refresh` drops the cache. ``bounds`` are
    converted from Quartz points to backend pixels.
    """

    CACHE_TTL = 0.5
    MAX_CHILDREN = 10000

    def __init__(self, element: Any, backend: "MacOSBackend", *, app: Optional[str] = None,
                 pid: Optional[int] = None, handle: Any = _UNSET, parent: Any = _UNSET):
        self._el = element
        self._be = backend
        self._ax: _AXApi = backend._ax  # type: ignore[assignment]
        self._app = app
        self._pid = pid
        self._handle = handle
        self._parent = parent
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time = 0.0

    # -- raw access ---------------------------------------------------------------------------
    @property
    def element(self) -> Any:
        """The underlying ``AXUIElementRef``."""
        return self._el

    def _attrs(self) -> Dict[str, Any]:
        now = time.monotonic()
        if self._cache is None or now - self._cache_time > self.CACHE_TTL:
            try:
                self._cache = self._ax.attrs(self._el, _BASIC_ATTRS)
            except Exception:
                self._cache = {}
            self._cache_time = now
        return self._cache

    def refresh(self) -> "AXNode":
        self._cache = None
        return self

    def get(self, attribute: str, default: Any = None) -> Any:
        """Read any raw AX attribute (``node.get("AXURL")``)."""
        if attribute in _BASIC_ATTRS:
            return self._attrs().get(attribute, default)
        return self._ax.attr(self._el, attribute, default)

    def perform(self, action: str) -> bool:
        """Perform a raw AX action (``node.perform("AXShowMenu")``)."""
        ok = self._ax.perform(self._el, action)
        self._cache = None
        return ok

    def _set(self, attribute: str, value: Any) -> bool:
        ok = self._ax.set_attr(self._el, attribute, value)
        self._cache = None
        return ok

    # -- identity -------------------------------------------------------------------------------
    @property
    def native_role(self) -> str:
        return str(self._attrs().get("AXRole") or "")

    @property
    def subrole(self) -> str:
        return str(self._attrs().get("AXSubrole") or "")

    @property
    def role(self) -> str:
        return map_ax_role(self.native_role, self.subrole)

    @property
    def name(self) -> str:
        a = self._attrs()
        for key in ("AXTitle", "AXDescription"):
            v = a.get(key)
            if isinstance(v, str) and v.strip():
                return v
        native = a.get("AXRole")
        if native == "AXStaticText":
            v = _value_to_str(a.get("AXValue"))
            if v:
                return v
        if native in _EDIT_ROLES or native in ("AXPopUpButton", "AXSlider", "AXCheckBox", "AXRadioButton"):
            label_el = self._ax.attr(self._el, "AXTitleUIElement")
            if label_el is not None:
                label = self._ax.attr(label_el, "AXValue") or self._ax.attr(label_el, "AXTitle")
                if isinstance(label, str) and label.strip():
                    return label
            ph = a.get("AXPlaceholderValue")
            if isinstance(ph, str) and ph:
                return ph
        return ""

    @property
    def value(self) -> Optional[str]:
        return _value_to_str(self._attrs().get("AXValue"))

    @property
    def automation_id(self) -> str:
        ident = str(self._attrs().get("AXIdentifier") or "")
        return "" if _GENERATED_ID_RE.match(ident) else ident

    @property
    def class_name(self) -> str:
        a = self._attrs()
        return str(a.get("AXSubrole") or a.get("AXRole") or "")

    @property
    def description(self) -> str:
        a = self._attrs()
        help_text = a.get("AXHelp")
        if isinstance(help_text, str) and help_text:
            return help_text
        desc = a.get("AXDescription")
        if isinstance(desc, str) and desc and desc != self.name:
            return desc
        return ""

    @property
    def bounds(self) -> Optional[Rect]:
        a = self._attrs()
        pos, size = a.get("AXPosition"), a.get("AXSize")
        if not pos or not size:
            return None
        try:
            return self._be._points_rect_to_pixels(pos[0], pos[1], size[0], size[1])
        except Exception:
            return None

    @property
    def states(self) -> frozenset:
        a = self._attrs()
        native = a.get("AXRole")
        st: Set[str] = set()
        enabled = a.get("AXEnabled")
        if enabled is False:
            st.add("disabled")
        else:
            st.add("enabled")
        if a.get("AXFocused") is True:
            st.update(("focused", "focusable"))
        if a.get("AXSelected") is True:
            st.add("selected")
        if native in _CHECKABLE_ROLES or a.get("AXSubrole") in ("AXToggle", "AXSwitch"):
            st.add("checkable")
            v = a.get("AXValue")
            if v is not None and v is not _MISSING and not isinstance(v, str):
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    iv = 0
                if iv == 1:
                    st.add("checked")
                elif iv == 2:
                    st.add("mixed")
        for key in ("AXExpanded", "AXDisclosing"):
            if key in a:
                st.add("expanded" if a[key] else "collapsed")
                break
        if a.get("AXMinimized") is True:
            st.add("minimized")
        if a.get("AXModal") is True:
            st.add("modal")
        if native in _EDIT_ROLES:
            st.add("editable" if enabled is not False else "readonly")
        b = self.bounds
        if b is not None and not b.is_empty and self._be._rect_on_screen(b):
            st.add("visible")
        elif b is not None:
            st.add("offscreen")
        return frozenset(st)

    @property
    def pid(self) -> Optional[int]:
        if self._pid is None:
            self._pid = self._ax.pid(self._el)
        return self._pid

    @property
    def app(self) -> str:
        if self._app is None:
            pid = self.pid
            self._app = self._be._app_name(pid) if pid is not None else ""
        return self._app or ""

    @property
    def handle(self) -> Any:
        if self._handle is _UNSET:
            self._handle = None
            try:
                win = self._el if self.native_role == "AXWindow" else self._ax.attr(self._el, "AXWindow")
                if win is not None:
                    self._handle = self._be._handle_for_ax_window(win, self.pid)
            except Exception:
                self._handle = None
        return self._handle

    def attributes(self) -> Dict[str, Any]:
        a = self._attrs()
        out: Dict[str, Any] = {"app": self.app, "native_role": self.native_role}
        for key, alias in (("AXSubrole", "subrole"), ("AXRoleDescription", "role_description"),
                           ("AXIdentifier", "identifier"), ("AXHelp", "help"),
                           ("AXPlaceholderValue", "placeholder")):
            v = a.get(key)
            if isinstance(v, str) and v:
                out[alias] = v
        return out

    # -- tree ------------------------------------------------------------------------------------
    def _wrap(self, el: Any, parent: Any = _UNSET) -> "AXNode":
        return AXNode(el, self._be, app=self._app, pid=self._pid,
                      handle=self._handle if self.native_role != "AXApplication" else _UNSET,
                      parent=parent)

    def children(self) -> Sequence["AXNode"]:
        kids = self._ax.attr(self._el, "AXChildren") or []
        try:
            kids = list(kids)[: self.MAX_CHILDREN]
        except TypeError:
            return []
        return [self._wrap(k, parent=self) for k in kids if k is not None]

    @property
    def parent(self) -> Optional["AXNode"]:
        if self._parent is _UNSET:
            p = self._ax.attr(self._el, "AXParent")
            self._parent = AXNode(p, self._be, app=self._app, pid=self._pid) if p is not None else None
        return self._parent

    # -- actions ------------------------------------------------------------------------------------
    def actions(self) -> List[str]:
        out: List[str] = []
        native = self._ax.action_names(self._el)
        for n in native:
            mapped = _ACTION_NAMES.get(n)
            if mapped and mapped not in out:
                out.append(mapped)
        if self.native_role in _CHECKABLE_ROLES and "AXPress" in native and "toggle" not in out:
            out.append("toggle")
        if self._ax.settable(self._el, "AXValue"):
            out.append("set_value")
        if self._ax.settable(self._el, "AXFocused"):
            out.append("focus")
        a = self._attrs()
        if "AXExpanded" in a or "AXDisclosing" in a:
            out.extend(x for x in ("expand", "collapse") if x not in out)
        if "select" not in out and self._ax.settable(self._el, "AXSelected"):
            out.append("select")
        return out

    def invoke(self) -> bool:
        native = self._ax.action_names(self._el)
        for action in ("AXPress", "AXConfirm", "AXPick", "AXOpen"):
            if action in native:
                return self.perform(action)
        return False

    def set_value(self, text: str) -> bool:
        if self._set("AXValue", text):
            return True
        self._set("AXFocused", True)
        return self._set("AXValue", text)

    def focus(self) -> bool:
        if self.native_role == "AXWindow":
            pid = self.pid
            if pid is not None:
                self._be._activate_pid(pid)
            self._set("AXMain", True)
            raised = self.perform("AXRaise")
            return self._set("AXFocused", True) or raised
        return self._set("AXFocused", True)

    def toggle(self) -> bool:
        if self.native_role in _CHECKABLE_ROLES or self.subrole in ("AXToggle", "AXSwitch"):
            return self.perform("AXPress")
        return False

    def _set_expanded(self, expanded: bool) -> bool:
        a = self._attrs()
        if "AXExpanded" in a:
            if bool(a["AXExpanded"]) == expanded:
                return True
            if self._set("AXExpanded", expanded):
                return True
        if "AXDisclosing" in a:
            if bool(a["AXDisclosing"]) == expanded:
                return True
            if self._set("AXDisclosing", expanded):
                return True
        native = a.get("AXRole")
        if native == "AXDisclosureTriangle":
            current = a.get("AXValue")
            if current is not None and bool(current) == expanded:
                return True
            return self.perform("AXPress")
        if native in ("AXPopUpButton", "AXComboBox", "AXMenuButton"):
            if expanded:
                return self.perform("AXShowMenu") or self.perform("AXPress")
            return self.perform("AXCancel")
        return False

    def expand(self) -> bool:
        return self._set_expanded(True)

    def collapse(self) -> bool:
        return self._set_expanded(False)

    def select(self) -> bool:
        if self._set("AXSelected", True):
            return True
        native = self._ax.action_names(self._el)
        if "AXPick" in native:
            return self.perform("AXPick")
        if self.subrole == "AXTabButton" and "AXPress" in native:
            return self.perform("AXPress")
        if self.native_role == "AXRow":
            parent = self._ax.attr(self._el, "AXParent")
            if parent is not None and self._ax.set_attr(parent, "AXSelectedRows", [self._el]):
                self._cache = None
                return True
        return False

    def scroll_into_view(self) -> bool:
        return self.perform("AXScrollToVisible")

    def same_as(self, other: AccessibleNode) -> bool:
        if not isinstance(other, AXNode):
            return False
        return self is other or self._ax.same(self._el, other._el)
