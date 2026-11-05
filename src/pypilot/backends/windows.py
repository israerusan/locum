"""Windows backend built on ``ctypes`` (Win32) and UI Automation (``comtypes``).

Features
--------
* **DPI awareness**: the process is made *per-monitor DPI aware (v2)* on first
  use (with fallbacks to per-monitor v1, ``shcore.SetProcessDpiAwareness`` and
  ``SetProcessDPIAware``), so mouse coordinates, window rectangles, monitor
  geometry, UIA bounding rectangles and screenshots all use the same
  **physical pixel** coordinate space.
* **Input** through ``SendInput``:

  - mouse motion with ``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK``,
    normalized over the *virtual desktop* (multi-monitor, negative origins),
    with a ``SetCursorPos`` correction if the cursor did not land exactly;
  - left/right/middle/X1/X2 buttons (honouring swapped mouse buttons so
    ``"left"`` always means the *primary* button);
  - vertical and horizontal wheel (``WHEEL_DELTA`` = 120 per notch);
  - keyboard events by virtual-key code for every canonical name in
    :data:`pypilot.keys.KEYS` (with the extended-key flag where the hardware
    sends an ``E0`` prefix), single characters through ``VkKeyScanExW`` in the
    foreground window's keyboard layout, and
  - layout-independent text through ``KEYEVENTF_UNICODE`` (UTF-16 code units,
    so characters outside the BMP are sent as surrogate pairs).

  Every injected event carries :data:`INJECTED_EXTRA_INFO` in ``dwExtraInfo``
  so low-level hooks (e.g. the recorder) can recognise pypilot's own input.
* **Monitors** via ``EnumDisplayMonitors`` + ``GetMonitorInfoW`` +
  ``GetDpiForMonitor`` (primary monitor first).
* **Screenshots** via ``mss`` with a GDI ``BitBlt`` + ``GetDIBits`` fallback.
* **Windows** via ``EnumWindows`` (visible, titled, non-tool, non-cloaked, in
  Z-order), DWM extended frame bounds for rectangles (no invisible resize
  borders), process names via ``QueryFullProcessImageNameW``; activation uses
  ``AttachThreadInput`` and the ALT-tap trick to get past the foreground lock.
* **Clipboard** via ``CF_UNICODETEXT`` with retries while another process
  holds the clipboard open.
* **Accessibility** via UI Automation (``IUIAutomation`` through ``comtypes``),
  exposed as :class:`UIANode` with portable role names, native patterns
  (Invoke, Value, RangeValue, Toggle, ExpandCollapse, SelectionItem,
  ScrollItem, LegacyIAccessible), ``RuntimeId`` identity and lazy children.

The module imports cleanly on every platform: all ``windll``/``WINFUNCTYPE``
access happens lazily inside :class:`_Win32`, and :class:`WindowsBackend`
raises :class:`~pypilot.errors.BackendUnavailable` when not running on Windows.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import threading
import time
import weakref
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound
from ..keys import KEYS, normalize_key, split_text_tokens
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

# =========================================================================== C types
# Fixed-width types are used instead of ``ctypes.wintypes`` so that the struct
# layouts are identical to Windows (LLP64: LONG/DWORD are 32-bit even on 64-bit)
# and can be verified on any platform.
BOOL = ctypes.c_int32
INT = ctypes.c_int32
UINT = ctypes.c_uint32
LONG = ctypes.c_int32
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
SHORT = ctypes.c_int16
BYTE = ctypes.c_ubyte
WCHAR = ctypes.c_wchar
ULONG_PTR = ctypes.c_size_t
LONG_PTR = ctypes.c_ssize_t
SIZE_T = ctypes.c_size_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HRESULT = ctypes.c_int32
HANDLE = ctypes.c_void_p
HWND = HANDLE
HDC = HANDLE
HMONITOR = HANDLE
HBITMAP = HANDLE
HGDIOBJ = HANDLE
HGLOBAL = HANDLE
HKL = HANDLE
HMENU = HANDLE
HINSTANCE = HANDLE
LPVOID = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p
LPWSTR = ctypes.POINTER(ctypes.c_wchar)


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]

    def to_rect(self) -> Rect:
        return Rect.from_ltrb(self.left, self.top, self.right, self.bottom)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    """``INPUT``: 40 bytes on 64-bit Windows, 28 bytes on 32-bit."""

    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", DWORD),
        ("szDevice", WCHAR * 32),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", DWORD),
        ("biWidth", LONG),
        ("biHeight", LONG),
        ("biPlanes", WORD),
        ("biBitCount", WORD),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", LONG),
        ("biYPelsPerMeter", LONG),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("fMask", DWORD),  # ULONG: 32-bit on Windows
        ("hwnd", HWND),
        ("lpVerb", LPCWSTR),
        ("lpFile", LPCWSTR),
        ("lpParameters", LPCWSTR),
        ("lpDirectory", LPCWSTR),
        ("nShow", INT),
        ("hInstApp", HINSTANCE),
        ("lpIDList", LPVOID),
        ("lpClass", LPCWSTR),
        ("hkeyClass", HANDLE),
        ("dwHotKey", DWORD),
        ("hIconOrMonitor", HANDLE),
        ("hProcess", HANDLE),
    ]


# =========================================================================== constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
WHEEL_DELTA = 120

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MAPVK_VK_TO_VSC = 0
MAPVK_VK_TO_CHAR = 2
MAPVK_VK_TO_VSC_EX = 4

#: Marker placed in ``dwExtraInfo`` of every event pypilot injects ("PYOL").
INJECTED_EXTRA_INFO = 0x50594F4C

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_SWAPBUTTON = 23
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MONITORINFOF_PRIMARY = 0x1
MDT_EFFECTIVE_DPI = 0
LOGPIXELSX = 88

GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_MAXIMIZE = 3
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

WM_CLOSE = 0x0010

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

CF_UNICODETEXT = 13
#: Registered clipboard formats that keep text out of Windows Clipboard History
#: (Win+V), cloud clipboard sync and clipboard monitors ("Clipboard Formats",
#: Microsoft docs). Each is set alongside the text with a DWORD 0 payload.
CLIPBOARD_EXCLUSION_FORMATS = (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)
#: Clipboard formats whose data is a GDI/metafile handle or owner-drawn, not a global
#: memory block: :meth:`WindowsBackend.save_clipboard` cannot copy them (CF_BITMAP is
#: re-synthesized by Windows from the saved CF_DIB). 0x200-0x3FF: private/GDI objects.
CLIPBOARD_HANDLE_FORMATS = frozenset({2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E})
#: Formats larger than this are not kept by a clipboard snapshot.
CLIPBOARD_SNAPSHOT_MAX = 64 * 1024 * 1024
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3


class ClipboardSnapshot:
    """Every copyable format of the clipboard, as ``[(format id, bytes)]`` (see :meth:`WindowsBackend.save_clipboard`)."""

    __slots__ = ("items",)

    def __init__(self, items: List[Tuple[int, bytes]]):
        self.items = list(items)

    def __repr__(self) -> str:
        return f"<ClipboardSnapshot formats={[f for f, _ in self.items]}>"

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BI_RGB = 0
DIB_RGB_COLORS = 0

DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
PROCESS_PER_MONITOR_DPI_AWARE = 2
ERROR_ACCESS_DENIED = 5
E_ACCESSDENIED = -2147024891  # 0x80070005 as a signed HRESULT

# ------------------------------------------------------------------ virtual-key codes
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_TAB = 0x09

#: Canonical key name -> Windows virtual-key code. Covers every name in :data:`pypilot.keys.KEYS`.
VK_CODES: Dict[str, int] = {
    # modifiers (left/right specific codes are accepted by SendInput)
    "shift": 0xA0,  # VK_LSHIFT
    "shift_r": 0xA1,  # VK_RSHIFT
    "ctrl": 0xA2,  # VK_LCONTROL
    "ctrl_r": 0xA3,  # VK_RCONTROL
    "alt": 0xA4,  # VK_LMENU
    "alt_r": 0xA5,  # VK_RMENU
    "altgr": 0xA5,  # AltGr is the right Alt key (Windows synthesises Ctrl+Alt for it)
    "cmd": 0x5B,  # VK_LWIN
    "cmd_r": 0x5C,  # VK_RWIN
    # The Fn key is handled in keyboard firmware and has no virtual-key code;
    # 0xFF ("no VK mapping") is what keyboards that do report Fn send.
    "fn": 0xFF,
    # editing / navigation
    "enter": 0x0D,  # VK_RETURN
    "tab": 0x09,  # VK_TAB
    "space": 0x20,  # VK_SPACE
    "backspace": 0x08,  # VK_BACK
    "delete": 0x2E,  # VK_DELETE
    "escape": 0x1B,  # VK_ESCAPE
    "insert": 0x2D,  # VK_INSERT
    "home": 0x24,  # VK_HOME
    "end": 0x23,  # VK_END
    "pageup": 0x21,  # VK_PRIOR
    "pagedown": 0x22,  # VK_NEXT
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    # locks / system
    "capslock": 0x14,  # VK_CAPITAL
    "numlock": 0x90,  # VK_NUMLOCK
    "scrolllock": 0x91,  # VK_SCROLL
    "printscreen": 0x2C,  # VK_SNAPSHOT
    "pause": 0x13,  # VK_PAUSE
    "menu": 0x5D,  # VK_APPS
    # function keys F1..F24
    **{f"f{i}": 0x70 + (i - 1) for i in range(1, 25)},
    # numpad
    **{f"num{i}": 0x60 + i for i in range(10)},  # VK_NUMPAD0..9
    "num_multiply": 0x6A,
    "num_add": 0x6B,
    "num_subtract": 0x6D,
    "num_decimal": 0x6E,
    "num_divide": 0x6F,
    "num_enter": 0x0D,  # VK_RETURN + extended flag
    # media
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "nexttrack": 0xB0,
    "prevtrack": 0xB1,
    "stop": 0xB2,  # VK_MEDIA_STOP
    "playpause": 0xB3,
    # browser
    "browser_back": 0xA6,
    "browser_forward": 0xA7,
    "browser_refresh": 0xA8,
}

#: Keys whose hardware scan code carries the ``E0`` prefix and therefore need
#: ``KEYEVENTF_EXTENDEDKEY`` (right Ctrl/Alt, Win keys, the navigation cluster,
#: arrows, NumLock, PrintScreen, numpad divide/enter, media/browser keys).
EXTENDED_KEYS = frozenset(
    {
        "ctrl_r", "alt_r", "altgr", "cmd", "cmd_r", "menu",
        "insert", "delete", "home", "end", "pageup", "pagedown",
        "up", "down", "left", "right",
        "numlock", "printscreen", "num_divide", "num_enter",
        "volumemute", "volumedown", "volumeup", "nexttrack", "prevtrack", "stop", "playpause",
        "browser_back", "browser_forward", "browser_refresh",
    }
)

#: Canonical modifier name -> VK used when ``VkKeyScanExW`` reports a shift state.
_SHIFT_STATE_MODIFIERS = ((0x01, "shift"), (0x02, "ctrl"), (0x04, "alt"))

#: Upper bound on INPUT structures passed to a single SendInput call.
_SENDINPUT_CHUNK = 256


# =========================================================================== pure helpers
def vk_for_key(key: str) -> Tuple[int, bool]:
    """Return ``(virtual_key, extended)`` for a canonical key name.

    Raises:
        KeyError: ``key`` is not a canonical multi-character key name.
    """
    return VK_CODES[key], key in EXTENDED_KEYS


def iter_vk_table() -> Iterable[Tuple[str, int, bool]]:
    """Yield ``(canonical_name, vk, extended)`` for every name in :data:`pypilot.keys.KEYS`."""
    for name in sorted(KEYS):
        vk, ext = vk_for_key(name)
        yield name, vk, ext


def _norm_axis(value: int, origin: int, size: int) -> int:
    if size <= 1:
        return 0
    v = min(max(int(value), origin), origin + size - 1) - origin
    # Aim at the centre of the pixel's bucket in the 0..65535 space so the
    # result is exact whether Windows truncates or rounds when de-normalizing.
    return min(65535, ((2 * v + 1) * 32768) // size)


def normalize_absolute(x: int, y: int, virtual: Rect) -> Tuple[int, int]:
    """Map virtual-desktop pixel ``(x, y)`` to ``MOUSEEVENTF_ABSOLUTE|VIRTUALDESK`` units (0..65535).

    ``virtual`` is the virtual desktop rectangle (``SM_XVIRTUALSCREEN`` ...),
    whose origin may be negative when a monitor sits left of/above the
    primary one. Points outside it are clamped to its edge.
    """
    return _norm_axis(x, virtual.left, virtual.width), _norm_axis(y, virtual.top, virtual.height)


def denormalize_absolute(nx: int, ny: int, virtual: Rect) -> Tuple[int, int]:
    """Inverse of :func:`normalize_absolute` as Windows computes it (``origin + n * size / 65536``)."""
    return (
        virtual.left + (int(nx) * virtual.width) // 65536,
        virtual.top + (int(ny) * virtual.height) // 65536,
    )


def utf16_units(text: str) -> List[int]:
    """Split ``text`` into UTF-16 code units (non-BMP characters become surrogate pairs)."""
    data = text.encode("utf-16-le", "surrogatepass")
    return [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]


def decode_vk_scan(result: int) -> Optional[Tuple[int, int]]:
    """Decode a ``VkKeyScan(Ex)W`` result into ``(vk, shift_state)`` or None if unmappable.

    ``shift_state`` bits: 1 = Shift, 2 = Ctrl, 4 = Alt (bits 8/16/32 are
    Hankaku/reserved and make the result unusable for synthesis).
    """
    result = int(result)
    if result == -1 or result == 0xFFFF:
        return None
    result &= 0xFFFF
    vk, state = result & 0xFF, (result >> 8) & 0xFF
    if vk == 0xFF or state & 0xF8:
        return None
    return vk, state


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
    """Map a UIA control type id to the portable role vocabulary of :data:`pypilot.backends.base.ROLES`.

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


# =========================================================================== Win32 binding
def _proto(dll: Any, name: str, restype: Any, argtypes: Sequence[Any], required: bool = True) -> Any:
    fn = getattr(dll, name, None) if dll is not None else None
    if fn is None:
        if required:
            raise BackendUnavailable(f"Win32 function {name} is not available")
        return None
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


class _Win32:
    """Lazily-created table of Win32 functions with exact ``argtypes``/``restype``.

    Private ``WinDLL`` instances are used (not ``ctypes.windll``) so prototypes
    set by other libraries cannot conflict with ours. Functions are exposed as
    attributes named like the API (``api.SendInput``); optional ones are None
    when missing (older Windows versions).
    """

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - guarded by the backend
            raise BackendUnavailable("Win32 API is only available on Windows")
        WinDLL = getattr(ctypes, "WinDLL")
        user32 = WinDLL("user32", use_last_error=True)
        kernel32 = WinDLL("kernel32", use_last_error=True)
        gdi32 = WinDLL("gdi32", use_last_error=True)

        def opt_dll(name: str) -> Any:
            try:
                return WinDLL(name, use_last_error=True)
            except OSError:
                return None

        shcore = opt_dll("shcore")
        dwmapi = opt_dll("dwmapi")
        shell32 = opt_dll("shell32")
        ole32 = opt_dll("ole32")
        WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE")
        self.WNDENUMPROC = WINFUNCTYPE(BOOL, HWND, LPARAM)
        self.MONITORENUMPROC = WINFUNCTYPE(BOOL, HMONITOR, HDC, ctypes.POINTER(RECT), LPARAM)
        P = ctypes.POINTER
        u = user32
        spec = [
            # user32 -- input
            (u, "SendInput", UINT, [UINT, P(INPUT), INT]),
            (u, "GetCursorPos", BOOL, [P(POINT)]),
            (u, "SetCursorPos", BOOL, [INT, INT]),
            (u, "GetSystemMetrics", INT, [INT]),
            (u, "MapVirtualKeyW", UINT, [UINT, UINT]),
            (u, "VkKeyScanExW", SHORT, [WCHAR, HKL]),
            (u, "MapVirtualKeyExW", UINT, [UINT, UINT, HKL]),
            (u, "GetKeyState", SHORT, [INT]),
            (u, "GetKeyboardLayout", HKL, [DWORD]),
            # user32 -- monitors
            (u, "EnumDisplayMonitors", BOOL, [HDC, P(RECT), self.MONITORENUMPROC, LPARAM]),
            (u, "GetMonitorInfoW", BOOL, [HMONITOR, P(MONITORINFOEXW)]),
            # user32 -- windows
            (u, "EnumWindows", BOOL, [self.WNDENUMPROC, LPARAM]),
            (u, "GetForegroundWindow", HWND, []),
            (u, "GetDesktopWindow", HWND, []),
            (u, "GetWindowThreadProcessId", DWORD, [HWND, P(DWORD)]),
            (u, "IsWindow", BOOL, [HWND]),
            (u, "IsWindowVisible", BOOL, [HWND]),
            (u, "IsIconic", BOOL, [HWND]),
            (u, "IsZoomed", BOOL, [HWND]),
            (u, "GetWindowTextLengthW", INT, [HWND]),
            (u, "GetWindowTextW", INT, [HWND, LPWSTR, INT]),
            (u, "GetClassNameW", INT, [HWND, LPWSTR, INT]),
            (u, "GetWindowRect", BOOL, [HWND, P(RECT)]),
            (u, "SetForegroundWindow", BOOL, [HWND]),
            (u, "BringWindowToTop", BOOL, [HWND]),
            (u, "ShowWindow", BOOL, [HWND, INT]),
            (u, "AttachThreadInput", BOOL, [DWORD, DWORD, BOOL]),
            (u, "MoveWindow", BOOL, [HWND, INT, INT, INT, INT, BOOL]),
            (u, "PostMessageW", BOOL, [HWND, UINT, WPARAM, LPARAM]),
            # user32 -- clipboard
            (u, "OpenClipboard", BOOL, [HWND]),
            (u, "CloseClipboard", BOOL, []),
            (u, "EmptyClipboard", BOOL, []),
            (u, "GetClipboardData", HANDLE, [UINT]),
            (u, "SetClipboardData", HANDLE, [UINT, HANDLE]),
            (u, "IsClipboardFormatAvailable", BOOL, [UINT]),
            (u, "RegisterClipboardFormatW", UINT, [LPCWSTR]),
            (u, "EnumClipboardFormats", UINT, [UINT]),
            (u, "CreateWindowExW", HWND,
             [DWORD, LPCWSTR, LPCWSTR, DWORD, INT, INT, INT, INT, HWND, HMENU, HINSTANCE, LPVOID]),
            (u, "DestroyWindow", BOOL, [HWND]),
            # user32 -- DC
            (u, "GetDC", HDC, [HWND]),
            (u, "ReleaseDC", INT, [HWND, HDC]),
            # kernel32
            (kernel32, "GetCurrentThreadId", DWORD, []),
            (kernel32, "OpenProcess", HANDLE, [DWORD, BOOL, DWORD]),
            (kernel32, "CloseHandle", BOOL, [HANDLE]),
            (kernel32, "QueryFullProcessImageNameW", BOOL, [HANDLE, DWORD, LPWSTR, P(DWORD)]),
            (kernel32, "GlobalAlloc", HGLOBAL, [UINT, SIZE_T]),
            (kernel32, "GlobalLock", LPVOID, [HGLOBAL]),
            (kernel32, "GlobalUnlock", BOOL, [HGLOBAL]),
            (kernel32, "GlobalFree", HGLOBAL, [HGLOBAL]),
            (kernel32, "GlobalSize", SIZE_T, [HGLOBAL]),
            # gdi32
            (gdi32, "CreateCompatibleDC", HDC, [HDC]),
            (gdi32, "CreateCompatibleBitmap", HBITMAP, [HDC, INT, INT]),
            (gdi32, "SelectObject", HGDIOBJ, [HDC, HGDIOBJ]),
            (gdi32, "BitBlt", BOOL, [HDC, INT, INT, INT, INT, HDC, INT, INT, DWORD]),
            (gdi32, "GetDIBits", INT, [HDC, HBITMAP, UINT, UINT, LPVOID, P(BITMAPINFO), UINT]),
            (gdi32, "DeleteObject", BOOL, [HGDIOBJ]),
            (gdi32, "DeleteDC", BOOL, [HDC]),
            (gdi32, "GetDeviceCaps", INT, [HDC, INT]),
        ]
        for dll, name, res, args in spec:
            setattr(self, name, _proto(dll, name, res, args))
        optional = [
            (u, "SetProcessDpiAwarenessContext", BOOL, [HANDLE]),
            (u, "SetThreadDpiAwarenessContext", HANDLE, [HANDLE]),
            (u, "SetProcessDPIAware", BOOL, []),
            (u, "AllowSetForegroundWindow", BOOL, [DWORD]),
            (shcore, "GetDpiForMonitor", HRESULT, [HMONITOR, INT, P(UINT), P(UINT)]),
            (shcore, "SetProcessDpiAwareness", HRESULT, [INT]),
            (dwmapi, "DwmGetWindowAttribute", HRESULT, [HWND, DWORD, LPVOID, DWORD]),
            # launching like Start > Run (App Paths, documents, URLs)
            (shell32, "ShellExecuteExW", BOOL, [P(SHELLEXECUTEINFOW)]),
            (kernel32, "GetProcessId", DWORD, [HANDLE]),
            (kernel32, "WaitForSingleObject", DWORD, [HANDLE, DWORD]),
            (kernel32, "GetExitCodeProcess", BOOL, [HANDLE, P(DWORD)]),
            (ole32, "CoInitializeEx", HRESULT, [LPVOID, DWORD]),
            (ole32, "CoUninitialize", None, []),
        ]
        for dll, name, res, args in optional:
            setattr(self, name, _proto(dll, name, res, args, required=False))
        # GetWindowLongPtrW only exists in 64-bit user32; 32-bit uses GetWindowLongW.
        self.GetWindowLongPtrW = _proto(u, "GetWindowLongPtrW", LONG_PTR, [HWND, INT], required=False) or _proto(
            u, "GetWindowLongW", LONG, [HWND, INT]
        )

    @staticmethod
    def get_last_error() -> int:
        return int(getattr(ctypes, "get_last_error")())

    @staticmethod
    def format_error(code: int) -> str:
        fmt = getattr(ctypes, "FormatError", None)
        try:
            return fmt(code).strip() if fmt else f"error {code}"
        except Exception:
            return f"error {code}"


_api_lock = threading.Lock()
_api_instance: Optional[_Win32] = None


def _win32() -> _Win32:
    global _api_instance
    if _api_instance is None:
        with _api_lock:
            if _api_instance is None:
                _api_instance = _Win32()
    return _api_instance


# =========================================================================== DPI awareness
_dpi_lock = threading.Lock()
_dpi_mode: Optional[str] = None


def enable_dpi_awareness(api: Any = None) -> str:
    """Make the process per-monitor DPI aware (idempotent). Returns the mode obtained.

    Tries, in order: ``SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)``
    (Windows 10 1703+), ``...(PER_MONITOR_AWARE)``,
    ``shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)`` (8.1+),
    ``SetProcessDPIAware`` (Vista+). If the awareness was already set (by a
    manifest or an earlier call, which makes later calls fail with
    access-denied) the calling *thread* is switched with
    ``SetThreadDpiAwarenessContext`` when possible.

    Returns one of ``"per_monitor_v2"``, ``"per_monitor"``, ``"system"``,
    ``"thread_per_monitor_v2"``, ``"preset"`` (already set elsewhere) or
    ``"unaware"``.
    """
    global _dpi_mode
    if _dpi_mode is not None and api is None:
        return _dpi_mode
    with _dpi_lock:
        if _dpi_mode is not None and api is None:
            return _dpi_mode
        a = api or _win32()
        mode = "unaware"
        denied = False
        fn = getattr(a, "SetProcessDpiAwarenessContext", None)
        if fn is not None:
            for ctx, label in (
                (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, "per_monitor_v2"),
                (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE, "per_monitor"),
            ):
                if fn(ctypes.c_void_p(ctx)):
                    mode = label
                    break
                if a.get_last_error() == ERROR_ACCESS_DENIED:
                    denied = True
                    break
        if mode == "unaware" and not denied:
            fn2 = getattr(a, "SetProcessDpiAwareness", None)
            if fn2 is not None:
                hr = int(fn2(PROCESS_PER_MONITOR_DPI_AWARE))
                if hr == 0:
                    mode = "per_monitor"
                elif hr == E_ACCESSDENIED:
                    denied = True
        if mode == "unaware" and not denied:
            fn3 = getattr(a, "SetProcessDPIAware", None)
            if fn3 is not None and fn3():
                mode = "system"
        if mode == "unaware" and denied:
            thread_fn = getattr(a, "SetThreadDpiAwarenessContext", None)
            if thread_fn is not None and thread_fn(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
                mode = "thread_per_monitor_v2"
            else:
                mode = "preset"
        if api is None:
            _dpi_mode = mode
        return mode


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


# =========================================================================== backend
_BUTTON_FLAGS: Dict[MouseButton, Tuple[int, int, int]] = {
    MouseButton.LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    MouseButton.RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    MouseButton.MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    MouseButton.X1: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    MouseButton.X2: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}


def mouse_input(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> INPUT:
    """Build a mouse ``INPUT``. ``data`` may be negative (wheel); it is stored as a 32-bit DWORD."""
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(int(dx), int(dy), int(data) & 0xFFFFFFFF, int(flags), 0, INJECTED_EXTRA_INFO)
    return inp


def key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    """Build a keyboard ``INPUT``."""
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(int(vk) & 0xFFFF, int(scan) & 0xFFFF, int(flags), 0, INJECTED_EXTRA_INFO)
    return inp


def unicode_inputs(text: str) -> List[INPUT]:
    """Down/up ``KEYEVENTF_UNICODE`` pairs for every UTF-16 code unit of ``text``."""
    out: List[INPUT] = []
    for unit in utf16_units(text):
        out.append(key_input(0, unit, KEYEVENTF_UNICODE))
        out.append(key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return out


# ShellExecuteEx flags
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_SHOWNORMAL = 1
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259
_PROGRAM_EXTS = (".exe", ".com", ".bat", ".cmd")


def shell_launch_target(command: str) -> Optional[Tuple[str, str]]:
    """``(file, parameters)`` when ``command`` must be started with ``ShellExecuteEx``, else None.

    ``CreateProcess`` (``subprocess.Popen``) only finds programs on ``PATH``, in
    System32 and the current folder. Start > Run also uses the *App Paths*
    registry (``excel``, ``winword``, ``outlook`` are registered there, not on
    ``PATH``) and file associations (``Q3.xlsx``, ``https://...``). A command
    whose program is an existing ``.exe``/``.bat`` path, or is found on
    ``PATH``, keeps using ``Popen`` (exact process, exact arguments).
    """
    cmd = command.strip()
    if not cmd:
        return None
    if os.path.isfile(cmd):  # an unquoted path, possibly with spaces
        return None if cmd.lower().endswith(_PROGRAM_EXTS) else (cmd, "")
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end < 0:
            return None
        file, params = cmd[1:end], cmd[end + 1:].strip()
    else:
        parts = cmd.split(None, 1)
        file, params = parts[0], (parts[1] if len(parts) > 1 else "")
    if not file:
        return None
    if os.path.isfile(file):
        return None if file.lower().endswith(_PROGRAM_EXTS) else (file, params)
    if shutil.which(file) is not None:
        return None
    return file, params


class ShellProcess:
    """What :meth:`WindowsBackend.launch` returns for a ``ShellExecuteEx`` launch.

    A small ``subprocess.Popen``-like object: ``args``, ``pid`` (None when the
    request was handed to an already-running instance, e.g. a second workbook
    opened in the running Excel), ``returncode``, ``poll()`` and ``wait()``.
    """

    def __init__(self, api: Any, args: str, handle: Any) -> None:
        self._api = api
        self.args = args
        self._handle = handle or None
        self.pid: Optional[int] = None
        self.returncode: Optional[int] = None
        if self._handle is not None:
            try:
                self.pid = int(api.GetProcessId(self._handle)) or None
            except Exception:
                self.pid = None
        else:
            self.returncode = 0  # no process of our own: the shell (or a running instance) handled it

    def poll(self) -> Optional[int]:
        return self.wait(0)

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if self.returncode is not None or self._handle is None:
            return self.returncode
        ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        if int(self._api.WaitForSingleObject(self._handle, ms)) != WAIT_OBJECT_0:
            if timeout:
                import subprocess

                raise subprocess.TimeoutExpired(self.args, timeout)
            return None
        code = DWORD(0)
        if self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            self.returncode = int(code.value)
        else:
            self.returncode = 0
        self._close()
        return self.returncode

    def _close(self) -> None:
        h, self._handle = self._handle, None
        if h is not None:
            try:
                self._api.CloseHandle(h)
            except Exception:
                pass

    def __del__(self) -> None:  # pragma: no cover - finalizer
        self._close()

    def __repr__(self) -> str:
        return f"<ShellProcess {self.args!r} pid={self.pid} returncode={self.returncode}>"


class _Held:
    """A key pressed through :meth:`WindowsBackend.key_down` (for symmetric release)."""

    __slots__ = ("events", "release")

    def __init__(self, events: List[INPUT], release: List[INPUT]) -> None:
        self.events = events
        self.release = release


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


class WindowsBackend(Backend):
    """Backend for Windows 7+ (best on Windows 10/11). See the module docstring.

    Args:
        dpi_aware: make the process per-monitor DPI aware (strongly recommended;
            without it coordinates are virtualised on scaled displays).
        screenshot_method: ``"auto"`` (mss, falling back to GDI), ``"mss"`` or ``"gdi"``.
        accessibility: enable UI Automation (needs ``comtypes``).
        uia_view: tree view for :class:`UIANode` children: ``"control"`` (default),
            ``"raw"`` or ``"content"``.
        uia_timeout_ms: UIA connection timeout for unresponsive apps (None = system default).
        clipboard_retries: attempts to open a clipboard held by another process.
        clipboard_retry_delay: seconds between those attempts.
        clipboard_history: let text written by :meth:`set_clipboard` enter Windows
            Clipboard History / cloud clipboard (default False: it is excluded, so
            pasted passwords and bank data are not kept there).
        respect_swapped_buttons: when the user swapped mouse buttons, map
            ``"left"`` to the *primary* button (as users expect).
        type_char_delay: seconds between characters in :meth:`type_text`, and the
            minimum gap between consecutive :meth:`key_down` presses.
        click_key_settle: minimum seconds between a mouse button release and the
            next keyboard event. Applications handle a click asynchronously;
            Windows 11 Notepad, clicked on text that was just typed, loses the
            Ctrl of an immediately following Ctrl+A and types ``a`` (verified on
            a real machine; 50 ms was already enough). Only the first key after a
            click waits, so typing and repeated clicks keep full speed.
        sleep: sleep function for clipboard retries and input pacing.

    Raises:
        BackendUnavailable: not running on Windows.
    """

    name = "windows"

    def __init__(
        self,
        *,
        dpi_aware: bool = True,
        screenshot_method: str = "auto",
        accessibility: bool = True,
        uia_view: str = "control",
        uia_timeout_ms: Optional[int] = 5000,
        clipboard_retries: int = 20,
        clipboard_retry_delay: float = 0.01,
        clipboard_history: bool = False,
        respect_swapped_buttons: bool = True,
        type_char_delay: float = 0.01,
        click_key_settle: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        _api: Any = None,
    ) -> None:
        if _api is None and sys.platform != "win32":
            raise BackendUnavailable(f"The Windows backend requires Windows (running on {sys.platform!r})")
        if screenshot_method not in ("auto", "mss", "gdi"):
            raise ValueError("screenshot_method must be 'auto', 'mss' or 'gdi'")
        if uia_view not in ("control", "raw", "content"):
            raise ValueError("uia_view must be 'control', 'raw' or 'content'")
        try:
            self._api = _api if _api is not None else _win32()
        except BackendUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - platform specific
            raise BackendUnavailable(f"Cannot load the Win32 API: {exc}") from exc
        self.dpi_mode = enable_dpi_awareness(self._api if _api is not None else None) if dpi_aware else "unchanged"
        self._screenshot_method = screenshot_method
        self._mss_broken = screenshot_method == "gdi"
        self._mss_local = threading.local()
        self._mss_lock = threading.Lock()
        self._mss_closers: List[Any] = []  # weakref.finalize objects closing per-thread mss grabbers
        self._accessibility = bool(accessibility)
        self._uia_view = uia_view
        self._uia_timeout = uia_timeout_ms
        self._uia_ctx: Any = None
        self._uia_error: Optional[str] = None
        self._clip_retries = max(1, int(clipboard_retries))
        self._clip_delay = float(clipboard_retry_delay)
        self._clip_history = bool(clipboard_history)
        self._clip_exclusions: Optional[List[int]] = None
        self._pid_names: Dict[int, str] = {}
        self._respect_swap = respect_swapped_buttons
        self._sleep = sleep
        if type_char_delay < 0:
            raise ValueError("type_char_delay must be >= 0")
        self._type_char_delay = float(type_char_delay)
        if click_key_settle < 0:
            raise ValueError("click_key_settle must be >= 0")
        self._click_key_settle = float(click_key_settle)
        self._last_button_up: Optional[float] = None
        self._last_key_down: Optional[float] = None
        self._lock = threading.RLock()
        self._held: Dict[str, _Held] = {}
        self._buttons_down: Dict[MouseButton, INPUT] = {}
        self._closed = False

    # =================================================================== basics
    @property
    def capabilities(self) -> frozenset:
        caps = {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_SCREENSHOT, CAP_WINDOWS, CAP_CLIPBOARD, CAP_HSCROLL}
        if self._accessibility and self._uia_available():
            caps.add(CAP_ACCESSIBILITY)
        return frozenset(caps)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Release keys/buttons still held by this backend and free screenshot resources."""
        if self._closed:
            return
        try:
            self.release_all()
        except Exception:
            pass
        with self._mss_lock:
            closers, self._mss_closers = self._mss_closers, []
        for closer in closers:
            closer()  # closes that thread's grabber now (no-op if its thread already ended)
        self._closed = True

    def __enter__(self) -> "WindowsBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _check(self) -> None:
        if self._closed:
            raise BackendError("Windows backend has been closed")

    def _error(self, what: str) -> BackendError:
        code = self._api.get_last_error()
        return BackendError(f"{what} failed: {self._api.format_error(code)} (error {code})")

    def _send(self, inputs: Sequence[INPUT]) -> None:
        """SendInput in chunks; raise if Windows rejects the events."""
        self._check()
        for start in range(0, len(inputs), _SENDINPUT_CHUNK):
            chunk = inputs[start:start + _SENDINPUT_CHUNK]
            arr = (INPUT * len(chunk))(*chunk)
            sent = int(self._api.SendInput(len(chunk), arr, ctypes.sizeof(INPUT)))
            if sent != len(chunk):
                code = self._api.get_last_error()
                hint = ""
                if code == ERROR_ACCESS_DENIED:
                    hint = " (input is blocked: the secure desktop/UAC prompt or a locked workstation is active)"
                raise BackendError(
                    f"SendInput injected {sent}/{len(chunk)} events: {self._api.format_error(code)}{hint}"
                )

    # ==================================================================== mouse
    def mouse_position(self) -> Point:
        self._check()
        pt = POINT()
        if not self._api.GetCursorPos(ctypes.byref(pt)):
            raise self._error("GetCursorPos")
        return Point(int(pt.x), int(pt.y))

    def _virtual_metrics(self) -> Rect:
        a = self._api
        w = int(a.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        h = int(a.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if w <= 0 or h <= 0:
            return Rect(0, 0, int(a.GetSystemMetrics(SM_CXSCREEN)), int(a.GetSystemMetrics(SM_CYSCREEN)))
        return Rect(int(a.GetSystemMetrics(SM_XVIRTUALSCREEN)), int(a.GetSystemMetrics(SM_YVIRTUALSCREEN)), w, h)

    def mouse_move(self, x: int, y: int) -> None:
        """Move with an absolute SendInput (real WM_MOUSEMOVE / hover), then verify the position."""
        self._check()
        x, y = int(x), int(y)
        with self._lock:
            vs = self._virtual_metrics()
            nx, ny = normalize_absolute(x, y, vs)
            flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE_NOCOALESCE
            self._send([mouse_input(nx, ny, 0, flags)])
            tx = min(max(x, vs.left), vs.right - 1)
            ty = min(max(y, vs.top), vs.bottom - 1)
            try:
                pos = self.mouse_position()
            except BackendError:
                pos = None
            if pos != (tx, ty):
                # Rounding in exotic configurations, or the move is still queued:
                # SetCursorPos lands exactly on the requested physical pixel.
                self._api.SetCursorPos(tx, ty)

    def _physical_button(self, button: Any) -> MouseButton:
        b = MouseButton.coerce(button)
        if self._respect_swap and b in (MouseButton.LEFT, MouseButton.RIGHT):
            if self._api.GetSystemMetrics(SM_SWAPBUTTON):
                b = MouseButton.RIGHT if b == MouseButton.LEFT else MouseButton.LEFT
        return b

    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._check()
        logical = MouseButton.coerce(button)
        phys = self._physical_button(logical)
        down, up, data = _BUTTON_FLAGS[phys]
        with self._lock:
            self._send([mouse_input(0, 0, data, down)])
            self._buttons_down[logical] = mouse_input(0, 0, data, up)

    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._check()
        logical = MouseButton.coerce(button)
        with self._lock:
            ev = self._buttons_down.get(logical)
            if ev is None:
                phys = self._physical_button(logical)
                _down, up, data = _BUTTON_FLAGS[phys]
                ev = mouse_input(0, 0, data, up)
            self._send([ev])
            self._last_button_up = time.monotonic()
            # Forgotten only once released: if Windows rejected the release (secure
            # desktop, locked workstation) release_all()/close() can retry it.
            self._buttons_down.pop(logical, None)

    def _settle_after_click(self) -> None:
        """Before the first keyboard event after a click, let the app finish handling the click."""
        last, self._last_button_up = self._last_button_up, None
        if last is None or self._click_key_settle <= 0:
            return
        remaining = self._click_key_settle - (time.monotonic() - last)
        if remaining > 0:
            self._sleep(remaining)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Wheel notches: ``dy > 0`` scrolls up, ``dx > 0`` scrolls right (one event per notch)."""
        self._check()
        dx, dy = int(dx), int(dy)
        events: List[INPUT] = []
        step = WHEEL_DELTA if dy > 0 else -WHEEL_DELTA
        events += [mouse_input(0, 0, step, MOUSEEVENTF_WHEEL) for _ in range(abs(dy))]
        step = WHEEL_DELTA if dx > 0 else -WHEEL_DELTA
        events += [mouse_input(0, 0, step, MOUSEEVENTF_HWHEEL) for _ in range(abs(dx))]
        if events:
            with self._lock:
                self._send(events)

    # ================================================================= keyboard
    def _scan(self, vk: int) -> Tuple[int, bool]:
        """Hardware scan code for ``vk`` and whether Windows reports it as E0-extended."""
        try:
            sc = int(self._api.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX))
        except Exception:
            sc = 0
        if sc == 0:
            try:
                sc = int(self._api.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC))
            except Exception:
                sc = 0
        return sc & 0xFF, (sc >> 8) == 0xE0

    def _vk_events(self, vk: int, extended: bool) -> Tuple[INPUT, INPUT]:
        scan, ext2 = self._scan(vk)
        # num_enter shares VK_RETURN, so the caller's ``extended`` decides; for
        # other keys MapVirtualKey's E0 prefix is honoured as well.
        flags = KEYEVENTF_EXTENDEDKEY if (extended or (ext2 and vk != VK_RETURN)) else 0
        return key_input(vk, scan, flags), key_input(vk, scan, flags | KEYEVENTF_KEYUP)

    def _layout(self) -> Any:
        a = self._api
        try:
            fg = a.GetForegroundWindow()
            tid = a.GetWindowThreadProcessId(fg, None) if fg else 0
            return a.GetKeyboardLayout(tid)
        except Exception:
            return None

    def _char_plan(self, ch: str) -> Tuple[List[INPUT], List[INPUT]]:
        """(press events, release events) for a single character."""
        if len(ch) == 1 and ord(ch) < 0x10000:
            hkl = self._layout()
            try:
                scanned = decode_vk_scan(self._api.VkKeyScanExW(ch, hkl))
            except Exception:
                scanned = None
            if scanned is not None and self._is_dead_key(scanned[0], hkl):
                scanned = None  # the key would start a composition instead of typing ``ch``
            if scanned is not None:
                vk, state = scanned
                if state in (0, 1) and self._caps_flips(ch):
                    state ^= 1  # CapsLock is on: Shift now gives the other case
                press: List[INPUT] = []
                release: List[INPUT] = []
                for bit, mod in _SHIFT_STATE_MODIFIERS:
                    if state & bit and not self._modifier_held(mod):
                        d, u = self._vk_events(*vk_for_key(mod))
                        press.append(d)
                        release.insert(0, u)
                d, u = self._vk_events(vk, False)
                press.append(d)
                release.insert(0, u)
                return press, release
        units = utf16_units(ch)
        press = [key_input(0, unit, KEYEVENTF_UNICODE) for unit in units]
        release = [key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP) for unit in units]
        return press, release

    def _is_dead_key(self, vk: int, hkl: Any) -> bool:
        """Whether ``vk`` is a dead (diacritic) key on layout ``hkl`` (circumflex and
        grave on German/French, quote/tilde on US-International). Such characters are
        typed as Unicode packets instead (unless Ctrl/Alt/Win is held, where the
        key itself is wanted for a shortcut)."""
        if any(self._modifier_held(m) for m in ("ctrl", "alt", "cmd")):
            return False
        try:
            # MAPVK_VK_TO_CHAR sets the top bit for dead keys.
            return bool(int(self._api.MapVirtualKeyExW(vk, MAPVK_VK_TO_CHAR, hkl)) & 0x80000000)
        except Exception:
            return False

    def _caps_flips(self, ch: str) -> bool:
        """Whether CapsLock is on and would change the case of letter ``ch`` typed as text
        (not while Ctrl/Alt/Win is held: shortcuts keep the key's own Shift state)."""
        lo, up = ch.lower(), ch.upper()
        if lo == up or len(lo) != 1 or len(up) != 1:
            return False
        if any(self._modifier_held(m) for m in ("ctrl", "alt", "cmd")):
            return False
        try:
            return bool(self._api.GetKeyState(0x14) & 1)  # VK_CAPITAL toggled
        except Exception:
            return False

    def _modifier_held(self, mod: str) -> bool:
        return mod in self._held or f"{mod}_r" in self._held

    def _plan(self, key: str) -> Tuple[str, List[INPUT], List[INPUT]]:
        if not isinstance(key, str) or key == "":
            raise ValueError(f"Invalid key: {key!r}")
        # Canonical names pass straight through; aliases ("Return", "\n", " ") are
        # normalized; unknown multi-character names raise ValueError.
        name = key if key in VK_CODES else normalize_key(key)
        if name in VK_CODES:
            d, u = self._vk_events(*vk_for_key(name))
            return name, [d], [u]
        press, release = self._char_plan(name)
        return name, press, release

    def key_down(self, key: str) -> None:
        """Press a canonical key name or a single character (adding Shift/AltGr as the layout needs)."""
        self._check()
        self._settle_after_click()
        self._pace_key()
        with self._lock:
            name, press, release = self._plan(key)
            self._send(press)
            self._held[name] = _Held(press, release)
            self._last_key_down = time.monotonic()

    def _pace_key(self) -> None:
        """Keep key presses at least ``type_char_delay`` apart.

        Key-by-key typing (``mode="keys"``) is a burst of presses just like a
        unicode burst; Windows 11 Notepad intermittently dropped whole runs of
        them (about 1 in 6 runs of a 30-symbol string on a real machine).
        """
        last = self._last_key_down
        if last is None or self._type_char_delay <= 0:
            return
        remaining = self._type_char_delay - (time.monotonic() - last)
        if remaining > 0:
            self._sleep(remaining)

    def key_up(self, key: str) -> None:
        self._check()
        with self._lock:
            name, _press, release = self._plan(key)
            held = self._held.get(name)
            self._send(held.release if held is not None else release)
            self._held.pop(name, None)  # only once released (see mouse_up)

    def release_all(self) -> None:
        """Release every key and mouse button pressed through this backend (reverse order)."""
        with self._lock:
            if self._closed:
                self._held.clear()
                self._buttons_down.clear()
                return
            # Each key/button is released and forgotten separately: one whose release
            # Windows rejects (secure desktop, locked workstation) stays recorded so
            # a later call can retry it; the first error is raised at the end.
            first: Optional[BaseException] = None
            pending: List[Tuple[Dict[Any, Any], Any, List[INPUT]]] = [
                (self._held, name, self._held[name].release) for name in reversed(list(self._held))
            ]
            pending += [(self._buttons_down, b, [ev]) for b, ev in list(self._buttons_down.items())]
            for table, k, events in pending:
                try:
                    self._send(events)
                except BackendError as exc:
                    if first is None:
                        first = exc
                    continue
                table.pop(k, None)
            if first is not None:
                raise first

    def type_text(self, text: str) -> None:
        """Type ``text`` layout-independently (``KEYEVENTF_UNICODE``); newlines/tabs as Enter/Tab keys.

        Characters are sent one at a time (a surrogate pair stays in one
        ``SendInput`` call) with ``type_char_delay`` seconds between them:
        modern apps such as Windows 11 Notepad drop or garble long bursts of
        ``KEYEVENTF_UNICODE`` input (verified on a real machine), while a
        10 ms pace is reliable and still ~100 characters per second. Use
        clipboard typing for very long text.
        """
        self._check()
        if not text:
            return
        self._settle_after_click()
        groups: List[List[INPUT]] = []
        for token in split_text_tokens(text):
            if token in ("enter", "tab"):
                d, u = self._vk_events(*vk_for_key(token))
                groups.append([d, u])
            else:
                groups.append(unicode_inputs(token))
        delay = self._type_char_delay
        for i, events in enumerate(groups):
            if i and delay > 0:
                self._sleep(delay)
            with self._lock:
                self._send(events)

    # =================================================================== screen
    def virtual_screen(self) -> Rect:
        self._check()
        return self._virtual_metrics()

    def _monitor_scale(self, hmon: Any) -> float:
        a = self._api
        fn = getattr(a, "GetDpiForMonitor", None)
        if fn is not None:
            dx, dy = UINT(0), UINT(0)
            try:
                if int(fn(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy))) == 0 and dx.value:
                    return round(dx.value / 96.0, 4)
            except Exception:
                pass
        hdc = a.GetDC(None)
        if hdc:
            try:
                dpi = int(a.GetDeviceCaps(hdc, LOGPIXELSX))
                if dpi > 0:
                    return round(dpi / 96.0, 4)
            finally:
                a.ReleaseDC(None, hdc)
        return 1.0

    def monitors(self) -> List[Monitor]:
        """All monitors in physical pixels; the primary first, others ordered left-to-right."""
        self._check()
        a = self._api
        handles: List[Any] = []

        def cb(hmon: Any, hdc: Any, lprect: Any, lparam: Any) -> int:
            handles.append(hmon)
            return 1

        proc = a.MONITORENUMPROC(cb)
        if not a.EnumDisplayMonitors(None, None, proc, 0):
            raise self._error("EnumDisplayMonitors")
        found: List[Tuple[Rect, bool, str, float]] = []
        for h in handles:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if not a.GetMonitorInfoW(h, ctypes.byref(info)):
                continue
            found.append((info.rcMonitor.to_rect(), bool(info.dwFlags & MONITORINFOF_PRIMARY),
                          str(info.szDevice), self._monitor_scale(h)))
        if not found:
            vs = self._virtual_metrics()
            return [Monitor(0, vs, 1.0, True, "")]
        found.sort(key=lambda m: (not m[1], m[0].left, m[0].top))
        return [Monitor(i, r, s, prim, name) for i, (r, prim, name, s) in enumerate(found)]

    def _mss(self) -> Any:
        """This thread's mss grabber (it holds GDI device contexts and a bitmap).

        It lives in a thread-local slot whose finalizer closes it as soon as the
        thread exits, so thread-per-job services never pile up GDI handles (capped
        per process); :meth:`close` closes the ones of threads still running.
        """
        holder = getattr(self._mss_local, "holder", None)
        if holder is None:
            import mss

            factory = getattr(mss, "MSS", None) or mss.mss
            sct = factory()
            holder = _MssHolder(sct)
            with self._mss_lock:
                self._mss_closers = [c for c in self._mss_closers if c.alive]
                self._mss_closers.append(weakref.finalize(holder, _close_quietly, sct))
            self._mss_local.holder = holder
        return holder.sct

    def _grab_mss(self, r: Rect) -> Any:
        from PIL import Image

        shot = self._mss().grab({"left": r.left, "top": r.top, "width": r.width, "height": r.height})
        return Image.frombuffer("RGB", (shot.width, shot.height), bytes(shot.bgra), "raw", "BGRX", 0, 1)

    def _grab_gdi(self, r: Rect) -> Any:
        from PIL import Image

        a = self._api
        hdc_screen = a.GetDC(None)
        if not hdc_screen:
            raise self._error("GetDC")
        hdc_mem = hbm = None
        try:
            hdc_mem = a.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                raise self._error("CreateCompatibleDC")
            hbm = a.CreateCompatibleBitmap(hdc_screen, r.width, r.height)
            if not hbm:
                raise self._error("CreateCompatibleBitmap")
            old = a.SelectObject(hdc_mem, hbm)
            try:
                if not a.BitBlt(hdc_mem, 0, 0, r.width, r.height, hdc_screen, r.left, r.top, SRCCOPY | CAPTUREBLT):
                    raise self._error("BitBlt")
            finally:
                a.SelectObject(hdc_mem, old)  # GetDIBits needs the bitmap deselected
            bmi = BITMAPINFO()
            hdr = bmi.bmiHeader
            hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            hdr.biWidth = r.width
            hdr.biHeight = -r.height  # negative = top-down rows
            hdr.biPlanes = 1
            hdr.biBitCount = 32
            hdr.biCompression = BI_RGB
            buf = ctypes.create_string_buffer(r.width * r.height * 4)
            lines = a.GetDIBits(hdc_mem, hbm, 0, r.height, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
            if int(lines) != r.height:
                raise self._error("GetDIBits")
            return Image.frombuffer("RGB", (r.width, r.height), buf.raw, "raw", "BGRX", 0, 1)
        finally:
            if hbm:
                a.DeleteObject(hbm)
            if hdc_mem:
                a.DeleteDC(hdc_mem)
            a.ReleaseDC(None, hdc_screen)

    def _grab(self, r: Rect) -> Any:
        if not self._mss_broken:
            try:
                return self._grab_mss(r)
            except Exception:
                if self._screenshot_method == "mss":
                    raise
                self._mss_broken = True
        return self._grab_gdi(r)

    def screenshot(self, region: Optional[Rect] = None) -> Any:
        """RGB screenshot of ``region`` (default: whole virtual screen); off-screen parts are black."""
        from PIL import Image

        self._check()
        vs = self._virtual_metrics()
        r = vs if region is None else Rect.coerce(region)
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Screenshot region must have a positive size, got {r}")
        inter = r.intersect(vs)
        if inter is None:
            return Image.new("RGB", (r.width, r.height), (0, 0, 0))
        img = self._grab(inter)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if inter == r:
            return img
        canvas = Image.new("RGB", (r.width, r.height), (0, 0, 0))
        canvas.paste(img, (inter.left - r.left, inter.top - r.top))
        return canvas

    # ================================================================== windows
    def _hwnd(self, handle: Any) -> int:
        h = getattr(handle, "handle", handle)
        try:
            hwnd = int(h)
        except (TypeError, ValueError):
            raise WindowNotFound(f"Invalid window handle {handle!r}") from None
        if hwnd == 0 or not self._api.IsWindow(hwnd):
            raise WindowNotFound(f"No window with handle {hwnd:#x}")
        return hwnd

    def _text(self, hwnd: int) -> str:
        a = self._api
        n = int(a.GetWindowTextLengthW(hwnd))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 2)
        got = int(a.GetWindowTextW(hwnd, buf, n + 2))
        return buf.value[:got] if got >= 0 else ""

    def _class(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(257)
        got = int(self._api.GetClassNameW(hwnd, buf, 257))
        return buf.value[:got] if got > 0 else ""

    def _pid(self, hwnd: int) -> int:
        pid = DWORD(0)
        self._api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _process_name(self, pid: int) -> str:
        if not pid:
            return ""
        a = self._api
        hproc = a.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not hproc:
            return ""
        try:
            size = DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not a.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return ""
            path = buf.value[: size.value]
        finally:
            a.CloseHandle(hproc)
        return path.replace("/", "\\").rsplit("\\", 1)[-1]

    def _is_cloaked(self, hwnd: int) -> bool:
        fn = getattr(self._api, "DwmGetWindowAttribute", None)
        if fn is None:
            return False
        val = DWORD(0)
        try:
            hr = int(fn(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)))
        except Exception:
            return False
        return hr == 0 and val.value != 0

    def _outer_rect(self, hwnd: int) -> Rect:
        rc = RECT()
        if not self._api.GetWindowRect(hwnd, ctypes.byref(rc)):
            raise self._error("GetWindowRect")
        return rc.to_rect()

    def _frame_rect(self, hwnd: int) -> Rect:
        """Visible window bounds (DWM extended frame bounds, without invisible resize borders)."""
        fn = getattr(self._api, "DwmGetWindowAttribute", None)
        if fn is not None:
            rc = RECT()
            try:
                hr = int(fn(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rc), ctypes.sizeof(rc)))
            except Exception:
                hr = -1
            if hr == 0 and rc.right > rc.left and rc.bottom > rc.top:
                return rc.to_rect()
        return self._outer_rect(hwnd)

    def _is_candidate(self, hwnd: int) -> bool:
        a = self._api
        if not a.IsWindowVisible(hwnd):
            return False
        exstyle = int(a.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        if exstyle & WS_EX_TOOLWINDOW and not exstyle & WS_EX_APPWINDOW:
            return False
        if int(a.GetWindowTextLengthW(hwnd)) <= 0:
            return False
        return not self._is_cloaked(hwnd)

    def _info(self, hwnd: int, foreground: Any, names: Optional[Dict[int, str]] = None) -> WindowInfo:
        a = self._api
        pid = self._pid(hwnd)
        if names is not None and pid in names:
            app = names[pid]
        else:
            app = self._process_name(pid)
            if names is not None:
                names[pid] = app
        return WindowInfo(
            handle=hwnd,
            title=self._text(hwnd),
            rect=self._frame_rect(hwnd),
            app=app,
            pid=pid or None,
            is_active=bool(foreground) and int(foreground) == hwnd,
            is_minimized=bool(a.IsIconic(hwnd)),
            is_maximized=bool(a.IsZoomed(hwnd)),
            is_visible=bool(a.IsWindowVisible(hwnd)),
            class_name=self._class(hwnd),
        )

    def _toplevel_hwnds(self) -> List[int]:
        """Every top-level window, front-most first (``EnumWindows`` order)."""
        a = self._api
        hwnds: List[int] = []

        def cb(hwnd: Any, lparam: Any) -> int:
            if hwnd:
                hwnds.append(int(hwnd))
            return 1

        proc = a.WNDENUMPROC(cb)
        if not a.EnumWindows(proc, 0):
            raise self._error("EnumWindows")
        return hwnds

    def _windows_from(self, hwnds: Sequence[int], names: Dict[int, str]) -> List[WindowInfo]:
        fg = self._api.GetForegroundWindow()
        out: List[WindowInfo] = []
        for hwnd in hwnds:
            try:
                if self._is_candidate(hwnd):
                    out.append(self._info(hwnd, fg, names))
            except BackendError:
                continue  # window vanished while enumerating
        return out

    def list_windows(self) -> List[WindowInfo]:
        """Visible, titled, non-tool, non-cloaked top-level windows, front-most first."""
        self._check()
        return self._windows_from(self._toplevel_hwnds(), {})

    def active_window(self) -> Optional[WindowInfo]:
        self._check()
        fg = self._api.GetForegroundWindow()
        if not fg or not self._api.IsWindow(fg):
            return None
        try:
            return self._info(int(fg), fg)
        except BackendError:
            return None

    def window_info(self, handle: Any) -> WindowInfo:
        """Snapshot of any top-level window by handle (even ones :meth:`list_windows` filters out)."""
        self._check()
        hwnd = self._hwnd(handle)
        return self._info(hwnd, self._api.GetForegroundWindow())

    def _is_foreground(self, hwnd: int) -> bool:
        fg = self._api.GetForegroundWindow()
        return bool(fg) and int(fg) == hwnd

    def activate_window(self, handle: Any) -> None:
        """Restore (if minimized) and bring a window to the foreground.

        ``SetForegroundWindow`` is refused unless the caller "owns" the last
        input event, so this escalates: plain call → ``AttachThreadInput`` to
        the foreground and target threads → ALT tap (makes this process the
        source of the last input event).

        Raises:
            WindowNotFound: the handle is invalid.
            BackendError: Windows refused every attempt (foreground lock).
        """
        self._check()
        a = self._api
        hwnd = self._hwnd(handle)
        with self._lock:
            if a.IsIconic(hwnd):
                a.ShowWindow(hwnd, SW_RESTORE)
            if self._is_foreground(hwnd):
                return
            allow = getattr(a, "AllowSetForegroundWindow", None)
            if allow is not None:
                allow(0xFFFFFFFF)  # ASFW_ANY
            if a.SetForegroundWindow(hwnd) and self._is_foreground(hwnd):
                return
            # 1) share the input state with the foreground and target threads
            cur = int(a.GetCurrentThreadId())
            fg = a.GetForegroundWindow()
            tids = []
            for w in (fg, hwnd):
                if w:
                    tid = int(a.GetWindowThreadProcessId(w, None))
                    if tid and tid != cur and tid not in tids:
                        tids.append(tid)
            attached = [t for t in tids if a.AttachThreadInput(cur, t, True)]
            try:
                a.BringWindowToTop(hwnd)
                a.SetForegroundWindow(hwnd)
            finally:
                for t in attached:
                    a.AttachThreadInput(cur, t, False)
            if self._is_foreground(hwnd):
                return
            # 2) ALT tap: our injected input becomes the last input event.
            if self._modifier_held("alt"):
                a.SetForegroundWindow(hwnd)
            else:
                alt_down, alt_up = self._vk_events(VK_MENU, False)
                self._send([alt_down])
                try:
                    a.SetForegroundWindow(hwnd)
                finally:
                    self._send([alt_up])
            if self._is_foreground(hwnd):
                return
        raise BackendError(f"Windows refused to bring window {hwnd:#x} to the foreground (foreground lock)")

    def move_window(self, handle: Any, rect: Rect) -> None:
        """Place the window's *visible* frame at ``rect`` (DWM invisible borders are compensated)."""
        self._check()
        a = self._api
        hwnd = self._hwnd(handle)
        r = Rect.coerce(rect)
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Window size must be positive, got {r}")
        self._restore_normal(hwnd)
        outer = self._outer_rect(hwnd)
        frame = self._frame_rect(hwnd)
        dl = max(0, frame.left - outer.left)
        dt = max(0, frame.top - outer.top)
        dr = max(0, outer.right - frame.right)
        db = max(0, outer.bottom - frame.bottom)
        if not a.MoveWindow(hwnd, r.left - dl, r.top - dt, r.width + dl + dr, r.height + dt + db, True):
            raise self._error("MoveWindow")

    _SHOW_STATES = {
        "minimized": SW_MINIMIZE,
        "minimize": SW_MINIMIZE,
        "maximized": SW_MAXIMIZE,
        "maximize": SW_MAXIMIZE,
        "normal": SW_RESTORE,
        "restored": SW_RESTORE,
        "restore": SW_RESTORE,
        "hidden": SW_HIDE,
        "hide": SW_HIDE,
        "shown": SW_SHOW,
        "show": SW_SHOW,
    }

    def set_window_state(self, handle: Any, state: str) -> None:
        """``state``: ``minimized``, ``maximized``, ``normal`` (also ``hidden``/``shown``)."""
        self._check()
        key = str(state).strip().lower()
        if key not in self._SHOW_STATES:
            raise ValueError(f"Unknown window state {state!r}; use minimized, maximized or normal")
        hwnd = self._hwnd(handle)
        cmd = self._SHOW_STATES[key]
        if cmd == SW_RESTORE:
            self._restore_normal(hwnd, always=True)  # SW_RESTORE also shows a hidden window
        else:
            self._api.ShowWindow(hwnd, cmd)  # returns previous visibility, not success

    def _restore_normal(self, hwnd: int, always: bool = False) -> None:
        """Bring ``hwnd`` to the normal (neither minimized nor maximized) state.

        A window minimized while maximized (WPF_RESTORETOMAXIMIZED) comes back
        *maximized* from the first SW_RESTORE; a second one un-maximizes it.
        """
        a = self._api
        for i in range(2):
            if not (a.IsIconic(hwnd) or a.IsZoomed(hwnd)) and not (always and i == 0):
                return
            a.ShowWindow(hwnd, SW_RESTORE)

    def close_window(self, handle: Any) -> None:
        """Ask the window to close (``WM_CLOSE``; the application may prompt to save)."""
        self._check()
        hwnd = self._hwnd(handle)
        if not self._api.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            raise self._error("PostMessageW(WM_CLOSE)")

    def launch(self, command: Any, **kwargs: Any) -> Any:
        """Start an application, a document or a URL the way Start > Run does.

        A command string whose program is a bare name not on ``PATH`` (``excel``,
        ``winword`` -- found through the *App Paths* registry), a document
        (``C:\\Reports\\Q3.xlsx``) or a URL is opened with ``ShellExecuteExW``
        and returns a :class:`ShellProcess`. Explicit program paths, commands
        found on ``PATH``, argument lists and ``subprocess.Popen`` keyword options
        (other than ``cwd``) use ``subprocess.Popen``.
        """
        target = shell_launch_target(command) if isinstance(command, str) and set(kwargs) <= {"cwd"} else None
        shell = getattr(self._api, "ShellExecuteExW", None)
        if target is None or shell is None:
            return super().launch(command, **kwargs)
        file, params = target
        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
        info.lpVerb = None  # the default verb ("open" for documents)
        info.lpFile = file
        info.lpParameters = params or None
        cwd = kwargs.get("cwd")
        info.lpDirectory = os.fspath(cwd) if cwd is not None else None
        info.nShow = SW_SHOWNORMAL
        co_init = getattr(self._api, "CoInitializeEx", None)
        hr = None
        if co_init is not None:  # shell extensions may need COM (STA, no OLE1 DDE)
            try:
                hr = int(co_init(None, 0x2 | 0x4))
            except Exception:
                hr = None
        try:
            ok = shell(ctypes.byref(info))
            err = self._api.get_last_error() if not ok else 0
        finally:
            if hr is not None and hr >= 0 and getattr(self._api, "CoUninitialize", None) is not None:
                self._api.CoUninitialize()
        if not ok:
            msg = f"Cannot launch {command!r}: {self._api.format_error(err)}"
            if err in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND, like Popen
                raise FileNotFoundError(2, msg, file)
            raise OSError(err, msg)
        return ShellProcess(self._api, command, info.hProcess)

    # ================================================================ clipboard
    def _open_clipboard(self, owner: Any = None) -> None:
        a = self._api
        for attempt in range(self._clip_retries):
            if a.OpenClipboard(owner):
                return
            if attempt + 1 < self._clip_retries:
                self._sleep(self._clip_delay)
        raise self._error("OpenClipboard (held by another process)")

    def get_clipboard(self) -> str:
        """Unicode text on the clipboard ('' when it holds no text)."""
        self._check()
        a = self._api
        with self._lock:
            self._open_clipboard(None)
            try:
                if not a.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return ""
                h = a.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                ptr = a.GlobalLock(h)
                if not ptr:
                    raise self._error("GlobalLock")
                try:
                    size = int(a.GlobalSize(h))
                    raw = ctypes.string_at(ptr, size - (size % 2)) if size > 0 else b""
                finally:
                    a.GlobalUnlock(h)
            finally:
                a.CloseClipboard()
        text = raw.decode("utf-16-le", "replace")
        nul = text.find("\x00")
        return text if nul < 0 else text[:nul]

    def save_clipboard(self) -> Any:
        """Snapshot every format on the clipboard (text, an image, a file list, an Excel range...).

        Clipboard-mode typing restores the user's clipboard from this with
        :meth:`restore_clipboard`; saving only its text would replace a copied
        image or Excel range with plain or empty text. Formats that are not
        global memory blocks (:data:`CLIPBOARD_HANDLE_FORMATS`) or larger than
        :data:`CLIPBOARD_SNAPSHOT_MAX` are skipped.
        """
        enum = getattr(self._api, "EnumClipboardFormats", None)
        if enum is None:
            return self.get_clipboard()
        self._check()
        a = self._api
        items: List[Tuple[int, bytes]] = []
        with self._lock:
            self._open_clipboard(None)
            try:
                fmt = 0
                while True:
                    fmt = int(enum(fmt) or 0)
                    if not fmt:
                        break
                    if fmt in CLIPBOARD_HANDLE_FORMATS or 0x200 <= fmt <= 0x3FF:
                        continue
                    h = a.GetClipboardData(fmt)
                    if not h:
                        continue
                    size = int(a.GlobalSize(h) or 0)
                    if size > CLIPBOARD_SNAPSHOT_MAX:
                        continue
                    ptr = a.GlobalLock(h)
                    if not ptr:
                        continue
                    try:
                        items.append((fmt, ctypes.string_at(ptr, size) if size > 0 else b""))
                    finally:
                        a.GlobalUnlock(h)
            finally:
                a.CloseClipboard()
        return ClipboardSnapshot(items)

    def restore_clipboard(self, saved: Any) -> None:
        """Put back a :meth:`save_clipboard` snapshot (an empty one empties the clipboard)."""
        if not isinstance(saved, ClipboardSnapshot):
            self.set_clipboard(saved if saved is not None else "")
            return
        self._check()
        a = self._api
        with self._lock:
            owner = a.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
            try:
                self._open_clipboard(owner)
                try:
                    if not a.EmptyClipboard():
                        raise self._error("EmptyClipboard")
                    for fmt, data in saved.items:
                        try:
                            hmem = self._global_bytes(data or b"\x00")
                        except BackendError:
                            continue
                        try:
                            ok = a.SetClipboardData(fmt, hmem)
                        except Exception:
                            ok = None
                        if not ok:
                            a.GlobalFree(hmem)
                finally:
                    a.CloseClipboard()
            finally:
                if owner:
                    a.DestroyWindow(owner)

    def _global_bytes(self, data: bytes) -> Any:
        """A GMEM_MOVEABLE global memory block holding ``data`` (caller frees it unless the clipboard owns it)."""
        a = self._api
        hmem = a.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not hmem:
            raise self._error("GlobalAlloc")
        try:
            ptr = a.GlobalLock(hmem)
            if not ptr:
                raise self._error("GlobalLock")
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                a.GlobalUnlock(hmem)
        except BaseException:
            a.GlobalFree(hmem)
            raise
        return hmem

    def _clipboard_exclusion_formats(self) -> List[int]:
        """Registered ids of :data:`CLIPBOARD_EXCLUSION_FORMATS` (empty when unavailable)."""
        if self._clip_exclusions is None:
            ids: List[int] = []
            register = getattr(self._api, "RegisterClipboardFormatW", None)
            if register is not None:
                for fmt in CLIPBOARD_EXCLUSION_FORMATS:
                    try:
                        fid = int(register(fmt) or 0)
                    except Exception:
                        fid = 0
                    if fid:
                        ids.append(fid)
            self._clip_exclusions = ids
        return self._clip_exclusions

    def set_clipboard(self, text: str) -> None:
        """Replace the clipboard with ``text`` (``CF_UNICODETEXT``).

        Unless the backend was created with ``clipboard_history=True``, the text
        is marked as excluded from Windows Clipboard History (Win+V), cloud
        clipboard sync and clipboard monitors, so passwords or bank data typed
        through clipboard paste do not linger there.
        """
        self._check()
        a = self._api
        data = (str(text) + "\x00").encode("utf-16-le", "surrogatepass")
        with self._lock:
            exclusions = [] if self._clip_history else self._clipboard_exclusion_formats()
            hmem = self._global_bytes(data)
            owned_by_system = False
            try:
                # A (message-only) owner window: EmptyClipboard with a NULL owner can
                # make SetClipboardData fail on some systems.
                owner = a.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
                try:
                    self._open_clipboard(owner)
                    try:
                        if not a.EmptyClipboard():
                            raise self._error("EmptyClipboard")
                        if not a.SetClipboardData(CF_UNICODETEXT, hmem):
                            raise self._error("SetClipboardData")
                        owned_by_system = True
                        for fmt in exclusions:
                            self._set_dword_format(fmt, 0)
                    finally:
                        a.CloseClipboard()
                finally:
                    if owner:
                        a.DestroyWindow(owner)
            finally:
                if not owned_by_system:
                    a.GlobalFree(hmem)

    def _set_dword_format(self, fmt: int, value: int) -> None:
        """Best effort: put a DWORD under clipboard format ``fmt`` (clipboard already open)."""
        try:
            h = self._global_bytes(int(value).to_bytes(4, "little"))
        except BackendError:
            return
        try:
            ok = self._api.SetClipboardData(fmt, h)
        except Exception:
            ok = None
        if not ok:
            self._api.GlobalFree(h)

    # ============================================================ accessibility
    def _uia_available(self) -> bool:
        if self._uia_ctx is not None:
            return True
        if self._uia_error is not None:
            return False
        if sys.platform != "win32":
            return False
        import importlib.util

        return importlib.util.find_spec("comtypes") is not None

    @property
    def uia(self) -> Any:
        """The :class:`_UIAContext` (raises NotSupported when UI Automation is unavailable)."""
        if not self._accessibility:
            raise NotSupported("Accessibility was disabled for this WindowsBackend")
        if self._uia_ctx is None:
            if self._uia_error is not None:
                raise NotSupported(self._uia_error)
            try:
                self._uia_ctx = _UIAContext(self._uia_view, self._uia_timeout)
            except NotSupported as exc:
                self._uia_error = str(exc)
                raise
        ctx = self._uia_ctx
        if getattr(ctx, "app_name", None) is None:
            try:
                ctx.app_name = self._app_for_pid  # lets UIANode report its process name
            except Exception:
                pass
        return ctx

    def _app_for_pid(self, pid: int) -> str:
        """:meth:`_process_name`, cached per tree snapshot.

        Nodes ask once per element, so names are cached -- but only until the
        next :meth:`accessibility_roots` / :meth:`element_at` /
        :meth:`focused_element` call: Windows recycles PIDs quickly, and a
        lifetime cache would report an exited process's name for a new one.
        """
        cache = self._pid_names
        name = cache.get(pid)
        if name is None:
            name = self._process_name(int(pid))
            if len(cache) > 512:
                cache.clear()
            cache[pid] = name
        return name

    def _popup_candidate(self, hwnd: int) -> Optional[bool]:
        """For a top-level window :meth:`list_windows` skips: True for a visible Win32
        pop-up menu (``#32768``), None for another visible ``WS_POPUP`` window (a menu
        only if its UIA element says so, as WinForms/WPF/Office context menus do),
        False otherwise."""
        a = self._api
        if not a.IsWindowVisible(hwnd):
            return False
        if self._class(hwnd) == "#32768":
            menu: Optional[bool] = True
        elif int(a.GetWindowLongPtrW(hwnd, GWL_STYLE)) & WS_POPUP:
            menu = None
        else:
            return False
        return False if self._is_cloaked(hwnd) else menu

    def accessibility_roots(self) -> List[AccessibleNode]:
        """One UIA root per window from :meth:`list_windows`, plus open pop-up menus
        (context menus, menu-bar drop-downs), in Z-order; ``handle`` = HWND.

        UIA parents pop-up menus to the desktop rather than to the window that
        opened them, so without these roots an open menu's items could not be found.
        """
        self._check()
        ctx = self.uia
        names: Dict[int, str] = {}
        self._pid_names = names  # fresh per snapshot: PIDs are recycled
        hwnds = self._toplevel_hwnds()
        listed = {int(w.handle) for w in self._windows_from(hwnds, names)}
        roots: List[AccessibleNode] = []
        for hwnd in hwnds:
            if hwnd in listed:
                el = ctx.element_from_handle(hwnd)
            else:
                try:
                    menu = self._popup_candidate(hwnd)
                except Exception:
                    continue  # window vanished while enumerating
                if menu is False:
                    continue
                el = ctx.element_from_handle(hwnd)
                if menu is None and _safe(lambda: int(el.CurrentControlType), 0) != UIA_MenuControlTypeId:
                    continue
            if el is not None:
                roots.append(UIANode(el, ctx, None, handle=hwnd, top_level=True))
        return roots

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        """Deepest UIA element at screen point ``(x, y)`` (``IUIAutomation::ElementFromPoint``)."""
        self._check()
        ctx = self.uia
        self._pid_names = {}
        el = ctx.element_from_point(x, y)
        return UIANode(el, ctx) if el is not None else None

    def focused_element(self) -> Optional[AccessibleNode]:
        """The element with keyboard focus (``IUIAutomation::GetFocusedElement``)."""
        self._check()
        ctx = self.uia
        self._pid_names = {}
        el = ctx.focused()
        return UIANode(el, ctx) if el is not None else None

    def __repr__(self) -> str:
        return f"<WindowsBackend dpi={self.dpi_mode}{' closed' if self._closed else ''}>"


__all__ = [
    "EXTENDED_KEYS",
    "INJECTED_EXTRA_INFO",
    "INPUT",
    "KEYBDINPUT",
    "MOUSEINPUT",
    "UIA_CONTROL_TYPES",
    "UIANode",
    "VK_CODES",
    "WindowsBackend",
    "decode_vk_scan",
    "denormalize_absolute",
    "enable_dpi_awareness",
    "iter_vk_table",
    "key_input",
    "load_uia_module",
    "map_uia_control_type",
    "mouse_input",
    "normalize_absolute",
    "uia_control_type_name",
    "unicode_inputs",
    "utf16_units",
    "vk_for_key",
]
