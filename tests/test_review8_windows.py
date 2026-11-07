"""Review 8: Windows backend / Window / wait regressions.

Nothing here touches the real desktop: every Win32 call goes to a fake API.
"""

from __future__ import annotations

import ctypes
import threading
from typing import Any, List, Optional

import pytest
from PIL import Image

import locum.backends.windows as winmod
import locum.backends.windows._win32api as w32
from locum.backends.base import AccessibleNode
from locum.backends.windows import WindowsBackend
from locum.desktop import Desktop
from locum.errors import NotSupported
from locum.selectors import parse
from locum.types import Rect
from locum.wait import wait_until
from locum.window import Window

from test_window_capture import GREEN, FakeCaptureApi, _bgrx, _black, _frame_painter
from test_windows_backend import FakeApi, FakeWin, _deref, _desktop

PMV2 = w32.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2


def _signed(v: Any) -> int:
    return ctypes.c_ssize_t(int(getattr(v, "value", v) or 0)).value


# ============================================================ 1. per-thread DPI awareness
class PresetDpiApi(FakeApi):
    """Process awareness already set elsewhere: only SetThreadDpiAwarenessContext works."""

    def __init__(self) -> None:
        super().__init__()
        self.thread_switches: List[tuple] = []

    def SetProcessDpiAwarenessContext(self, ctx):
        self.last_error = w32.ERROR_ACCESS_DENIED
        return 0

    def SetThreadDpiAwarenessContext(self, ctx):
        self.thread_switches.append((threading.get_ident(), _signed(ctx)))
        return 0x11


def test_every_calling_thread_is_switched_to_per_monitor_v2():
    api = PresetDpiApi()
    be = WindowsBackend(_api=api, sleep=lambda s: None)
    assert be.dpi_mode == "thread_per_monitor_v2"
    seen = []

    def worker():
        seen.append(threading.get_ident())
        be.virtual_screen()
        be.monitors()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    worker_calls = [c[1] for c in api.thread_switches if c[0] == seen[0]]
    # Switched for each call and switched back afterwards (review 9: not permanently).
    assert worker_calls == [PMV2, 0x11, PMV2, 0x11]


def test_preset_awareness_is_not_switched_when_backend_is_not_dpi_aware():
    api = PresetDpiApi()
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    be.virtual_screen()
    assert api.thread_switches == []


class LogicalThreadApi(FakeApi):
    """A 125 % monitor seen from a DPI-unaware thread: GetWindowRect is logical, DWM physical."""

    SCALE = 1.25

    def __init__(self) -> None:
        super().__init__()
        self.ctx = 0x11  # unaware
        self.phys_outer = Rect.from_ltrb(240, 120, 1260, 760)
        self.phys_frame = Rect.from_ltrb(250, 120, 1250, 750)  # invisible borders 10, 0, 10, 10

    def GetThreadDpiAwarenessContext(self):
        return self.ctx

    def GetAwarenessFromDpiAwarenessContext(self, ctx):
        return w32.DPI_AWARENESS_PER_MONITOR_AWARE if _signed(ctx) in (-3, -4) else 0

    def SetThreadDpiAwarenessContext(self, ctx):
        old, self.ctx = self.ctx, _signed(ctx)
        return old

    def GetWindowRect(self, hwnd, rc):
        o = self.phys_outer
        if self.GetAwarenessFromDpiAwarenessContext(self.ctx) != w32.DPI_AWARENESS_PER_MONITOR_AWARE:
            s = self.SCALE
            o = Rect.from_ltrb(round(o.left / s), round(o.top / s), round(o.right / s), round(o.bottom / s))
        r = _deref(rc)
        r.left, r.top, r.right, r.bottom = o.left, o.top, o.right, o.bottom
        return 1

    def DwmGetWindowAttribute(self, hwnd, attr, out, size):
        if attr == winmod.DWMWA_EXTENDED_FRAME_BOUNDS:
            f, obj = self.phys_frame, _deref(out)
            obj.left, obj.top, obj.right, obj.bottom = f.left, f.top, f.right, f.bottom
            return 0
        return super().DwmGetWindowAttribute(hwnd, attr, out, size)


def test_dwm_frame_is_converted_to_the_logical_space_of_the_thread():
    api = LogicalThreadApi()
    api.add(FakeWin(0x100, "Notepad", Rect(0, 0, 10, 10)))
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    # logical outer (192, 96, 1008, 608); physical insets 10/0/10/10 -> 8/0/8/8 logical
    assert be.window_info(0x100).rect == Rect.from_ltrb(200, 96, 1000, 600)
    assert api.ctx == 0x11  # the thread's awareness is put back
    be.move_window(0x100, Rect(100, 100, 400, 300))
    mv = [c for c in api.calls if c[0] == "MoveWindow"][-1]
    assert mv == ("MoveWindow", 0x100, 92, 100, 416, 308, True)  # border deltas in one space


# ============================================================ 2. dpi_aware=False and mss
def test_dpi_unaware_backend_never_starts_mss(monkeypatch):
    be = WindowsBackend(_api=FakeApi(), dpi_aware=False, sleep=lambda s: None)
    mss_starts: List[int] = []
    monkeypatch.setattr(be, "_mss", lambda: mss_starts.append(1) or (_ for _ in ()).throw(RuntimeError("mss")))
    monkeypatch.setattr(be, "_grab_gdi", lambda r: Image.new("RGB", (r.width, r.height)))
    assert be.screenshot(Rect(0, 0, 4, 4)).size == (4, 4)
    assert mss_starts == []  # mss would make the whole process per-monitor aware
    explicit = WindowsBackend(_api=FakeApi(), dpi_aware=False, screenshot_method="mss")
    assert not explicit._mss_broken  # asked for explicitly: still used
    assert not WindowsBackend(_api=FakeApi(), sleep=lambda s: None)._mss_broken


# ============================================================ 3. move/resize a minimized window
class ParkingApi(FakeApi):
    """A minimized window reports its parked placeholder rect."""

    PARKED = Rect(-31992, -32000, 183, 26)

    def GetWindowRect(self, hwnd, rc):
        w = self.wins.get(hwnd)
        if w is not None and w.iconic:
            p, r = self.PARKED, _deref(rc)
            r.left, r.top, r.right, r.bottom = p.left, p.top, p.right, p.bottom
            return 1
        return super().GetWindowRect(hwnd, rc)

    def DwmGetWindowAttribute(self, hwnd, attr, out, size):
        w = self.wins[hwnd]
        if attr == winmod.DWMWA_EXTENDED_FRAME_BOUNDS and w.iconic:
            p, obj = self.PARKED, _deref(out)
            obj.left, obj.top, obj.right, obj.bottom = p.left, p.top, p.right, p.bottom
            return 0
        return super().DwmGetWindowAttribute(hwnd, attr, out, size)


def _minimized_window(restore_max: bool = False):
    api = ParkingApi()
    win = api.add(FakeWin(0x100, "Untitled - Notepad", Rect(100, 100, 800, 600), iconic=True))
    win.restore_max = restore_max
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    d = Desktop(be, sleep=lambda s: None, default_timeout=0.1)
    return api, Window(be.window_info(0x100), d)


@pytest.mark.parametrize("restore_max", [False, True])
def test_move_of_a_minimized_window_keeps_its_normal_size(restore_max):
    api, w = _minimized_window(restore_max)
    assert w.rect == ParkingApi.PARKED
    w.move(50, 60)
    assert api.wins[0x100].frame == Rect(50, 60, 800, 600)
    assert not w.is_minimized and not w.is_maximized


def test_resize_of_a_minimized_window_keeps_its_normal_position():
    api, w = _minimized_window()
    w.resize(640, 480)
    assert api.wins[0x100].frame == Rect(100, 100, 640, 480)


# ============================================================ 4. lookup by handle
def test_window_filtered_out_of_list_windows_still_exists():
    api = FakeApi()
    _desktop(api)
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    d = Desktop(be, sleep=lambda s: None, default_timeout=0.05, poll_interval=0.01)
    for h in (0x200, 0x300, 0x500):  # untitled, tool window, cloaked
        w = Window(be.window_info(h), d)
        assert all(x.handle != h for x in be.list_windows())
        assert w.exists
        assert w.refresh().handle == h
    w = Window(be.window_info(0x200), d)
    from locum.errors import ActionTimeout

    with pytest.raises(ActionTimeout):
        w.wait_closed(timeout=0.05)
    del api.wins[0x200]
    api.z.remove(0x200)
    assert not w.exists and w.wait_closed(timeout=0.05)
    hidden = Window(be.window_info(0x600), d)
    assert not hidden.exists  # a hidden window still counts as gone


# ============================================================ 5/6. capture_window
class PlacementCaptureApi(FakeCaptureApi):
    """Adds WPF_RESTORETOMAXIMIZED bookkeeping and a client area 10 px below the frame top."""

    def __init__(self) -> None:
        super().__init__()
        self.restore_max = False
        self.placements: List[int] = []

    def ShowWindow(self, hwnd, cmd):
        if cmd == w32.SW_SHOWNOACTIVATE:
            self.restore_max = False  # what Windows does: back to the normal size
        return super().ShowWindow(hwnd, cmd)

    def GetWindowPlacement(self, hwnd, pwp):
        wp = _deref(pwp)
        assert wp.length == ctypes.sizeof(w32.WINDOWPLACEMENT)
        wp.flags = w32.WPF_RESTORETOMAXIMIZED if self.restore_max else 0
        wp.showCmd = w32.SW_SHOWMINIMIZED if self.iconic else w32.SW_SHOWNORMAL
        return 1

    def SetWindowPlacement(self, hwnd, pwp):
        wp = _deref(pwp)
        self.placements.append(wp.showCmd)
        self.ShowWindow(hwnd, wp.showCmd)
        if self.iconic and wp.flags & w32.WPF_RESTORETOMAXIMIZED:
            self.restore_max = True
        return 1

    def GetClientRect(self, hwnd, prc):
        r = _deref(prc)
        r.left, r.top, r.right, r.bottom = 0, 0, self.frame.width, self.frame.height - 10
        return 1

    def ClientToScreen(self, hwnd, ppt):
        p = _deref(ppt)
        p.x, p.y = self.frame.left, self.frame.top + 10
        return 1


def _capture_backend(api):
    return WindowsBackend(_api=api, dpi_aware=False, accessibility=False, sleep=lambda s: None)


def test_restore_minimized_capture_keeps_restore_to_maximized():
    api = PlacementCaptureApi()
    api.iconic = api.parked = True
    api.restore_max = True
    api.painters[w32.PW_RENDERFULLCONTENT] = _frame_painter(api)
    img = _capture_backend(api).capture_window(0x100, restore_minimized=True)
    assert img.size == (40, 30)
    assert api.iconic and api.restore_max  # the user's next restore still maximizes
    assert api.placements == [w32.SW_SHOWMINNOACTIVE]  # put back without activating it
    assert w32.SW_SHOWMINIMIZED not in [c[1] for c in api.calls if c[0] == "ShowWindow"]


def test_restore_minimized_capture_of_a_normal_window_sets_no_placement():
    api = PlacementCaptureApi()
    api.iconic = api.parked = True
    api.painters[w32.PW_RENDERFULLCONTENT] = _frame_painter(api)
    _capture_backend(api).capture_window(0x100, restore_minimized=True)
    assert api.iconic and api.placements == []


def _black_client_painter(api, client=(0, 0, 0)):
    l, t, _, _ = api.border

    def paint(w, h):
        def color(x, y):
            fy = y - t
            return client if 10 <= fy < api.frame.height and l <= x < l + api.frame.width else GREEN

        return _bgrx(w, h, color)

    return paint


def test_classic_print_with_a_black_client_area_is_a_failure():
    api = PlacementCaptureApi()
    api.painters[w32.PW_RENDERFULLCONTENT] = _black
    api.painters[0] = _black_client_painter(api)
    with pytest.raises(NotSupported, match="black client area"):
        _capture_backend(api).capture_window(0x100)


def test_classic_print_with_client_content_still_succeeds():
    api = PlacementCaptureApi()
    api.painters[w32.PW_RENDERFULLCONTENT] = _black
    api.painters[0] = _black_client_painter(api, client=(40, 40, 40))
    img = _capture_backend(api).capture_window(0x100)
    assert img.getpixel((5, 20)) == (40, 40, 40)


# ============================================================ 7. wait_until interval=0
def test_wait_until_with_zero_interval_does_not_busy_spin():
    calls: List[int] = []
    sleeps: List[float] = []

    def pred():
        calls.append(1)
        return len(calls) >= 4

    assert wait_until(pred, timeout=5, interval=0, sleep=sleeps.append)
    assert len(sleeps) == 3 and all(s >= 0.01 for s in sleeps)
    sleeps.clear()
    with pytest.raises(Exception):
        wait_until(lambda: False, timeout=0, interval=0, sleep=sleeps.append)
    assert sleeps == []  # timeout=0 is still exactly one check


# ============================================================ 8. bare-text selector reads name first
class CountingNode(AccessibleNode):
    def __init__(self, name: str, value: Optional[str] = None, children=()):
        self._name, self._value, self._children = name, value, list(children)
        self.value_reads = 0

    @property
    def role(self) -> str:
        return "button"

    @property
    def name(self) -> str:
        return self._name

    @property
    def value(self) -> Optional[str]:
        self.value_reads += 1
        return self._value

    @property
    def bounds(self) -> Optional[Rect]:
        return Rect(0, 0, 10, 10)

    def children(self):
        return self._children


def test_bare_string_selector_reads_value_only_when_the_name_misses():
    ok = CountingNode("OK")
    field = CountingNode("", value="OK")
    root = CountingNode("root", children=[ok, field])
    found = parse('"OK"').find_all(root)
    assert found == [ok, field]  # the value still matches
    assert ok.value_reads == 0
    assert field.value_reads == 1
