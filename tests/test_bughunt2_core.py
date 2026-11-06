"""Regression tests for bug-hunt round 2 (core: window / desktop / element / wait)."""
import math

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.errors import ActionTimeout, WindowNotFound
from locum.types import Rect


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


# ---------------------------------------------------------------- #9
def _capturing(b):
    cap = []
    orig = b.screenshot
    b.screenshot = lambda region=None: (cap.append(region), orig(region))[1]
    return cap


def test_desktop_region_window_uses_current_rect():
    b = MockBackend()
    mw = b.add_window("App", rect=(100, 100, 300, 200))
    d = Desktop(b, sleep=lambda s: None)
    win = d.window("App")
    b.move_window(mw.handle, Rect(400, 300, 300, 200))
    cap = _capturing(b)
    d.screenshot(win)
    assert cap[-1] == Rect(400, 300, 300, 200)


def test_desktop_region_closed_window_raises():
    b = MockBackend()
    mw = b.add_window("App", rect=(100, 100, 300, 200))
    d = Desktop(b, sleep=lambda s: None)
    win = d.window("App")
    b.remove_window(mw)
    with pytest.raises(WindowNotFound):
        d.screenshot(win)


def test_resolve_point_window_uses_current_rect_and_raises_when_closed():
    b = MockBackend()
    mw = b.add_window("App", rect=(100, 100, 300, 200))
    d = Desktop(b, sleep=lambda s: None)
    win = d.window("App")
    b.move_window(mw.handle, Rect(400, 300, 300, 200))
    assert d.resolve_point(win) == Rect(400, 300, 300, 200).center
    b.remove_window(mw)
    with pytest.raises(WindowNotFound):
        d.resolve_point(win)


# ---------------------------------------------------------------- #10
def test_wait_closed_message_names_resolved_timeout():
    fc = FakeClock()
    b = MockBackend()
    b.add_window("Doc")
    d = Desktop(b, default_timeout=2, sleep=fc.sleep, clock=fc.clock)
    win = d.window("Doc")
    with pytest.raises(ActionTimeout) as ei:
        win.wait_closed(timeout=None)
    assert "None" not in str(ei.value)
    assert "after 2s" in str(ei.value)


# ---------------------------------------------------------------- #11
@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0, None])
def test_poll_interval_must_be_finite(bad):
    with pytest.raises(ValueError):
        Desktop(MockBackend(), poll_interval=bad)


def test_poll_interval_zero_still_accepted():
    assert Desktop(MockBackend(), poll_interval=0).poll_interval == 0


# ---------------------------------------------------------------- #12
def test_desktop_wait_until_interval_zero_advances_injected_clock():
    fc = FakeClock()
    d = Desktop(MockBackend(), sleep=fc.sleep, clock=fc.clock)
    calls = []

    def pred():
        calls.append(1)
        if len(calls) > 1000:
            raise AssertionError("busy-spinning")
        return False

    with pytest.raises(ActionTimeout):
        d.wait_until(pred, timeout=1, interval=0)
    assert fc.sleeps and all(s > 0 for s in fc.sleeps)


def test_element_wait_until_interval_zero_advances_injected_clock():
    fc = FakeClock()
    b = MockBackend()
    w = b.add_window("Doc")
    w.add_button("OK", (10, 10, 80, 30))
    d = Desktop(b, sleep=fc.sleep, clock=fc.clock)
    el = d.find("button", timeout=0)
    calls = []

    def state(e):
        calls.append(1)
        if len(calls) > 1000:
            raise AssertionError("busy-spinning")
        return False

    with pytest.raises(ActionTimeout):
        el.wait_until(state, timeout=1, interval=0)
    assert fc.sleeps and all(s > 0 for s in fc.sleeps)


def test_closed_window_never_resolves_to_a_same_titled_other_window():
    import pytest as _pytest
    from locum import Desktop as _Desktop
    from locum.backends.mock import MockBackend as _Mock
    from locum.errors import ElementNotFound as _NotFound

    be = _Mock(width=1280, height=800)
    a = be.add_window("Untitled - Notepad", (0, 0, 400, 300), app="notepad.exe", pid=1)
    a.add_button("Save", (10, 10, 80, 30))
    b = be.add_window("Untitled - Notepad", (500, 0, 400, 300), app="notepad.exe", pid=2)
    b.add_button("Delete everything", (10, 10, 80, 30))
    d = _Desktop(be, sleep=lambda s: None, default_timeout=0)
    win_a = next(w for w in d.windows() if w.handle == a.handle)
    be.close_window(a.handle)
    with _pytest.raises(_NotFound):
        win_a.find("button", timeout=0)
    with _pytest.raises(_NotFound):
        _ = win_a.element
