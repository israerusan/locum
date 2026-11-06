"""Regression tests for bug-hunt findings in window.py / desktop.py / element.py.

Mock backend and a fake clock only: no real input, windows or clipboard.
"""

import threading

import pytest

from locum import backends
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import ActionTimeout, BackendError, BackendUnavailable, ElementNotFound, WindowNotFound
from locum.types import Point, Rect


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def make(**kw):
    be = MockBackend(width=1280, height=800)
    mw = be.add_window("Invoice Editor", (100, 100, 500, 400), app="erp")
    ft = FakeClock()
    kw.setdefault("default_timeout", 5.0)
    d = Desktop(be, sleep=ft.sleep, clock=ft, failsafe=False, **kw)
    return be, mw, d, ft


# ------------------------------------------------------- Window.wait_closed
def test_wait_closed_raises_fatal_backend_error():
    be, mw, d, ft = make()
    win = d.window("Invoice Editor")

    def boom():
        raise BackendUnavailable("X server gone")

    be.list_windows = boom
    with pytest.raises(BackendUnavailable):
        win.wait_closed(timeout=2)


def test_wait_closed_retries_transient_error_while_window_open():
    be, mw, d, ft = make()
    win = d.window("Invoice Editor")
    orig = be.list_windows
    calls = [0]

    def flaky():
        calls[0] += 1
        if calls[0] == 1:
            raise BackendError("busy")
        return orig()

    be.list_windows = flaky
    with pytest.raises(ActionTimeout):
        win.wait_closed(timeout=2)
    assert calls[0] > 1


def test_wait_closed_still_returns_when_closed():
    be, mw, d, ft = make()
    win = d.window("Invoice Editor")
    be.remove_window(mw)
    assert win.wait_closed(timeout=1) is True


# ------------------------------------------------------- Window.move / resize
def test_move_keeps_current_size():
    be, mw, d, ft = make()
    win = d.window("Invoice Editor")
    be.move_window(mw.handle, (100, 100, 900, 700))  # resized behind our back
    win.move(0, 0)
    assert mw.rect == Rect(0, 0, 900, 700)


def test_resize_keeps_current_position():
    be, mw, d, ft = make()
    win = d.window("Invoice Editor")
    be.move_window(mw.handle, (40, 50, 500, 400))  # moved behind our back
    win.resize(320, 240)
    assert mw.rect == Rect(40, 50, 320, 240)


# ------------------------------------------------------- poll_interval=0
def _run_with_watchdog(fn, seconds=3.0):
    res = {}

    def run():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            res["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "wait hung (busy-spin with poll_interval=0)"
    return res.get("exc")


def test_poll_interval_zero_find_terminates_with_fake_clock():
    be, mw, d, ft = make(poll_interval=0)
    exc = _run_with_watchdog(lambda: d.find('button "nope"', timeout=1))
    assert isinstance(exc, ElementNotFound)


def test_poll_interval_zero_scoped_waits_terminate_with_fake_clock():
    be, mw, d, ft = make(poll_interval=0)
    win = d.window("Invoice Editor", timeout=0)
    assert isinstance(_run_with_watchdog(lambda: win.find('button "nope"', timeout=1)), ElementNotFound)
    assert isinstance(_run_with_watchdog(lambda: win.wait_closed(timeout=1)), ActionTimeout)


# ------------------------------------------------------- Window.screenshot
def test_screenshot_of_closed_window_raises():
    be, mw, d, ft = make()
    win = d.window("Invoice Editor")
    be.remove_window(mw)
    with pytest.raises(WindowNotFound):
        win.screenshot()


# ------------------------------------------------------- Desktop.click return value
def test_click_returns_point_actually_clicked_for_partly_offscreen_element():
    be = MockBackend(width=1280, height=800)
    mw = be.add_window("A", (-100, 100, 400, 300))
    mw.add_button("OK", (0, 10, 160, 20))
    d = Desktop(be, sleep=lambda s: None, failsafe=False)
    be.clear_events()
    ret = d.click('button "OK"')
    downs = [e for e in be.events if e[0] == "down"]
    assert downs, be.events
    assert ret == Point(downs[-1][2], downs[-1][3])
    assert ret.x >= 0


def test_click_returns_center_for_fully_visible_element():
    be = MockBackend(width=1280, height=800)
    mw = be.add_window("A", (100, 100, 400, 300))
    mw.add_button("OK", (10, 10, 60, 20))
    d = Desktop(be, sleep=lambda s: None, failsafe=False)
    el = d.find('button "OK"')
    assert d.click(el) == el.bounds.center


# ------------------------------------------------------- Desktop.launch
def test_launch_validates_timeout_before_launching():
    be = MockBackend()
    d = Desktop(be, sleep=lambda s: None, failsafe=False)
    for bad in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            d.launch("notepad", wait_window="Notepad", timeout=bad)
        with pytest.raises(ValueError):
            d.launch("notepad", wait_window="Notepad", timeout=bad, new_window=False)
    assert be.launched == []


# ------------------------------------------------------- Desktop.__init__ leak
def test_init_with_bad_option_closes_owned_backend():
    closed = []

    class Leaky(MockBackend):
        def close(self):
            closed.append(1)
            super().close()

    backends.register_backend("bughunt-leaky", Leaky)
    try:
        for bad in ({"typing_mode": "bogus"}, {"default_timeout": -1}, {"poll_interval": -1}, {"humanize": "x"}):
            closed.clear()
            with pytest.raises((ValueError, TypeError)):
                Desktop("bughunt-leaky", failsafe=False, **bad)
            assert closed == [1], bad
    finally:
        backends._REGISTRY.pop("bughunt-leaky", None)


def test_init_with_bad_option_does_not_close_caller_backend():
    closed = []

    class B(MockBackend):
        def close(self):
            closed.append(1)

    with pytest.raises(ValueError):
        Desktop(B(), typing_mode="bogus")
    assert closed == []
