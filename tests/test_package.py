"""Tests for the top-level ``pypilot`` package: lazy imports, default desktop, convenience API."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys

import pytest

import pypilot
from pypilot import backends
from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop


@pytest.fixture
def mock_desktop():
    be = MockBackend(width=1024, height=768)
    win = be.add_window("Login", (100, 100, 400, 300), app="auth")
    user = win.add_edit("User", (20, 40, 200, 28))
    ok = win.add_button("Sign in", (20, 200, 100, 30))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0.0)
    old = pypilot.set_desktop(d)
    try:
        yield d, be, user, ok
    finally:
        pypilot.set_desktop(old)


# Variables the interpreter itself needs to start. Without SYSTEMROOT, Python 3.9 on
# Windows aborts with "Fatal Python error: _Py_HashRandomization_Init: failed to get
# random numbers" before running a single line (seen on GitHub's windows-latest).
_OS_ESSENTIAL_ENV = ("SYSTEMROOT", "SystemDrive", "WINDIR", "TEMP", "TMP", "HOME", "TMPDIR", "LANG")


def _package_root() -> str:
    """The directory that contains the ``pypilot`` package under test."""
    return os.path.dirname(os.path.dirname(os.path.abspath(pypilot.__file__)))


def _isolated_env(environ=None) -> dict:
    """A minimal environment for a child interpreter: OS essentials only.

    Nothing that customises Python or pypilot leaks in from the parent (no
    inherited ``PYTHONPATH``, ``PYTHONSTARTUP``, ``PYPILOT_*``, ...).
    ``PYTHONPATH`` is set explicitly to the directory of the pypilot *under
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
        "PYTHONPATH": "/somewhere/else", "PYTHONSTARTUP": "/evil.py", "PYPILOT_BACKEND": "mock",
    }
    env = _isolated_env(parent)
    assert env == {"PATH": "", "SYSTEMROOT": r"C:\Windows", "TEMP": r"C:\Temp", "HOME": "/home/u",
                   "PYTHONPATH": _package_root()}
    assert os.path.isfile(os.path.join(env["PYTHONPATH"], "pypilot", "__init__.py"))


def test_import_is_side_effect_free():
    code = (
        "import sys, json, pypilot\n"
        "heavy = [m for m in sys.modules if m.split('.')[0] in ('numpy', 'PIL', 'mss', 'Xlib', 'comtypes', 'Quartz')]\n"
        "import pypilot.backends as b\n"
        "print(json.dumps({'heavy': heavy, 'backend': b._default is None, 'desktop': pypilot._default_desktop is None,\n"
        "                  'version': pypilot.__version__, 'file': pypilot.__file__}))\n"
    )
    # -s: no user site-packages either.
    out = subprocess.run([sys.executable, "-s", "-c", code], capture_output=True, text=True, env=_isolated_env())
    assert out.returncode == 0, out.stderr
    info = json.loads(out.stdout.strip().splitlines()[-1])
    imported = info.pop("file")
    assert os.path.normcase(os.path.abspath(imported)) == os.path.normcase(os.path.abspath(pypilot.__file__))
    assert info == {"heavy": [], "backend": True, "desktop": True, "version": pypilot.__version__}


def test_version_and_reexports():
    assert isinstance(pypilot.__version__, str) and pypilot.__version__.count(".") >= 2
    from pypilot.element import Element
    from pypilot.errors import ElementNotFound
    from pypilot.motion import Humanize
    from pypilot.types import Point, Rect
    from pypilot.window import Window

    assert pypilot.Desktop is Desktop
    assert pypilot.Element is Element
    assert pypilot.Window is Window
    assert pypilot.Humanize is Humanize
    assert pypilot.Point is Point and pypilot.Rect is Rect
    assert pypilot.ElementNotFound is ElementNotFound
    assert issubclass(pypilot.FailSafeTriggered, pypilot.PypilotError)
    for name in pypilot.__all__:
        assert getattr(pypilot, name) is not None, name
    assert "Desktop" in dir(pypilot)
    with pytest.raises(AttributeError):
        pypilot.does_not_exist  # noqa: B018


def test_wait_and_window_are_functions_and_modules():
    import pypilot.wait as wait_mod
    import pypilot.window as window_mod

    assert callable(pypilot.wait) and callable(pypilot.window)
    assert wait_mod.wait_until is sys.modules["pypilot.wait"].wait_until
    assert pypilot.wait.retry is sys.modules["pypilot.wait"].retry
    assert pypilot.window.Window is sys.modules["pypilot.window"].Window
    assert window_mod.match_window is sys.modules["pypilot.window"].match_window
    assert "selector" in inspect.signature(pypilot.wait).parameters
    assert "title" in inspect.signature(pypilot.window).parameters
    assert "Wait for an element" in pypilot.wait.__doc__
    assert "retry" in dir(pypilot.wait)
    assert "pypilot.wait" in repr(pypilot.wait)


def test_proxy_setattr_reaches_module(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("pypilot.wait.SENTINEL_FOR_TEST", sentinel, raising=False)
    assert sys.modules["pypilot.wait"].SENTINEL_FOR_TEST is sentinel
    monkeypatch.undo()
    assert not hasattr(sys.modules["pypilot.wait"], "SENTINEL_FOR_TEST")


def test_get_desktop_is_lazy_singleton(monkeypatch):
    old = pypilot.set_desktop(None)
    old_backend = backends._default
    monkeypatch.setenv("PYPILOT_BACKEND", "mock")
    backends.set_backend(None)
    try:
        d1 = pypilot.get_desktop()
        assert isinstance(d1.backend, MockBackend)
        assert pypilot.get_desktop() is d1
    finally:
        pypilot.set_desktop(old)
        backends.set_backend(old_backend)


def test_convenience_functions_drive_default_desktop(mock_desktop):
    d, be, user, ok = mock_desktop
    assert pypilot.get_desktop() is d
    assert pypilot.size() == (1024, 768)
    pypilot.move(300, 300)
    assert pypilot.position() == (300, 300)
    pypilot.click('edit "User"')
    pypilot.type("alice")
    assert user.value == "alice"
    pypilot.hotkey("ctrl+a")
    pypilot.press("backspace")
    assert user.value == ""
    pypilot.click('button "Sign in"')
    assert ok.clicks == 1
    pypilot.double_click('button "Sign in"')
    assert ok.clicks == 3
    pypilot.right_click(5, 5)
    pypilot.triple_click(5, 5)
    pypilot.middle_click(5, 5)
    pypilot.drag((10, 10), (50, 50), duration=0)
    pypilot.scroll(-2)
    assert ("scroll", 0, -2, 50, 50) in be.events
    assert pypilot.find('button "Sign in"').name == "Sign in"
    assert len(pypilot.find_all("edit")) == 1
    assert pypilot.exists("button") and not pypilot.exists("checkbox")
    assert pypilot.wait('button "Sign in"').name == "Sign in"
    assert pypilot.window("Login").app == "auth"
    assert [w.title for w in pypilot.windows()] == ["Login"]
    assert pypilot.active_window().title == "Login"
    assert pypilot.screenshot().size == (1024, 768)
    assert pypilot.locate(pypilot.screenshot((150, 150, 40, 40))) is not None
    pypilot.clipboard_set("x")
    assert pypilot.clipboard_get() == "x"
    assert 'button "Sign in"' in pypilot.tree()
    assert pypilot.launch("app") is None and be.launched == ["app"]


def test_errors_bubble_from_convenience_functions(mock_desktop):
    with pytest.raises(pypilot.ElementNotFound):
        pypilot.find('button "Nope"')
    with pytest.raises(pypilot.WindowNotFound):
        pypilot.window("Nope")
