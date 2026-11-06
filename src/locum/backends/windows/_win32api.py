"""Win32 plumbing for the Windows backend: fixed-width C types, ``ctypes``
structures (POINT, RECT, INPUT, MONITORINFOEXW, BITMAPINFO, SHELLEXECUTEINFOW ...),
Win32 constants and the lazily bound :class:`_Win32` API object (:func:`_win32`).
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Any, Optional, Sequence

from ...errors import BackendUnavailable
from ...types import Rect


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

#: Marker placed in ``dwExtraInfo`` of every event locum injects ("PYOL").
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


GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3


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
        advapi32 = opt_dll("advapi32")
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
            (u, "ToUnicodeEx", INT, [UINT, UINT, P(BYTE), LPWSTR, INT, UINT, HKL]),
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
            # integrity levels (UIPI: input to a higher-integrity window is dropped silently)
            (kernel32, "GetCurrentProcess", HANDLE, []),
            (advapi32, "OpenProcessToken", BOOL, [HANDLE, DWORD, P(HANDLE)]),
            (advapi32, "GetTokenInformation", BOOL, [HANDLE, INT, LPVOID, DWORD, P(DWORD)]),
            (advapi32, "GetSidSubAuthorityCount", P(ctypes.c_ubyte), [LPVOID]),
            (advapi32, "GetSidSubAuthority", P(DWORD), [LPVOID, DWORD]),
            (u, "WindowFromPoint", HWND, [POINT]),
            (u, "GetAncestor", HWND, [HWND, UINT]),
            (u, "GetWindow", HWND, [HWND, UINT]),
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
