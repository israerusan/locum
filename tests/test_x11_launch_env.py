"""X11Backend.launch keeps the child on the backend's display even when the caller passes ``env``.

Runs everywhere: it drives the real launch code of the X11 windows mixin with
``subprocess.Popen`` stubbed out (no Xlib, no display needed).
"""

from __future__ import annotations

import subprocess

import pytest

from locum.backends.base import Backend
from locum.backends.x11.windowing import _WindowsMixin


class _PlainLaunch:
    launch = Backend.launch  # what X11Backend's super().launch resolves to


class _Fake(_WindowsMixin, _PlainLaunch):
    display_name = ":7"


@pytest.fixture
def popen(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    return calls


def test_default_env_gets_display(popen, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    _Fake().launch(["xterm"])
    assert popen[0][1]["env"]["DISPLAY"] == ":7"


def test_supplied_env_without_display_gets_display_and_xauthority(popen, monkeypatch):
    monkeypatch.setenv("XAUTHORITY", "/home/u/.Xauthority")
    caller_env = {"LANG": "C"}
    _Fake().launch(["xterm"], env=caller_env)
    env = popen[0][1]["env"]
    assert env == {"LANG": "C", "DISPLAY": ":7", "XAUTHORITY": "/home/u/.Xauthority"}
    assert caller_env == {"LANG": "C"}  # the caller's dict is not mutated


def test_explicit_display_in_env_is_respected(popen):
    _Fake().launch(["xterm"], env={"DISPLAY": ":99"})
    assert popen[0][1]["env"]["DISPLAY"] == ":99"
