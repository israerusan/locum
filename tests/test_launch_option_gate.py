"""Guarded launches must run the program the policy checked, never a shell or another binary.

Found by an adversarial review: Policy.strict(allow=["launch=notepad.exe"]) plus
launch_kwargs={"shell": True} let ``launch_app("notepad.exe & powershell ...")`` run
PowerShell through cmd.exe -- the policy saw ``notepad.exe``.
"""

from __future__ import annotations

import subprocess

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.errors import PolicyViolation
from locum.llm.safety import (FORBIDDEN_LAUNCH_OPTIONS, SAFE_LAUNCH_OPTIONS, GuardedDesktop, Policy,
                              check_launch_options)
from locum.llm.tools import DesktopToolkit


class RecordingBackend(MockBackend):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.launched = []

    def launch(self, command, **kw):
        self.launched.append((command, kw))
        return None


def _desktop():
    be = RecordingBackend()
    return Desktop(be, sleep=lambda s: None, default_timeout=0), be


ALLOW_NOTEPAD = ["launch=notepad.exe"]
SHELL_CHAIN = "notepad.exe & powershell -NoProfile -Command whoami"


@pytest.mark.parametrize("opts", [{"shell": True}, {"executable": "powershell.exe"}, {"preexec_fn": print}])
def test_toolkit_refuses_forbidden_launch_options_at_construction(opts):
    d, be = _desktop()
    with pytest.raises(PolicyViolation, match="never allowed"):
        DesktopToolkit(d, Policy.strict(allow=ALLOW_NOTEPAD), launch_kwargs=opts)
    assert be.launched == []


def test_toolkit_refuses_shell_set_after_construction():
    d, be = _desktop()
    tk = DesktopToolkit(d, Policy.strict(allow=ALLOW_NOTEPAD))
    tk.launch_kwargs["shell"] = True  # e.g. mutated by integration code later
    res = tk.call("launch_app", {"command": SHELL_CHAIN})
    assert res.is_error and "never allowed" in res.text
    assert be.launched == []


def test_toolkit_still_launches_with_safe_options():
    d, be = _desktop()
    tk = DesktopToolkit(d, Policy.strict(allow=ALLOW_NOTEPAD),
                        launch_kwargs={"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL})
    res = tk.call("launch_app", {"command": "notepad.exe"})
    assert not res.is_error, res.text
    assert be.launched == [("notepad.exe", {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL})]


@pytest.mark.parametrize("opts", [{"shell": True}, {"executable": "powershell.exe"}])
def test_guarded_desktop_refuses_forbidden_options_without_asking(opts):
    d, be = _desktop()
    asked = []
    g = GuardedDesktop(d, Policy.strict(allow=ALLOW_NOTEPAD), confirm=lambda *a: asked.append(a) or True)
    with pytest.raises(PolicyViolation, match="never allowed"):
        g.launch(SHELL_CHAIN, **opts)
    assert be.launched == [] and asked == []  # refused before any human is asked


def test_cwd_and_env_need_an_explicit_policy_opt_in():
    d, be = _desktop()
    g = GuardedDesktop(d, Policy.strict(allow=ALLOW_NOTEPAD))
    with pytest.raises(PolicyViolation, match=r"launch_options=\['env'\]"):
        g.launch("notepad.exe", env={"PATH": r"C:\evil"})  # PATH decides which notepad.exe runs
    opted = GuardedDesktop(d, Policy.strict(allow=ALLOW_NOTEPAD, launch_options=["cwd", "env"]))
    opted.launch("notepad.exe", cwd=".", env={"LANG": "C"})
    assert be.launched == [("notepad.exe", {"cwd": ".", "env": {"LANG": "C"}})]


def test_shell_false_is_fine_and_policy_cannot_opt_into_forbidden_options():
    check_launch_options({"shell": False, "stdout": subprocess.DEVNULL}, Policy())
    for key in FORBIDDEN_LAUNCH_OPTIONS:
        with pytest.raises(ValueError, match="may not include"):
            Policy(launch_options=[key])
    assert not (FORBIDDEN_LAUNCH_OPTIONS & SAFE_LAUNCH_OPTIONS)


def test_mcp_server_stdio_protection_still_passes_the_gate():
    """The MCP server adds stdin/stdout=DEVNULL to launch_kwargs; those must stay allowed."""
    from locum.llm.mcp_server import MCPServer

    d, be = _desktop()
    tk = DesktopToolkit(d, Policy.strict(allow=ALLOW_NOTEPAD))
    MCPServer._protect_launches(tk)
    assert not tk.call("launch_app", {"command": "notepad.exe"}).is_error
