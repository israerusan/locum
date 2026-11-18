"""Polish pass 2 (2026-11): Windows backend regressions.

Nothing here touches the real desktop: every Win32 / UIA call goes to a fake.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import locum.backends.windows._win32api as w32
import locum.backends.windows.accessibility as acc
import locum.backends.windows.uia_events as uia_events
from locum.backends.windows import WindowsBackend
from locum.backends.windows.uia import UIANode
from locum.types import Rect

from test_window_capture import FakeCaptureApi, _bgrx
from test_windows_backend import FakeApi, FakeCtx, FakeElement, FakeWin, _deref

#: Work area of the fake monitor 0x1 (its rcMonitor is 0,-200 2560x1440 in FakeApi; taskbar 40 px).
WORK = Rect(0, -200, 2560, 1400)


class WorkAreaApi(FakeApi):
    """FakeApi plus MonitorFromWindow and a work area (rcWork) per monitor."""

    def __init__(self, with_monitor_api: bool = True) -> None:
        super().__init__()
        if with_monitor_api:
            self.MonitorFromWindow = self._monitor_from_window

    def _monitor_from_window(self, hwnd, flags):
        assert flags == w32.MONITOR_DEFAULTTONEAREST
        return 0x1

    def GetMonitorInfoW(self, h, info):
        ok = super().GetMonitorInfoW(h, info)
        if ok and h == 0x1:
            _deref(info).rcWork = w32.RECT(WORK.left, WORK.top, WORK.right, WORK.bottom)
        return ok


def _maximized(api: FakeApi) -> FakeWin:
    # A maximized File Explorer: its DWM bounds keep 8 px of the resize border on top
    # (and the right/bottom ones hang over the neighbour monitor / the taskbar).
    return api.add(FakeWin(0x500, "logs - File Explorer", Rect.from_ltrb(0, -208, 2568, 1208), zoomed=True,
                           cls="CabinetWClass", exe="C:\\Windows\\explorer.exe"))


# ============================================================ W1: maximized window rect
def test_maximized_window_rect_is_clipped_to_the_monitor_work_area():
    api = WorkAreaApi()
    _maximized(api)
    normal = api.add(FakeWin(0x600, "Notepad", Rect(-300, -250, 400, 300)))  # not maximized: untouched
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    by_handle = {w.handle: w for w in be.list_windows()}
    assert by_handle[0x500].rect == WORK and by_handle[0x500].is_maximized
    assert by_handle[0x600].rect == normal.frame
    assert be.window_info(0x500).rect == WORK
    # windows_above reports the same rectangle (occlusion must not cover the next monitor)
    assert be.windows_above(0x600) == [WORK]


def test_maximized_window_rect_unchanged_without_monitor_api():
    api = WorkAreaApi(with_monitor_api=False)
    w = _maximized(api)
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    assert be.window_info(0x500).rect == w.frame


def test_move_window_still_places_the_raw_frame():
    api = WorkAreaApi()
    w = api.add(FakeWin(0x500, "x", Rect(10, 10, 300, 200)))
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    be.move_window(0x500, Rect(20, 30, 400, 300))
    assert w.frame == Rect(20, 30, 400, 300)


class MaximizedCaptureApi(FakeCaptureApi):
    def __init__(self) -> None:
        super().__init__()
        self.frame = Rect(100, 92, 40, 38)  # 8 px above the work area

    def IsZoomed(self, hwnd):
        return 1

    def GetMonitorInfoW(self, h, info):
        i = _deref(info)
        i.rcMonitor = w32.RECT(0, 0, 400, 400)
        i.rcWork = w32.RECT(0, 100, 400, 400)
        return 1


def test_capture_of_a_maximized_window_covers_its_window_info_rect():
    api = MaximizedCaptureApi()
    l, t, _, _ = api.border
    api.painters[w32.PW_RENDERFULLCONTENT] = lambda w, h: _bgrx(w, h, lambda x, y: (x, y, 50))
    be = WindowsBackend(_api=api, dpi_aware=False, accessibility=False, sleep=lambda s: None)
    img = be.capture_window(0x100)
    assert img.size == (40, 30)  # the frame clipped to the work area, as WindowInfo.rect
    assert img.getpixel((0, 0)) == (l, t + 8, 50)  # pixel (0, 0) is WindowInfo.rect's corner


# ============================================================ W2: UIA bounds of top-level roots
def test_top_level_uia_bounds_are_the_visible_frame():
    api = WorkAreaApi()
    _maximized(api)
    api.add(FakeWin(0x700, "Notepad", Rect(100, 100, 400, 300)))
    api.add(FakeWin(0x800, "Minimized", Rect(-32000, -32000, 160, 28), iconic=True))
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    # UIA reports GetWindowRect: invisible borders included
    elements = {h: FakeElement(50032, w.title, rect=w.outer.as_ltrb(), rid=(h,)) for h, w in api.wins.items()}

    class Ctx(FakeCtx):
        def element_from_handle(self, hwnd):
            return elements.get(hwnd)

    be._uia_ctx = Ctx()
    roots = {r.handle: r for r in be.accessibility_roots()}
    assert roots[0x500].bounds == WORK == be.window_info(0x500).rect
    assert roots[0x700].bounds == Rect(100, 100, 400, 300)
    # minimized: the backend has no better answer, UIA's is used
    minimized = UIANode(elements[0x800], be.uia, handle=0x800, top_level=True)
    assert minimized.bounds == api.wins[0x800].outer
    # a descendant keeps UIA's rectangle
    assert UIANode(elements[0x700], be.uia).bounds == api.wins[0x700].outer


def test_top_level_bounds_fall_back_to_uia_when_the_resolver_fails():
    el = FakeElement(50032, "x", rect=(1, 2, 30, 40))
    ctx = FakeCtx()
    ctx.window_bounds = lambda h: (_ for _ in ()).throw(OSError("gone"))
    assert UIANode(el, ctx, handle=0x10, top_level=True).bounds == Rect.from_ltrb(1, 2, 30, 40)


# ============================================================ W3: minimized Store app
class FakeUser32:
    """The user32 subset window_content_pids uses (see accessibility._user32)."""

    def __init__(self, wins: Dict[int, tuple], children: Dict[int, List[int]], z: List[int]) -> None:
        self.wins = wins  # hwnd -> (pid, class, title)
        self.children = children
        self.z = z

    def IsWindow(self, h):
        return int(h in self.wins)

    def GetWindowThreadProcessId(self, h, out):
        _deref(out).value = self.wins.get(h, (0,))[0]
        return 1

    def EnumChildWindows(self, h, proc, lp):
        for c in self.children.get(h, []):
            if not proc(c, lp):
                break
        return 1

    def EnumWindows(self, proc, lp):
        for h in self.z:
            if not proc(h, lp):
                break
        return 1

    def GetClassNameW(self, h, buf, n):
        buf.value = self.wins[h][1]
        return len(buf.value)

    def GetWindowTextW(self, h, buf, n):
        buf.value = self.wins[h][2]
        return len(buf.value)


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes WINFUNCTYPE callbacks")
def test_minimized_store_app_frame_reports_its_reparented_core_window(monkeypatch):
    AFH, TODO, OTHER = 11696, 34180, 999
    wins = {
        0x40C86: (AFH, "ApplicationFrameWindow", "Microsoft To Do"),  # minimized frame: no children
        0x10C94: (TODO, "Windows.UI.Core.CoreWindow", "Microsoft To Do"),  # re-parented, cloaked
        0x20494: (OTHER, "Windows.UI.Core.CoreWindow", "Windows Input Experience"),
        0x30000: (OTHER, "Notepad", "Microsoft To Do"),  # same title, not a CoreWindow
        0x50000: (500, "Notepad", "Notes"),
    }
    u32 = FakeUser32(wins, {}, [0x50000, 0x30000, 0x20494, 0x10C94, 0x40C86])
    monkeypatch.setattr(acc, "_user32", lambda: u32)
    be = WindowsBackend(_api=FakeApi(), dpi_aware=False, sleep=lambda s: None)
    assert be.window_content_pids(0x40C86) == frozenset({AFH, TODO})
    # an ordinary window is not looked up among top-level windows
    assert be.window_content_pids(0x50000) == frozenset({500})


# ============================================================ W5: event subscription cleanup
class _FakeUIA:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.added: List[str] = []
        self.removed = 0

    def GetRootElement(self):
        return object()

    def _add(self, name):
        if name == self.fail_on:
            raise OSError("E_FAIL")
        self.added.append(name)

    def AddFocusChangedEventHandler(self, *a):
        self._add("focus")

    def AddStructureChangedEventHandler(self, *a):
        self._add("structure")

    def AddPropertyChangedEventHandler(self, *a):
        self._add("property")

    def RemoveAllEventHandlers(self):
        self.removed += 1


def test_partial_event_subscription_removes_the_handlers_it_added(monkeypatch):
    comtypes = pytest.importorskip("comtypes")
    fake = _FakeUIA(fail_on="property")
    monkeypatch.setattr(comtypes, "CoCreateInstance", lambda *a, **k: fake)
    monkeypatch.setattr(uia_events, "_make_handler", lambda UIA, tracker: object())
    module = SimpleNamespace(IUIAutomation=object, TreeScope_Subtree=4)
    tracker = uia_events.ChangeTracker(module, ("{ff48dba4-60ef-4201-aa87-54103eef594e}",))
    assert not tracker.alive and isinstance(tracker.error, OSError)
    assert fake.added == ["focus", "structure"] and fake.removed == 1


def test_a_tracker_that_did_not_come_up_is_stopped(monkeypatch):
    closed: List[float] = []

    class SlowTracker:
        alive = False  # still subscribing when START_TIMEOUT ran out

        def __init__(self, module, clsids):
            pass

        def close(self, timeout=5.0):
            closed.append(timeout)

    monkeypatch.setattr(uia_events, "ChangeTracker", SlowTracker)
    monkeypatch.setattr(uia_events, "events_enabled", lambda: True)
    be = WindowsBackend(_api=FakeApi(), dpi_aware=False, sleep=lambda s: None)
    be._uia_ctx = SimpleNamespace(module=object())
    assert be.change_tracker() is None
    assert closed == [0]  # non-blocking: the late subscription unsubscribes itself


# ============================================================ W6: fewer cross-process reads
class CountingElement(FakeElement):
    def __init__(self, *a, **k):
        object.__setattr__(self, "reads", {})
        super().__init__(*a, **k)

    def __getattribute__(self, name):
        if name.startswith("Current"):
            reads = object.__getattribute__(self, "reads")
            reads[name] = reads.get(name, 0) + 1
        return super().__getattribute__(name)


def test_role_reads_the_class_name_only_for_windows():
    btn = CountingElement(50000, "OK", cls="Button")
    assert UIANode(btn, FakeCtx()).role == "button"
    assert "CurrentClassName" not in btn.reads
    dlg = CountingElement(50032, "Save As", cls="#32770")
    assert UIANode(dlg, FakeCtx()).role == "dialog"


def test_states_reads_the_control_type_once():
    from locum.backends.windows.uia import UIA_ValuePatternId

    vp = SimpleNamespace(CurrentIsReadOnly=False)
    edit = CountingElement(50004, "Name", patterns={UIA_ValuePatternId: vp})
    assert "editable" in UIANode(edit, FakeCtx()).states
    assert edit.reads["CurrentControlType"] == 1


# ============================================================ perf: accessibility_roots
def test_accessibility_roots_does_not_build_window_infos():
    api = FakeApi()
    api.add(FakeWin(0x100, "Notepad", Rect(0, 0, 300, 200)))
    api.add(FakeWin(0x200, "", Rect(0, 0, 30, 20)))  # untitled: not listed, not a pop-up
    api.add(FakeWin(0x300, "Hidden", Rect(0, 0, 30, 20), visible=False))
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    elements = {h: FakeElement(50032, w.title, rid=(h,)) for h, w in api.wins.items()}

    class Ctx(FakeCtx):
        def element_from_handle(self, hwnd):
            return elements.get(hwnd)

    be._uia_ctx = Ctx()

    def no_info(*a, **k):
        raise AssertionError("accessibility_roots built a WindowInfo")

    be._info = no_info
    assert [r.handle for r in be.accessibility_roots()] == [0x100]
