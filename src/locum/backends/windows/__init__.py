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
    :data:`locum.keys.KEYS` (with the extended-key flag where the hardware
    sends an ``E0`` prefix), single characters through ``VkKeyScanExW`` in the
    foreground window's keyboard layout, and
  - layout-independent text through ``KEYEVENTF_UNICODE`` (UTF-16 code units,
    so characters outside the BMP are sent as surrogate pairs).

  Every injected event carries :data:`INJECTED_EXTRA_INFO` in ``dwExtraInfo``
  so low-level hooks (e.g. the recorder) can recognise locum's own input.
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
raises :class:`~locum.errors.BackendUnavailable` when not running on Windows.
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

from ...errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound
from ...keys import KEYS, normalize_key, split_text_tokens
from ...types import Monitor, MouseButton, Point, Rect, WindowInfo
from ..base import (
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

from ._win32api import (
    BITMAPINFO,
    BITMAPINFOHEADER,
    BI_RGB,
    BOOL,
    BYTE,
    CAPTUREBLT,
    CF_UNICODETEXT,
    DIB_RGB_COLORS,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    DPI_AWARENESS_CONTEXT_SYSTEM_AWARE,
    DWMWA_CLOAKED,
    DWMWA_EXTENDED_FRAME_BOUNDS,
    DWORD,
    ERROR_ACCESS_DENIED,
    E_ACCESSDENIED,
    GMEM_MOVEABLE,
    GWL_EXSTYLE,
    GWL_STYLE,
    HANDLE,
    HARDWAREINPUT,
    HBITMAP,
    HDC,
    HGDIOBJ,
    HGLOBAL,
    HINSTANCE,
    HKL,
    HMENU,
    HMONITOR,
    HRESULT,
    HWND,
    HWND_MESSAGE,
    INJECTED_EXTRA_INFO,
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    INT,
    KEYBDINPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    KEYEVENTF_UNICODE,
    LOGPIXELSX,
    LONG,
    LONG_PTR,
    LPARAM,
    LPCWSTR,
    LPVOID,
    LPWSTR,
    MAPVK_VK_TO_CHAR,
    MAPVK_VK_TO_VSC,
    MAPVK_VK_TO_VSC_EX,
    MDT_EFFECTIVE_DPI,
    MONITORINFOEXW,
    MONITORINFOF_PRIMARY,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_MOVE_NOCOALESCE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_XDOWN,
    MOUSEEVENTF_XUP,
    MOUSEINPUT,
    POINT,
    PROCESS_PER_MONITOR_DPI_AWARE,
    PROCESS_QUERY_LIMITED_INFORMATION,
    RECT,
    SHELLEXECUTEINFOW,
    SHORT,
    SIZE_T,
    SM_CXSCREEN,
    SM_CXVIRTUALSCREEN,
    SM_CYSCREEN,
    SM_CYVIRTUALSCREEN,
    SM_SWAPBUTTON,
    SM_XVIRTUALSCREEN,
    SM_YVIRTUALSCREEN,
    SRCCOPY,
    SW_HIDE,
    SW_MAXIMIZE,
    SW_MINIMIZE,
    SW_RESTORE,
    SW_SHOW,
    SW_SHOWMINIMIZED,
    UINT,
    ULONG_PTR,
    WCHAR,
    WHEEL_DELTA,
    WM_CLOSE,
    WORD,
    WPARAM,
    WS_EX_APPWINDOW,
    WS_EX_TOOLWINDOW,
    WS_POPUP,
    XBUTTON1,
    XBUTTON2,
    _INPUTUNION,
    _Win32,
    _api_instance,
    _api_lock,
    _proto,
    _win32,
)
from .keys import (
    EXTENDED_KEYS,
    VK_CODES,
    VK_CONTROL,
    VK_MENU,
    VK_RETURN,
    VK_SHIFT,
    VK_TAB,
    _BUTTON_FLAGS,
    _SENDINPUT_CHUNK,
    _SHIFT_STATE_MODIFIERS,
    _norm_axis,
    decode_vk_scan,
    denormalize_absolute,
    iter_vk_table,
    key_input,
    mouse_input,
    normalize_absolute,
    unicode_inputs,
    utf16_units,
    vk_for_key,
)
from .uia import (
    UIA_CONTROL_TYPES,
    UIA_PATTERN_INTERFACES,
    _DIALOG_CLASSES,
    _MAX_TEXT,
    CLSID_CUIAutomation,
    CLSID_CUIAutomation8,
    ExpandCollapseState_Collapsed,
    ExpandCollapseState_Expanded,
    ExpandCollapseState_LeafNode,
    ExpandCollapseState_PartiallyExpanded,
    ToggleState_Indeterminate,
    ToggleState_Off,
    ToggleState_On,
    UIANode,
    UIA_ButtonControlTypeId,
    UIA_ExpandCollapsePatternId,
    UIA_InvokePatternId,
    UIA_IsDialogPropertyId,
    UIA_LegacyIAccessiblePatternId,
    UIA_MenuControlTypeId,
    UIA_PaneControlTypeId,
    UIA_RangeValuePatternId,
    UIA_ScrollItemPatternId,
    UIA_SelectionItemPatternId,
    UIA_TextPatternId,
    UIA_TogglePatternId,
    UIA_ValuePatternId,
    UIA_WindowControlTypeId,
    _UIAContext,
    _com_ok,
    _safe,
    _uia_module,
    _uia_module_lock,
    load_uia_module,
    map_uia_control_type,
    uia_control_type_name,
)
from .screen import (
    _MssHolder,
    _close_quietly,
    _dpi_lock,
    _dpi_mode,
    enable_dpi_awareness,
)
from .clipboard import (
    CLIPBOARD_EXCLUSION_FORMATS,
    CLIPBOARD_HANDLE_FORMATS,
    CLIPBOARD_SNAPSHOT_MAX,
    ClipboardSnapshot,
)
from .process import (
    SEE_MASK_FLAG_NO_UI,
    SEE_MASK_NOASYNC,
    SEE_MASK_NOCLOSEPROCESS,
    STILL_ACTIVE,
    SW_SHOWNORMAL,
    WAIT_OBJECT_0,
    _PROGRAM_EXTS,
    ShellProcess,
    shell_launch_target,
)
from .input import (
    _Held,
)
from .backend import (
    WindowsBackend,
)

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
