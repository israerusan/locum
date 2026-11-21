"""Polish pass 3 -- backends: hung windows, leaked UIA event threads, the macOS
app-name cache, mss failure handling, the mss buffer copy, the shared AT-SPI loader.

Everything runs against fakes (no real desktop, any OS).
"""

from __future__ import annotations

import gc
import threading
from types import SimpleNamespace
from typing import Any, List

import pytest
from PIL import Image

from locum.backends.windows import WindowsBackend
from locum.errors import NotSupported
from locum.types import Rect


# ================================================================ Windows fakes
class FakeApi:
    """Just enough of the Win32 table for the code paths under test."""

    def __init__(self, hung: Any = ()) -> None:
        self.hung = set(hung)
        self.calls: List[tuple] = []

    def IsWindow(self, hwnd):
        return 1

    def IsHungAppWindow(self, hwnd):
        self.calls.append(("IsHungAppWindow", hwnd))
        return 1 if hwnd in self.hung else 0

    def IsIconic(self, hwnd):
        self.calls.append(("IsIconic", hwnd))
        return 0

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))
        return 1

    def PrintWindow(self, hwnd, hdc, flags):
        self.calls.append(("PrintWindow", hwnd, flags))
        return 0

    CreateDIBSection = object()  # only probed for presence by _can_print_window

    @staticmethod
    def get_last_error():
        return 0

    @staticmethod
    def format_error(code):
        return f"error {code}"


def _wbe(api: Any, **kw: Any) -> WindowsBackend:
    kw.setdefault("accessibility", False)
    return WindowsBackend(_api=api, dpi_aware=False, **kw)


# ============================================================ 1. hung capture
def test_capture_window_of_a_hung_window_is_not_supported_without_printwindow():
    api = FakeApi(hung={0x100})
    be = _wbe(api)
    with pytest.raises(NotSupported, match="not responding"):
        be.capture_window(0x100, restore_minimized=True)
    names = [c[0] for c in api.calls]
    assert "PrintWindow" not in names and "ShowWindow" not in names and "IsIconic" not in names


def test_capture_window_of_a_responsive_window_still_prints():
    api = FakeApi()
    be = _wbe(api)
    be._window_image = lambda hwnd: ("img", hwnd)  # the PrintWindow pipeline itself is tested elsewhere
    assert be.capture_window(0x100) == ("img", 0x100)
    assert ("IsHungAppWindow", 0x100) in api.calls


def test_is_hung_tolerates_a_missing_or_failing_function():
    class NoHung(FakeApi):
        IsHungAppWindow = None

    assert _wbe(NoHung())._is_hung(0x100) is False

    class Boom(FakeApi):
        def IsHungAppWindow(self, hwnd):
            raise OSError("gone")

    assert _wbe(Boom())._is_hung(0x100) is False


def test_win32_table_binds_is_hung_app_window_as_optional():
    import inspect

    from locum.backends.windows import _win32api

    src = inspect.getsource(_win32api._Win32.__init__)
    optional = src[src.index("optional = ["):]
    assert '"IsHungAppWindow"' in optional


def test_window_screenshot_auto_falls_back_to_the_screen_for_a_hung_window():
    """Window.screenshot(mode='auto') turns the NotSupported into a screen grab."""
    from locum.backends.mock import MockBackend
    from locum.desktop import Desktop

    be = MockBackend(width=400, height=300)
    target = be.add_window("Target", (10, 10, 100, 80), fill=(10, 200, 10))
    be.add_window("Cover", (20, 20, 30, 30), fill=(200, 10, 10))

    def hung(handle, *, restore_minimized=False):
        raise NotSupported("Window is not responding (IsHungAppWindow): PrintWindow would block.")

    be.capture_window = hung  # type: ignore[assignment]
    d = Desktop(be, sleep=lambda s: None, default_timeout=0.2, poll_interval=0.01)
    w = d.window("Target")
    assert w.is_covered()  # "auto" tries the window capture first
    be.clear_events()
    img = w.screenshot()
    assert img.size == (100, 80)
    assert [e for e in be.events if e[0] == "screenshot"]  # fell back to a screen grab
    assert target is not None


# =================================================== 2. hung accessibility roots
class FakeCtx:
    module = SimpleNamespace()

    def __init__(self) -> None:
        self.asked: List[int] = []

    def element_from_handle(self, hwnd):
        self.asked.append(hwnd)
        return SimpleNamespace(hwnd=hwnd)


def test_accessibility_roots_skip_hung_windows_before_uia():
    api = FakeApi(hung={0x200, 0x400})
    be = _wbe(api, accessibility=True)
    ctx = be._uia_ctx = FakeCtx()
    be._toplevel_hwnds = lambda: [0x100, 0x200, 0x300, 0x400]
    be._is_candidate = lambda h: h in (0x100, 0x200)
    be._popup_candidate = lambda h: True  # 0x300 / 0x400: Win32 pop-up menus
    roots = be.accessibility_roots()
    assert [r.handle for r in roots] == [0x100, 0x300]
    assert ctx.asked == [0x100, 0x300]  # no 5 s UIA timeout on the hung ones


# ================================================ 3. change tracker finalizer
class FakeTracker:
    instances: List["FakeTracker"] = []

    def __init__(self, module, clsids) -> None:
        self._stop = threading.Event()
        self.alive = True
        self.identity_generation = 0
        self.closed = 0
        FakeTracker.instances.append(self)

    def close(self, timeout: float = 5.0) -> None:
        self.closed += 1
        self._stop.set()

    def identity_dirty_since(self, stamp):
        return set()


@pytest.fixture
def fake_tracker(monkeypatch):
    from locum.backends.windows import uia_events

    FakeTracker.instances = []
    monkeypatch.setattr(uia_events, "ChangeTracker", FakeTracker)
    monkeypatch.setattr(uia_events, "events_enabled", lambda: True)
    return FakeTracker


def test_change_tracker_is_stopped_when_an_unclosed_backend_is_collected(fake_tracker):
    be = _wbe(FakeApi(), accessibility=True)
    be._uia_ctx = FakeCtx()
    tracker = be.change_tracker()
    assert isinstance(tracker, FakeTracker) and not tracker._stop.is_set()
    del be
    gc.collect()
    assert tracker._stop.is_set()  # the event thread is told to exit


def test_change_tracker_finalizer_is_detached_by_close(fake_tracker):
    be = _wbe(FakeApi(), accessibility=True)
    be._uia_ctx = FakeCtx()
    tracker = be.change_tracker()
    fin = be._change_tracker_finalizer
    assert fin.alive
    be.close()
    assert tracker.closed == 1 and tracker._stop.is_set()
    assert not fin.alive and be._change_tracker_finalizer is None


# ==================================================== 4. macOS app-name cache
def _mac_be(names: dict):
    from locum.backends.macos.accessibility_mixin import _AccessibilityMixin
    from locum.backends.macos.windows_mixin import _WindowsMixin

    lookups: List[int] = []

    def running(pid):
        lookups.append(pid)
        name = names.get(pid)
        return None if name is None else SimpleNamespace(localizedName=lambda: name)

    class FakeAx:
        system_wide = object()
        hit: Any = None

        def element_at(self, x, y):
            return self.hit

        def pid(self, el):
            return el.pid

        def attr(self, el, name, default=None):
            return self.hit if name == "AXFocusedUIElement" else default

    class Be(_AccessibilityMixin, _WindowsMixin):
        pass

    be = Be()
    be._app_names = {}
    be._appkit = SimpleNamespace(
        NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=running))
    be._ax = FakeAx()
    be._require_ax = lambda: be._ax
    be._get_layout = lambda: SimpleNamespace(pixel_to_point=lambda x, y: (x, y))
    return be, lookups


def test_macos_app_name_does_not_cache_empty_names():
    names: dict = {}
    be, lookups = _mac_be(names)
    assert be._app_name(42) == ""  # not registered yet
    names[42] = "TextEdit"
    assert be._app_name(42) == "TextEdit"  # looked up again, not a cached ""
    assert be._app_name(42) == "TextEdit"
    assert lookups == [42, 42]  # the real name is cached


def test_macos_app_name_cache_is_reset_per_snapshot():
    names = {42: "TextEdit"}
    be, lookups = _mac_be(names)
    be._ax.hit = SimpleNamespace(pid=42)
    assert be.element_at(1, 1).app == "TextEdit"
    names[42] = "Preview"  # PID recycled by another app
    assert be._app_name(42) == "TextEdit"  # same snapshot: cached
    assert be.element_at(1, 1).app == "Preview"  # new snapshot: fresh
    names[42] = "Notes"
    assert be.focused_element().app == "Notes"


# ===================================================== 5. mss failure counter
def _mss_be():
    be = _wbe(FakeApi())
    be._mss_broken = False  # dpi_aware=False picks GDI for "auto"; force the mss path
    gdi: List[Rect] = []

    def fake_gdi(r):
        gdi.append(r)
        return Image.new("RGB", (r.width, r.height))

    be._grab_gdi = fake_gdi
    return be, gdi


def test_one_mss_failure_falls_back_once_and_drops_the_grabber():
    be, gdi = _mss_be()
    outcomes = iter([RuntimeError("transient"), "ok"])
    mss_calls: List[Rect] = []

    def flaky(r):
        mss_calls.append(r)
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        return Image.new("RGB", (r.width, r.height), (1, 2, 3))

    be._grab_mss = flaky
    be._mss_local.holder = object()  # this thread's grabber
    be._grab(Rect(0, 0, 4, 4))
    assert len(gdi) == 1 and not be._mss_broken
    assert not hasattr(be._mss_local, "holder")  # dropped: a fresh one is made next time
    img = be._grab(Rect(0, 0, 4, 4))
    assert img.getpixel((0, 0)) == (1, 2, 3) and len(mss_calls) == 2 and len(gdi) == 1
    assert be._mss_failures == 0


def test_mss_is_given_up_after_three_consecutive_failures():
    be, gdi = _mss_be()
    mss_calls: List[Rect] = []

    def bad(r):
        mss_calls.append(r)
        raise RuntimeError("no mss")

    be._grab_mss = bad
    for i in range(3):
        be._grab(Rect(0, 0, 4, 4))
        assert be._mss_broken is (i == 2)
    be._grab(Rect(0, 0, 4, 4))
    assert len(mss_calls) == 3 and len(gdi) == 4


def test_mss_success_resets_the_failure_counter():
    be, gdi = _mss_be()
    seq = iter([True, True, False, True, True, False])  # True: fail

    def grab(r):
        if next(seq):
            raise RuntimeError("blip")
        return Image.new("RGB", (r.width, r.height))

    be._grab_mss = grab
    for _ in range(6):
        be._grab(Rect(0, 0, 2, 2))
    assert not be._mss_broken and be._mss_failures == 0


def test_explicit_mss_method_still_raises():
    be = _wbe(FakeApi(), screenshot_method="mss")
    assert not be._mss_broken

    def bad(r):
        raise RuntimeError("no mss")

    be._grab_mss = bad
    with pytest.raises(RuntimeError):
        be._grab(Rect(0, 0, 2, 2))


# ========================================================= 6. mss raw buffer
def _fake_sct(w: int, h: int):
    from mss.screenshot import ScreenShot

    data = bytearray()
    for y in range(h):
        for x in range(w):
            data += bytes((x * 7 % 256, y * 13 % 256, (x + y) % 256, 0x5A))  # B G R garbage-A
    shot = ScreenShot.from_size(data, w, h)
    assert hasattr(shot, "raw")  # mss >= 9.0
    return SimpleNamespace(grab=lambda mon: shot), shot


@pytest.mark.parametrize("module", ["locum.backends.windows.screen", "locum.backends.x11.screen"])
def test_grab_mss_uses_the_raw_buffer_with_identical_pixels(module):
    import importlib

    mod = importlib.import_module(module)
    sct, shot = _fake_sct(5, 3)
    fake_self = SimpleNamespace(_mss=lambda: sct)
    img = mod._ScreenMixin._grab_mss(fake_self, Rect(0, 0, 5, 3))
    expected = Image.frombuffer("RGB", (5, 3), bytes(shot.bgra), "raw", "BGRX", 0, 1)
    assert img.mode == "RGB" and img.size == (5, 3)
    assert img.tobytes() == expected.tobytes()
    shot.raw[:4] = b"\xff\xff\xff\xff"  # the image does not alias mss's buffer
    assert img.tobytes() == expected.tobytes()


# ===================================================== 7. shared AT-SPI loader
def test_x11_and_wayland_share_the_atspi_loader():
    from locum.backends.wayland.backend import WaylandBackend
    from locum.backends.x11.accessibility import _AccessibilityMixin
    from locum.backends.x11.atspi import _AtspiLoaderMixin

    assert issubclass(_AccessibilityMixin, _AtspiLoaderMixin)
    assert issubclass(WaylandBackend, _AtspiLoaderMixin)
    assert WaylandBackend._require_atspi is _AccessibilityMixin._require_atspi


def test_atspi_loader_semantics(monkeypatch):
    from locum.backends.x11 import atspi as atspi_mod

    loads: List[int] = []
    monkeypatch.setattr(atspi_mod, "load_atspi", lambda: loads.append(1) or "ATSPI")

    class Be(atspi_mod._AtspiLoaderMixin):
        def __init__(self, source):
            self._atspi_source, self._atspi_checked, self._atspi_mod = source, False, None
            self.checks = 0

        def _check(self):
            self.checks += 1

    auto = Be("auto")
    assert auto._require_atspi() == "ATSPI" and auto._require_atspi() == "ATSPI"
    assert loads == [1] and auto.checks == 2  # loaded once, closed-check every time
    given = object()
    assert Be(given)._require_atspi() is given
    with pytest.raises(NotSupported, match="AT-SPI accessibility is unavailable"):
        Be(None)._require_atspi()


def test_find_focused_prefers_active_roots():
    from locum.backends.x11.atspi import find_focused

    class N:
        def __init__(self, states, kids=()):
            self.states, self.kids = set(states), list(kids)

        def walk(self, max_depth=48):
            yield self
            for k in self.kids:
                yield from k.walk(max_depth)

    f1, f2 = N({"focused"}), N({"focused"})
    inactive, active = N(set(), [f1]), N({"active"}, [f2])
    assert find_focused([inactive, active], lambda r: r.states) is f2
    assert find_focused([inactive], lambda r: r.states) is f1  # no active root: all roots
    assert find_focused([N(set())], lambda r: r.states) is None


def test_windows_package_no_longer_reexports_frozen_globals():
    import locum.backends.windows as winmod

    assert not hasattr(winmod, "_uia_module") and not hasattr(winmod, "_dpi_mode")
    assert callable(winmod.load_uia_module) and callable(winmod.enable_dpi_awareness)
