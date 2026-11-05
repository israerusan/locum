"""Element/Window-level waits and mss capture errors follow the transient/fatal rule.

Fake clock: ``ft.now`` is the simulated time spent waiting.
"""

from __future__ import annotations

import pytest
from PIL import Image

from pypilot import failsafe as fs_mod
from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.errors import ActionTimeout, BackendError, is_transient


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def _foreign(module: str, name: str, base: type = Exception) -> type:
    cls = type(name, (base,), {})
    cls.__module__ = module
    return cls


# Same shape as mss.exception.ScreenShotError: a bare Exception subclass.
ScreenShotError = _foreign("mss.exception", "ScreenShotError")


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
def be():
    b = MockBackend(width=1280, height=800)
    w = b.add_window("Invoice Editor", (100, 100, 500, 400), app="erp")
    w.add_button("OK", (20, 340, 80, 30))
    return b


@pytest.fixture
def d(be, ft):
    desk = Desktop(be, sleep=ft.sleep, clock=ft.clock, default_timeout=5.0)
    yield desk
    fs_mod.reset_abort()


# ------------------------------------------------------------ mss capture
def test_mss_screenshot_error_is_transient():
    assert is_transient(ScreenShotError("gdi: BitBlt failed"))


def test_mss_type_error_subclass_stays_fatal():
    bad = _foreign("mss.base", "BadArg", TypeError)
    assert not is_transient(bad("oops"))


def test_real_mss_screenshot_error_is_transient():
    exc_mod = pytest.importorskip("mss.exception")
    assert is_transient(exc_mod.ScreenShotError("gdi"))


def test_locate_retries_mss_capture_failure_then_reports_cause(d, be, ft):
    def shot(region=None):
        raise ScreenShotError("workstation locked")

    be.screenshot = shot
    with pytest.raises(ScreenShotError, match="workstation locked"):
        d.locate(Image.new("RGB", (8, 8), (255, 0, 0)), timeout=2)
    assert ft.now == pytest.approx(2.0)


# ------------------------------------------------------ Element.wait_until
def test_element_wait_until_callable_attribute_error_raises_immediately(d, ft):
    el = d.find('button "OK"')
    calls = {"n": 0}

    def pred(e):
        calls["n"] += 1
        return e.nonexistent_attr_xyz

    with pytest.raises(AttributeError):
        el.wait_until(pred, timeout=5)
    assert ft.now == 0.0
    assert calls["n"] == 1


def test_element_wait_until_callable_type_error_raises_immediately(d, ft):
    el = d.find('button "OK"')
    with pytest.raises(TypeError):
        el.wait_until(lambda e: len(5), timeout=5)
    assert ft.now == 0.0


def test_element_wait_until_retries_transient_error_then_succeeds(d, ft):
    el = d.find('button "OK"')
    calls = {"n": 0}

    def pred(e):
        calls["n"] += 1
        if calls["n"] < 3:
            raise BackendError("stale element")
        return True

    assert el.wait_until(pred, timeout=5) is el
    assert calls["n"] == 3
    assert 0.0 < ft.now < 5.0


def test_element_wait_until_false_predicate_still_times_out(d, ft):
    el = d.find('button "OK"')
    with pytest.raises(ActionTimeout):
        el.wait_until(lambda e: False, timeout=2)
    assert ft.now == pytest.approx(2.0)


def test_element_wait_until_named_state_still_works(d, ft):
    el = d.find('button "OK"')
    assert el.wait_until("visible", timeout=1) is el
    assert ft.now == 0.0


# ------------------------------------------------------- Window.wait_closed
def test_window_wait_closed_times_out_while_open(d, ft):
    w = d.window("Invoice Editor")
    with pytest.raises(ActionTimeout):
        w.wait_closed(timeout=2)
    assert ft.now == pytest.approx(2.0)


def test_window_wait_closed_returns_when_closed(d, ft):
    w = d.window("Invoice Editor")
    w.close()
    assert w.wait_closed(timeout=2) is True
