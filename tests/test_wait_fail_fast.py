"""Waits fail fast on non-transient errors and keep retrying transient ones.

Everything runs on the MockBackend with a fake clock: ``ft.now`` is the
simulated time spent waiting, so "raised immediately" means ``ft.now == 0``.
"""

from __future__ import annotations

import pytest
from PIL import Image

from locum import failsafe as fs_mod
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.element import Element
from locum.errors import (
    ActionTimeout,
    AmbiguousElement,
    BackendError,
    BackendUnavailable,
    ElementNotFound,
    FailSafeTriggered,
    ImageNotFound,
    NotSupported,
    PermissionDenied,
    PolicyViolation,
    LocumError,
    SelectorSyntaxError,
    WindowNotFound,
    is_transient,
)
from locum.wait import wait_until


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


COMError = _foreign("_ctypes", "COMError")


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


def _break_roots(be, exc, times=None):
    """Make accessibility_roots raise ``exc`` (``times`` times, or forever)."""
    calls = {"n": 0}
    orig = be.accessibility_roots

    def roots():
        calls["n"] += 1
        if times is None or calls["n"] <= times:
            raise exc
        return orig()

    be.accessibility_roots = roots
    return calls


# ------------------------------------------------------------- classification
@pytest.mark.parametrize("exc", [
    ElementNotFound("x"), WindowNotFound("x"), ImageNotFound("x"), AmbiguousElement("x"),
    ActionTimeout("inner wait"), BackendError("stale element"), OSError("busy"),
    TimeoutError("slow"), ConnectionResetError("pipe"), COMError("RPC_E_CALL_REJECTED"),
    _foreign("Xlib.error", "BadWindow")(), _foreign("gi.repository.GLib", "Error")(),
    _foreign("objc", "error")(), _foreign("comtypes", "COMError")(),
])
def test_transient_errors(exc):
    assert is_transient(exc)


@pytest.mark.parametrize("exc", [
    TypeError("x"), AttributeError("x"), NameError("x"), ValueError("x"), AssertionError(),
    ImportError("x"), ModuleNotFoundError("x"), PermissionError("x"), PermissionDenied("x"),
    BackendUnavailable("x"), NotSupported("x"), FailSafeTriggered("x"), PolicyViolation("x"),
    SelectorSyntaxError("x"), RuntimeError("x"), KeyError("x"), IndexError("x"),
    _foreign("somelib", "Oops")(), _foreign("_ctypes", "ArgumentError")(),
    _foreign("Xlib.error", "XTypeError", TypeError)(),
])
def test_fatal_errors(exc):
    assert not is_transient(exc)


def test_permission_denied_is_a_backend_and_permission_error():
    err = PermissionDenied("Screen Recording not granted")
    assert isinstance(err, BackendError) and isinstance(err, PermissionError)


def test_transient_flag_overrides_the_type_rule():
    assert is_transient(RuntimeError("x")) is False
    assert is_transient(BackendError("UI busy", transient=True)) is True
    assert is_transient(BackendError("driver crashed", transient=False)) is False
    err = LocumError("x")
    err.transient = True
    assert is_transient(err) is True
    # stop requests can never be marked retryable
    assert is_transient(FailSafeTriggered("stop", transient=True)) is False
    assert is_transient(PolicyViolation("no", transient=True)) is False


def test_transient_kwarg_keeps_message_and_args():
    err = ActionTimeout("late", transient=False)
    assert str(err) == "late" and err.args == ("late",) and err.transient is False
    assert ElementNotFound("x").transient is None


# ---------------------------------------------------------------- Desktop.find
def test_type_error_in_find_probe_raises_immediately(d, be, ft):
    calls = _break_roots(be, TypeError("roots() got an unexpected keyword argument"))
    with pytest.raises(TypeError, match="unexpected keyword"):
        d.find('button "OK"', timeout=5)
    assert ft.now == 0.0
    assert calls["n"] == 1


@pytest.mark.parametrize("exc", [
    AttributeError("'NoneType' object has no attribute 'children'"),
    PermissionError("Access is denied"),
    PermissionDenied("macOS Accessibility permission not granted"),
    BackendUnavailable("no display"),
    RuntimeError("bug"),
])
def test_fatal_find_probe_errors_surface_unwrapped_on_first_probe(d, be, ft, exc):
    calls = _break_roots(be, exc)
    with pytest.raises(type(exc)) as info:
        d.find('button "OK"', timeout=5)
    assert info.value is exc  # the original exception, not an ActionTimeout / ElementNotFound
    assert ft.now == 0.0
    assert calls["n"] == 1


def test_element_not_found_retries_until_timeout(d, ft):
    with pytest.raises(ElementNotFound):
        d.find('button "Nope"', timeout=2)
    assert ft.now == pytest.approx(2.0)


@pytest.mark.parametrize("exc", [COMError("UIA_E_ELEMENTNOTAVAILABLE"), OSError("stale"), BackendError("busy")])
def test_transient_probe_errors_are_retried_then_succeed(d, be, ft, exc):
    calls = _break_roots(be, exc, times=2)
    assert d.find('button "OK"', timeout=5).name == "OK"
    assert calls["n"] == 3
    assert 0 < ft.now < 5


def test_timeout_message_names_last_transient_error(d, be, ft):
    _break_roots(be, BackendError("UI Automation tree is busy"))
    with pytest.raises(ElementNotFound) as info:
        d.find('button "OK"', timeout=1)
    assert ft.now == pytest.approx(1.0)
    timeout = info.value.__cause__
    assert isinstance(timeout, ActionTimeout)
    assert "last error: BackendError: UI Automation tree is busy" in str(timeout)


def test_inner_action_timeout_is_transient(d, be, ft):
    calls = _break_roots(be, ActionTimeout("inner wait expired"), times=1)
    assert d.find('button "OK"', timeout=5).name == "OK"
    assert calls["n"] == 2


# ------------------------------------------------------------- other waits
def test_wait_state_callable_type_error_raises_immediately(d, ft):
    def broken(el):
        return el.nmae == "OK"  # typo: AttributeError

    with pytest.raises(AttributeError):
        d.wait('button "OK"', state=broken, timeout=5)
    assert ft.now == 0.0


def test_window_probe_permission_error_raises_immediately(d, be, ft):
    def denied():
        raise PermissionError("Access is denied")

    be.list_windows = denied
    with pytest.raises(PermissionError):
        d.window("Invoice Editor", timeout=5)
    assert ft.now == 0.0


def test_window_not_found_still_waits(d, ft):
    with pytest.raises(WindowNotFound):
        d.window("Nope", timeout=1.5)
    assert ft.now == pytest.approx(1.5)


def test_scoped_element_find_fails_fast_on_programming_error(d, ft, monkeypatch):
    from locum.selectors import Selector

    root = d.window("Invoice Editor", timeout=0).element

    def broken(self, *a, **k):
        raise TypeError("bad probe")

    monkeypatch.setattr(Selector, "find_first", broken)
    with pytest.raises(TypeError, match="bad probe"):
        Element(root.node, d).find("button", timeout=5)
    assert ft.now == 0.0


def test_scoped_element_find_retries_transient_errors(d, ft, monkeypatch):
    from locum.selectors import Selector

    root = d.window("Invoice Editor", timeout=0).element
    orig = Selector.find_first
    n = []

    def flaky(self, *a, **k):
        n.append(1)
        if len(n) < 3:
            raise COMError("busy")
        return orig(self, *a, **k)

    monkeypatch.setattr(Selector, "find_first", flaky)
    assert Element(root.node, d).find("button", timeout=5).name == "OK"
    assert len(n) == 3


def test_desktop_wait_until_fails_fast_on_type_error(d, ft):
    with pytest.raises(TypeError):
        d.wait_until(lambda: len(None), timeout=5)
    assert ft.now == 0.0


def test_desktop_wait_until_retries_transient_and_opt_out_swallows_all(d, ft):
    n = []

    def flaky():
        n.append(1)
        if len(n) < 3:
            raise ElementNotFound("not yet")
        return "ok"

    assert d.wait_until(flaky, timeout=5) == "ok"
    with pytest.raises(ActionTimeout):
        d.wait_until(lambda: len(None), timeout=1, ignore_exceptions=True)
    assert ft.now >= 1.0


# ---------------------------------------------------------------- locate
def _needle():
    return Image.new("RGB", (8, 8), (255, 0, 0))


@pytest.mark.parametrize("exc", [
    PermissionDenied("Screen Recording permission not granted"),
    PermissionError("Access is denied"),
    BackendUnavailable("display gone"),
    NotSupported("no screenshots"),
])
def test_locate_fatal_capture_failure_raises_immediately(d, be, ft, exc):
    calls = {"n": 0}

    def shot(region=None):
        calls["n"] += 1
        raise exc

    be.screenshot = shot
    with pytest.raises(type(exc)):
        d.locate(_needle(), timeout=5)
    assert ft.now == 0.0
    assert calls["n"] == 1


def test_wait_image_fatal_capture_failure_raises_immediately(d, be, ft):
    def shot(region=None):
        raise PermissionDenied("Screen Recording permission not granted")

    be.screenshot = shot
    with pytest.raises(PermissionDenied):
        d.wait_image(_needle(), timeout=5)
    assert ft.now == 0.0


def test_locate_transient_capture_failure_retries_then_reports_cause(d, be, ft):
    def shot(region=None):
        raise BackendError("workstation locked")

    be.screenshot = shot
    with pytest.raises(BackendError, match="workstation locked"):
        d.locate(_needle(), timeout=2)
    assert ft.now == pytest.approx(2.0)


def test_locate_not_found_still_returns_none_after_timeout(d, ft):
    assert d.locate(_needle(), timeout=1) is None
    assert ft.now == pytest.approx(1.0)


# ----------------------------------------------------- locum.wait.wait_until
def test_wait_until_transient_mode():
    with pytest.raises(TypeError):
        wait_until(lambda: len(None), timeout=5, ignore_exceptions="transient", sleep=lambda s: None)
    n = []

    def flaky():
        n.append(1)
        if len(n) < 3:
            raise OSError("busy")
        return 1

    assert wait_until(flaky, timeout=5, interval=0, ignore_exceptions="transient", sleep=lambda s: None) == 1


def test_wait_until_default_still_swallows_everything():
    n = []

    def flaky():
        n.append(1)
        if len(n) < 3:
            raise RuntimeError("transient")
        return "ok"

    assert wait_until(flaky, timeout=5, interval=0, sleep=lambda s: None) == "ok"


def test_wait_until_rejects_unknown_mode():
    with pytest.raises(ValueError, match="ignore_exceptions"):
        wait_until(lambda: 1, timeout=0, ignore_exceptions="sometimes")
