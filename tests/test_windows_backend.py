"""Tests for the Windows backend.

Windows itself is not available in CI, so these tests cover:

* importing the module and the constructor's ``BackendUnavailable`` off Windows;
* the pure helpers (VK table, extended keys, absolute-coordinate normalization,
  UTF-16 splitting, ``VkKeyScan`` decoding, UIA role mapping);
* ctypes struct layouts (``INPUT`` & friends must match the Win32 ABI);
* the backend logic driven through a *fake Win32 API* object (the ``_api``
  hook), which decodes every ``INPUT`` passed to ``SendInput`` and simulates
  windows, monitors, the clipboard and the foreground lock;
* :class:`UIANode` against fake UI Automation elements.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from typing import Any, Dict, List, Optional

import pytest
from PIL import Image

import pypilot.backends.windows as winmod
from pypilot.backends import create_backend
from pypilot.backends.base import (
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOWS,
    ROLES,
)
from pypilot.backends.windows import (
    EXTENDED_KEYS,
    INJECTED_EXTRA_INFO,
    INPUT,
    KEYBDINPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_XDOWN,
    MOUSEEVENTF_XUP,
    MOUSEINPUT,
    UIA_CONTROL_TYPES,
    VK_CODES,
    WindowsBackend,
    UIANode,
    decode_vk_scan,
    denormalize_absolute,
    enable_dpi_awareness,
    iter_vk_table,
    map_uia_control_type,
    normalize_absolute,
    uia_control_type_name,
    unicode_inputs,
    utf16_units,
    vk_for_key,
)
from pypilot.errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound
from pypilot.keys import KEYS
from pypilot.types import MouseButton, Point, Rect

PTR64 = ctypes.sizeof(ctypes.c_void_p) == 8


# ================================================================== import / availability
def test_module_imports_without_windows():
    # A fresh interpreter: importing must not touch windll/WINFUNCTYPE or comtypes.
    code = (
        "import sys; import pypilot.backends.windows as w; "
        "assert 'comtypes' not in sys.modules; print(w.WindowsBackend.name)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "windows"


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows guard")
def test_constructor_raises_off_windows():
    with pytest.raises(BackendUnavailable):
        WindowsBackend()
    with pytest.raises(BackendUnavailable):
        create_backend("windows")


# ================================================================== struct layouts
def test_input_struct_layout_matches_win32_abi():
    assert ctypes.sizeof(winmod.POINT) == 8
    assert ctypes.sizeof(winmod.RECT) == 16
    assert ctypes.sizeof(winmod.HARDWAREINPUT) == 8
    assert ctypes.sizeof(winmod.BITMAPINFOHEADER) == 40
    if PTR64:
        assert ctypes.sizeof(MOUSEINPUT) == 32
        assert ctypes.sizeof(KEYBDINPUT) == 24
        assert ctypes.sizeof(INPUT) == 40
        assert INPUT.u.offset == 8
        assert MOUSEINPUT.dwExtraInfo.offset == 24
        assert KEYBDINPUT.dwExtraInfo.offset == 16
    else:  # pragma: no cover - 32-bit interpreters
        assert ctypes.sizeof(MOUSEINPUT) == 24
        assert ctypes.sizeof(KEYBDINPUT) == 16
        assert ctypes.sizeof(INPUT) == 28
        assert INPUT.u.offset == 4
    assert MOUSEINPUT.mouseData.offset == 8 and MOUSEINPUT.dwFlags.offset == 12
    assert KEYBDINPUT.wScan.offset == 2 and KEYBDINPUT.dwFlags.offset == 4


def test_mouse_input_stores_negative_wheel_as_dword():
    inp = winmod.mouse_input(0, 0, -120, MOUSEEVENTF_WHEEL)
    assert inp.type == winmod.INPUT_MOUSE
    assert inp.mi.mouseData == 0xFFFFFF88
    assert ctypes.c_int32(inp.mi.mouseData).value == -120
    assert inp.mi.dwExtraInfo == INJECTED_EXTRA_INFO


def test_key_input_fields():
    inp = winmod.key_input(0x41, 0x1E, KEYEVENTF_KEYUP)
    assert inp.type == winmod.INPUT_KEYBOARD
    assert (inp.ki.wVk, inp.ki.wScan, inp.ki.dwFlags) == (0x41, 0x1E, KEYEVENTF_KEYUP)
    assert inp.ki.dwExtraInfo == INJECTED_EXTRA_INFO


# ================================================================== pure helpers
def test_vk_map_covers_every_canonical_key():
    assert set(KEYS) <= set(VK_CODES)
    table = list(iter_vk_table())
    assert {name for name, _, _ in table} == set(KEYS)
    for name, vk, ext in table:
        assert 0 < vk <= 0xFF, name
        assert ext == (name in EXTENDED_KEYS)
    assert EXTENDED_KEYS <= set(KEYS)


def test_vk_codes_known_values():
    expect = {
        "enter": 0x0D, "tab": 0x09, "space": 0x20, "backspace": 0x08, "escape": 0x1B, "delete": 0x2E,
        "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "f1": 0x70, "f12": 0x7B, "f24": 0x87,
        "num0": 0x60, "num9": 0x69, "num_add": 0x6B, "num_divide": 0x6F, "shift": 0xA0, "shift_r": 0xA1,
        "ctrl": 0xA2, "ctrl_r": 0xA3, "alt": 0xA4, "alt_r": 0xA5, "cmd": 0x5B, "cmd_r": 0x5C, "menu": 0x5D,
        "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91, "printscreen": 0x2C, "pause": 0x13,
        "volumemute": 0xAD, "volumeup": 0xAF, "playpause": 0xB3, "browser_back": 0xA6,
    }
    for name, vk in expect.items():
        assert VK_CODES[name] == vk, name


def test_extended_key_set():
    for k in ("up", "down", "left", "right", "home", "end", "pageup", "pagedown", "insert", "delete",
              "ctrl_r", "alt_r", "altgr", "cmd", "cmd_r", "num_enter", "num_divide", "numlock", "printscreen"):
        assert k in EXTENDED_KEYS, k
    for k in ("enter", "shift", "shift_r", "ctrl", "alt", "num0", "num_add", "space", "a" * 1, "pause", "f1"):
        assert k not in EXTENDED_KEYS, k
    assert vk_for_key("num_enter") == (0x0D, True)
    assert vk_for_key("enter") == (0x0D, False)
    with pytest.raises(KeyError):
        vk_for_key("nonsense")


@pytest.mark.parametrize(
    "virtual",
    [
        Rect(0, 0, 1920, 1080),
        Rect(-1920, 0, 3840, 1080),  # secondary monitor left of primary
        Rect(-2560, -360, 4480, 1440),  # mixed sizes, negative origin on both axes
        Rect(0, -1080, 1366, 1848),
        Rect(-3, -7, 5, 3),  # tiny, odd
    ],
)
def test_absolute_normalization_round_trips_every_pixel(virtual):
    xs = range(virtual.left, virtual.right) if virtual.width < 50 else list(range(virtual.left, virtual.left + 40)) + list(
        range(virtual.right - 40, virtual.right)) + list(range(virtual.left, virtual.right, 97))
    ys = range(virtual.top, virtual.bottom) if virtual.height < 50 else list(range(virtual.top, virtual.top + 40)) + list(
        range(virtual.bottom - 40, virtual.bottom)) + list(range(virtual.top, virtual.bottom, 89))
    for x in xs:
        nx, _ = normalize_absolute(x, virtual.top, virtual)
        assert 0 <= nx <= 65535
        assert denormalize_absolute(nx, 0, virtual)[0] == x
        # also exact under the alternative "round(n * (size-1) / 65535)" model
        assert virtual.left + round(nx * (virtual.width - 1) / 65535) == x
    for y in ys:
        _, ny = normalize_absolute(virtual.left, y, virtual)
        assert 0 <= ny <= 65535
        assert denormalize_absolute(0, ny, virtual)[1] == y
        assert virtual.top + round(ny * (virtual.height - 1) / 65535) == y


def test_absolute_normalization_full_range_and_clamping():
    vs = Rect(-1920, -200, 4480, 1640)
    assert normalize_absolute(-1920, -200, vs)[0] < 16
    assert normalize_absolute(-1920 + 4479, 0, vs)[0] > 65520
    # outside points clamp to the edges
    assert normalize_absolute(-99999, -99999, vs) == normalize_absolute(-1920, -200, vs)
    assert normalize_absolute(99999, 99999, vs) == normalize_absolute(vs.right - 1, vs.bottom - 1, vs)
    # monotonic
    prev = -1
    for x in range(vs.left, vs.right):
        nx = normalize_absolute(x, 0, vs)[0]
        assert nx > prev
        prev = nx
    assert normalize_absolute(5, 5, Rect(0, 0, 1, 1)) == (0, 0)


def test_utf16_units_and_surrogate_pairs():
    assert utf16_units("aZ") == [0x61, 0x5A]
    assert utf16_units("é€") == [0xE9, 0x20AC]
    assert utf16_units("😀") == [0xD83D, 0xDE00]
    assert utf16_units("a𝄞b") == [0x61, 0xD834, 0xDD1E, 0x62]
    assert utf16_units("") == []
    events = unicode_inputs("😀")
    assert [(e.ki.wVk, e.ki.wScan, e.ki.dwFlags) for e in events] == [
        (0, 0xD83D, KEYEVENTF_UNICODE),
        (0, 0xD83D, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        (0, 0xDE00, KEYEVENTF_UNICODE),
        (0, 0xDE00, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]


def test_decode_vk_scan():
    assert decode_vk_scan(0x41) == (0x41, 0)
    assert decode_vk_scan(0x141) == (0x41, 1)  # Shift+A
    assert decode_vk_scan(0x645) == (0x45, 6)  # Ctrl+Alt+E (AltGr) -> €
    assert decode_vk_scan(-1) is None
    assert decode_vk_scan(0xFFFF) is None
    assert decode_vk_scan(0x0841) is None  # Hankaku bit
    assert decode_vk_scan(ctypes.c_int16(0x0131).value) == (0x31, 1)


def test_uia_control_type_mapping():
    assert map_uia_control_type(50000) == "button"
    assert map_uia_control_type(50000, has_toggle=True) == "togglebutton"
    assert map_uia_control_type(50004) == "edit"
    assert map_uia_control_type(50005) == "link"
    assert map_uia_control_type(50018) == "tablist"
    assert map_uia_control_type(50019) == "tab"
    assert map_uia_control_type(50016) == "spinbutton"
    assert map_uia_control_type(50031) == "button"
    assert map_uia_control_type(50032) == "window"
    assert map_uia_control_type(50032, class_name="#32770") == "dialog"
    assert map_uia_control_type(50032, is_dialog=True) == "dialog"
    assert map_uia_control_type(50033) == "pane"
    assert map_uia_control_type(50033, is_root=True) == "desktop"
    assert map_uia_control_type(50035) == "columnheader"
    assert map_uia_control_type(50029) == "row"
    assert map_uia_control_type(12345) == "unknown"
    assert map_uia_control_type(None) == "unknown"  # type: ignore[arg-type]
    # Every mapping is a lowercase identifier; the bulk are portable ROLES.
    roles = {r for _, r in UIA_CONTROL_TYPES.values()}
    assert all(r == r.lower() and " " not in r for r in roles)
    assert len(roles & ROLES) >= len(roles) - 4
    assert set(range(50000, 50041)) == set(UIA_CONTROL_TYPES)
    assert uia_control_type_name(50000) == "Button"
    assert uia_control_type_name(1) == "ControlType(1)"


# ================================================================== fake Win32 API
def _deref(arg: Any) -> Any:
    return getattr(arg, "_obj", arg)


class FakeWin:
    def __init__(self, hwnd, title, frame, *, pid=100, cls="Notepad", visible=True, exstyle=0, cloaked=0,
                 iconic=False, zoomed=False, border=(7, 0, 7, 7), exe="C:\\Windows\\notepad.exe", style=0):
        self.hwnd = hwnd
        self.title = title
        self.frame = frame
        self.pid = pid
        self.tid = pid * 10
        self.cls = cls
        self.visible = visible
        self.exstyle = exstyle
        self.style = style
        self.restore_max = False  # WPF_RESTORETOMAXIMIZED: minimized while maximized
        self.cloaked = cloaked
        self.iconic = iconic
        self.zoomed = zoomed
        self.border = border  # invisible DWM borders (l, t, r, b)
        self.exe = exe

    @property
    def outer(self) -> Rect:
        l, t, r, b = self.border
        f = self.frame
        return Rect.from_ltrb(f.left - l, f.top - t, f.right + r, f.bottom + b)


class FakeApi:
    """Simulates the subset of Win32 the backend uses, recording every call."""

    WNDENUMPROC = staticmethod(lambda f: f)
    MONITORENUMPROC = staticmethod(lambda f: f)

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.inputs: List[tuple] = []
        self.virtual = Rect(-1920, -200, 1920 + 2560, 1440 + 200)
        self.cursor = [0, 0]
        self.apply_moves = True
        self.swap = 0
        self.send_limit: Optional[int] = None
        self.last_error = 0
        self.wins: Dict[int, FakeWin] = {}
        self.z: List[int] = []
        self.fg: Optional[int] = None
        self.fg_policy = "free"  # free | attach | alt | never
        self.attached: set = set()
        self.alt_down = False
        self.clip_busy = 0
        self.clip_open = False
        self.clip_data: Optional[bytes] = None
        self.clip_extra: Dict[int, bytes] = {}
        self.registered: Dict[str, int] = {}
        self.caps_lock = False
        self.dead_vks: set = set()
        self.mem: Dict[int, Any] = {}
        self.freed: List[int] = []
        self.monitors = [
            (0x2, Rect(-1920, 0, 1920, 1080), 0, "\\\\.\\DISPLAY2", 96),
            (0x1, Rect(0, -200, 2560, 1440), 1, "\\\\.\\DISPLAY1", 144),
        ]
        self.layout_table = {"a": 0x41, "A": 0x141, "!": 0x131, "1": 0x31, "€": 0x645, "+": 0x1BB}

    # ---------------------------------------------------------------- generic
    def get_last_error(self) -> int:
        return self.last_error

    @staticmethod
    def format_error(code: int) -> str:
        return f"fake error {code}"

    def add(self, win: FakeWin, front: bool = False) -> FakeWin:
        self.wins[win.hwnd] = win
        if front:
            self.z.insert(0, win.hwnd)
        else:
            self.z.append(win.hwnd)
        return win

    # ------------------------------------------------------------------ input
    def SendInput(self, n, arr, size):
        assert size == ctypes.sizeof(INPUT)
        assert n == len(arr)
        count = n if self.send_limit is None else min(n, self.send_limit)
        for i in range(count):
            e = arr[i]
            if e.type == winmod.INPUT_MOUSE:
                mi = e.mi
                assert mi.dwExtraInfo == INJECTED_EXTRA_INFO
                self.inputs.append(("mouse", mi.dx, mi.dy, ctypes.c_int32(mi.mouseData).value, mi.dwFlags))
                if mi.dwFlags & MOUSEEVENTF_ABSOLUTE and self.apply_moves:
                    assert mi.dwFlags & MOUSEEVENTF_VIRTUALDESK
                    self.cursor = list(denormalize_absolute(mi.dx, mi.dy, self.virtual))
            else:
                ki = e.ki
                assert ki.dwExtraInfo == INJECTED_EXTRA_INFO
                self.inputs.append(("key", ki.wVk, ki.wScan, ki.dwFlags))
                if ki.wVk == winmod.VK_MENU:
                    self.alt_down = not ki.dwFlags & KEYEVENTF_KEYUP
        if count != n:
            self.last_error = 5
        return count

    def GetCursorPos(self, p):
        pt = _deref(p)
        pt.x, pt.y = self.cursor
        return 1

    def SetCursorPos(self, x, y):
        self.calls.append(("SetCursorPos", x, y))
        self.cursor = [x, y]
        return 1

    def GetSystemMetrics(self, idx):
        v = self.virtual
        return {
            winmod.SM_XVIRTUALSCREEN: v.left, winmod.SM_YVIRTUALSCREEN: v.top,
            winmod.SM_CXVIRTUALSCREEN: v.width, winmod.SM_CYVIRTUALSCREEN: v.height,
            winmod.SM_CXSCREEN: 2560, winmod.SM_CYSCREEN: 1440, winmod.SM_SWAPBUTTON: self.swap,
        }.get(idx, 0)

    def MapVirtualKeyW(self, vk, mode):
        table = {0x0D: 0x1C, 0x25: 0xE04B, 0x26: 0xE048, 0x41: 0x1E, 0xA0: 0x2A, 0xA2: 0x1D, 0xA4: 0x38,
                 0x12: 0x38, 0x45: 0x12, 0x31: 0x31 - 0x31 + 0x02, 0xBB: 0x0D, 0x09: 0x0F, 0x2E: 0xE053}
        sc = table.get(vk, 0)
        return sc if mode == winmod.MAPVK_VK_TO_VSC_EX else sc & 0xFF

    def VkKeyScanExW(self, ch, hkl):
        assert hkl == 0x04090409
        return ctypes.c_int16(self.layout_table.get(ch, -1)).value

    def GetKeyboardLayout(self, tid):
        return 0x04090409

    def MapVirtualKeyExW(self, vk, mode, hkl):
        assert mode == winmod.MAPVK_VK_TO_CHAR and hkl == 0x04090409
        return (0x80000000 | 0x5E) if vk in self.dead_vks else vk

    def GetKeyState(self, vk):
        return int(vk == 0x14 and self.caps_lock)

    # --------------------------------------------------------------- monitors
    def EnumDisplayMonitors(self, hdc, clip, proc, lparam):
        for h, *_ in self.monitors:
            if not proc(h, None, None, lparam):
                break
        return 1

    def GetMonitorInfoW(self, h, info):
        info = _deref(info)
        assert info.cbSize == ctypes.sizeof(winmod.MONITORINFOEXW)
        for hh, rect, primary, name, _dpi in self.monitors:
            if hh == h:
                info.rcMonitor = winmod.RECT(rect.left, rect.top, rect.right, rect.bottom)
                info.dwFlags = primary
                info.szDevice = name
                return 1
        return 0

    def GetDpiForMonitor(self, h, kind, px, py):
        for hh, _r, _p, _n, dpi in self.monitors:
            if hh == h:
                _deref(px).value = dpi
                _deref(py).value = dpi
                return 0
        return -1

    # ---------------------------------------------------------------- windows
    def EnumWindows(self, proc, lparam):
        for h in list(self.z):
            if not proc(h, lparam):
                break
        return 1

    def GetForegroundWindow(self):
        return self.fg

    def GetDesktopWindow(self):
        return 0x10010

    def GetWindowThreadProcessId(self, hwnd, pid_ptr):
        w = self.wins.get(hwnd)
        if w is None:
            return 0
        if pid_ptr is not None:
            _deref(pid_ptr).value = w.pid
        return w.tid

    def GetCurrentThreadId(self):
        return 4242

    def IsWindow(self, hwnd):
        return int(hwnd in self.wins)

    def IsWindowVisible(self, hwnd):
        return int(self.wins[hwnd].visible)

    def IsIconic(self, hwnd):
        return int(self.wins[hwnd].iconic)

    def IsZoomed(self, hwnd):
        return int(self.wins[hwnd].zoomed)

    def GetWindowLongPtrW(self, hwnd, idx):
        assert idx in (winmod.GWL_EXSTYLE, -16)  # GWL_STYLE
        w = self.wins[hwnd]
        return w.exstyle if idx == winmod.GWL_EXSTYLE else w.style

    def GetWindowTextLengthW(self, hwnd):
        return len(self.wins[hwnd].title)

    def GetWindowTextW(self, hwnd, buf, n):
        text = self.wins[hwnd].title[: n - 1]
        buf.value = text
        return len(text)

    def GetClassNameW(self, hwnd, buf, n):
        text = self.wins[hwnd].cls[: n - 1]
        buf.value = text
        return len(text)

    def GetWindowRect(self, hwnd, rc):
        w = self.wins.get(hwnd)
        if w is None:
            self.last_error = 1400
            return 0
        o = w.outer
        r = _deref(rc)
        r.left, r.top, r.right, r.bottom = o.left, o.top, o.right, o.bottom
        return 1

    def DwmGetWindowAttribute(self, hwnd, attr, out, size):
        w = self.wins[hwnd]
        obj = _deref(out)
        assert size == ctypes.sizeof(obj)
        if attr == winmod.DWMWA_CLOAKED:
            obj.value = w.cloaked
            return 0
        if attr == winmod.DWMWA_EXTENDED_FRAME_BOUNDS:
            f = w.frame
            obj.left, obj.top, obj.right, obj.bottom = f.left, f.top, f.right, f.bottom
            return 0
        return -1

    def OpenProcess(self, access, inherit, pid):
        assert access == winmod.PROCESS_QUERY_LIMITED_INFORMATION
        return 50000 + pid if any(w.pid == pid and w.exe for w in self.wins.values()) else None

    def QueryFullProcessImageNameW(self, h, flags, buf, size):
        pid = h - 50000
        exe = next(w.exe for w in self.wins.values() if w.pid == pid)
        buf.value = exe
        _deref(size).value = len(exe)
        return 1

    def CloseHandle(self, h):
        self.calls.append(("CloseHandle", h))
        return 1

    def _try_fg(self, hwnd):
        ok = bool(
            self.fg_policy == "free"
            or (self.fg_policy == "attach" and self.attached)
            or (self.fg_policy == "alt" and self.alt_down)
        )
        if ok:
            self.fg = hwnd
            self.z.remove(hwnd)
            self.z.insert(0, hwnd)
        return int(ok)

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))
        return self._try_fg(hwnd)

    def BringWindowToTop(self, hwnd):
        self.calls.append(("BringWindowToTop", hwnd))
        return 1

    def AllowSetForegroundWindow(self, pid):
        self.calls.append(("AllowSetForegroundWindow", pid))
        return 1

    def AttachThreadInput(self, a, b, attach):
        self.calls.append(("AttachThreadInput", a, b, bool(attach)))
        if attach:
            self.attached.add(b)
        else:
            self.attached.discard(b)
        return 1

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))
        w = self.wins[hwnd]
        if cmd == winmod.SW_RESTORE:
            if w.iconic:  # back to the pre-minimize state (maximized if it was)
                w.iconic, w.zoomed = False, w.restore_max
            else:
                w.zoomed = False
        elif cmd == winmod.SW_MINIMIZE:
            w.restore_max = w.zoomed or (w.iconic and w.restore_max)
            w.iconic, w.zoomed = True, False
        elif cmd == winmod.SW_MAXIMIZE:
            w.iconic, w.zoomed = False, True
        return 1

    def MoveWindow(self, hwnd, x, y, w, h, repaint):
        self.calls.append(("MoveWindow", hwnd, x, y, w, h, repaint))
        win = self.wins[hwnd]
        l, t, r, b = win.border
        win.frame = Rect(x + l, y + t, w - l - r, h - t - b)
        return 1

    def PostMessageW(self, hwnd, msg, wp, lp):
        self.calls.append(("PostMessageW", hwnd, msg, wp, lp))
        return 1

    # -------------------------------------------------------------- clipboard
    def _alloc(self, data: bytes) -> int:
        buf = ctypes.create_string_buffer(data, len(data))
        addr = ctypes.addressof(buf)
        self.mem[addr] = buf
        return addr

    def OpenClipboard(self, owner):
        self.calls.append(("OpenClipboard", owner))
        if self.clip_busy > 0:
            self.clip_busy -= 1
            self.last_error = 5
            return 0
        assert not self.clip_open
        self.clip_open = True
        return 1

    def CloseClipboard(self):
        assert self.clip_open
        self.clip_open = False
        return 1

    def EmptyClipboard(self):
        assert self.clip_open
        self.clip_data = None
        self.clip_extra.clear()
        return 1

    def IsClipboardFormatAvailable(self, fmt):
        return int(fmt == winmod.CF_UNICODETEXT and self.clip_data is not None)

    def GetClipboardData(self, fmt):
        assert self.clip_open
        return self._alloc(self.clip_data) if self.clip_data is not None else None

    def SetClipboardData(self, fmt, h):
        assert self.clip_open
        data = ctypes.string_at(h, len(self.mem[h]))
        if fmt == winmod.CF_UNICODETEXT:
            self.clip_data = data
        else:
            assert fmt in self.registered.values()
            self.clip_extra[fmt] = data
        return h

    def RegisterClipboardFormatW(self, name):
        return self.registered.setdefault(name, 0xC000 + len(self.registered))

    def GlobalAlloc(self, flags, size):
        assert flags == winmod.GMEM_MOVEABLE
        return self._alloc(b"\xAA" * size)

    def GlobalLock(self, h):
        return h

    def GlobalUnlock(self, h):
        return 1

    def GlobalSize(self, h):
        return len(self.mem[h])

    def GlobalFree(self, h):
        self.freed.append(h)
        return None

    def CreateWindowExW(self, *args):
        assert args[1] == "STATIC" and args[8] == winmod.HWND_MESSAGE
        return 0x777

    def DestroyWindow(self, hwnd):
        self.calls.append(("DestroyWindow", hwnd))
        return 1

    def GetDC(self, hwnd):
        return 0x99

    def ReleaseDC(self, hwnd, hdc):
        return 1

    def GetDeviceCaps(self, hdc, idx):
        return 120


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def be(api):
    return WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)


def keys_of(api: FakeApi) -> List[tuple]:
    return [e for e in api.inputs if e[0] == "key"]


# ================================================================== backend: basics
def test_capabilities_and_repr(be):
    caps = be.capabilities
    for c in (CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_SCREENSHOT, CAP_WINDOWS, CAP_CLIPBOARD, CAP_HSCROLL):
        assert c in caps
    if sys.platform != "win32":
        assert CAP_ACCESSIBILITY not in caps
    assert "WindowsBackend" in repr(be)
    with pytest.raises(ValueError):
        WindowsBackend(_api=FakeApi(), dpi_aware=False, screenshot_method="bogus")
    with pytest.raises(ValueError):
        WindowsBackend(_api=FakeApi(), dpi_aware=False, uia_view="bogus")


def test_closed_backend_raises(be):
    be.close()
    assert be.closed
    with pytest.raises(BackendError):
        be.mouse_position()
    be.close()  # idempotent


# ================================================================== backend: mouse
def test_mouse_move_sends_absolute_virtualdesk(be, api):
    be.mouse_move(-1000, 500)
    kind, nx, ny, data, flags = api.inputs[-1]
    assert kind == "mouse"
    assert flags & MOUSEEVENTF_MOVE and flags & MOUSEEVENTF_ABSOLUTE and flags & MOUSEEVENTF_VIRTUALDESK
    assert (nx, ny) == normalize_absolute(-1000, 500, api.virtual)
    assert be.mouse_position() == Point(-1000, 500)
    assert not [c for c in api.calls if c[0] == "SetCursorPos"]


def test_mouse_move_corrects_with_setcursorpos(be, api):
    api.apply_moves = False
    be.mouse_move(2000, -150)
    assert ("SetCursorPos", 2000, -150) in api.calls
    assert be.mouse_position() == Point(2000, -150)
    be.mouse_move(99999, 99999)  # clamped to the virtual desktop
    assert be.mouse_position() == Point(api.virtual.right - 1, api.virtual.bottom - 1)


@pytest.mark.parametrize(
    "button,down,up,data",
    [
        ("left", MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
        ("right", MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
        ("middle", MOUSEEVENTF_MIDDLEDOWN, winmod.MOUSEEVENTF_MIDDLEUP, 0),
        ("x1", MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, winmod.XBUTTON1),
        (MouseButton.X2, MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, winmod.XBUTTON2),
    ],
)
def test_mouse_buttons(be, api, button, down, up, data):
    be.mouse_down(button)
    be.mouse_up(button)
    assert api.inputs == [("mouse", 0, 0, data, down), ("mouse", 0, 0, data, up)]


def test_swapped_buttons_map_left_to_primary(be, api):
    api.swap = 1
    be.mouse_down("left")
    api.swap = 0  # even if the setting flips mid-press, the matching up is sent
    be.mouse_up("left")
    assert [e[4] for e in api.inputs] == [MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP]
    api.inputs.clear()
    api.swap = 1
    nb = WindowsBackend(_api=api, dpi_aware=False, respect_swapped_buttons=False)
    nb.mouse_down("left")
    assert api.inputs[-1][4] == MOUSEEVENTF_LEFTDOWN


def test_scroll_wheel_delta(be, api):
    be.scroll(dy=2)
    be.scroll(dy=-1, dx=3)
    be.scroll()
    assert api.inputs == [
        ("mouse", 0, 0, 120, MOUSEEVENTF_WHEEL),
        ("mouse", 0, 0, 120, MOUSEEVENTF_WHEEL),
        ("mouse", 0, 0, -120, MOUSEEVENTF_WHEEL),
        ("mouse", 0, 0, 120, MOUSEEVENTF_HWHEEL),
        ("mouse", 0, 0, 120, MOUSEEVENTF_HWHEEL),
        ("mouse", 0, 0, 120, MOUSEEVENTF_HWHEEL),
    ]
    api.inputs.clear()
    be.scroll(dx=-1)
    assert api.inputs == [("mouse", 0, 0, -120, MOUSEEVENTF_HWHEEL)]


def test_sendinput_failure_raises(be, api):
    api.send_limit = 0
    with pytest.raises(BackendError, match="SendInput"):
        be.mouse_down()


# ================================================================== backend: keyboard
def test_key_down_up_named_keys(be, api):
    be.key_down("enter")
    be.key_up("enter")
    be.key_down("left")
    be.key_up("left")
    be.key_down("num_enter")
    be.key_up("num_enter")
    assert keys_of(api) == [
        ("key", 0x0D, 0x1C, 0),
        ("key", 0x0D, 0x1C, KEYEVENTF_KEYUP),
        ("key", 0x25, 0x4B, KEYEVENTF_EXTENDEDKEY),
        ("key", 0x25, 0x4B, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP),
        ("key", 0x0D, 0x1C, KEYEVENTF_EXTENDEDKEY),
        ("key", 0x0D, 0x1C, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP),
    ]


def test_every_canonical_key_can_be_pressed(be, api):
    for name in sorted(KEYS):
        be.key_down(name)
        be.key_up(name)
    ks = keys_of(api)
    assert len(ks) == 2 * len(KEYS)
    for name, (down, up) in zip(sorted(KEYS), zip(ks[::2], ks[1::2])):
        vk, ext = vk_for_key(name)
        assert down[1] == up[1] == vk
        assert bool(down[3] & KEYEVENTF_EXTENDEDKEY) == ext, name
        assert not down[3] & KEYEVENTF_KEYUP and up[3] & KEYEVENTF_KEYUP


def test_aliases_are_normalized(be, api):
    be.key_down("Return")
    be.key_up("\n")
    assert [k[1] for k in keys_of(api)] == [0x0D, 0x0D]
    with pytest.raises(ValueError):
        be.key_down("definitely-not-a-key")
    with pytest.raises(ValueError):
        be.key_down("")


def test_char_keys_use_layout_and_add_shift(be, api):
    be.key_down("a")
    be.key_up("a")
    assert keys_of(api) == [("key", 0x41, 0x1E, 0), ("key", 0x41, 0x1E, KEYEVENTF_KEYUP)]
    api.inputs.clear()
    be.key_down("A")
    be.key_up("A")
    assert keys_of(api) == [
        ("key", 0xA0, 0x2A, 0),
        ("key", 0x41, 0x1E, 0),
        ("key", 0x41, 0x1E, KEYEVENTF_KEYUP),
        ("key", 0xA0, 0x2A, KEYEVENTF_KEYUP),
    ]


def test_char_key_skips_shift_already_held(be, api):
    be.key_down("shift")
    be.key_down("!")
    be.key_up("!")
    be.key_up("shift")
    assert [(k[1], bool(k[3] & KEYEVENTF_KEYUP)) for k in keys_of(api)] == [
        (0xA0, False), (0x31, False), (0x31, True), (0xA0, True),
    ]


def test_altgr_chars_press_ctrl_alt(be, api):
    be.key_down("€")
    be.key_up("€")
    vks = [(k[1], bool(k[3] & KEYEVENTF_KEYUP)) for k in keys_of(api)]
    assert vks == [(0xA2, False), (0xA4, False), (0x45, False), (0x45, True), (0xA4, True), (0xA2, True)]


def test_unmappable_chars_fall_back_to_unicode(be, api):
    be.key_down("é")
    be.key_up("é")
    be.key_down("😀")
    be.key_up("😀")
    assert keys_of(api) == [
        ("key", 0, 0xE9, KEYEVENTF_UNICODE),
        ("key", 0, 0xE9, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ("key", 0, 0xD83D, KEYEVENTF_UNICODE),
        ("key", 0, 0xDE00, KEYEVENTF_UNICODE),
        ("key", 0, 0xD83D, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ("key", 0, 0xDE00, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]


def test_release_all_releases_keys_and_buttons(be, api):
    be.key_down("ctrl")
    be.key_down("A")
    be.mouse_down("right")
    api.inputs.clear()
    be.release_all()
    assert [(e[1], e[3]) if e[0] == "key" else e[4] for e in api.inputs] == [
        (0x41, KEYEVENTF_KEYUP), (0xA0, KEYEVENTF_KEYUP), (0xA2, KEYEVENTF_KEYUP), MOUSEEVENTF_RIGHTUP,
    ]
    api.inputs.clear()
    be.release_all()
    assert api.inputs == []


def test_close_releases_held_keys(be, api):
    be.key_down("shift")
    be.close()
    assert keys_of(api)[-1] == ("key", 0xA0, 0x2A, KEYEVENTF_KEYUP)


def test_type_text_unicode_with_enter_and_tab(be, api):
    be.type_text("hi\r\n😀\tx")
    ks = keys_of(api)
    assert ks == [
        ("key", 0, ord("h"), KEYEVENTF_UNICODE),
        ("key", 0, ord("h"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ("key", 0, ord("i"), KEYEVENTF_UNICODE),
        ("key", 0, ord("i"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ("key", 0x0D, 0x1C, 0),
        ("key", 0x0D, 0x1C, KEYEVENTF_KEYUP),
        ("key", 0, 0xD83D, KEYEVENTF_UNICODE),
        ("key", 0, 0xD83D, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ("key", 0, 0xDE00, KEYEVENTF_UNICODE),
        ("key", 0, 0xDE00, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ("key", 0x09, 0x0F, 0),
        ("key", 0x09, 0x0F, KEYEVENTF_KEYUP),
        ("key", 0, ord("x"), KEYEVENTF_UNICODE),
        ("key", 0, ord("x"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]
    api.inputs.clear()
    be.type_text("")
    assert api.inputs == []


def test_type_text_long_is_chunked(be, api):
    be.type_text("x" * 1000)
    assert len(keys_of(api)) == 2000


# ================================================================== backend: screen
def test_monitors_primary_first_with_dpi(be, api):
    mons = be.monitors()
    assert [m.index for m in mons] == [0, 1]
    assert mons[0].is_primary and mons[0].rect == Rect(0, -200, 2560, 1440) and mons[0].scale == 1.5
    assert not mons[1].is_primary and mons[1].rect == Rect(-1920, 0, 1920, 1080) and mons[1].scale == 1.0
    assert mons[0].name.endswith("DISPLAY1")
    assert be.virtual_screen() == api.virtual


def test_monitor_scale_falls_back_to_device_caps(be, api):
    api.GetDpiForMonitor = None  # shcore missing (Windows 7)
    assert {m.scale for m in be.monitors()} == {1.25}


def test_screenshot_region_padding(be, api, monkeypatch):
    grabbed = []

    def fake_grab(r):
        grabbed.append(r)
        return Image.new("RGB", (r.width, r.height), (10, 20, 30))

    monkeypatch.setattr(be, "_grab", fake_grab)
    img = be.screenshot(Rect(-1930, -210, 20, 20))  # partially outside the virtual desktop
    assert img.size == (20, 20) and img.mode == "RGB"
    assert grabbed[-1] == Rect(-1920, -200, 10, 10)
    assert img.getpixel((0, 0)) == (0, 0, 0) and img.getpixel((15, 15)) == (10, 20, 30)
    full = be.screenshot()
    assert full.size == (api.virtual.width, api.virtual.height)
    off = be.screenshot(Rect(-99999, 0, 5, 5))
    assert off.getpixel((2, 2)) == (0, 0, 0)
    with pytest.raises(ValueError):
        be.screenshot(Rect(0, 0, 0, 5))


def test_screenshot_falls_back_to_gdi(be, monkeypatch):
    calls = []

    def bad_mss(r):
        raise RuntimeError("no mss")

    def gdi(r):
        calls.append(r)
        return Image.new("RGB", (r.width, r.height))

    monkeypatch.setattr(be, "_grab_mss", bad_mss)
    monkeypatch.setattr(be, "_grab_gdi", gdi)
    be.screenshot(Rect(0, 0, 4, 4))
    be.screenshot(Rect(0, 0, 4, 4))
    assert len(calls) == 2 and be._mss_broken


# ================================================================== backend: windows
def _desktop(api: FakeApi):
    api.add(FakeWin(0x100, "Untitled - Notepad", Rect(100, 100, 800, 600), pid=100))
    api.add(FakeWin(0x200, "", Rect(0, 0, 50, 50), pid=200, exe="C:\\x\\shell.exe"))  # untitled
    api.add(FakeWin(0x300, "Tool", Rect(0, 0, 50, 50), pid=300, exstyle=winmod.WS_EX_TOOLWINDOW))
    api.add(FakeWin(0x400, "App Tool", Rect(5, 5, 300, 200), pid=400,
                    exstyle=winmod.WS_EX_TOOLWINDOW | winmod.WS_EX_APPWINDOW, exe="C:\\Apps\\app.exe"))
    api.add(FakeWin(0x500, "Cloaked UWP", Rect(0, 0, 500, 500), pid=500, cloaked=2))
    api.add(FakeWin(0x600, "Hidden", Rect(0, 0, 500, 500), pid=600, visible=False))
    api.add(FakeWin(0x700, "Book1 - Excel", Rect(-1900, 10, 1200, 900), pid=700, zoomed=True,
                    exe="C:\\Program Files\\Microsoft Office\\EXCEL.EXE", cls="XLMAIN"))
    api.add(FakeWin(0x800, "Minimized", Rect(-32000, -32000, 160, 28), pid=100, iconic=True))
    api.fg = 0x100


def test_list_windows_filters_and_orders(be, api):
    _desktop(api)
    wins = be.list_windows()
    assert [w.handle for w in wins] == [0x100, 0x400, 0x700, 0x800]
    np_ = wins[0]
    assert np_.title == "Untitled - Notepad" and np_.app == "notepad.exe" and np_.pid == 100
    assert np_.is_active and not np_.is_minimized and np_.class_name == "Notepad"
    assert np_.rect == Rect(100, 100, 800, 600)  # DWM frame, not the outer rect with borders
    xl = wins[2]
    assert xl.app == "EXCEL.EXE" and xl.is_maximized and not xl.is_active and xl.class_name == "XLMAIN"
    assert wins[3].is_minimized
    # process handles were closed and names cached per pid (0x800 shares notepad's pid)
    opened = [c for c in api.calls if c[0] == "CloseHandle"]
    assert len(opened) == 3
    assert be.active_window().handle == 0x100


def test_window_rect_without_dwm(be, api):
    _desktop(api)
    api.DwmGetWindowAttribute = None  # dwmapi missing
    info = be.window_info(0x100)
    assert info.rect == Rect(93, 100, 814, 607)  # outer rect incl. invisible borders


def test_active_window_none(be, api):
    api.fg = None
    assert be.active_window() is None


def test_invalid_handles_raise_window_not_found(be, api):
    _desktop(api)
    for bad in (0, 0x999, "zzz", None):
        with pytest.raises(WindowNotFound):
            be.activate_window(bad)
    with pytest.raises(WindowNotFound):
        be.close_window(0x999)


def test_activate_simple_and_restores_minimized(be, api):
    _desktop(api)
    be.activate_window(0x800)
    assert ("ShowWindow", 0x800, winmod.SW_RESTORE) in api.calls
    assert api.fg == 0x800
    api.calls.clear()
    be.activate_window(0x800)  # already foreground: no-op
    assert not [c for c in api.calls if c[0] == "SetForegroundWindow"]


def test_activate_accepts_windowinfo(be, api):
    _desktop(api)
    info = [w for w in be.list_windows() if w.handle == 0x700][0]
    be.activate_window(info)
    assert api.fg == 0x700


def test_activate_uses_attach_thread_input(be, api):
    _desktop(api)
    api.fg_policy = "attach"
    be.activate_window(0x700)
    assert api.fg == 0x700
    attach = [c for c in api.calls if c[0] == "AttachThreadInput"]
    assert ("AttachThreadInput", 4242, 1000, True) in attach  # foreground (notepad) thread
    assert ("AttachThreadInput", 4242, 7000, True) in attach  # target thread
    assert ("AttachThreadInput", 4242, 1000, False) in attach
    assert ("AttachThreadInput", 4242, 7000, False) in attach
    assert not api.attached


def test_activate_uses_alt_tap(be, api):
    _desktop(api)
    api.fg_policy = "alt"
    be.activate_window(0x400)
    assert api.fg == 0x400
    ks = keys_of(api)
    assert [(k[1], bool(k[3] & KEYEVENTF_KEYUP)) for k in ks] == [(winmod.VK_MENU, False), (winmod.VK_MENU, True)]
    assert not api.alt_down


def test_activate_refused_raises(be, api):
    _desktop(api)
    api.fg_policy = "never"
    with pytest.raises(BackendError, match="foreground"):
        be.activate_window(0x400)
    assert not api.alt_down  # ALT is never left stuck


def test_move_window_compensates_invisible_borders(be, api):
    _desktop(api)
    be.move_window(0x100, Rect(10, 20, 640, 480))
    mv = [c for c in api.calls if c[0] == "MoveWindow"][-1]
    assert mv == ("MoveWindow", 0x100, 3, 20, 654, 487, True)
    assert be.window_info(0x100).rect == Rect(10, 20, 640, 480)
    # maximized windows are restored first
    be.move_window(0x700, (0, 0, 300, 200))
    assert ("ShowWindow", 0x700, winmod.SW_RESTORE) in api.calls
    with pytest.raises(ValueError):
        be.move_window(0x100, Rect(0, 0, 0, 10))


def test_set_window_state_and_close(be, api):
    _desktop(api)
    be.set_window_state(0x100, "minimized")
    assert api.wins[0x100].iconic
    be.set_window_state(0x100, "normal")
    assert not api.wins[0x100].iconic
    be.set_window_state(0x100, "Maximized")
    assert api.wins[0x100].zoomed
    with pytest.raises(ValueError):
        be.set_window_state(0x100, "sideways")
    be.close_window(0x100)
    assert ("PostMessageW", 0x100, winmod.WM_CLOSE, 0, 0) in api.calls


# ================================================================== backend: clipboard
def test_clipboard_round_trip(be, api):
    assert be.get_clipboard() == ""
    be.set_clipboard("héllo\nwörld 😀")
    assert api.clip_data == ("héllo\nwörld 😀\x00").encode("utf-16-le")
    assert be.get_clipboard() == "héllo\nwörld 😀"
    assert not api.clip_open
    assert ("OpenClipboard", 0x777) in api.calls and ("DestroyWindow", 0x777) in api.calls
    assert api.freed == []  # the system owns the memory after SetClipboardData


def test_clipboard_retries_on_contention(be, api):
    api.clip_busy = 3
    be.set_clipboard("x")
    assert be.get_clipboard() == "x"
    assert len([c for c in api.calls if c[0] == "OpenClipboard"]) == 5


def test_clipboard_gives_up_and_frees_memory(api):
    sleeps = []
    be = WindowsBackend(_api=api, dpi_aware=False, clipboard_retries=4, clipboard_retry_delay=0.5,
                        sleep=sleeps.append)
    api.clip_busy = 100
    with pytest.raises(BackendError, match="OpenClipboard"):
        be.set_clipboard("x")
    assert sleeps == [0.5, 0.5, 0.5]
    assert len(api.freed) == 1
    with pytest.raises(BackendError):
        be.get_clipboard()


def test_clipboard_stops_at_nul(be, api):
    api.clip_data = "abc\x00garbage".encode("utf-16-le")
    assert be.get_clipboard() == "abc"


# ================================================================== DPI awareness
class DpiApi:
    def __init__(self, ctx_results=(), shcore=None, legacy=None, thread=None, err=0):
        self.ctx_results = list(ctx_results or ())
        self.calls = []
        self.err = err
        if ctx_results is not None:
            self.SetProcessDpiAwarenessContext = self._ctx
        else:
            self.SetProcessDpiAwarenessContext = None
        self.SetProcessDpiAwareness = (lambda v: self._log("shcore", v, shcore)) if shcore is not None else None
        self.SetProcessDPIAware = (lambda: self._log("legacy", None, legacy)) if legacy is not None else None
        self.SetThreadDpiAwarenessContext = (lambda c: self._log("thread", c.value, thread)) if thread is not None else None

    def _log(self, what, arg, result):
        self.calls.append((what, arg))
        return result

    def _ctx(self, c):
        self.calls.append(("ctx", ctypes.c_ssize_t(c.value).value))
        return self.ctx_results.pop(0) if self.ctx_results else 0

    def get_last_error(self):
        return self.err


def test_dpi_awareness_prefers_per_monitor_v2():
    a = DpiApi(ctx_results=[1])
    assert enable_dpi_awareness(a) == "per_monitor_v2"
    assert a.calls == [("ctx", -4)]


def test_dpi_awareness_fallbacks():
    a = DpiApi(ctx_results=[0, 1])
    assert enable_dpi_awareness(a) == "per_monitor"
    assert a.calls == [("ctx", -4), ("ctx", -3)]
    a = DpiApi(ctx_results=None, shcore=0)
    assert enable_dpi_awareness(a) == "per_monitor"
    a = DpiApi(ctx_results=None, shcore=-1, legacy=1)
    assert enable_dpi_awareness(a) == "system"
    a = DpiApi(ctx_results=None, shcore=None, legacy=0)
    assert enable_dpi_awareness(a) == "unaware"


def test_dpi_awareness_already_set():
    a = DpiApi(ctx_results=[0], err=winmod.ERROR_ACCESS_DENIED, shcore=0, thread=0x12)
    assert enable_dpi_awareness(a) == "thread_per_monitor_v2"
    assert ("shcore", 2) not in a.calls
    a = DpiApi(ctx_results=None, shcore=winmod.E_ACCESSDENIED, legacy=1)
    assert enable_dpi_awareness(a) == "preset"


# ================================================================== UI Automation
class FakeRect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class Pattern:
    def __init__(self, **attrs):
        self.calls = []
        self.__dict__.update(attrs)

    def __getattr__(self, name):
        if name.startswith("Current") or name == "DocumentRange":
            raise AttributeError(name)

        def method(*args):
            self.calls.append((name, args))
            if name in self.__dict__.get("fail", ()):
                raise OSError("COMError")
        return method


class FakeElement:
    def __init__(self, ct, name="", *, rect=(0, 0, 10, 10), aid="", cls="", enabled=True, focus=False,
                 focusable=True, offscreen=False, pid=321, hwnd=0, rid=None, patterns=None, props=None,
                 children=None, stale=False):
        self.CurrentControlType = ct
        self.CurrentName = name
        self.CurrentBoundingRectangle = FakeRect(*rect) if rect else FakeRect(0, 0, 0, 0)
        self.CurrentAutomationId = aid
        self.CurrentClassName = cls
        self.CurrentIsEnabled = enabled
        self.CurrentHasKeyboardFocus = focus
        self.CurrentIsKeyboardFocusable = focusable
        self.CurrentIsOffscreen = offscreen
        self.CurrentProcessId = pid
        self.CurrentNativeWindowHandle = hwnd
        self.CurrentFrameworkId = "Win32"
        self.CurrentLocalizedControlType = "button"
        self.CurrentHelpText = ""
        self.CurrentIsPassword = False
        self._rid = rid
        self.patterns = patterns or {}
        self.props = props or {}
        self.kids = children or []
        self.parent = None
        self.focused_calls = 0
        for k in self.kids:
            k.parent = self
        self._stale = stale

    def __getattribute__(self, name):
        if name.startswith("Current") and object.__getattribute__(self, "_stale"):
            raise OSError("UIA_E_ELEMENTNOTAVAILABLE")
        return object.__getattribute__(self, name)

    def GetRuntimeId(self):
        if self._rid is None:
            raise OSError("no runtime id")
        return self._rid

    def SetFocus(self):
        self.focused_calls += 1


class FakeCtx:
    def __init__(self, root=None):
        self.root = root

    def pattern(self, el, pid):
        return el.patterns.get(pid)

    def property_value(self, el, pid):
        return el.props.get(pid)

    def compare(self, a, b):
        return a is b

    def is_root(self, el):
        return el is self.root

    def first_child(self, el):
        return el.kids[0] if el.kids else None

    def next_sibling(self, el):
        p = el.parent
        if p is None:
            return None
        i = p.kids.index(el)
        return p.kids[i + 1] if i + 1 < len(p.kids) else None

    def parent_of(self, el):
        return el.parent


def test_uianode_basic_properties():
    el = FakeElement(50000, "OK", rect=(10, 20, 90, 50), aid="okBtn", cls="Button", hwnd=0x1234, rid=(42, 7, 1),
                     patterns={winmod.UIA_InvokePatternId: Pattern()})
    n = UIANode(el, FakeCtx())
    assert n.role == "button" and n.native_role == "Button"
    assert n.name == "OK" and n.automation_id == "okBtn" and n.class_name == "Button"
    assert n.bounds == Rect(10, 20, 80, 30)
    assert n.pid == 321 and n.handle == 0x1234
    assert {"enabled", "focusable", "visible"} <= n.states
    assert n.is_enabled and n.is_visible
    assert n.value is None
    attrs = n.attributes()
    assert attrs["runtime_id"] == "42.7.1" and attrs["control_type"] == 50000 and attrs["framework"] == "Win32"
    assert "invoke" in n.actions() and "focus" in n.actions()
    assert n.invoke() and el.patterns[winmod.UIA_InvokePatternId].calls == [("Invoke", ())]
    assert n.focus() and el.focused_calls == 1
    assert "OK" in repr(n)


def test_uianode_toggle_button_and_checkbox_states():
    tp = Pattern(CurrentToggleState=winmod.ToggleState_On)
    btn = UIANode(FakeElement(50000, "Bold", patterns={winmod.UIA_TogglePatternId: tp}), FakeCtx())
    assert btn.role == "togglebutton"
    assert "checked" in btn.states
    assert btn.toggle() and tp.calls == [("Toggle", ())]
    # invoke falls back to Toggle when there is no Invoke pattern
    assert btn.invoke() and len(tp.calls) == 2
    mixed = UIANode(FakeElement(50002, "x", patterns={
        winmod.UIA_TogglePatternId: Pattern(CurrentToggleState=winmod.ToggleState_Indeterminate)}), FakeCtx())
    assert mixed.role == "checkbox" and "mixed" in mixed.states and "checked" not in mixed.states


def test_uianode_dialog_and_desktop_roles():
    dlg = UIANode(FakeElement(50032, "Save As", cls="#32770"), FakeCtx(), top_level=True, handle=0x10)
    assert dlg.role == "dialog" and dlg.handle == 0x10 and dlg.parent is None
    dlg2 = UIANode(FakeElement(50032, "x", props={winmod.UIA_IsDialogPropertyId: True}), FakeCtx())
    assert dlg2.role == "dialog"
    win = UIANode(FakeElement(50032, "Main"), FakeCtx())
    assert win.role == "window"
    root_el = FakeElement(50033, "Desktop 1")
    assert UIANode(root_el, FakeCtx(root_el)).role == "desktop"
    assert UIANode(FakeElement(50033, "pane"), FakeCtx(root_el)).role == "pane"


def test_uianode_value_patterns_and_set_value():
    vp = Pattern(CurrentValue="hello", CurrentIsReadOnly=False)
    edit = UIANode(FakeElement(50004, "Name", patterns={winmod.UIA_ValuePatternId: vp}), FakeCtx())
    assert edit.value == "hello"
    assert "editable" in edit.states and "set_value" in edit.actions()
    assert edit.set_value("world") and vp.calls == [("SetValue", ("world",))]

    ro = Pattern(CurrentValue="x", CurrentIsReadOnly=True)
    ro_node = UIANode(FakeElement(50004, "RO", patterns={winmod.UIA_ValuePatternId: ro}), FakeCtx())
    assert "readonly" in ro_node.states and "editable" not in ro_node.states
    assert not ro_node.set_value("y") and ro.calls == []

    rv = Pattern(CurrentValue=40.0, CurrentIsReadOnly=False)
    slider = UIANode(FakeElement(50015, "Volume", patterns={winmod.UIA_RangeValuePatternId: rv}), FakeCtx())
    assert slider.value == "40"
    assert slider.set_value("55.5") and rv.calls == [("SetValue", (55.5,))]
    assert not slider.set_value("loud")

    class Range:
        def GetText(self, n):
            assert n > 0
            return "document body"

    doc = UIANode(FakeElement(50030, "Doc", patterns={winmod.UIA_TextPatternId: Pattern(DocumentRange=Range())}),
                  FakeCtx())
    assert doc.value == "document body"

    legacy = Pattern(CurrentValue="lv", CurrentDefaultAction="Press", CurrentDescription="d")
    lnode = UIANode(FakeElement(50025, "c", patterns={winmod.UIA_LegacyIAccessiblePatternId: legacy}), FakeCtx())
    assert lnode.value == "lv" and lnode.description == "d"
    assert "invoke" in lnode.actions()
    assert lnode.invoke() and ("DoDefaultAction", ()) in legacy.calls
    assert lnode.set_value("new") and ("SetValue", ("new",)) in legacy.calls


def test_uianode_expand_select_scroll():
    ep = Pattern(CurrentExpandCollapseState=winmod.ExpandCollapseState_Collapsed)
    sp = Pattern(CurrentIsSelected=True)
    si = Pattern()
    item = UIANode(FakeElement(50024, "Node", patterns={
        winmod.UIA_ExpandCollapsePatternId: ep, winmod.UIA_SelectionItemPatternId: sp,
        winmod.UIA_ScrollItemPatternId: si}), FakeCtx())
    assert {"collapsed", "selected", "selectable"} <= item.states
    assert {"expand", "collapse", "select", "scroll_into_view"} <= set(item.actions())
    assert item.expand() and item.collapse() and item.select() and item.scroll_into_view()
    assert [c[0] for c in ep.calls] == ["Expand", "Collapse"]
    assert sp.calls == [("Select", ())] and si.calls == [("ScrollIntoView", ())]
    # invoke without Invoke/Toggle -> SelectionItem.Select
    assert item.invoke() and len(sp.calls) == 2
    only_expand = UIANode(FakeElement(50003, "Combo", patterns={winmod.UIA_ExpandCollapsePatternId: ep}), FakeCtx())
    assert only_expand.invoke() and ep.calls[-1][0] == "Expand"
    bare = UIANode(FakeElement(50020, "label"), FakeCtx())
    assert not any([bare.invoke(), bare.set_value("x"), bare.toggle(), bare.expand(), bare.collapse(),
                    bare.select(), bare.scroll_into_view()])


def test_uianode_pattern_failures_return_false():
    ip = Pattern(fail=("Invoke",))
    n = UIANode(FakeElement(50000, "Broken", patterns={winmod.UIA_InvokePatternId: ip}), FakeCtx())
    assert n.invoke() is False


def test_uianode_children_parent_and_walk():
    leaf1 = FakeElement(50000, "A", rid=(1,))
    leaf2 = FakeElement(50004, "B", rid=(2,))
    group = FakeElement(50026, "G", children=[leaf1, leaf2], rid=(3,))
    win = FakeElement(50032, "Win", children=[group], rid=(4,))
    root = FakeElement(50033, "Desktop", children=[win])
    ctx = FakeCtx(root)
    top = UIANode(win, ctx, top_level=True, handle=0x55)
    kids = top.children()
    assert [k.name for k in kids] == ["G"]
    assert [k.name for k in kids[0].children()] == ["A", "B"]
    assert kids[0].children()[1].parent.name == "G"
    assert [n.name for n in top.walk()] == ["Win", "G", "A", "B"]
    # parent resolved lazily through the walker; the desktop root maps to None
    orphan = UIANode(leaf1, ctx)
    assert orphan.parent.name == "G" and orphan.parent.parent.name == "Win"
    assert orphan.parent.parent.parent is None
    capped = UIANode(group, ctx, max_children=1)
    assert len(capped.children()) == 1


def test_uianode_same_as_uses_runtime_id():
    a = UIANode(FakeElement(50000, "x", rid=(1, 2, 3)), FakeCtx())
    b = UIANode(FakeElement(50000, "x", rid=(1, 2, 3)), FakeCtx())
    c = UIANode(FakeElement(50000, "x", rid=(9,)), FakeCtx())
    assert a.same_as(b) and not a.same_as(c) and a.same_as(a)
    el = FakeElement(50000, "x")
    assert UIANode(el, FakeCtx()).same_as(UIANode(el, FakeCtx()))  # compare fallback
    assert not UIANode(FakeElement(50000, "x"), FakeCtx()).same_as(UIANode(FakeElement(50000, "x"), FakeCtx()))
    assert not a.same_as(object())  # type: ignore[arg-type]


def test_uianode_stale_element_never_raises():
    n = UIANode(FakeElement(50000, "gone", stale=True), FakeCtx())
    assert n.name == "" and n.role == "unknown" and n.bounds is None and n.pid is None and n.handle is None
    assert n.states == frozenset() and n.automation_id == "" and n.runtime_id == ()
    assert n.attributes()["control_type"] == 0
    empty = UIANode(FakeElement(50000, "zero", rect=None), FakeCtx())
    assert empty.bounds is None and not empty.is_visible


def test_backend_accessibility_via_uia_context(be, api):
    _desktop(api)
    elements = {h: FakeElement(50032, api.wins[h].title, rid=(h,)) for h in api.wins}

    class Ctx(FakeCtx):
        def element_from_handle(self, hwnd):
            return elements.get(hwnd)

        def element_from_point(self, x, y):
            return FakeElement(50000, f"at {x},{y}")

        def focused(self):
            return FakeElement(50004, "focused edit", focus=True)

    be._uia_ctx = Ctx()
    assert CAP_ACCESSIBILITY in be.capabilities
    roots = be.accessibility_roots()
    assert [r.handle for r in roots] == [0x100, 0x400, 0x700, 0x800]
    assert roots[0].name == "Untitled - Notepad" and roots[0].parent is None
    assert be.element_at(5, 6).name == "at 5,6"
    f = be.focused_element()
    assert f.name == "focused edit" and "focused" in f.states


def test_backend_accessibility_disabled(api):
    be = WindowsBackend(_api=api, dpi_aware=False, accessibility=False)
    assert CAP_ACCESSIBILITY not in be.capabilities
    with pytest.raises(NotSupported):
        be.accessibility_roots()


@pytest.mark.skipif(sys.platform == "win32", reason="comtypes import path differs on Windows")
def test_uia_unavailable_reports_not_supported(be, monkeypatch):
    monkeypatch.setitem(sys.modules, "comtypes", None)
    monkeypatch.setattr(winmod, "_uia_module", None)
    with pytest.raises(NotSupported, match="comtypes"):
        be.accessibility_roots()
    # the failure is remembered
    assert CAP_ACCESSIBILITY not in be.capabilities
    with pytest.raises(NotSupported):
        be.element_at(0, 0)


def test_uia_nodes_expose_app_for_selectors(be, api):
    """[app=...] / <wnd app=...> selectors must work on UIA trees (regression)."""
    from pypilot.selectors import parse

    _desktop(api)
    edit = FakeElement(50004, "Text Editor", pid=100, hwnd=0)  # WPF-like: no own HWND
    elements = {h: FakeElement(50032, api.wins[h].title, rid=(h,), pid=api.wins[h].pid) for h in api.wins}
    elements[0x100] = FakeElement(50032, "Untitled - Notepad", rid=(0x100,), pid=100, children=[edit])

    class Ctx(FakeCtx):
        def element_from_handle(self, hwnd):
            return elements.get(hwnd)

    be._uia_ctx = Ctx()
    roots = be.accessibility_roots()
    assert roots[0].attributes()["app"] == "notepad.exe"
    assert roots[0].children()[0].attributes()["app"] == "notepad.exe"
    assert [r.name for r in parse("[app=notepad.exe]").find_all(roots)][:2] == ["Untitled - Notepad", "Text Editor"]
    assert [r.name for r in parse('window[app="notepad.exe"]').find_all(roots)] == ["Untitled - Notepad", "Minimized"]
    assert [r.name for r in parse("<wnd app='EXCEL.EXE' />").find_all(roots)] == ["Book1 - Excel"]
    assert parse("window[app=chrome.exe]").find_all(roots) == []
    # process names are looked up once per pid
    opened = [c for c in api.calls if c[0] == "CloseHandle"]
    roots[0].attributes(), roots[0].attributes()
    assert [c for c in api.calls if c[0] == "CloseHandle"] == opened


def test_char_keys_honour_capslock(be, api):
    """With CapsLock on, Shift gives the other case: key_down("A") must not add Shift
    and key_down("a") must, so press()/keys-mode typing is not case-inverted."""
    api.GetKeyState = lambda vk: 1 if vk == 0x14 else 0  # VK_CAPITAL toggled
    be.key_down("A")
    be.key_up("A")
    assert [(k[1], bool(k[3] & KEYEVENTF_KEYUP)) for k in keys_of(api)] == [(0x41, False), (0x41, True)]
    api.inputs.clear()
    be.key_down("a")
    be.key_up("a")
    assert [(k[1], bool(k[3] & KEYEVENTF_KEYUP)) for k in keys_of(api)] == [
        (0xA0, False), (0x41, False), (0x41, True), (0xA0, True),
    ]
    api.inputs.clear()
    # shortcuts keep the key's own shift state (ctrl+a, not ctrl+shift+a)
    be.key_down("ctrl")
    be.key_down("a")
    be.key_up("a")
    be.key_up("ctrl")
    assert [(k[1], bool(k[3] & KEYEVENTF_KEYUP)) for k in keys_of(api)] == [
        (0xA2, False), (0x41, False), (0x41, True), (0xA2, True),
    ]
    api.inputs.clear()
    be.key_down("!")  # not a letter: CapsLock does not apply
    be.key_up("!")
    assert [k[1] for k in keys_of(api)] == [0xA0, 0x31, 0x31, 0xA0]


def test_keyboard_layout_chars_reach_backend_unchanged(be, api):
    """Keyboard.press/type(mode="keys") must not rewrite "@" into US "shift+2": on a
    German layout "@" is AltGr+Q and Shift+2 types '"' (regression)."""
    from pypilot.keyboard import Keyboard

    api.layout_table = {"@": 0x651, '"': 0x132, "2": 0x32, "q": 0x51, "Q": 0x151, "#": 0xBF}
    altgr_q = [(0xA2, False), (0xA4, False), (0x51, False), (0x51, True), (0xA4, True), (0xA2, True)]
    kb = Keyboard(be, sleep=lambda s: None)
    for action in (lambda: kb.press("@"), lambda: kb.type("@", mode="keys")):
        api.inputs.clear()
        action()
        assert [(k[1], bool(k[3] & KEYEVENTF_KEYUP)) for k in keys_of(api)] == altgr_q
    api.inputs.clear()
    kb.type("#", mode="keys")  # unshifted VK_OEM_2 on de-DE, not US Shift+3
    assert [(k[1], bool(k[3] & KEYEVENTF_KEYUP)) for k in keys_of(api)] == [(0xBF, False), (0xBF, True)]
    assert not kb.pressed and not be._held


def test_dead_key_characters_fall_back_to_unicode(be, api):
    """'^' is a dead key on German/French layouts: pressing its VK would start a
    composition instead of typing it, so it is sent as a Unicode packet."""
    api.layout_table["^"] = 0xDC  # VK_OEM_5, dead on de-DE
    api.dead_vks = {0xDC}
    be.key_down("^")
    be.key_up("^")
    assert keys_of(api) == [("key", 0, ord("^"), KEYEVENTF_UNICODE),
                            ("key", 0, ord("^"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    api.inputs.clear()
    be.key_down("a")  # live keys are unaffected
    be.key_up("a")
    assert [k[1] for k in keys_of(api)] == [0x41, 0x41]
    api.inputs.clear()
    # in a shortcut the key itself is wanted (ctrl+^), not a Unicode packet
    be.key_down("ctrl")
    be.key_down("^")
    be.key_up("^")
    be.key_up("ctrl")
    assert [k[1] for k in keys_of(api)] == [0xA2, 0xDC, 0xDC, 0xA2]


def test_popup_menus_are_accessibility_roots(be, api):
    """Context menus / menu-bar drop-downs are untitled tool windows that UIA parents
    to the desktop: they must still be searchable roots (front-most first)."""
    from pypilot.desktop import Desktop

    excel = api.add(FakeWin(0x100, "Book1 - Excel", Rect(0, 0, 1200, 800), pid=50, exe="C:\\Office\\EXCEL.EXE",
                            cls="XLMAIN"))
    popup_style = winmod.WS_POPUP
    tool = winmod.WS_EX_TOOLWINDOW | 0x101
    api.add(FakeWin(0x300, "", Rect(300, 300, 200, 250), pid=50, cls="#32768", exstyle=tool, style=popup_style,
                    exe=excel.exe), front=True)
    api.add(FakeWin(0x310, "", Rect(0, 0, 10, 10), pid=50, cls="#32768", exstyle=tool, visible=False))  # closed
    api.add(FakeWin(0x320, "", Rect(0, 0, 90, 20), pid=50, cls="tooltips_class32", exstyle=tool,
                    style=popup_style), front=True)
    api.add(FakeWin(0x330, "", Rect(500, 100, 150, 90), pid=60, cls="WindowsForms10.Window.808.app.0",
                    exstyle=tool, style=popup_style, exe="C:\\Apps\\erp.exe"))
    api.fg = 0x100
    copy = FakeElement(50011, "Copy", rect=(310, 310, 490, 330), pid=50)
    post = FakeElement(50011, "Post", rect=(510, 110, 640, 130), pid=60)
    elements = {
        0x100: FakeElement(50032, "Book1 - Excel", rid=(1,), pid=50, hwnd=0x100),
        0x300: FakeElement(50009, "Context", rect=(300, 300, 500, 550), pid=50, hwnd=0x300, children=[copy]),
        0x310: FakeElement(50009, "Hidden", pid=50, hwnd=0x310),
        0x320: FakeElement(50022, "Tip", pid=50, hwnd=0x320),
        0x330: FakeElement(50009, "Grid menu", pid=60, hwnd=0x330, children=[post]),
    }

    class Ctx(FakeCtx):
        def element_from_handle(self, hwnd):
            return elements.get(hwnd)

    be._uia_ctx = Ctx()
    roots = be.accessibility_roots()
    assert [(r.name, r.handle) for r in roots] == [("Context", 0x300), ("Book1 - Excel", 0x100),
                                                   ("Grid menu", 0x330)]
    assert [w.handle for w in be.list_windows()] == [0x100]  # window listing is unchanged
    d = Desktop(backend=be)
    assert d.find('menuitem[name="Copy"]', timeout=0).name == "Copy"
    assert d.find("menuitem[name=Post]", timeout=0).name == "Post"


def test_app_names_follow_pid_reuse(be, api):
    """Windows recycles PIDs: [app=...] must name the process that owns the PID now,
    as list_windows() does, not a cached exited one (regression)."""
    from pypilot.selectors import parse

    def setup(title, exe):
        api.wins.clear()
        api.z.clear()
        api.add(FakeWin(0x100, title, Rect(0, 0, 800, 600), pid=4321, exe=exe))
        elements = {0x100: FakeElement(50032, title, rid=(0x100,), pid=4321)}

        class Ctx(FakeCtx):
            def element_from_handle(self, hwnd):
                return elements.get(hwnd)

        be._uia_ctx = Ctx()

    setup("Payments - SAP", "C:\\SAP\\saplogon.exe")
    assert [r.name for r in parse("window[app=saplogon.exe]").find_all(be.accessibility_roots())] == ["Payments - SAP"]
    setup("Untitled - Notepad", "C:\\Windows\\notepad.exe")  # SAP exited; its PID was reused
    roots = be.accessibility_roots()
    assert parse("window[app=saplogon.exe]").find_all(roots) == []
    assert [r.name for r in parse("window[app=notepad.exe]").find_all(roots)] == ["Untitled - Notepad"]


def test_set_clipboard_is_excluded_from_clipboard_history(be, api):
    """Pasted passwords / bank data must not be kept in Win+V history or synced to
    the cloud clipboard: the documented exclusion formats are set with the text."""
    be.set_clipboard("P@ssw0rd!")
    assert be.get_clipboard() == "P@ssw0rd!"
    names = winmod.CLIPBOARD_EXCLUSION_FORMATS
    assert set(api.registered) == set(names)
    assert api.clip_extra == {api.registered[n]: b"\x00\x00\x00\x00" for n in names}
    assert api.freed == []
    be.set_clipboard("again")  # format ids are registered once
    assert len(api.registered) == 3 and len(api.clip_extra) == 3
    # opt-out keeps the classic behaviour
    keep = WindowsBackend(_api=api, dpi_aware=False, clipboard_history=True, sleep=lambda s: None)
    keep.set_clipboard("report total 1,234.00")
    assert api.clip_extra == {} and keep.get_clipboard() == "report total 1,234.00"


def test_restore_un_maximizes_a_window_minimized_while_maximized(be, api):
    """SW_RESTORE brings a window minimized from maximized back *maximized*
    (WPF_RESTORETOMAXIMIZED); 'normal' and move_window must end un-maximized."""
    _desktop(api)
    xl = api.wins[0x700]
    be.set_window_state(0x700, "minimized")
    assert xl.iconic and not xl.zoomed
    be.set_window_state(0x700, "normal")
    assert not xl.iconic and not xl.zoomed
    be.set_window_state(0x700, "maximized")
    be.set_window_state(0x700, "minimized")
    be.move_window(0x700, Rect(10, 20, 300, 200))
    assert not xl.iconic and not xl.zoomed
    assert be.window_info(0x700).rect == Rect(10, 20, 300, 200)
    # activation keeps the user's maximized layout
    be.set_window_state(0x700, "maximized")
    be.set_window_state(0x700, "minimized")
    be.activate_window(0x700)
    assert not xl.iconic and xl.zoomed


def test_failed_release_stays_tracked_for_retry(be, api):
    """A key/button whose release Windows rejected (UAC secure desktop, locked
    workstation) is still down: it must stay tracked so release_all() retries it."""
    be.key_down("ctrl")
    be.key_down("a")
    be.mouse_down("left")
    api.send_limit = 0
    with pytest.raises(BackendError):
        be.key_up("ctrl")
    with pytest.raises(BackendError):
        be.mouse_up("left")
    with pytest.raises(BackendError):
        be.release_all()
    assert list(be._held) == ["ctrl", "a"] and list(be._buttons_down) == [MouseButton.LEFT]
    api.send_limit, api.last_error = None, 0  # input possible again
    api.inputs.clear()
    be.release_all()
    assert [(e[1], e[3]) if e[0] == "key" else e[4] for e in api.inputs] == [
        (0x41, KEYEVENTF_KEYUP), (0xA2, KEYEVENTF_KEYUP), MOUSEEVENTF_LEFTUP,
    ]
    assert not be._held and not be._buttons_down
    # Keyboard keeps its own record too, so its release_all() can retry
    from pypilot.keyboard import Keyboard

    kb = Keyboard(be, sleep=lambda s: None)
    kb.down("shift")
    api.send_limit = 0
    with pytest.raises(BackendError):
        kb.up("shift")
    assert kb.pressed == ["shift"] and "shift" in be._held
    api.send_limit, api.last_error = None, 0
    assert kb.release_all() == ["shift"] and not be._held


def test_type_text_is_paced_per_character_keeping_surrogate_pairs(api):
    """Windows 11 Notepad garbles long KEYEVENTF_UNICODE bursts (seen on a real
    machine: 'Hello pypilot \\ude80\\ude80...'). Each character must be its own
    SendInput call -- a surrogate pair together -- with a pause in between."""
    sleeps = []
    nb = WindowsBackend(_api=api, dpi_aware=False, sleep=sleeps.append, type_char_delay=0.01)
    calls = []
    orig = api.SendInput
    api.SendInput = lambda n, arr, size: (calls.append(n), orig(n, arr, size))[1]
    nb.type_text("a😀\nb")
    ks = keys_of(api)
    assert [k[2] for k in ks if k[3] & KEYEVENTF_UNICODE and not k[3] & KEYEVENTF_KEYUP] == [ord("a"), 0xD83D, 0xDE00, ord("b")]
    assert sleeps == [0.01, 0.01, 0.01]  # 4 groups: 'a', the emoji pair, Enter, 'b'
    assert calls == [2, 4, 2, 2]  # one SendInput per character; the emoji's two UTF-16 units together
    with pytest.raises(ValueError):
        WindowsBackend(_api=api, dpi_aware=False, type_char_delay=-1)


def test_first_key_after_a_click_waits_for_the_app_to_handle_it(api):
    """Windows 11 Notepad, clicked on just-typed text, lost the Ctrl of an
    immediate Ctrl+A and typed 'a' (seen on a real machine). The first keyboard
    event after a button release waits ``click_key_settle``; later keys do not."""
    sleeps = []
    nb = WindowsBackend(_api=api, dpi_aware=False, sleep=sleeps.append, click_key_settle=0.1, type_char_delay=0)
    nb.key_down("ctrl")
    nb.key_up("ctrl")
    assert sleeps == []  # no click yet: no settle
    nb.mouse_down()
    nb.mouse_up()
    nb.key_down("ctrl")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 0.1
    nb.key_down("a")
    nb.key_up("a")
    nb.key_up("ctrl")
    assert len(sleeps) == 1  # only the first key after the click waits
    nb.mouse_down()
    nb.mouse_up()
    nb.type_text("x")
    assert len(sleeps) == 2  # type_text settles too
    off = []
    nb2 = WindowsBackend(_api=api, dpi_aware=False, sleep=off.append, click_key_settle=0, type_char_delay=0)
    nb2.mouse_down()
    nb2.mouse_up()
    nb2.key_down("a")
    nb2.key_up("a")
    assert off == []
    with pytest.raises(ValueError):
        WindowsBackend(_api=api, dpi_aware=False, click_key_settle=-1)


def test_key_presses_are_paced(api):
    """Key-by-key typing is a burst too: Windows 11 Notepad dropped runs of it."""
    sleeps = []
    nb = WindowsBackend(_api=api, dpi_aware=False, sleep=sleeps.append, type_char_delay=0.05, click_key_settle=0)
    nb.key_down("a")
    nb.key_up("a")
    assert sleeps == []  # the first press never waits
    nb.key_down("b")
    nb.key_up("b")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 0.05
    fast = []
    nb2 = WindowsBackend(_api=api, dpi_aware=False, sleep=fast.append, type_char_delay=0, click_key_settle=0)
    for k in "abc":
        nb2.key_down(k)
        nb2.key_up(k)
    assert fast == []
