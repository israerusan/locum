"""Regression tests for Windows-backend bugs found in the bug hunt (fakes only, no real desktop)."""

import os
import subprocess

import pytest

from locum.backends.windows import KEYEVENTF_UNICODE, WindowsBackend
from locum.backends.windows import process as P
from locum.backends.windows.process import ShellProcess

from test_windows_backend import FakeApi, keys_of


# ------------------------------------------------ unquoted program path + args
def test_unquoted_exe_path_with_spaces_and_args_stays_on_popen(tmp_path):
    d = tmp_path / "dir with space"
    d.mkdir()
    exe = d / "erp.exe"
    exe.write_text("x")
    assert P.shell_launch_target(str(exe)) is None
    assert P.shell_launch_target(f"{exe} --flag") is None
    # CreateProcess also tries ``prefix + ".exe"``
    assert P.shell_launch_target(f"{d / 'erp'} --flag x") is None


def test_unquoted_document_path_with_spaces_and_args_split_at_the_file(tmp_path):
    d = tmp_path / "my docs"
    d.mkdir()
    doc = d / "Q3 report.xlsx"
    doc.write_text("x")
    assert P.shell_launch_target(f"{doc} /r") == (str(doc), "/r")


def test_bare_names_still_classified_as_before(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(P.shutil, "which", lambda name: None if name == "excel" else "C:/x/" + name)
    assert P.shell_launch_target("excel /e") == ("excel", "/e")
    assert P.shell_launch_target("notepad x.txt") is None


# ------------------------------------------------------- shifted dead key
class _DeadShiftApi(FakeApi):
    """US-International: '6' is plain, Shift+6 is the dead circumflex."""

    def __init__(self):
        super().__init__()
        self.layout_table["^"] = 0x100 | 0x36
        self.layout_table["6"] = 0x36
        self.to_unicode_calls = []

    def ToUnicodeEx(self, vk, scan, keys, buf, n, flags, hkl):
        shift = bool(keys[0x10] & 0x80)
        self.to_unicode_calls.append((vk, shift, flags))
        if vk == 0x36 and shift:
            return -1
        return 1


def _backend(api):
    return WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None, type_char_delay=0, click_key_settle=0)


def test_shifted_dead_key_is_typed_as_unicode():
    api = _DeadShiftApi()
    be = _backend(api)
    be.key_down("^")
    be.key_up("^")
    ev = keys_of(api)
    assert ev and all(e[3] & KEYEVENTF_UNICODE for e in ev)
    assert all(c[2] == 0x4 for c in api.to_unicode_calls)  # never changes kernel keyboard state


def test_shifted_non_dead_key_still_pressed_as_key():
    api = _DeadShiftApi()
    be = _backend(api)
    be.key_down("!")
    be.key_up("!")
    ev = keys_of(api)
    assert not any(e[3] & KEYEVENTF_UNICODE for e in ev)
    assert any(e[1] == 0x31 for e in ev)


def test_unshifted_char_on_dead_shift_key_pressed_as_key():
    api = _DeadShiftApi()
    be = _backend(api)
    be.key_down("6")
    be.key_up("6")
    ev = keys_of(api)
    assert [e[1] for e in ev] == [0x36, 0x36]


# ------------------------------------------------------- ShellProcess.wait(0)
class _RunningApi:
    def GetProcessId(self, h):
        return 99

    def WaitForSingleObject(self, h, ms):
        return 0x102  # WAIT_TIMEOUT

    def CloseHandle(self, h):
        return 1


def test_shellprocess_wait_zero_raises_timeout_and_poll_returns_none():
    p = ShellProcess(_RunningApi(), "excel", 1234)
    with pytest.raises(subprocess.TimeoutExpired):
        p.wait(0)
    with pytest.raises(subprocess.TimeoutExpired):
        p.wait(0.5)
    assert p.poll() is None
    assert p.returncode is None


def test_shellprocess_without_handle_wait_returns_zero():
    p = ShellProcess(object(), "Q3.xlsx", None)
    assert p.wait(0) == 0
    assert p.poll() == 0
