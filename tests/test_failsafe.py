from __future__ import annotations

import threading

import pytest

from locum import failsafe as fs_mod
from locum.backends.mock import MockBackend
from locum.errors import FailSafeTriggered
from locum.failsafe import FailSafe, abort, install_kill_hotkey, is_aborted, reset_abort
from locum.types import Point


@pytest.fixture(autouse=True)
def _clean_abort():
    reset_abort()
    yield
    reset_abort()


def test_default_corners(mock_backend):
    fs = FailSafe(mock_backend)
    assert fs.corners == [Point(0, 0), Point(1279, 0), Point(0, 799), Point(1279, 799)]


def test_multi_monitor_virtual_corners():
    be = MockBackend(monitors=[(0, 0, 1920, 1080), (-1280, 0, 1280, 1024)])
    fs = FailSafe(be)
    assert Point(-1280, 0) in fs.corners
    assert Point(1919, 1079) in fs.corners


@pytest.mark.parametrize("corner", [(0, 0), (1279, 0), (0, 799), (1279, 799)])
def test_check_triggers_in_corners(mock_backend, corner):
    fs = FailSafe(mock_backend)
    mock_backend.mouse_move(640, 400)
    fs.check()
    mock_backend.mouse_move(*corner)
    with pytest.raises(FailSafeTriggered):
        fs.check()


def test_disabled_and_margin(mock_backend):
    fs = FailSafe(mock_backend, enabled=False)
    mock_backend.mouse_move(0, 0)
    fs.check()
    fs.enabled = True
    with pytest.raises(FailSafeTriggered):
        fs.check()
    mock_backend.mouse_move(3, 2)
    fs.check()
    with pytest.raises(FailSafeTriggered):
        FailSafe(mock_backend, margin=3).check()
    with pytest.raises(ValueError):
        FailSafe(mock_backend, margin=-1)


def test_custom_corners_and_ignore(mock_backend):
    fs = FailSafe(mock_backend, corners=[(100, 100)])
    mock_backend.mouse_move(0, 0)
    fs.check()  # not a configured corner
    mock_backend.mouse_move(100, 100)
    fs.check(ignore=(100, 100))
    with pytest.raises(FailSafeTriggered):
        fs.check()
    fs.corners = None  # back to defaults
    fs.check()


def test_abort_flag_is_sticky_and_global(mock_backend):
    fs = FailSafe(mock_backend, enabled=False)
    assert not is_aborted()
    t = threading.Thread(target=abort, args=("stop button",))
    t.start()
    t.join()
    assert is_aborted()
    with pytest.raises(FailSafeTriggered, match="stop button"):
        fs.check()
    with pytest.raises(FailSafeTriggered):
        fs.check()
    reset_abort()
    assert not is_aborted()
    fs.check()


def test_pynput_combo_conversion():
    assert fs_mod._pynput_combo("ctrl+alt+shift+q") == "<ctrl>+<alt>+<shift>+q"
    assert fs_mod._pynput_combo("ctrl+F12") == "<ctrl>+<f12>"
    assert fs_mod._pynput_combo("Ctrl+Esc") == "<ctrl>+<esc>"


def test_install_kill_hotkey_without_pynput(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "pynput" or name.startswith("pynput."):
            raise ImportError("no pynput")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert install_kill_hotkey() is False


def test_wayland_session_detection():
    wl = fs_mod._is_wayland_session
    assert wl({"XDG_SESSION_TYPE": "wayland"}, "linux")
    assert wl({"WAYLAND_DISPLAY": "wayland-0"}, "linux")
    assert not wl({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0"}, "linux")  # nested compositor
    assert not wl({"DISPLAY": ":0"}, "linux")
    assert not wl({"XDG_SESSION_TYPE": "wayland"}, "win32")


def test_kill_hotkey_warns_that_it_is_unreliable_on_wayland(monkeypatch):
    import sys
    import types
    import warnings

    class FakeHotKeys:
        def __init__(self, mapping):
            self.mapping = mapping

        def start(self):
            pass

        def stop(self):
            pass

    kb = types.ModuleType("pynput.keyboard")
    kb.GlobalHotKeys = FakeHotKeys
    pkg = types.ModuleType("pynput")
    pkg.keyboard = kb
    monkeypatch.setitem(sys.modules, "pynput", pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", kb)
    monkeypatch.setattr(fs_mod, "_is_wayland_session", lambda: True)
    try:
        with pytest.warns(RuntimeWarning, match="XWayland.*NOT a reliable emergency stop"):
            assert install_kill_hotkey("ctrl+alt+k") is True
        monkeypatch.setattr(fs_mod, "_is_wayland_session", lambda: False)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert install_kill_hotkey("ctrl+alt+k") is True  # X11: no warning
    finally:
        fs_mod.uninstall_kill_hotkey()


def test_install_kill_hotkey_with_fake_pynput(monkeypatch):
    import sys
    import types

    created = []

    class FakeHotKeys:
        def __init__(self, mapping):
            self.mapping = mapping
            self.started = self.stopped = False
            created.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    kb = types.ModuleType("pynput.keyboard")
    kb.GlobalHotKeys = FakeHotKeys
    pkg = types.ModuleType("pynput")
    pkg.keyboard = kb
    monkeypatch.setitem(sys.modules, "pynput", pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", kb)
    try:
        assert install_kill_hotkey("ctrl+alt+k") is True
        assert created[0].started and "<ctrl>+<alt>+k" in created[0].mapping
        created[0].mapping["<ctrl>+<alt>+k"]()
        assert is_aborted()
        assert install_kill_hotkey() is True
        assert created[0].stopped  # replaced listener is stopped
    finally:
        fs_mod.uninstall_kill_hotkey()
