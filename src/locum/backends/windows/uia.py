"""UI Automation for the Windows backend: control-type and pattern-id maps,
the lazily loaded ``comtypes`` UIA module, the per-backend :class:`_UIAContext`
and the :class:`UIANode` accessible-node implementation.
"""

from __future__ import annotations

import ctypes
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ...errors import NotSupported
from ...types import Rect
from ..base import AccessibleNode
from ._win32api import POINT


# ------------------------------------------------------------------- UIA control types
#: UIA control type id -> (programmatic name, portable role).
UIA_CONTROL_TYPES: Dict[int, Tuple[str, str]] = {
    50000: ("Button", "button"),
    50001: ("Calendar", "calendar"),
    50002: ("CheckBox", "checkbox"),
    50003: ("ComboBox", "combobox"),
    50004: ("Edit", "edit"),
    50005: ("Hyperlink", "link"),
    50006: ("Image", "image"),
    50007: ("ListItem", "listitem"),
    50008: ("List", "list"),
    50009: ("Menu", "menu"),
    50010: ("MenuBar", "menubar"),
    50011: ("MenuItem", "menuitem"),
    50012: ("ProgressBar", "progressbar"),
    50013: ("RadioButton", "radiobutton"),
    50014: ("ScrollBar", "scrollbar"),
    50015: ("Slider", "slider"),
    50016: ("Spinner", "spinbutton"),
    50017: ("StatusBar", "statusbar"),
    50018: ("Tab", "tablist"),
    50019: ("TabItem", "tab"),
    50020: ("Text", "text"),
    50021: ("ToolBar", "toolbar"),
    50022: ("ToolTip", "tooltip"),
    50023: ("Tree", "tree"),
    50024: ("TreeItem", "treeitem"),
    50025: ("Custom", "custom"),
    50026: ("Group", "group"),
    50027: ("Thumb", "thumb"),
    50028: ("DataGrid", "table"),
    50029: ("DataItem", "row"),
    50030: ("Document", "document"),
    50031: ("SplitButton", "button"),
    50032: ("Window", "window"),
    50033: ("Pane", "pane"),
    50034: ("Header", "header"),
    50035: ("HeaderItem", "columnheader"),
    50036: ("Table", "table"),
    50037: ("TitleBar", "titlebar"),
    50038: ("Separator", "separator"),
    50039: ("SemanticZoom", "semanticzoom"),
    50040: ("AppBar", "toolbar"),
}
UIA_ButtonControlTypeId = 50000
UIA_MenuControlTypeId = 50009
UIA_WindowControlTypeId = 50032
UIA_PaneControlTypeId = 50033

#: Window classes that are dialogs even when UIA reports a plain Window.
_DIALOG_CLASSES = frozenset({"#32770", "NUIDialog"})


def map_uia_control_type(
    control_type: int,
    *,
    class_name: str = "",
    is_dialog: bool = False,
    has_toggle: bool = False,
    is_root: bool = False,
) -> str:
    """Map a UIA control type id to the portable role vocabulary of :data:`locum.backends.base.ROLES`.

    Args:
        control_type: ``UIA_*ControlTypeId`` (e.g. 50000 for Button).
        class_name: the element's class name (``#32770`` windows are dialogs).
        is_dialog: the UIA ``IsDialog`` property.
        has_toggle: the element supports the Toggle pattern (a toggling Button
            becomes ``togglebutton``).
        is_root: the element is the desktop root.
    """
    if is_root:
        return "desktop"
    entry = UIA_CONTROL_TYPES.get(int(control_type) if control_type is not None else -1)
    if entry is None:
        return "unknown"
    role = entry[1]
    if control_type == UIA_WindowControlTypeId and (is_dialog or class_name in _DIALOG_CLASSES):
        return "dialog"
    if control_type == UIA_ButtonControlTypeId and has_toggle:
        return "togglebutton"
    return role


def uia_control_type_name(control_type: int) -> str:
    """Programmatic UIA name of a control type id (``"Button"``), or ``"ControlType(<id>)"``."""
    entry = UIA_CONTROL_TYPES.get(int(control_type) if control_type is not None else -1)
    return entry[0] if entry else f"ControlType({control_type})"


# UIA pattern ids
UIA_InvokePatternId = 10000
UIA_ValuePatternId = 10002
UIA_RangeValuePatternId = 10003
UIA_ExpandCollapsePatternId = 10005
UIA_SelectionItemPatternId = 10010
UIA_TextPatternId = 10014
UIA_TogglePatternId = 10015
UIA_ScrollItemPatternId = 10017
UIA_LegacyIAccessiblePatternId = 10018
UIA_IsDialogPropertyId = 30174

#: Pattern id -> comtypes interface name in ``comtypes.gen.UIAutomationClient``.
UIA_PATTERN_INTERFACES: Dict[int, str] = {
    UIA_InvokePatternId: "IUIAutomationInvokePattern",
    UIA_ValuePatternId: "IUIAutomationValuePattern",
    UIA_RangeValuePatternId: "IUIAutomationRangeValuePattern",
    UIA_ExpandCollapsePatternId: "IUIAutomationExpandCollapsePattern",
    UIA_SelectionItemPatternId: "IUIAutomationSelectionItemPattern",
    UIA_TextPatternId: "IUIAutomationTextPattern",
    UIA_TogglePatternId: "IUIAutomationTogglePattern",
    UIA_ScrollItemPatternId: "IUIAutomationScrollItemPattern",
    UIA_LegacyIAccessiblePatternId: "IUIAutomationLegacyIAccessiblePattern",
}

ToggleState_Off = 0
ToggleState_On = 1
ToggleState_Indeterminate = 2
ExpandCollapseState_Collapsed = 0
ExpandCollapseState_Expanded = 1
ExpandCollapseState_PartiallyExpanded = 2
ExpandCollapseState_LeafNode = 3

CLSID_CUIAutomation = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
CLSID_CUIAutomation8 = "{e22ad333-b25f-460c-83d0-0581107395c9}"

_MAX_TEXT = 1 << 14


# =========================================================================== UI Automation
_uia_module_lock = threading.Lock()
_uia_module: Any = None


def load_uia_module() -> Any:
    """Import (generating on first use) ``comtypes.gen.UIAutomationClient``.

    Raises:
        NotSupported: comtypes is not installed or the type library cannot be loaded.
    """
    global _uia_module
    if _uia_module is not None:
        return _uia_module
    with _uia_module_lock:
        if _uia_module is not None:
            return _uia_module
        try:
            import comtypes  # noqa: F401
            import comtypes.client
        except ImportError as exc:
            raise NotSupported(
                "UI Automation requires comtypes; install it with `pip install comtypes`"
            ) from exc
        try:
            from comtypes.gen import UIAutomationClient as UIA  # type: ignore
        except ImportError:
            try:
                comtypes.client.GetModule("UIAutomationCore.dll")
                from comtypes.gen import UIAutomationClient as UIA  # type: ignore
            except Exception as exc:
                raise NotSupported(f"Cannot load the UI Automation type library: {exc}") from exc
        _uia_module = UIA
        return UIA


def _com_ok(value: Any) -> bool:
    """True when a comtypes interface pointer is non-NULL."""
    if value is None:
        return False
    try:
        return bool(value)
    except Exception:
        return False


class _UIAContext:
    """Per-backend UI Automation state (COM initialisation, IUIAutomation, walkers).

    COM objects are apartment bound, so the ``IUIAutomation`` instance is
    created once per thread.
    """

    def __init__(self, view: str = "control", timeout_ms: Optional[int] = 5000) -> None:
        if view not in ("control", "raw", "content"):
            raise ValueError("view must be 'control', 'raw' or 'content'")
        self.view = view
        self.timeout_ms = timeout_ms
        self.module = load_uia_module()
        self._local = threading.local()

    # ------------------------------------------------------------------ COM state
    def _init_thread(self) -> None:
        if getattr(self._local, "com", False):
            return
        import comtypes

        try:
            comtypes.CoInitializeEx(getattr(comtypes, "COINIT_APARTMENTTHREADED", 2))
        except Exception:
            pass  # already initialised in another mode (RPC_E_CHANGED_MODE): usable as is
        self._local.com = True

    @property
    def uia(self) -> Any:
        obj = getattr(self._local, "uia", None)
        if obj is not None:
            return obj
        self._init_thread()
        import comtypes

        UIA = self.module
        obj = None
        last_exc: Optional[Exception] = None
        for clsid in (CLSID_CUIAutomation8, CLSID_CUIAutomation):
            try:
                obj = comtypes.CoCreateInstance(
                    comtypes.GUID(clsid), interface=UIA.IUIAutomation, clsctx=comtypes.CLSCTX_INPROC_SERVER
                )
                break
            except Exception as exc:  # CUIAutomation8 needs Windows 8+
                last_exc = exc
        if obj is None:
            raise NotSupported(f"Cannot create the UI Automation client: {last_exc}")
        if self.timeout_ms:
            iface2 = getattr(UIA, "IUIAutomation2", None)
            if iface2 is not None:
                try:
                    obj2 = obj.QueryInterface(iface2)
                    obj2.ConnectionTimeout = int(self.timeout_ms)
                    obj2.TransactionTimeout = int(self.timeout_ms) * 4
                except Exception:
                    pass
        self._local.uia = obj
        return obj

    @property
    def walker(self) -> Any:
        w = getattr(self._local, "walker", None)
        if w is None:
            u = self.uia
            name = {"control": "ControlViewWalker", "raw": "RawViewWalker", "content": "ContentViewWalker"}[self.view]
            w = getattr(u, name)
            self._local.walker = w
        return w

    def root_element(self) -> Any:
        r = getattr(self._local, "root", None)
        if r is None:
            r = self.uia.GetRootElement()
            self._local.root = r
        return r

    # ------------------------------------------------------------------- helpers
    def pattern(self, element: Any, pattern_id: int) -> Any:
        """The element's pattern object for ``pattern_id`` (None when unsupported)."""
        name = UIA_PATTERN_INTERFACES.get(pattern_id)
        iface = getattr(self.module, name, None) if name else None
        if iface is None:
            return None
        try:
            unk = element.GetCurrentPattern(pattern_id)
        except Exception:
            return None
        if not _com_ok(unk):
            return None
        try:
            return unk.QueryInterface(iface)
        except Exception:
            return None

    def property_value(self, element: Any, property_id: int) -> Any:
        try:
            return element.GetCurrentPropertyValue(property_id)
        except Exception:
            return None

    def compare(self, a: Any, b: Any) -> bool:
        try:
            return bool(self.uia.CompareElements(a, b))
        except Exception:
            return False

    def is_root(self, element: Any) -> bool:
        try:
            return self.compare(element, self.root_element())
        except Exception:
            return False

    def first_child(self, element: Any) -> Any:
        try:
            c = self.walker.GetFirstChildElement(element)
        except Exception:
            return None
        return c if _com_ok(c) else None

    def next_sibling(self, element: Any) -> Any:
        try:
            c = self.walker.GetNextSiblingElement(element)
        except Exception:
            return None
        return c if _com_ok(c) else None

    def parent_of(self, element: Any) -> Any:
        try:
            p = self.walker.GetParentElement(element)
        except Exception:
            return None
        return p if _com_ok(p) else None

    def element_from_handle(self, hwnd: int) -> Any:
        try:
            e = self.uia.ElementFromHandle(ctypes.c_void_p(int(hwnd)))
        except Exception:
            return None
        return e if _com_ok(e) else None

    def element_from_point(self, x: int, y: int) -> Any:
        pt_cls = getattr(self.module, "tagPOINT", None) or POINT
        try:
            e = self.uia.ElementFromPoint(pt_cls(int(x), int(y)))
        except Exception:
            return None
        return e if _com_ok(e) else None

    def focused(self) -> Any:
        try:
            e = self.uia.GetFocusedElement()
        except Exception:
            return None
        return e if _com_ok(e) else None


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        value = fn()
    except Exception:
        return default
    return default if value is None else value


class UIANode(AccessibleNode):
    """A UI Automation element (``IUIAutomationElement``) as a portable :class:`AccessibleNode`.

    Properties never raise for stale elements; they return empty values.

    Args:
        element: the ``IUIAutomationElement`` pointer.
        ctx: the owning :class:`_UIAContext` (patterns, tree walker...).
        parent: parent node if known; otherwise resolved lazily via the walker.
        handle: native window handle to report (used for top-level windows
            whose HWND is already known).
        top_level: the node is a top-level window root; its :attr:`parent` is None.
        max_children: safety cap on children returned for huge containers.
    """

    def __init__(
        self,
        element: Any,
        ctx: Any,
        parent: Optional["UIANode"] = None,
        *,
        handle: Optional[int] = None,
        top_level: bool = False,
        max_children: int = 5000,
    ) -> None:
        self._el = element
        self._ctx = ctx
        self._parent = parent
        self._handle_hint = handle
        self._top_level = top_level
        self._max_children = max_children

    @property
    def element(self) -> Any:
        """The underlying ``IUIAutomationElement``."""
        return self._el

    def _get(self, attr: str, default: Any = None) -> Any:
        return _safe(lambda: getattr(self._el, attr), default)

    def _pattern(self, pattern_id: int) -> Any:
        return self._ctx.pattern(self._el, pattern_id)

    # ------------------------------------------------------------- identity
    @property
    def control_type(self) -> int:
        return int(self._get("CurrentControlType", 0) or 0)

    @property
    def native_role(self) -> str:
        return uia_control_type_name(self.control_type)

    @property
    def role(self) -> str:
        ct = self.control_type
        is_dialog = False
        has_toggle = False
        if ct == UIA_WindowControlTypeId:
            is_dialog = bool(self._ctx.property_value(self._el, UIA_IsDialogPropertyId) or False)
        elif ct == UIA_ButtonControlTypeId:
            has_toggle = self._pattern(UIA_TogglePatternId) is not None
        is_root = ct == UIA_PaneControlTypeId and self._parent is None and not self._top_level and self._ctx.is_root(self._el)
        return map_uia_control_type(ct, class_name=self.class_name, is_dialog=is_dialog, has_toggle=has_toggle,
                                    is_root=is_root)

    @property
    def name(self) -> str:
        return str(self._get("CurrentName", "") or "")

    @property
    def value(self) -> Optional[str]:
        p = self._pattern(UIA_ValuePatternId)
        if p is not None:
            v = _safe(lambda: p.CurrentValue)
            if v is not None:
                return str(v)
        p = self._pattern(UIA_RangeValuePatternId)
        if p is not None:
            v = _safe(lambda: p.CurrentValue)
            if v is not None:
                f = float(v)
                return str(int(f)) if f.is_integer() else str(f)
        p = self._pattern(UIA_TextPatternId)
        if p is not None:
            text = _safe(lambda: p.DocumentRange.GetText(_MAX_TEXT))
            if text is not None:
                return str(text)
        p = self._pattern(UIA_LegacyIAccessiblePatternId)
        if p is not None:
            v = _safe(lambda: p.CurrentValue)
            if v:
                return str(v)
        return None

    @property
    def automation_id(self) -> str:
        return str(self._get("CurrentAutomationId", "") or "")

    @property
    def class_name(self) -> str:
        return str(self._get("CurrentClassName", "") or "")

    @property
    def description(self) -> str:
        help_text = self._get("CurrentHelpText", "") or ""
        if help_text:
            return str(help_text)
        p = self._pattern(UIA_LegacyIAccessiblePatternId)
        if p is not None:
            return str(_safe(lambda: p.CurrentDescription, "") or "")
        return ""

    @property
    def bounds(self) -> Optional[Rect]:
        r = self._get("CurrentBoundingRectangle")
        if r is None:
            return None
        try:
            left, top, right, bottom = int(r.left), int(r.top), int(r.right), int(r.bottom)
        except Exception:
            return None
        if right <= left or bottom <= top:
            return None
        return Rect.from_ltrb(left, top, right, bottom)

    @property
    def states(self) -> frozenset:
        st = set()
        enabled = self._get("CurrentIsEnabled")
        if enabled is not None:
            st.add("enabled" if enabled else "disabled")
        if self._get("CurrentHasKeyboardFocus", False):
            st.add("focused")
        if self._get("CurrentIsKeyboardFocusable", False):
            st.add("focusable")
        offscreen = self._get("CurrentIsOffscreen")
        if offscreen is not None:
            st.add("offscreen" if offscreen else "visible")
        if self._get("CurrentIsPassword", False):
            st.add("password")
        tp = self._pattern(UIA_TogglePatternId)
        if tp is not None:
            ts = _safe(lambda: tp.CurrentToggleState)
            st.add("checkable")
            if ts == ToggleState_On:
                st.add("checked")
            elif ts == ToggleState_Indeterminate:
                st.add("mixed")
        sp = self._pattern(UIA_SelectionItemPatternId)
        if sp is not None:
            st.add("selectable")
            if _safe(lambda: sp.CurrentIsSelected, False):
                st.add("selected")
        ep = self._pattern(UIA_ExpandCollapsePatternId)
        if ep is not None:
            es = _safe(lambda: ep.CurrentExpandCollapseState)
            if es in (ExpandCollapseState_Expanded, ExpandCollapseState_PartiallyExpanded):
                st.add("expanded")
            elif es == ExpandCollapseState_Collapsed:
                st.add("collapsed")
        if self.control_type == UIA_ButtonControlTypeId:
            # STATE_SYSTEM_DEFAULT: the dialog/form button that Enter in a text field presses.
            lp = self._pattern(UIA_LegacyIAccessiblePatternId)
            if lp is not None and _safe(lambda: int(lp.CurrentState) & 0x100, 0):
                st.add("default")
        vp = self._pattern(UIA_ValuePatternId)
        if vp is not None:
            if _safe(lambda: vp.CurrentIsReadOnly, False):
                st.add("readonly")
            elif self.control_type in (50004, 50030, 50003):  # Edit, Document, ComboBox
                st.add("editable")
        return frozenset(st)

    @property
    def pid(self) -> Optional[int]:
        p = self._get("CurrentProcessId")
        return int(p) if p else None

    @property
    def handle(self) -> Any:
        if self._handle_hint is not None:
            return self._handle_hint
        h = self._get("CurrentNativeWindowHandle")
        try:
            h = int(h) if h else 0
        except (TypeError, ValueError):
            return None
        return h or None

    @property
    def runtime_id(self) -> Tuple[int, ...]:
        rid = _safe(lambda: self._el.GetRuntimeId(), ())
        try:
            return tuple(int(x) for x in rid)
        except Exception:
            return ()

    def attributes(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "control_type": self.control_type,
            "framework": str(self._get("CurrentFrameworkId", "") or ""),
            "localized_role": str(self._get("CurrentLocalizedControlType", "") or ""),
            "help_text": str(self._get("CurrentHelpText", "") or ""),
            "access_key": str(self._get("CurrentAccessKey", "") or ""),
            "accelerator_key": str(self._get("CurrentAcceleratorKey", "") or ""),
            "item_status": str(self._get("CurrentItemStatus", "") or ""),
            "is_password": bool(self._get("CurrentIsPassword", False)),
        }
        rid = self.runtime_id
        if rid:
            out["runtime_id"] = ".".join(str(x) for x in rid)
        h = self.handle
        if h:
            out["native_handle"] = h
        app = self.app_name()
        if app:
            out["app"] = app
        return out

    def app_name(self) -> str:
        """Process image name of the element's process (e.g. ``"notepad.exe"``), used by
        ``[app=...]`` selectors; matches :attr:`WindowInfo.app` from ``list_windows``."""
        pid = self.pid
        resolver = getattr(self._ctx, "app_name", None)
        if not pid or not callable(resolver):
            return ""
        try:
            return str(resolver(pid) or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------- tree
    def _wrap(self, element: Any, parent: Optional["UIANode"]) -> "UIANode":
        return UIANode(element, self._ctx, parent, max_children=self._max_children)

    def children(self) -> Sequence["UIANode"]:
        out: List[UIANode] = []
        child = self._ctx.first_child(self._el)
        while child is not None and len(out) < self._max_children:
            out.append(self._wrap(child, self))
            child = self._ctx.next_sibling(child)
        return out

    @property
    def parent(self) -> Optional["UIANode"]:
        if self._parent is not None or self._top_level:
            return self._parent
        p = self._ctx.parent_of(self._el)
        if p is None or self._ctx.is_root(p):
            return None
        self._parent = self._wrap(p, None)
        return self._parent

    # ---------------------------------------------------------------- actions
    def actions(self) -> List[str]:
        acts: List[str] = []
        if self._pattern(UIA_InvokePatternId) is not None:
            acts.append("invoke")
        vp = self._pattern(UIA_ValuePatternId)
        if vp is not None and not _safe(lambda: vp.CurrentIsReadOnly, False):
            acts.append("set_value")
        elif self._pattern(UIA_RangeValuePatternId) is not None:
            acts.append("set_value")
        if self._pattern(UIA_TogglePatternId) is not None:
            acts.append("toggle")
        if self._pattern(UIA_ExpandCollapsePatternId) is not None:
            acts += ["expand", "collapse"]
        if self._pattern(UIA_SelectionItemPatternId) is not None:
            acts.append("select")
        if self._pattern(UIA_ScrollItemPatternId) is not None:
            acts.append("scroll_into_view")
        if self._get("CurrentIsKeyboardFocusable", False):
            acts.append("focus")
        if "invoke" not in acts:
            lp = self._pattern(UIA_LegacyIAccessiblePatternId)
            if lp is not None and _safe(lambda: lp.CurrentDefaultAction, ""):
                acts.append("invoke")
        return acts

    def invoke(self) -> bool:
        """Invoke → Toggle → SelectionItem.Select → ExpandCollapse → LegacyIAccessible default action."""
        p = self._pattern(UIA_InvokePatternId)
        if p is not None:
            try:
                p.Invoke()
                return True
            except Exception:
                pass
        if self.toggle():
            return True
        p = self._pattern(UIA_SelectionItemPatternId)
        if p is not None:
            try:
                p.Select()
                return True
            except Exception:
                pass
        p = self._pattern(UIA_ExpandCollapsePatternId)
        if p is not None:
            state = _safe(lambda: p.CurrentExpandCollapseState)
            if state != ExpandCollapseState_LeafNode:
                try:
                    if state == ExpandCollapseState_Collapsed:
                        p.Expand()
                    else:
                        p.Collapse()
                    return True
                except Exception:
                    pass
        p = self._pattern(UIA_LegacyIAccessiblePatternId)
        if p is not None and _safe(lambda: p.CurrentDefaultAction, ""):
            try:
                p.DoDefaultAction()
                return True
            except Exception:
                pass
        return False

    def set_value(self, text: str) -> bool:
        p = self._pattern(UIA_ValuePatternId)
        if p is not None and not _safe(lambda: p.CurrentIsReadOnly, False):
            try:
                p.SetValue(str(text))
                return True
            except Exception:
                pass
        p = self._pattern(UIA_RangeValuePatternId)
        if p is not None and not _safe(lambda: p.CurrentIsReadOnly, False):
            try:
                number = float(text)
            except (TypeError, ValueError):
                number = None
            if number is not None:
                try:
                    p.SetValue(number)
                    return True
                except Exception:
                    pass
        p = self._pattern(UIA_LegacyIAccessiblePatternId)
        if p is not None:
            try:
                p.SetValue(str(text))
                return True
            except Exception:
                pass
        return False

    def focus(self) -> bool:
        try:
            self._el.SetFocus()
            return True
        except Exception:
            return False

    def toggle(self) -> bool:
        p = self._pattern(UIA_TogglePatternId)
        if p is None:
            return False
        try:
            p.Toggle()
            return True
        except Exception:
            return False

    def _expand_collapse(self, expand: bool) -> bool:
        p = self._pattern(UIA_ExpandCollapsePatternId)
        if p is None:
            return False
        try:
            if expand:
                p.Expand()
            else:
                p.Collapse()
            return True
        except Exception:
            return False

    def expand(self) -> bool:
        return self._expand_collapse(True)

    def collapse(self) -> bool:
        return self._expand_collapse(False)

    def select(self) -> bool:
        p = self._pattern(UIA_SelectionItemPatternId)
        if p is None:
            return False
        try:
            p.Select()
            return True
        except Exception:
            return False

    def scroll_into_view(self) -> bool:
        p = self._pattern(UIA_ScrollItemPatternId)
        if p is None:
            return False
        try:
            p.ScrollIntoView()
            return True
        except Exception:
            return False

    def same_as(self, other: AccessibleNode) -> bool:
        if not isinstance(other, UIANode):
            return False
        if other._el is self._el:
            return True
        a, b = self.runtime_id, other.runtime_id
        if a and b:
            return a == b
        return self._ctx.compare(self._el, other._el)
