"""Tests for the top-level ``locum`` package: lazy imports, default desktop, convenience API."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys

import pytest

import locum
from locum import backends
from locum.backends.mock import MockBackend
from locum.desktop import Desktop


@pytest.fixture
def mock_desktop():
    be = MockBackend(width=1024, height=768)
    win = be.add_window("Login", (100, 100, 400, 300), app="auth")
    user = win.add_edit("User", (20, 40, 200, 28))
    ok = win.add_button("Sign in", (20, 200, 100, 30))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0.0)
    old = locum.set_desktop(d)
    try:
        yield d, be, user, ok
    finally:
        locum.set_desktop(old)


# Variables the interpreter itself needs to start. Without SYSTEMROOT, Python 3.9 on
# Windows aborts with "Fatal Python error: _Py_HashRandomization_Init: failed to get
# random numbers" before running a single line (seen on GitHub's windows-latest).
_OS_ESSENTIAL_ENV = ("SYSTEMROOT", "SystemDrive", "WINDIR", "TEMP", "TMP", "HOME", "TMPDIR", "LANG")


def _package_root() -> str:
    """The directory that contains the ``locum`` package under test."""
    return os.path.dirname(os.path.dirname(os.path.abspath(locum.__file__)))


def _isolated_env(environ=None) -> dict:
    """A minimal environment for a child interpreter: OS essentials only.

    Nothing that customises Python or locum leaks in from the parent (no
    inherited ``PYTHONPATH``, ``PYTHONSTARTUP``, ``LOCUM_*``, ...).
    ``PYTHONPATH`` is set explicitly to the directory of the locum *under
    test*, so the child imports this very package whether it is installed
    editable, from a wheel, or only placed on the path.
    """
    environ = os.environ if environ is None else environ
    upper = {k.upper(): v for k, v in environ.items()}  # Windows names are case-insensitive
    env = {"PATH": ""}
    for name in _OS_ESSENTIAL_ENV:
        if name.upper() in upper:
            env[name] = upper[name.upper()]
    env["PYTHONPATH"] = _package_root()
    return env


def test_isolated_env_keeps_os_essentials_but_drops_python_customisation():
    parent = {
        "SystemRoot": r"C:\Windows", "TEMP": r"C:\Temp", "HOME": "/home/u", "PATH": "/usr/bin",
        "PYTHONPATH": "/somewhere/else", "PYTHONSTARTUP": "/evil.py", "LOCUM_BACKEND": "mock",
    }
    env = _isolated_env(parent)
    assert env == {"PATH": "", "SYSTEMROOT": r"C:\Windows", "TEMP": r"C:\Temp", "HOME": "/home/u",
                   "PYTHONPATH": _package_root()}
    assert os.path.isfile(os.path.join(env["PYTHONPATH"], "locum", "__init__.py"))


def test_import_is_side_effect_free():
    code = (
        "import sys, json, locum\n"
        "heavy = [m for m in sys.modules if m.split('.')[0] in ('numpy', 'PIL', 'mss', 'Xlib', 'comtypes', 'Quartz')]\n"
        "import locum.backends as b\n"
        "print(json.dumps({'heavy': heavy, 'backend': b._default is None, 'desktop': locum._default_desktop is None,\n"
        "                  'version': locum.__version__, 'file': locum.__file__}))\n"
    )
    # -s: no user site-packages either.
    out = subprocess.run([sys.executable, "-s", "-c", code], capture_output=True, text=True, env=_isolated_env())
    assert out.returncode == 0, out.stderr
    info = json.loads(out.stdout.strip().splitlines()[-1])
    imported = info.pop("file")
    assert os.path.normcase(os.path.abspath(imported)) == os.path.normcase(os.path.abspath(locum.__file__))
    assert info == {"heavy": [], "backend": True, "desktop": True, "version": locum.__version__}


def test_version_and_reexports():
    assert isinstance(locum.__version__, str) and locum.__version__.count(".") >= 2
    from locum.element import Element
    from locum.errors import ElementNotFound
    from locum.motion import Humanize
    from locum.types import Point, Rect
    from locum.window import Window

    assert locum.Desktop is Desktop
    assert locum.Element is Element
    assert locum.Window is Window
    assert locum.Humanize is Humanize
    assert locum.Point is Point and locum.Rect is Rect
    assert locum.ElementNotFound is ElementNotFound
    assert issubclass(locum.FailSafeTriggered, locum.LocumError)
    for name in locum.__all__:
        assert getattr(locum, name) is not None, name
    assert "Desktop" in dir(locum)
    with pytest.raises(AttributeError):
        locum.does_not_exist  # noqa: B018


def test_wait_and_window_are_functions_and_modules():
    import locum.wait as wait_mod
    import locum.window as window_mod

    assert callable(locum.wait) and callable(locum.window)
    assert wait_mod.wait_until is sys.modules["locum.wait"].wait_until
    assert locum.wait.retry is sys.modules["locum.wait"].retry
    assert locum.window.Window is sys.modules["locum.window"].Window
    assert window_mod.match_window is sys.modules["locum.window"].match_window
    assert "selector" in inspect.signature(locum.wait).parameters
    assert "title" in inspect.signature(locum.window).parameters
    assert "Wait for an element" in locum.wait.__doc__
    assert "retry" in dir(locum.wait)
    assert "locum.wait" in repr(locum.wait)


def test_proxy_setattr_reaches_module(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("locum.wait.SENTINEL_FOR_TEST", sentinel, raising=False)
    assert sys.modules["locum.wait"].SENTINEL_FOR_TEST is sentinel
    monkeypatch.undo()
    assert not hasattr(sys.modules["locum.wait"], "SENTINEL_FOR_TEST")


def test_get_desktop_is_lazy_singleton(monkeypatch):
    old = locum.set_desktop(None)
    old_backend = backends._default
    monkeypatch.setenv("LOCUM_BACKEND", "mock")
    backends.set_backend(None)
    try:
        d1 = locum.get_desktop()
        assert isinstance(d1.backend, MockBackend)
        assert locum.get_desktop() is d1
    finally:
        locum.set_desktop(old)
        backends.set_backend(old_backend)


def test_convenience_functions_drive_default_desktop(mock_desktop):
    d, be, user, ok = mock_desktop
    assert locum.get_desktop() is d
    assert locum.size() == (1024, 768)
    locum.move(300, 300)
    assert locum.position() == (300, 300)
    locum.click('edit "User"')
    locum.type("alice")
    assert user.value == "alice"
    locum.hotkey("ctrl+a")
    locum.press("backspace")
    assert user.value == ""
    locum.click('button "Sign in"')
    assert ok.clicks == 1
    locum.double_click('button "Sign in"')
    assert ok.clicks == 3
    locum.right_click(5, 5)
    locum.triple_click(5, 5)
    locum.middle_click(5, 5)
    locum.drag((10, 10), (50, 50), duration=0)
    locum.scroll(-2)
    assert ("scroll", 0, -2, 50, 50) in be.events
    assert locum.find('button "Sign in"').name == "Sign in"
    assert len(locum.find_all("edit")) == 1
    assert locum.exists("button") and not locum.exists("checkbox")
    assert locum.wait('button "Sign in"').name == "Sign in"
    assert locum.window("Login").app == "auth"
    assert [w.title for w in locum.windows()] == ["Login"]
    assert locum.active_window().title == "Login"
    assert locum.screenshot().size == (1024, 768)
    assert locum.locate(locum.screenshot((150, 150, 40, 40))) is not None
    locum.clipboard_set("x")
    assert locum.clipboard_get() == "x"
    assert 'button "Sign in"' in locum.tree()
    assert locum.launch("app") is None and be.launched == ["app"]


def test_errors_bubble_from_convenience_functions(mock_desktop):
    with pytest.raises(locum.ElementNotFound):
        locum.find('button "Nope"')
    with pytest.raises(locum.WindowNotFound):
        locum.window("Nope")
