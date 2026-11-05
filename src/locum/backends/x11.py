"""X11 backend (Linux / BSD desktops, XWayland) built on ``python-xlib``.

Features
--------
* **Input** through the XTEST extension: absolute pointer motion, buttons
  1/2/3/8/9, wheel buttons 4/5 (vertical) and 6/7 (horizontal), and key
  events for every canonical key name in :data:`locum.keys.KEYS`.
* **Layout-independent unicode typing** (:data:`CAP_UNICODE_TYPING`): characters
  that the current keymap cannot produce with at most Shift are bound to a
  *spare* keycode (one with no keysyms) on the fly, exactly like ``xdotool``
  does. Bindings are kept in a small LRU pool, re-used across calls and
  restored when the backend is closed (or garbage collected). Keycodes are
  claimed under a server grab and listed in the ``_LOCUM_SCRATCH_KEYCODES``
  root property, so several backends (in one or more processes) never share
  one. Characters follow the active XKB group (second layout) like a user's
  keystrokes would.
* **Screenshots** via ``mss`` (fast SHM path) with an ``XGetImage`` fallback,
  always returned as ``RGB`` PIL images; regions partially outside the screen
  are padded with black so pixel (0, 0) is always the region's top-left.
* **Monitors** via RandR 1.5 ``GetMonitors`` (falling back to RandR CRTCs, then
  the whole root window). The DPI scale is read from ``Xft.dpi``.
* **Windows** via EWMH (``_NET_CLIENT_LIST_STACKING``, ``_NET_ACTIVE_WINDOW``,
  ``_NET_WM_STATE``, ``_NET_MOVERESIZE_WINDOW``, ``_NET_CLOSE_WINDOW``...), with
  an ICCCM / bare-X fallback when no window manager is running (e.g. Xvfb).
* **Clipboard** through ``xclip`` / ``xsel`` / ``wl-copy`` when installed, else
  natively (:mod:`._x11clip`: the backend owns ``CLIPBOARD`` and serves it,
  large texts with the ``INCR`` protocol).
* **Accessibility** through AT-SPI (``gi.repository.Atspi`` / ``pyatspi``) when
  importable, exposed as :class:`AtspiNode` with portable role names.

The X connection is shared by all threads and guarded by a re-entrant lock;
every injected event is followed by ``XSync`` so the server has processed it
before the primitive returns. A lost X connection raises
:class:`~locum.errors.BackendError`.

Under a pure Wayland session XTEST only reaches XWayland clients; use the
backend inside an X11 session for full-desktop control.
"""

from __future__ import annotations

import collections
import functools
import os
import shutil
import subprocess
import threading
import time
import weakref
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ..errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound
from ..keys import KEYS, split_text_tokens
from ..types import Monitor, MouseButton, Point, Rect, WindowInfo
from .base import (
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOWS,
    AccessibleNode,
    Backend,
)

# --------------------------------------------------------------------------- keysyms
# Numeric keysym values (from X11/keysymdef.h and XF86keysym.h). They are
# hard-coded instead of looked up with ``XK.string_to_keysym`` because
# python-xlib only loads the latin1/miscellany groups by default, so names like
# ``ISO_Level3_Shift`` or the XF86 media keys would resolve to ``NoSymbol``.
KEYSYMS: Dict[str, int] = {
    # modifiers
    "shift": 0xFFE1,  # Shift_L
    "shift_r": 0xFFE2,  # Shift_R
    "ctrl": 0xFFE3,  # Control_L
    "ctrl_r": 0xFFE4,  # Control_R
    "alt": 0xFFE9,  # Alt_L
    "alt_r": 0xFFEA,  # Alt_R
    "cmd": 0xFFEB,  # Super_L
    "cmd_r": 0xFFEC,  # Super_R
    "altgr": 0xFE03,  # ISO_Level3_Shift
    # There is no X keysym for the laptop Fn key (it is handled in firmware);
    # XF86WakeUp is what many laptops report for Fn on its own.
    "fn": 0x1008FF2B,  # XF86WakeUp
    # editing / navigation
    "enter": 0xFF0D,  # Return
    "tab": 0xFF09,  # Tab
    "space": 0x0020,  # space
    "backspace": 0xFF08,  # BackSpace
    "delete": 0xFFFF,  # Delete
    "escape": 0xFF1B,  # Escape
    "insert": 0xFF63,  # Insert
    "home": 0xFF50,  # Home
    "end": 0xFF57,  # End
    "pageup": 0xFF55,  # Prior
    "pagedown": 0xFF56,  # Next
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    # locks / system
    "capslock": 0xFFE5,  # Caps_Lock
    "numlock": 0xFF7F,  # Num_Lock
    "scrolllock": 0xFF14,  # Scroll_Lock
    "printscreen": 0xFF61,  # Print
    "pause": 0xFF13,  # Pause
    "menu": 0xFF67,  # Menu
    # function keys F1..F24
    **{f"f{i}": 0xFFBE + (i - 1) for i in range(1, 25)},
    # numpad
    **{f"num{i}": 0xFFB0 + i for i in range(10)},  # KP_0..KP_9
    "num_add": 0xFFAB,
    "num_subtract": 0xFFAD,
    "num_multiply": 0xFFAA,
    "num_divide": 0xFFAF,
    "num_decimal": 0xFFAE,
    "num_enter": 0xFF8D,
    # media (XF86)
    "volumeup": 0x1008FF13,  # XF86AudioRaiseVolume
    "volumedown": 0x1008FF11,  # XF86AudioLowerVolume
    "volumemute": 0x1008FF12,  # XF86AudioMute
    "playpause": 0x1008FF14,  # XF86AudioPlay
    "nexttrack": 0x1008FF17,  # XF86AudioNext
    "prevtrack": 0x1008FF16,  # XF86AudioPrev
    "stop": 0x1008FF15,  # XF86AudioStop
    # browser
    "browser_back": 0x1008FF26,  # XF86Back
    "browser_forward": 0x1008FF27,  # XF86Forward
    "browser_refresh": 0x1008FF29,  # XF86Refresh
}

_CONTROL_CHAR_KEYS = {"\b": "backspace", "\x1b": "escape", "\x7f": "delete", "\n": "enter", "\r": "enter", "\t": "tab"}

_KEYSYM_SHIFT_L = 0xFFE1
_KEYSYM_NUM_LOCK = 0xFF7F
_KEYSYM_CAPS_LOCK = 0xFFE5

BUTTONS: Dict[MouseButton, int] = {
    MouseButton.LEFT: 1,
    MouseButton.MIDDLE: 2,
    MouseButton.RIGHT: 3,
    MouseButton.X1: 8,
    MouseButton.X2: 9,
}
_WHEEL_UP, _WHEEL_DOWN, _WHEEL_LEFT, _WHEEL_RIGHT = 4, 5, 6, 7


def char_to_keysym(ch: str) -> int:
    """Return the X keysym for a single character.

    Latin-1 characters map to their code point (X keysyms 0x20-0x7e, 0xa0-0xff);
    everything else uses the Unicode keysym range ``0x01000000 + codepoint``,
    which every modern X server and toolkit understands.
    """
    if not isinstance(ch, str) or len(ch) != 1:
        raise ValueError(f"Expected a single character, got {ch!r}")
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp


def key_to_keysym(key: str) -> int:
    """Keysym for a canonical key name or a single character."""
    if key in KEYSYMS:
        return KEYSYMS[key]
    if len(key) == 1:
        if key in _CONTROL_CHAR_KEYS:
            return KEYSYMS[_CONTROL_CHAR_KEYS[key]]
        return char_to_keysym(key)
    raise ValueError(f"Unknown key {key!r} (expected a canonical name from locum.keys.KEYS or one character)")


def _is_keypad_keysym(ks: int) -> bool:
    return 0xFF80 <= ks <= 0xFFBD


def _is_char_keysym(ks: int) -> bool:
    """True for keysyms of printable characters (Latin-1 or the Unicode keysym range),
    the only keysyms whose key differs between keyboard layouts (XKB groups)."""
    return 0x20 <= ks <= 0x7E or 0xA0 <= ks <= 0xFF or ks & 0xFF000000 == 0x01000000


def _lower_upper(ks: int) -> Tuple[int, int]:
    """(lowercase, uppercase) keysym pair for Latin-1/Unicode letters, else (ks, ks)."""
    if ks < 0x100:
        ch = chr(ks)
    elif ks & 0xFF000000 == 0x01000000:
        ch = chr(ks & 0x00FFFFFF)
    else:
        return ks, ks
    lo, up = ch.lower(), ch.upper()
    if len(lo) != 1 or len(up) != 1:
        return ks, ks
    return char_to_keysym(lo), char_to_keysym(up)


#: Held modifiers that turn a character tap into a shortcut (CapsLock is then ignored).
_COMMAND_MODIFIERS = ("ctrl", "ctrl_r", "alt", "alt_r", "cmd", "cmd_r")

# --------------------------------------------------------------------------- lazy imports

class _XlibModules:
    """Holder for lazily imported python-xlib modules."""

    def __init__(self) -> None:
        from Xlib import X, Xatom, display, error  # noqa: F401
        from Xlib.ext import randr, xtest
        from Xlib.protocol import event

        self.X = X
        self.Xatom = Xatom
        self.display = display
        self.error = error
        self.randr = randr
        self.xtest = xtest
        self.event = event


_XLIB: Optional[_XlibModules] = None


def _xlib() -> _XlibModules:
    global _XLIB
    if _XLIB is None:
        _XLIB = _XlibModules()
    return _XLIB


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


# --------------------------------------------------------------------------- key state
class _InputState:
    """State that must be cleaned up even if the backend is garbage collected.

    Held separately from :class:`X11Backend` so a :func:`weakref.finalize`
    callback can restore the keyboard mapping, release stuck keys and close the
    helper connections (clipboard owner, screenshot grabbers) without keeping
    the backend alive.
    """

    def __init__(self, display: Any, lock: threading.RLock, remap_settle: float, display_name: str = "") -> None:
        self.display = display
        self.display_name = display_name
        self.lock = lock
        self.remap_settle = remap_settle
        self.bound: Dict[int, int] = {}  # keysym -> scratch keycode
        self.keycode_sym: Dict[int, int] = {}  # scratch keycode -> keysym
        self.original: Dict[int, Tuple[int, ...]] = {}  # scratch keycode -> original keysyms
        self.last_used: Dict[int, float] = {}  # scratch keycode -> monotonic time of last use
        self.held_keycodes: Dict[int, int] = {}  # keycode -> press count
        self.held_buttons: set = set()
        self.clipboard: Any = None  # native X11Clipboard (own connection + serving thread)
        self.mss_closers: List[Any] = []  # weakref.finalize objects closing per-thread mss grabbers
        self.closed = False
        # A KeyboardInterrupt (or other BaseException) landed inside an X call: python-xlib
        # may have been waiting for a reply and its connection state is now unusable (every
        # later request would block forever). The connection is dropped and reopened on demand.
        self.desynced = False


class _MssHolder:
    """Thread-local owner of one mss grabber; its finalizer closes the grabber."""

    __slots__ = ("sct", "__weakref__")

    def __init__(self, sct: Any) -> None:
        self.sct = sct


def _close_quietly(obj: Any) -> None:
    try:
        obj.close()
    except Exception:
        pass


def _abandon_display(d: Any) -> None:
    """Drop a python-xlib connection without talking to the server.

    ``Display.close()`` flushes first, which blocks forever on a connection an
    interrupt left half way through a request/reply exchange (its internal
    send/receive lock or "receiving" flag is never reset). Closing the socket
    directly also wakes other threads blocked on it (they get a connection error).
    """
    try:
        d.display.close_internal("client")
    except Exception:
        try:
            d.display.socket.close()
        except Exception:
            pass


def _cleanup(state: _InputState) -> None:
    """Release keys/buttons we hold, restore scratch keycodes and close every X connection."""
    if state.closed:
        return
    state.closed = True
    for closer in list(state.mss_closers):
        try:
            closer()
        except Exception:
            pass
    state.mss_closers.clear()
    with state.lock:
        clipboard, state.clipboard = state.clipboard, None
    if clipboard is not None:
        _close_quietly(clipboard)
    try:
        X = _xlib().X
        xtest = _xlib().xtest
        with state.lock:
            d = state.display
            if state.desynced:
                # The old connection cannot be used any more (see _InputState.desynced): release
                # the keys and restore the keymap over a fresh one (that is server-side state).
                _abandon_display(d)
                try:
                    d = _xlib().display.Display(state.display_name)
                except Exception:
                    return
                state.display = d
                state.desynced = False
            try:
                for kc in list(state.held_keycodes):
                    xtest.fake_input(d, X.KeyRelease, kc)
                for btn in list(state.held_buttons):
                    xtest.fake_input(d, X.ButtonRelease, btn)
                state.held_keycodes.clear()
                state.held_buttons.clear()
                if state.original:
                    # Give clients time to look up the keysyms of the last keys
                    # we typed before the scratch keycodes disappear.
                    newest = max(state.last_used.values(), default=0.0)
                    wait = state.remap_settle - (time.monotonic() - newest)
                    if wait > 0:
                        time.sleep(min(wait, 1.0))
                    # Restore only keycodes that still hold our keysym: another
                    # client (e.g. a second backend) may have bound the keycode
                    # since, and its binding must not be clobbered.
                    d.grab_server()
                    try:
                        restored = set()
                        for kc, syms in state.original.items():
                            ours = state.keycode_sym.get(kc)
                            current = tuple(int(v) for v in d.get_keyboard_mapping(kc, 1)[0])
                            if ours is not None and current[:1] == (ours,):
                                d.change_keyboard_mapping(kc, [tuple(syms)])
                                restored.add(kc)
                        marks = _scratch_marks(d)
                        if marks & restored:
                            _set_scratch_marks(d, marks - restored)
                    finally:
                        d.ungrab_server()
                    state.original.clear()
                    state.bound.clear()
                    state.keycode_sym.clear()
                d.sync()
            except Exception:
                pass
            try:
                d.close()
            except Exception:
                pass
    except Exception:
        pass


#: Root-window property listing the spare keycodes locum backends (in any
#: process) have bound for unicode typing, so they leave each other's alone.
_SCRATCH_PROP = "_LOCUM_SCRATCH_KEYCODES"


def _scratch_marks(d: Any) -> frozenset:
    """Keycodes listed in :data:`_SCRATCH_PROP` (empty when unset or unreadable)."""
    xl = _xlib()
    try:
        prop = d.screen().root.get_full_property(d.intern_atom(_SCRATCH_PROP), xl.Xatom.CARDINAL)
    except xl.error.XError:
        return frozenset()
    if prop is None or prop.format != 32:
        return frozenset()
    return frozenset(int(v) for v in prop.value)


def _set_scratch_marks(d: Any, keycodes: Any) -> None:
    xl = _xlib()
    root, atom = d.screen().root, d.intern_atom(_SCRATCH_PROP)
    if keycodes:
        root.change_property(atom, xl.Xatom.CARDINAL, 32, sorted(int(k) for k in keycodes))
    else:
        root.delete_property(atom)


class _ErrorSink:
    """Recent asynchronous X errors, numbered so callers can tell which are new."""

    def __init__(self, keep: int = 64) -> None:
        self.seq = 0
        self.recent: "collections.deque[Tuple[int, Any]]" = collections.deque(maxlen=keep)

    def since(self, seq: int) -> List[Any]:
        return [err for n, err in list(self.recent) if n > seq]


def _make_error_handler(sink: _ErrorSink) -> Callable[..., None]:
    """Error handler for X requests without replies (they fail asynchronously).

    Recent errors are kept in ``sink`` so window operations can turn them into
    exceptions after an ``XSync``.
    """

    def handler(err: Any, request: Any = None) -> None:
        sink.seq += 1
        sink.recent.append((sink.seq, err))

    return handler


def _x_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn raw python-xlib failures inside a backend primitive into locum errors:
    a lost/closed connection -> :class:`BackendError`, a window that vanished
    mid-operation -> :class:`WindowNotFound`, any other X error -> :class:`BackendError`."""

    @functools.wraps(fn)
    def wrapper(self: "X11Backend", *args: Any, **kwargs: Any) -> Any:
        if self._state.desynced:
            self._reconnect()
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:
            xe = _xlib().error
            if isinstance(exc, (xe.ConnectionClosedError, xe.DisplayError)):
                if self._state.closed:
                    raise BackendError("X11 backend has been closed") from exc
                raise BackendError(f"Lost connection to X display {self.display_name!r}: {exc}") from exc
            if isinstance(exc, (xe.BadWindow, xe.BadDrawable)):
                raise WindowNotFound(f"X11 window vanished during {fn.__name__}: {exc}") from exc
            if isinstance(exc, xe.XError):
                raise BackendError(f"X error during {fn.__name__}: {exc}") from exc
            raise
        except BaseException:
            # KeyboardInterrupt (Ctrl+C) / SystemExit, typically while python-xlib waited
            # for a reply: the connection is out of sync and would block every later call
            # (and the exit-time cleanup). Drop it; the next call reconnects.
            self._desync()
            raise

    return wrapper


# --------------------------------------------------------------------------- backend
class X11Backend(Backend):
    """Backend for X11 desktops (see module docstring).

    Args:
        display: X display name (default: ``$DISPLAY``).
        screenshot_method: ``"auto"`` (mss, falling back to XGetImage),
            ``"mss"`` or ``"xlib"``.
        clipboard_tool: force a clipboard helper (``"xclip"``, ``"xsel"``,
            ``"wl-clipboard"``), ``"native"`` (pure python-xlib selection
            handling) or ``"none"``; default: a helper if installed, else native.
        accessibility: enable AT-SPI support when available. An AT-SPI module
            object may be passed instead of ``True`` (used for testing).
        remap_settle: seconds a scratch keycode must stay bound after its last
            use before it may be re-bound to another character. Prevents clients
            from looking up a key after its mapping already changed.
        max_scratch_keycodes: upper bound on spare keycodes used for unicode
            typing (``None`` = all spare keycodes).

    Raises:
        BackendUnavailable: python-xlib is missing or the display cannot be opened.
    """

    name = "x11"

    def __init__(
        self,
        display: Optional[str] = None,
        *,
        screenshot_method: str = "auto",
        clipboard_tool: Optional[str] = None,
        accessibility: Any = True,
        remap_settle: float = 0.05,
        max_scratch_keycodes: Optional[int] = None,
    ) -> None:
        try:
            xl = _xlib()
        except ImportError as exc:
            raise BackendUnavailable(
                f"python-xlib is required for the X11 backend ({exc}); install it with `pip install python-xlib`"
            ) from exc
        self._xl = xl
        if screenshot_method not in ("auto", "mss", "xlib"):
            raise ValueError("screenshot_method must be 'auto', 'mss' or 'xlib'")
        display_name = display or os.environ.get("DISPLAY")
        if not display_name:
            raise BackendUnavailable("No X display: $DISPLAY is not set and no display name was given")
        try:
            self._d = xl.display.Display(display_name)
        except Exception as exc:
            raise BackendUnavailable(f"Cannot open X display {display_name!r}: {exc}") from exc

        self.display_name = display_name
        self._lock = threading.RLock()
        self._screen = self._d.screen()
        self._root = self._screen.root
        self._xtest = bool(self._d.has_extension("XTEST"))
        self._randr = bool(self._d.has_extension("RANDR"))
        self._min_kc = int(self._d.display.info.min_keycode)
        self._max_kc = int(self._d.display.info.max_keycode)
        self._keymap: List[Tuple[int, ...]] = []
        self._keymap_dirty = True
        self._modmap_dirty = True
        self._numlock_mask = 0
        self._scratch_pool: Optional[List[int]] = None
        self._scratch_borrow = False
        self._foreign_scratch: frozenset = frozenset()  # keycodes bound by other locum backends
        self._max_scratch = max_scratch_keycodes
        self._held_keys: Dict[str, Tuple[int, Optional[int]]] = {}  # key -> (keycode, extra shift keycode)
        self._async_errors = _ErrorSink()
        # A closure (not a bound method) so the display does not keep ``self`` alive.
        self._d.set_error_handler(_make_error_handler(self._async_errors))
        self._screenshot_method = screenshot_method
        self._mss_local = threading.local()
        self._mss_broken = screenshot_method == "xlib"
        self._mss_failures = 0  # consecutive failed mss grabs
        self._restore_geometry: Dict[int, Rect] = {}
        self._iconified: set = set()  # windows we unmapped to "minimize" them without a WM
        try:
            self._clip_cmds = self._detect_clipboard(clipboard_tool)
        except Exception:
            self._d.close()
            raise
        if accessibility is True:
            self._atspi_source: Any = "auto"
        elif accessibility:
            self._atspi_source = accessibility
        else:
            self._atspi_source = None
        self._atspi_mod: Any = None
        self._atspi_checked = False
        self._state = _InputState(self._d, self._lock, float(remap_settle), display_name)
        self._finalizer = weakref.finalize(self, _cleanup, self._state)

    # =================================================================== basics
    @property
    def capabilities(self) -> frozenset:
        caps = {CAP_SCREENSHOT, CAP_WINDOWS}
        if self._xtest:
            caps |= {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_HSCROLL}
        if self._clip_cmds is not None or self._clip_mode != "none":
            caps.add(CAP_CLIPBOARD)
        if self._atspi() is not None:
            caps.add(CAP_ACCESSIBILITY)
        return frozenset(caps)

    @property
    def closed(self) -> bool:
        return self._state.closed

    def close(self) -> None:
        """Release held keys/buttons, restore remapped keycodes and close the X connections."""
        self._finalizer()

    def __enter__(self) -> "X11Backend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _check(self) -> None:
        if self._state.closed:
            raise BackendError("X11 backend has been closed")

    def _desync(self) -> None:
        """Drop the display connection after an interrupt left it mid-request (see ``_x_errors``)."""
        st = self._state
        if st.closed or st.desynced:
            return
        st.desynced = True
        _abandon_display(self._d)

    def _reconnect(self) -> None:
        """Replace a connection dropped by :meth:`_desync` with a fresh one."""
        with self._lock:
            st = self._state
            if st.closed:
                raise BackendError("X11 backend has been closed")
            if not st.desynced:
                return
            try:
                d = self._xl.display.Display(self.display_name)
            except Exception as exc:
                raise BackendError(f"Lost connection to X display {self.display_name!r} and could not reconnect: {exc}") from exc
            d.set_error_handler(_make_error_handler(self._async_errors))
            self._d = st.display = d
            self._screen = d.screen()
            self._root = self._screen.root
            self._keymap_dirty = True
            self._modmap_dirty = True
            st.desynced = False

    def _sync(self) -> None:
        try:
            self._d.sync()
        except self._xl.error.ConnectionClosedError as exc:
            self._check()  # closed by close() in another thread
            raise BackendError(f"Lost connection to X display {self.display_name!r}: {exc}") from exc

    def _need_xtest(self) -> None:
        self._check()
        if not self._xtest:
            raise NotSupported(f"X display {self.display_name!r} lacks the XTEST extension; input injection unavailable")

    def _fake(self, event_type: int, detail: int = 0, x: int = 0, y: int = 0) -> None:
        if event_type == self._xl.X.MotionNotify:
            self._xl.xtest.fake_input(self._d, event_type, detail, root=self._root, x=x, y=y)
        else:
            self._xl.xtest.fake_input(self._d, event_type, detail)

    def _screen_size(self) -> Tuple[int, int]:
        try:
            g = self._root.get_geometry()
            return int(g.width), int(g.height)
        except self._xl.error.XError:
            return int(self._screen.width_in_pixels), int(self._screen.height_in_pixels)

    # ==================================================================== mouse
    @_x_errors
    def mouse_position(self) -> Point:
        self._check()
        with self._lock:
            p = self._root.query_pointer()
            return Point(int(p.root_x), int(p.root_y))

    @_x_errors
    def mouse_move(self, x: int, y: int) -> None:
        self._check()
        with self._lock:
            w, h = self._screen_size()
            cx = min(max(int(round(x)), 0), max(w - 1, 0))
            cy = min(max(int(round(y)), 0), max(h - 1, 0))
            if self._xtest:
                self._fake(self._xl.X.MotionNotify, 0, cx, cy)
            else:
                self._root.warp_pointer(cx, cy)
            self._sync()

    def _button(self, button: Any) -> int:
        return BUTTONS[MouseButton.coerce(button)]

    @_x_errors
    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._need_xtest()
        btn = self._button(button)
        with self._lock:
            self._fake(self._xl.X.ButtonPress, btn)
            self._state.held_buttons.add(btn)
            self._sync()

    @_x_errors
    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._need_xtest()
        btn = self._button(button)
        with self._lock:
            self._fake(self._xl.X.ButtonRelease, btn)
            self._state.held_buttons.discard(btn)
            self._sync()

    @_x_errors
    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Scroll by wheel notches (``dy > 0`` up, ``dx > 0`` right) using buttons 4-7."""
        self._need_xtest()
        dx, dy = int(dx), int(dy)
        X = self._xl.X
        with self._lock:
            for count, btn in ((abs(dy), _WHEEL_UP if dy > 0 else _WHEEL_DOWN), (abs(dx), _WHEEL_RIGHT if dx > 0 else _WHEEL_LEFT)):
                for _ in range(count):
                    self._fake(X.ButtonPress, btn)
                    self._fake(X.ButtonRelease, btn)
            self._sync()

    # ================================================================= keyboard
    def _process_events(self) -> None:
        """Drain our event queue, noting keyboard/modifier mapping changes."""
        X = self._xl.X
        try:
            while self._d.pending_events():
                ev = self._d.next_event()
                if ev.type == X.MappingNotify:
                    if ev.request == X.MappingKeyboard:
                        self._keymap_dirty = True
                    elif ev.request == X.MappingModifier:
                        self._modmap_dirty = True
        except self._xl.error.ConnectionClosedError as exc:
            raise BackendError(f"Lost connection to X display {self.display_name!r}: {exc}") from exc

    def _ensure_keymap(self) -> None:
        self._process_events()
        if self._keymap_dirty or not self._keymap:
            count = self._max_kc - self._min_kc + 1
            self._keymap = [tuple(int(s) for s in syms) for syms in self._d.get_keyboard_mapping(self._min_kc, count)]
            self._keymap_dirty = False
            # Spare keycodes other locum backends have bound are not real keys:
            # they may be re-bound at any moment, so never type through them.
            self._foreign_scratch = _scratch_marks(self._d) - set(self._state.keycode_sym)
        if self._modmap_dirty:
            self._numlock_mask = 0
            try:
                for idx, keycodes in enumerate(self._d.get_modifier_mapping()):
                    for kc in keycodes:
                        if kc and self._syms(int(kc))[:1] == (_KEYSYM_NUM_LOCK,):
                            self._numlock_mask |= 1 << idx
            except Exception:
                pass
            self._modmap_dirty = False

    def _syms(self, keycode: int) -> Tuple[int, ...]:
        i = keycode - self._min_kc
        if 0 <= i < len(self._keymap):
            return self._keymap[i]
        return ()

    def _group_syms(self, keycode: int, group: int = 0) -> Tuple[int, int]:
        """(plain, shifted) keysyms of ``keycode`` in XKB group ``group`` (0 or 1).

        The core keymap of an XKB server lists group 1 in columns 0-1 and group 2
        in columns 2-3 (single-group keys repeat group 1 there).
        """
        syms = self._syms(keycode)
        c = 2 * group
        return (syms[c] if len(syms) > c else 0), (syms[c + 1] if len(syms) > c + 1 else 0)

    def _lookup(self, keysym: int, group: int = 0) -> Optional[Tuple[int, int]]:
        """Find ``(keycode, level)`` producing ``keysym`` with level 0 (plain) or 1 (shift)
        while XKB group ``group`` is active. Groups 3 and 4 cannot be read reliably
        from the core keymap, so nothing is found for them."""
        if group not in (0, 1):
            return None
        c = 2 * group
        best: Optional[Tuple[int, int]] = None
        foreign = self._foreign_scratch
        for i, syms in enumerate(self._keymap):
            kc = i + self._min_kc
            if len(syms) <= c or kc in foreign:
                continue
            if syms[c] == keysym:
                # A keycode whose second column is NoSymbol also yields keysym
                # with shift; plain is always preferred.
                return kc, 0
            if len(syms) > c + 1 and syms[c + 1] == keysym and best is None:
                best = (kc, 1)
        if best is None:
            lo, up = _lower_upper(keysym)
            if up == keysym and lo != keysym:
                # Uppercase letter on a key listing only the lowercase keysym.
                for i, syms in enumerate(self._keymap):
                    if len(syms) > c and syms[c] == lo and (len(syms) <= c + 1 or syms[c + 1] == 0):
                        if i + self._min_kc not in foreign:
                            return i + self._min_kc, 1
        return best

    def _shift_keycode(self) -> int:
        hit = self._lookup(_KEYSYM_SHIFT_L)
        if hit is not None:
            return hit[0]
        return self._scratch_bind(_KEYSYM_SHIFT_L)

    def _pool(self) -> List[int]:
        if self._scratch_pool is None:
            self._ensure_keymap()
            empty = [i + self._min_kc for i, syms in enumerate(self._keymap) if not any(syms)]
            # Keycode 8 maps to evdev KEY_RESERVED and is ignored by some
            # toolkits; use high keycodes first.
            pool = sorted((kc for kc in empty if kc > 8), reverse=True)
            self._scratch_borrow = not pool  # no spare keycode at all: borrow a real one
            if not pool:
                pool = [self._max_kc]
            if self._max_scratch is not None:
                pool = pool[: max(1, int(self._max_scratch))]
            self._scratch_pool = pool
        return self._scratch_pool

    def _scratch_bind(self, keysym: int) -> int:
        """Bind ``keysym`` to a spare keycode (re-using an existing binding) and return it.

        Other clients -- including other :class:`X11Backend` instances, in this
        process or another one -- may bind spare keycodes too. A keycode is only
        taken when the server still shows it empty (or bound by us), checked and
        changed under a server grab so two backends never share one.
        """
        st = self._state
        kc = st.bound.get(keysym)
        if kc is not None and self._syms(kc)[:1] == (keysym,):
            st.last_used[kc] = time.monotonic()
            return kc
        pool = self._pool()
        held = set(st.held_keycodes)
        free = [
            k for k in pool
            if k not in st.keycode_sym and k not in held and (self._scratch_borrow or not any(self._syms(k)))
        ]
        ours = sorted((k for k in pool if k in st.keycode_sym and k not in held), key=lambda k: st.last_used.get(k, 0.0))
        if not free and not ours:
            raise BackendError("No spare keycode available to type this character (all are in use or held down)")
        if not free:
            wait = st.remap_settle - (time.monotonic() - st.last_used.get(ours[0], 0.0))
            if wait > 0:
                time.sleep(wait)
        d = self._d
        chosen: Optional[int] = None
        current: Tuple[int, ...] = ()
        d.grab_server()
        try:
            for cand in free + ours:
                current = tuple(int(v) for v in d.get_keyboard_mapping(cand, 1)[0])
                mine = st.keycode_sym.get(cand)
                if mine is not None:
                    if current[:1] == (mine,):
                        chosen = cand
                        break
                    # Someone else re-bound our keycode: it is theirs now.
                    st.keycode_sym.pop(cand, None)
                    st.bound.pop(mine, None)
                    st.original.pop(cand, None)
                elif self._scratch_borrow or not any(current):
                    chosen = cand
                    break
                self._set_cached_syms(cand, current)  # bound by another client
            if chosen is None:
                raise BackendError("No spare keycode available to type this character (all are in use by other clients)")
            if chosen not in st.original:
                st.original[chosen] = current or (0,)
            old = st.keycode_sym.pop(chosen, None)
            if old is not None:
                st.bound.pop(old, None)
            marks = _scratch_marks(d)
            if chosen not in marks:
                _set_scratch_marks(d, marks | {chosen})
            d.change_keyboard_mapping(chosen, [(keysym, keysym)])
        finally:
            d.ungrab_server()
        self._sync()
        st.bound[keysym] = chosen
        st.keycode_sym[chosen] = keysym
        st.last_used[chosen] = time.monotonic()
        # Keep our cache coherent without waiting for MappingNotify.
        self._set_cached_syms(chosen, (keysym, keysym))
        return chosen

    def _set_cached_syms(self, keycode: int, syms: Tuple[int, ...]) -> None:
        i = keycode - self._min_kc
        if 0 <= i < len(self._keymap):
            width = max(len(self._keymap[i]), len(syms))
            self._keymap[i] = tuple(syms) + (0,) * (width - len(syms))

    def _pointer_mask(self) -> int:
        try:
            return int(self._root.query_pointer().mask)
        except Exception:
            return 0

    @staticmethod
    def _group(mask: int) -> int:
        """Active XKB group (0 = first layout) from a core event/pointer state mask."""
        return (mask >> 13) & 3

    def _resolve(self, keysym: int, *, text: bool, mask: int = 0, group: int = 0) -> Tuple[int, bool]:
        """Return ``(keycode, needs_shift)`` for ``keysym``, binding a scratch keycode if needed.

        Character keysyms are looked up in the active XKB ``group`` (the key that
        types "y" differs between layouts); other keys (modifiers, Return, F-keys)
        are the same in every group.
        """
        if not _is_char_keysym(keysym):
            group = 0
        hit = self._lookup(keysym, group)
        if hit is None:
            return self._scratch_bind(keysym), False
        kc, level = hit
        need_shift = level == 1
        col0, col1 = self._group_syms(kc, group)
        if self._numlock_mask and mask & self._numlock_mask and _is_keypad_keysym(col1) and col0 != col1 and col0 and col1:
            need_shift = not need_shift  # NumLock selects the second keypad column
        elif text and mask & self._xl.X.LockMask:
            lo, up = _lower_upper(col0)
            if lo != up and (col1 in (0, up)):
                need_shift = not need_shift  # CapsLock flips letter case
        if kc in self._state.keycode_sym:
            self._state.last_used[kc] = time.monotonic()
        return kc, need_shift

    def _press(self, kc: int) -> None:
        self._fake(self._xl.X.KeyPress, kc)
        self._state.held_keycodes[kc] = self._state.held_keycodes.get(kc, 0) + 1

    def _release(self, kc: int) -> None:
        self._fake(self._xl.X.KeyRelease, kc)
        n = self._state.held_keycodes.get(kc, 0) - 1
        if n > 0:
            self._state.held_keycodes[kc] = n
        else:
            self._state.held_keycodes.pop(kc, None)
        if kc in self._state.keycode_sym:
            self._state.last_used[kc] = time.monotonic()

    @_x_errors
    def key_down(self, key: str) -> None:
        """Press a canonical key or single character (Shift is added for shifted characters)."""
        self._need_xtest()
        keysym = key_to_keysym(key)
        with self._lock:
            held = self._held_keys.get(key)
            if held is not None:
                # Already down: send another press (auto-repeat), keep the original record.
                self._press(held[0])
                self._sync()
                return
            self._ensure_keymap()
            # A character tapped without Ctrl/Alt/Super is text: honour CapsLock so
            # key_down("A") types "A" whatever the lock state. In shortcuts the
            # keysym's own level is used (ctrl+a must not gain a Shift).
            text = len(key) == 1 and not any(k in self._held_keys for k in _COMMAND_MODIFIERS)
            char = _is_char_keysym(keysym)
            mask = self._pointer_mask() if (text or char or self._numlock_mask) else 0
            kc, need_shift = self._resolve(keysym, text=text, mask=mask, group=self._group(mask))
            shift_kc: Optional[int] = None
            if need_shift and not any(k in self._held_keys for k in ("shift", "shift_r")):
                shift_kc = self._shift_keycode()
                self._press(shift_kc)
            self._press(kc)
            self._held_keys[key] = (kc, shift_kc)
            self._sync()

    @_x_errors
    def key_up(self, key: str) -> None:
        self._need_xtest()
        keysym = key_to_keysym(key)
        with self._lock:
            rec = self._held_keys.pop(key, None)
            if rec is None:
                self._ensure_keymap()
                mask = self._pointer_mask() if _is_char_keysym(keysym) else 0
                kc, _ = self._resolve(keysym, text=False, group=self._group(mask))
                shift_kc = None
            else:
                kc, shift_kc = rec
            self._unhold(kc)
            if shift_kc is not None:
                self._unhold(shift_kc)
            self._sync()

    def _unhold(self, kc: int) -> None:
        """Drop one hold of ``kc``; release the key unless another held key still needs it.

        XTEST does not count presses, so a single release lifts the key: the
        implicit Shift of ``key_down("A")`` must stay down while ``shift`` (or
        another shifted character) is still held, and a repeated ``key_down()``
        must never leave the key stuck.
        """
        if any(kc in (rk, sk) for rk, sk in self._held_keys.values()):
            n = self._state.held_keycodes.get(kc, 0) - 1
            if n > 0:
                self._state.held_keycodes[kc] = n
            else:
                self._state.held_keycodes.pop(kc, None)
            return
        self._release(kc)
        self._state.held_keycodes.pop(kc, None)

    @_x_errors
    def release_all(self) -> None:
        """Release every key and mouse button this backend currently holds down."""
        self._need_xtest()
        X = self._xl.X
        with self._lock:
            for kc, n in list(self._state.held_keycodes.items()):
                self._fake(X.KeyRelease, kc)
            self._state.held_keycodes.clear()
            self._held_keys.clear()
            for btn in list(self._state.held_buttons):
                self._fake(X.ButtonRelease, btn)
            self._state.held_buttons.clear()
            self._sync()

    def _tap(self, kc: int, shift_kc: Optional[int]) -> None:
        if shift_kc is not None:
            self._press(shift_kc)
        self._press(kc)
        self._release(kc)
        if shift_kc is not None:
            self._release(shift_kc)

    @_x_errors
    def type_text(self, text: str) -> None:
        """Type ``text`` exactly, independent of the keyboard layout.

        Characters available on the current layout (with at most Shift) use
        their real keys; everything else (accents on a US layout, emoji, CJK...)
        is typed through temporarily bound spare keycodes. ``\\n`` presses
        Return and ``\\t`` presses Tab. CapsLock and NumLock are compensated
        (for lowercase letters typed through a spare keycode CapsLock is briefly
        switched off and restored afterwards).
        """
        self._need_xtest()
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        if not text:
            return
        with self._lock:
            self._ensure_keymap()
            mask = self._pointer_mask()
            group = self._group(mask)  # XKB group; group != 0 means a non-primary layout is active
            shift_held = any(k in self._held_keys for k in ("shift", "shift_r"))
            shift_kc: Optional[int] = None
            caps_kc: Optional[int] = None  # set while CapsLock is temporarily switched off
            try:
                for tok in split_text_tokens(text):
                    if len(tok) > 1:  # "enter" / "tab"
                        keysym = KEYSYMS[tok]
                    elif tok in _CONTROL_CHAR_KEYS:
                        keysym = KEYSYMS[_CONTROL_CHAR_KEYS[tok]]
                    elif ord(tok) < 0x20 or 0x7F <= ord(tok) < 0xA0:
                        continue  # other C0/C1 control characters cannot be typed
                    else:
                        keysym = char_to_keysym(tok)
                    if caps_kc is None and self._caps_would_uppercase(keysym, mask, group):
                        caps_kc = self._caps_lock_keycode()
                        if caps_kc is not None:
                            self._tap(caps_kc, None)  # CapsLock off
                            self._sync()
                            mask &= ~self._xl.X.LockMask
                    kc, need_shift = self._resolve(keysym, text=True, mask=mask, group=group)
                    extra = None
                    if need_shift and not shift_held:
                        if shift_kc is None:
                            shift_kc = self._shift_keycode()
                        extra = shift_kc
                    self._tap(kc, extra)
                    # Sync per character so scratch-keycode timestamps are accurate
                    # and the server never queues an unbounded burst.
                    self._sync()
            finally:
                if caps_kc is not None:
                    self._tap(caps_kc, None)  # restore CapsLock
                    self._sync()

    def _caps_would_uppercase(self, keysym: int, mask: int, group: int) -> bool:
        """True when CapsLock is on and ``keysym`` (a lowercase letter) would be typed
        on a keycode that is not a ``(lower, upper)`` letter key -- typically a
        scratch keycode bound as ``(keysym, keysym)``. Clients apply CapsLock's
        case conversion to both levels of such keys, so Shift cannot compensate."""
        if not mask & self._xl.X.LockMask:
            return False
        lo, up = _lower_upper(keysym)
        if lo == up or keysym != lo:
            return False
        hit = self._lookup(keysym, group)
        if hit is None:
            return True  # will be bound to a scratch keycode
        kc, level = hit
        col1 = self._group_syms(kc, group)[1]
        return not (level == 0 and col1 in (0, up))

    def _caps_lock_keycode(self) -> Optional[int]:
        """Keycode of a Caps_Lock key that drives the Lock modifier, if the keymap has one."""
        hit = self._lookup(_KEYSYM_CAPS_LOCK)
        if hit is None or hit[1] != 0:
            return None
        try:
            lock_codes = {int(k) for k in self._d.get_modifier_mapping()[self._xl.X.LockMapIndex] if k}
        except Exception:
            return None
        return hit[0] if hit[0] in lock_codes else None

    # =================================================================== screen
    @_x_errors
    def virtual_screen(self) -> Rect:
        self._check()
        with self._lock:
            w, h = self._screen_size()
        return Rect(0, 0, w, h)

    def _xft_scale(self) -> float:
        try:
            atom = self._d.get_atom("RESOURCE_MANAGER")
            prop = self._root.get_full_property(atom, self._xl.Xatom.STRING)
            if prop is None:
                return 1.0
            data = prop.value
            if isinstance(data, bytes):
                data = data.decode("latin-1", "replace")
            for line in str(data).splitlines():
                k, _, v = line.partition(":")
                if k.strip() == "Xft.dpi":
                    dpi = float(v.strip())
                    if dpi > 0:
                        return round(dpi / 96.0, 3)
        except Exception:
            pass
        return 1.0

    @_x_errors
    def monitors(self) -> List[Monitor]:
        """Monitors from RandR (primary first, then left-to-right, top-to-bottom)."""
        self._check()
        with self._lock:
            scale = self._xft_scale()
            raw: List[Tuple[Rect, bool, str]] = []
            if self._randr:
                raw = self._randr_monitors()
            if not raw:
                w, h = self._screen_size()
                raw = [(Rect(0, 0, w, h), True, f"screen{self._d.get_default_screen()}")]
        raw.sort(key=lambda m: (not m[1], m[0].top, m[0].left))
        return [Monitor(i, rect, scale, i == 0, name) for i, (rect, _, name) in enumerate(raw)]

    def _randr_monitors(self) -> List[Tuple[Rect, bool, str]]:
        randr = self._xl.randr
        out: List[Tuple[Rect, bool, str]] = []
        try:
            reply = randr.get_monitors(self._root, True)
            for m in reply.monitors:
                if m.width_in_pixels <= 0 or m.height_in_pixels <= 0:
                    continue
                try:
                    name = self._d.get_atom_name(m.name)
                except Exception:
                    name = ""
                out.append((Rect(int(m.x), int(m.y), int(m.width_in_pixels), int(m.height_in_pixels)), bool(m.primary), str(name)))
            if out:
                return out
        except Exception:
            out = []
        try:  # RandR < 1.5: walk the active CRTCs
            res = randr.get_screen_resources(self._root)
            ts = res.config_timestamp
            try:
                primary_output = randr.get_output_primary(self._root).output
            except Exception:
                primary_output = 0
            for crtc in res.crtcs:
                info = randr.get_crtc_info(self._d, crtc, ts)
                if not info.mode or info.width <= 0 or info.height <= 0:
                    continue
                name = ""
                if info.outputs:
                    try:
                        name = randr.get_output_info(self._d, info.outputs[0], ts).name
                        if isinstance(name, bytes):
                            name = name.decode("utf-8", "replace")
                    except Exception:
                        name = ""
                out.append((Rect(int(info.x), int(info.y), int(info.width), int(info.height)), primary_output in info.outputs, str(name)))
        except Exception:
            return []
        return out

    def _mss(self) -> Any:
        """This thread's mss grabber (older mss releases are bound to their thread).

        Each grabber has its own X connection. It lives in a thread-local slot,
        so it is closed as soon as its thread exits (or the backend is closed or
        garbage collected); short-lived worker threads therefore never pile up
        X connections.
        """
        holder = getattr(self._mss_local, "holder", None)
        if holder is None:
            import mss  # hard dependency, but keep the import lazy

            # mss >= 10 exposes ``mss.MSS``; older releases only the ``mss.mss`` factory.
            factory = getattr(mss, "MSS", None) or mss.mss
            try:
                sct = factory(display=self.display_name)
            except TypeError:
                if os.environ.get("DISPLAY") != self.display_name:
                    raise
                sct = factory()
            holder = _MssHolder(sct)
            with self._lock:
                closers = self._state.mss_closers
                closers[:] = [c for c in closers if c.alive]
                closers.append(weakref.finalize(holder, _close_quietly, sct))
            self._mss_local.holder = holder
        return holder.sct

    def _drop_mss(self) -> None:
        holder = getattr(self._mss_local, "holder", None)
        if holder is not None:
            del self._mss_local.holder  # the finalizer closes the grabber

    def _grab_mss(self, r: Rect) -> Any:
        from PIL import Image

        shot = self._mss().grab({"left": r.left, "top": r.top, "width": r.width, "height": r.height})
        return Image.frombuffer("RGB", (shot.width, shot.height), bytes(shot.bgra), "raw", "BGRX", 0, 1)

    def _grab_xlib(self, r: Rect) -> Any:
        from PIL import Image

        X = self._xl.X
        with self._lock:
            img = self._root.get_image(r.left, r.top, r.width, r.height, X.ZPixmap, 0xFFFFFFFF)
            depth = int(img.depth)
            bpp = next((f.bits_per_pixel for f in self._d.display.info.pixmap_formats if f.depth == depth), 32)
            msb = int(self._d.display.info.image_byte_order) == X.MSBFirst
        data = img.data if isinstance(img.data, bytes) else bytes(img.data)
        if bpp == 32:
            raw = "XRGB" if msb else "BGRX"
            stride = len(data) // r.height if r.height else 0
            return Image.frombuffer("RGB", (r.width, r.height), data, "raw", raw, stride, 1)
        if bpp == 16 and depth == 16 and not msb:
            stride = len(data) // r.height if r.height else 0
            return Image.frombuffer("RGB", (r.width, r.height), data, "raw", "BGR;16", stride, 1)
        if bpp == 24 and not msb:
            stride = len(data) // r.height if r.height else 0
            return Image.frombuffer("RGB", (r.width, r.height), data, "raw", "BGR", stride, 1)
        raise BackendError(f"Unsupported X visual for screenshots (depth {depth}, {bpp} bpp, msb={msb})")

    def _grab(self, r: Rect) -> Any:
        if not self._mss_broken:
            try:
                img = self._grab_mss(r)
            except Exception as exc:
                self._drop_mss()  # a fresh grabber (and connection) is made next time
                self._check()
                if self._screenshot_method == "mss":
                    raise BackendError(f"mss screenshot failed: {exc}") from exc
                # A one-off failure (e.g. the X server's client limit was hit
                # momentarily) only falls back for this grab; give up on mss
                # after repeated failures.
                self._mss_failures += 1
                if self._mss_failures >= 3:
                    self._mss_broken = True
            else:
                self._mss_failures = 0
                return img
        return self._grab_xlib(r)

    @_x_errors
    def screenshot(self, region: Optional[Rect] = None) -> Any:
        """RGB screenshot of ``region`` (default: whole screen); off-screen parts are black."""
        from PIL import Image

        self._check()
        vs = self.virtual_screen()
        r = vs if region is None else Rect.coerce(region)
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Screenshot region must have a positive size, got {r}")
        inter = r.intersect(vs)
        if inter is None:
            return Image.new("RGB", (r.width, r.height), (0, 0, 0))
        img = self._grab(inter)
        if inter == r:
            return img.convert("RGB") if img.mode != "RGB" else img
        canvas = Image.new("RGB", (r.width, r.height), (0, 0, 0))
        canvas.paste(img, (inter.left - r.left, inter.top - r.top))
        return canvas

    # ================================================================== windows
    def _atom(self, name: str) -> int:
        return self._d.get_atom(name)

    def _win_obj(self, xid: int) -> Any:
        return self._d.create_resource_object("window", int(xid))

    def _prop(self, win: Any, name: str, ptype: Any = None) -> Any:
        X = self._xl.X
        try:
            p = win.get_full_property(self._atom(name), X.AnyPropertyType if ptype is None else ptype)
        except self._xl.error.XError:
            return None
        return None if p is None else p.value

    def _prop_list(self, win: Any, name: str) -> List[int]:
        v = self._prop(win, name)
        if v is None:
            return []
        try:
            return [int(x) for x in v]
        except TypeError:
            return []

    def _supported(self) -> frozenset:
        """EWMH atoms supported by the running window manager (empty when none is running)."""
        check = self._prop_list(self._root, "_NET_SUPPORTING_WM_CHECK")
        if not check:
            return frozenset()
        child = self._win_obj(check[0])
        if self._prop_list(child, "_NET_SUPPORTING_WM_CHECK")[:1] != check[:1]:
            return frozenset()  # stale property left by a dead WM
        return frozenset(self._prop_list(self._root, "_NET_SUPPORTED"))

    def _has_wm_feature(self, name: str, supported: Optional[frozenset] = None) -> bool:
        sup = self._supported() if supported is None else supported
        return self._atom(name) in sup

    @staticmethod
    def _decode(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            try:
                return bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                return bytes(value).decode("latin-1", "replace")
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:
            return str(value)

    def _title(self, win: Any) -> str:
        title = self._decode(self._prop(win, "_NET_WM_NAME"))
        if not title:
            title = self._decode(self._prop(win, "WM_NAME"))
        return title.rstrip("\x00")

    def _wm_class(self, win: Any) -> Tuple[str, str]:
        raw = self._prop(win, "WM_CLASS")
        if raw is None:
            return "", ""
        parts = self._decode(raw).split("\x00")
        inst = parts[0] if parts else ""
        cls = parts[1] if len(parts) > 1 else ""
        return inst, cls

    @staticmethod
    def _proc_name(pid: Optional[int]) -> str:
        return _proc_comm(pid)

    def _client_window(self, win: Any, depth: int = 2) -> Optional[Any]:
        """ICCCM: find the window carrying WM_STATE in ``win``'s subtree (XmuClientWindow)."""
        if self._prop(win, "WM_STATE") is not None:
            return win
        if depth <= 0:
            return None
        try:
            kids = win.query_tree().children
        except self._xl.error.XError:
            return None
        for k in reversed(kids):
            found = self._client_window(k, depth - 1)
            if found is not None:
                return found
        return None

    def _top_level_xids(self, supported: frozenset) -> List[int]:
        """Client windows, front-most first."""
        if supported:
            stacking = self._prop_list(self._root, "_NET_CLIENT_LIST_STACKING")
            if stacking:
                return list(reversed(stacking))
            clients = self._prop_list(self._root, "_NET_CLIENT_LIST")
            if clients:
                return list(reversed(clients))
        X = self._xl.X
        out: List[int] = []
        try:
            children = self._root.query_tree().children  # bottom-to-top stacking order
        except self._xl.error.XError:
            return out
        for child in reversed(children):
            try:
                attrs = child.get_attributes()
            except self._xl.error.XError:
                continue
            if attrs.override_redirect:
                continue
            if child.id in self._iconified and attrs.map_state != X.IsViewable:
                out.append(child.id)
                continue
            client = self._client_window(child)
            if client is None:
                # No window manager at all: a named, viewable root child is a top-level.
                if attrs.map_state != X.IsViewable:
                    continue
                if not self._title(child) and not any(self._wm_class(child)):
                    continue
                client = child
            if client.id not in out:
                out.append(client.id)
        return out

    def _abs_geometry(self, win: Any) -> Rect:
        """Root-relative rect of ``win`` *including* its X border: the box whose
        top-left corner ``ConfigureWindow(x, y)`` positions (see :meth:`_configure_outer`)."""
        g = win.get_geometry()
        t = self._root.translate_coords(win, 0, 0)
        bw = int(g.border_width)
        return Rect(int(t.x) - bw, int(t.y) - bw, int(g.width) + 2 * bw, int(g.height) + 2 * bw)

    @staticmethod
    def _check_x_rect(r: Rect) -> None:
        """X geometry is INT16 positions and CARD16 sizes; refuse anything else up front."""
        if not (-0x8000 <= r.left <= 0x7FFF and -0x8000 <= r.top <= 0x7FFF and 0 < r.width <= 0xFFFF and 0 < r.height <= 0xFFFF):
            raise ValueError(f"Window rect {r} is outside the X11 coordinate range (x/y -32768..32767, size 1..65535)")

    def _configure_outer(self, win: Any, r: Rect, **kwargs: Any) -> None:
        """Place ``win`` (no window manager) so that its rect including the X border is ``r``."""
        bw = int(win.get_geometry().border_width)
        win.configure(x=r.left, y=r.top, width=max(1, r.width - 2 * bw), height=max(1, r.height - 2 * bw), **kwargs)

    def _frame_extents(self, win: Any) -> Tuple[int, int, int, int]:
        ext = self._prop_list(win, "_NET_FRAME_EXTENTS")
        if len(ext) == 4:
            return ext[0], ext[1], ext[2], ext[3]  # left, right, top, bottom
        return 0, 0, 0, 0

    def _window_info(self, xid: int, active: int, supported: frozenset, current_desktop: Optional[int]) -> Optional[WindowInfo]:
        X = self._xl.X
        win = self._win_obj(xid)
        try:
            attrs = win.get_attributes()
            client = self._abs_geometry(win)
        except self._xl.error.XError:
            return None  # window vanished meanwhile
        types = set(self._prop_list(win, "_NET_WM_WINDOW_TYPE"))
        if types & {self._atom("_NET_WM_WINDOW_TYPE_DESKTOP"), self._atom("_NET_WM_WINDOW_TYPE_DOCK")}:
            return None
        l, r, t, b = self._frame_extents(win)
        rect = Rect(client.left - l, client.top - t, client.width + l + r, client.height + t + b)
        states = set(self._prop_list(win, "_NET_WM_STATE"))
        wm_state = self._prop_list(win, "WM_STATE")
        minimized = self._atom("_NET_WM_STATE_HIDDEN") in states or (bool(wm_state) and wm_state[0] == 3)
        maximized = {self._atom("_NET_WM_STATE_MAXIMIZED_VERT"), self._atom("_NET_WM_STATE_MAXIMIZED_HORZ")} <= states
        if not supported:
            if xid in self._restore_geometry:
                maximized = True
            if xid in self._iconified:
                if attrs.map_state == X.IsViewable:
                    self._iconified.discard(xid)  # someone else mapped it again
                else:
                    minimized = True
        visible = attrs.map_state == X.IsViewable and not minimized
        if current_desktop is not None and visible:
            desk = self._prop_list(win, "_NET_WM_DESKTOP")
            if desk and desk[0] != 0xFFFFFFFF and desk[0] != current_desktop:
                visible = False
        pid_l = self._prop_list(win, "_NET_WM_PID")
        pid = pid_l[0] if pid_l else None
        inst, cls = self._wm_class(win)
        app = self._proc_name(pid) or inst or cls
        return WindowInfo(
            handle=int(xid),
            title=self._title(win),
            rect=rect,
            app=app,
            pid=pid,
            is_active=int(xid) == active,
            is_minimized=minimized,
            is_maximized=maximized,
            is_visible=visible,
            class_name=cls,
        )

    def _active_xid(self, supported: frozenset) -> int:
        if supported:
            act = self._prop_list(self._root, "_NET_ACTIVE_WINDOW")
            if act and act[0]:
                return act[0]
        try:
            focus = self._d.get_input_focus().focus
        except self._xl.error.XError:
            return 0
        if isinstance(focus, int) or focus is None:
            return 0
        # Climb to the root's child, then down to the client window.
        try:
            win = focus
            for _ in range(64):
                tree = win.query_tree()
                parent_id = getattr(tree.parent, "id", tree.parent)
                if not parent_id or parent_id == self._root.id:
                    break
                win = tree.parent
            if win.id == self._root.id:
                return 0
            client = self._client_window(win)
            return (client or win).id
        except self._xl.error.XError:
            return 0

    @_x_errors
    def list_windows(self) -> List[WindowInfo]:
        """Top-level client windows, front-most first (EWMH, or a bare-X fallback)."""
        self._check()
        with self._lock:
            supported = self._supported()
            active = self._active_xid(supported)
            cur = self._prop_list(self._root, "_NET_CURRENT_DESKTOP")
            current_desktop = cur[0] if cur else None
            out: List[WindowInfo] = []
            for xid in self._top_level_xids(supported):
                info = self._window_info(xid, active, supported, current_desktop)
                if info is not None:
                    out.append(info)
            return out

    @_x_errors
    def active_window(self) -> Optional[WindowInfo]:
        self._check()
        with self._lock:
            supported = self._supported()
            active = self._active_xid(supported)
            if not active:
                return None
            cur = self._prop_list(self._root, "_NET_CURRENT_DESKTOP")
            return self._window_info(active, active, supported, cur[0] if cur else None)

    def _handle(self, handle: Any) -> Any:
        """Validate ``handle`` (XID or WindowInfo) and return the Xlib window object."""
        if isinstance(handle, WindowInfo):
            handle = handle.handle
        try:
            xid = int(handle, 0) if isinstance(handle, str) else int(handle)
        except (TypeError, ValueError):
            raise WindowNotFound(f"Invalid X11 window handle {handle!r}") from None
        win = self._win_obj(xid)
        try:
            win.get_attributes()  # unlike GetGeometry, this fails for pixmaps and other non-windows
        except self._xl.error.XError:
            raise WindowNotFound(f"X11 window {xid:#x} does not exist") from None
        return win

    def _client_message(self, win: Any, msg_type: str, data: Sequence[int], to_root: bool = True) -> None:
        X = self._xl.X
        payload = (list(int(v) & 0xFFFFFFFF for v in data) + [0] * 5)[:5]
        ev = self._xl.event.ClientMessage(window=win, client_type=self._atom(msg_type), data=(32, payload))
        if to_root:
            self._root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        else:
            win.send_event(ev, event_mask=0)

    def _checked_sync(self, xid: int, what: str, since: Optional[int] = None) -> None:
        """XSync and raise for X errors reported since ``since`` (an error-sink sequence number)."""
        if since is None:
            since = self._async_errors.seq
        self._sync()
        errs = self._async_errors.since(since)
        if errs:
            err = errs[-1]
            if isinstance(err, (self._xl.error.BadWindow, self._xl.error.BadDrawable)):
                raise WindowNotFound(f"X11 window {xid:#x} disappeared during {what}")
            raise BackendError(f"X error during {what} of window {xid:#x}: {err}")

    @_x_errors
    def activate_window(self, handle: Any) -> None:
        """Raise and focus a window (``_NET_ACTIVE_WINDOW``; map + raise + focus without a WM)."""
        self._check()
        X = self._xl.X
        with self._lock:
            since = self._async_errors.seq
            win = self._handle(handle)
            supported = self._supported()
            if self._has_wm_feature("_NET_ACTIVE_WINDOW", supported):
                desk = self._prop_list(win, "_NET_WM_DESKTOP")
                cur = self._prop_list(self._root, "_NET_CURRENT_DESKTOP")
                if desk and cur and desk[0] not in (cur[0], 0xFFFFFFFF) and self._has_wm_feature("_NET_CURRENT_DESKTOP", supported):
                    self._client_message(self._root, "_NET_CURRENT_DESKTOP", [desk[0], X.CurrentTime])
                self._client_message(win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
            else:
                win.map()
                self._iconified.discard(win.id)
                win.configure(stack_mode=X.Above)
                self._sync()
                try:
                    if win.get_attributes().map_state == X.IsViewable:
                        win.set_input_focus(X.RevertToParent, X.CurrentTime)
                except self._xl.error.XError:
                    pass
            self._checked_sync(win.id, "activate", since)

    @_x_errors
    def move_window(self, handle: Any, rect: Rect) -> None:
        """Move/resize so the window's *outer* rect (including WM frame) becomes ``rect``."""
        self._check()
        r = Rect.coerce(rect)
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Window size must be positive, got {r}")
        self._check_x_rect(r)
        with self._lock:
            since = self._async_errors.seq
            win = self._handle(handle)
            supported = self._supported()
            l, rr, t, b = self._frame_extents(win)
            bw = int(win.get_geometry().border_width)
            cw, ch = max(1, r.width - l - rr - 2 * bw), max(1, r.height - t - b - 2 * bw)
            if supported:
                states = set(self._prop_list(win, "_NET_WM_STATE"))
                maxed = {self._atom("_NET_WM_STATE_MAXIMIZED_VERT"), self._atom("_NET_WM_STATE_MAXIMIZED_HORZ")} & states
                if maxed and self._has_wm_feature("_NET_WM_STATE", supported):
                    # WMs ignore geometry requests for maximized windows.
                    atoms = sorted(maxed) + [0]
                    self._client_message(win, "_NET_WM_STATE", [0, atoms[0], atoms[1], 2])
            if supported and self._has_wm_feature("_NET_MOVERESIZE_WINDOW", supported):
                flags = 1 | (0xF << 8) | (2 << 12)  # NorthWest gravity, x/y/w/h present, pager source
                self._client_message(win, "_NET_MOVERESIZE_WINDOW", [flags, r.left, r.top, cw, ch])
            else:
                x, y = r.left + (l if not supported else 0), r.top + (t if not supported else 0)
                self._check_x_rect(Rect(x, y, cw, ch))
                win.configure(x=x, y=y, width=cw, height=ch)
            self._restore_geometry.pop(win.id, None)
            self._checked_sync(win.id, "move", since)

    @_x_errors
    def set_window_state(self, handle: Any, state: str) -> None:
        """``state`` in {"minimized", "maximized", "normal"}."""
        self._check()
        st = str(state).strip().lower()
        if st not in ("minimized", "maximized", "normal"):
            raise ValueError(f"Unknown window state {state!r}; expected minimized, maximized or normal")
        X = self._xl.X
        with self._lock:
            since = self._async_errors.seq
            win = self._handle(handle)
            supported = self._supported()
            max_atoms = [self._atom("_NET_WM_STATE_MAXIMIZED_VERT"), self._atom("_NET_WM_STATE_MAXIMIZED_HORZ")]
            ewmh_state = self._has_wm_feature("_NET_WM_STATE", supported)
            if st == "minimized":
                if supported:
                    self._client_message(win, "WM_CHANGE_STATE", [3])  # ICCCM IconicState
                else:
                    win.unmap()
                    self._iconified.add(win.id)
            elif st == "maximized":
                if supported and ewmh_state:
                    if self._prop_list(win, "WM_STATE")[:1] == [3] or win.get_attributes().map_state != X.IsViewable:
                        self._client_message(win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
                    self._client_message(win, "_NET_WM_STATE", [1, max_atoms[0], max_atoms[1], 2])
                else:
                    win.map()
                    self._iconified.discard(win.id)
                    cur = self._abs_geometry(win)
                    mons = self.monitors()
                    target = max(mons, key=lambda m: (m.rect.intersect(cur).area if m.rect.intersect(cur) else 0, m.is_primary)).rect
                    if win.id not in self._restore_geometry:
                        self._restore_geometry[win.id] = cur
                    self._configure_outer(win, target, stack_mode=X.Above)
            else:  # normal
                if supported:
                    if ewmh_state:
                        self._client_message(win, "_NET_WM_STATE", [0, max_atoms[0], max_atoms[1], 2])
                    minimized = self._prop_list(win, "WM_STATE")[:1] == [3] or self._atom("_NET_WM_STATE_HIDDEN") in set(
                        self._prop_list(win, "_NET_WM_STATE")
                    )
                    if minimized:
                        win.map()
                        if self._has_wm_feature("_NET_ACTIVE_WINDOW", supported):
                            self._client_message(win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
                else:
                    win.map()
                    self._iconified.discard(win.id)
                    prev = self._restore_geometry.pop(win.id, None)
                    if prev is not None:
                        self._configure_outer(win, prev)
            self._checked_sync(win.id, f"set_window_state({st})", since)

    @_x_errors
    def close_window(self, handle: Any) -> None:
        """Politely close a window: ``_NET_CLOSE_WINDOW``, else ``WM_DELETE_WINDOW``, else destroy it."""
        self._check()
        X = self._xl.X
        with self._lock:
            since = self._async_errors.seq
            win = self._handle(handle)
            supported = self._supported()
            if self._has_wm_feature("_NET_CLOSE_WINDOW", supported):
                self._client_message(win, "_NET_CLOSE_WINDOW", [X.CurrentTime, 2])
            elif self._atom("WM_DELETE_WINDOW") in self._prop_list(win, "WM_PROTOCOLS"):
                self._client_message(win, "WM_PROTOCOLS", [self._atom("WM_DELETE_WINDOW"), X.CurrentTime], to_root=False)
            else:
                win.destroy()
            self._restore_geometry.pop(win.id, None)
            self._iconified.discard(win.id)
            self._checked_sync(win.id, "close", since)

    def launch(self, command: Any, **kwargs: Any) -> Any:
        """Start an application on *this* display (``DISPLAY`` is set in its environment)."""
        if "env" not in kwargs:
            env = dict(os.environ)
            env["DISPLAY"] = self.display_name
            kwargs["env"] = env
        return super().launch(command, **kwargs)

    # ================================================================ clipboard
    def _detect_clipboard(self, tool: Optional[str]) -> Optional[Tuple[List[str], List[str]]]:
        tools = {
            "xclip": (["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]),
            "xsel": (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]),
            "wl-clipboard": (["wl-paste", "--no-newline"], ["wl-copy"]),
        }
        # "native" = python-xlib selection handling (used automatically when no helper is installed)
        self._clip_mode = "native"
        if tool is not None:
            t = tool.lower()
            if t in ("none", ""):
                self._clip_mode = "none"
                return None
            if t in ("native", "xlib"):
                return None
            if t in ("wl-copy", "wl-paste", "wayland"):
                t = "wl-clipboard"
            if t not in tools:
                raise ValueError(f"Unknown clipboard tool {tool!r}; expected one of {', '.join(tools)} or 'none'")
            get, put = tools[t]
            if shutil.which(get[0]) is None or shutil.which(put[0]) is None:
                raise BackendUnavailable(f"Clipboard tool {tool!r} is not installed")
            return list(get), list(put)
        order = ["xclip", "xsel"]
        if os.environ.get("WAYLAND_DISPLAY"):
            order.insert(0, "wl-clipboard")
        for name in order:
            get, put = tools[name]
            if shutil.which(get[0]) and shutil.which(put[0]):
                return list(get), list(put)
        return None

    def _native_clipboard(self) -> Any:
        if self._clip_mode == "none":
            raise NotSupported("Clipboard support was disabled (clipboard_tool='none')")
        with self._lock:
            self._check()
            if self._state.clipboard is None:
                from ._x11clip import X11Clipboard

                # Kept in the finalizer state so it is closed (connection and
                # serving thread) even when the backend is never closed.
                self._state.clipboard = X11Clipboard(self.display_name)
            return self._state.clipboard

    def _clip_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["DISPLAY"] = self.display_name
        return env

    @_x_errors
    def get_clipboard(self) -> str:
        """Clipboard text (empty string when the clipboard is empty or holds no text)."""
        self._check()
        if self._clip_cmds is None:
            return self._native_clipboard().get()
        try:
            proc = subprocess.run(
                self._clip_cmds[0], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._clip_env(), timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"Clipboard read timed out ({' '.join(self._clip_cmds[0])})") from exc
        except OSError as exc:
            raise BackendError(f"Cannot run clipboard helper: {exc}") from exc
        if proc.returncode != 0:
            return ""  # helpers exit non-zero when there is no text selection owner
        return proc.stdout.decode("utf-8", "replace")

    @_x_errors
    def set_clipboard(self, text: str) -> None:
        self._check()
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        if self._clip_cmds is None:
            self._native_clipboard().set(text)
            return
        try:
            # The helpers fork a daemon that owns the selection; never capture
            # its output or run() would wait for the daemon to exit.
            proc = subprocess.run(
                self._clip_cmds[1], input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self._clip_env(), timeout=5, start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"Clipboard write timed out ({' '.join(self._clip_cmds[1])})") from exc
        except OSError as exc:
            raise BackendError(f"Cannot run clipboard helper: {exc}") from exc
        if proc.returncode != 0:
            raise BackendError(f"Clipboard helper {self._clip_cmds[1][0]!r} failed with exit code {proc.returncode}")

    def wait_clipboard_read(self, since: float, timeout: float) -> Optional[bool]:
        """With the native clipboard: wait until an application fetched the text we own
        (see :meth:`Backend.wait_clipboard_read`). Helper tools own the selection in
        another process, so they cannot tell (None)."""
        if self._clip_cmds is not None or self._clip_mode == "none":
            return None
        with self._lock:
            clip = self._state.clipboard
        if clip is None:
            return None
        return clip.wait_read(since, timeout)

    # ============================================================ accessibility
    def _atspi(self) -> Any:
        if not self._atspi_checked:
            self._atspi_checked = True
            if self._atspi_source == "auto":
                self._atspi_mod = load_atspi()
            else:
                self._atspi_mod = self._atspi_source
        return self._atspi_mod

    def _require_atspi(self) -> Any:
        self._check()
        mod = self._atspi()
        if mod is None:
            raise NotSupported(
                "AT-SPI accessibility is unavailable: install PyGObject with the Atspi typelib "
                "(e.g. `apt install python3-gi gir1.2-atspi-2.0 at-spi2-core`)"
            )
        return mod

    def _make_resolver(self) -> Callable[[AtspiNode], Any]:
        cache: Dict[str, Any] = {}

        def windows() -> List[WindowInfo]:
            if "w" not in cache:
                try:
                    cache["w"] = self.list_windows()
                except Exception:
                    cache["w"] = []
            return cache["w"]

        def resolve(node: AtspiNode) -> Any:
            if node.role not in ("window", "dialog"):
                return None
            pid, title, bounds = node.pid, node.name, node.bounds
            wins = windows()
            same_pid = [w for w in wins if pid and w.pid == pid]
            for w in same_pid:
                if title and w.title == title:
                    return w.handle
            if bounds is not None:
                pool = same_pid or wins
                best = max(pool, key=lambda w: w.rect.iou(bounds), default=None)
                if best is not None and best.rect.iou(bounds) > 0.5:
                    return best.handle
            if len(same_pid) == 1:
                return same_pid[0].handle
            for w in wins:
                if title and w.title == title:
                    return w.handle
            return None

        return resolve

    def accessibility_roots(self) -> List[AccessibleNode]:
        """Top-level accessible windows of every AT-SPI application."""
        atspi = self._require_atspi()
        try:
            desktop = atspi.get_desktop(0)
        except Exception as exc:
            raise NotSupported(f"AT-SPI registry unavailable: {exc}") from exc
        if desktop is None:
            raise NotSupported("AT-SPI registry unavailable (is at-spi2-registryd running?)")
        resolver = self._make_resolver()
        desktop_node = AtspiNode(desktop, atspi)
        roots: List[AccessibleNode] = []
        for app in desktop_node.children():
            for win in app.children():
                roots.append(AtspiNode(win.accessible, atspi, app, resolver))
        return roots

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        """Deepest accessible at ``(x, y)``, descending with AT-SPI hit-testing."""
        roots = [r for r in self.accessibility_roots() if isinstance(r, AtspiNode)]
        try:
            order = {w.handle: i for i, w in enumerate(self.list_windows()) if w.is_visible}
        except Exception:
            order = {}
        hits = []
        for r in roots:
            b = r.bounds
            if b is None or not b.contains((x, y)) or "offscreen" in r.states:
                continue
            h = r.handle
            hits.append((order.get(h, len(order) + 1), b.area, r))
        if not hits:
            return None
        hits.sort(key=lambda t: (t[0], t[1]))
        node: AtspiNode = hits[0][2]
        for _ in range(256):
            child = node.child_at_point(x, y)
            if child is None:
                break
            node = child
        return node

    def focused_element(self) -> Optional[AccessibleNode]:
        roots = self.accessibility_roots()
        active = [r for r in roots if "active" in r.states] or roots
        for root in active:
            for node in root.walk(max_depth=48):
                if "focused" in node.states:
                    return node
        return None

    def __repr__(self) -> str:
        return f"<X11Backend display={self.display_name!r}{' closed' if self.closed else ''}>"


def iter_keysym_table() -> Iterator[Tuple[str, int]]:
    """Yield ``(canonical_name, keysym)`` for every name in :data:`locum.keys.KEYS`."""
    for name in sorted(KEYS):
        yield name, KEYSYMS[name]


__all__ = [
    "AtspiNode",
    "BUTTONS",
    "KEYSYMS",
    "X11Backend",
    "char_to_keysym",
    "iter_keysym_table",
    "key_to_keysym",
    "load_atspi",
    "map_atspi_role",
]
