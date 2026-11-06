"""Input to an elevated window is reported, not silently lost (UIPI).

Live session 2026-11-05 follow-up: Windows drops SendInput aimed at a higher-integrity
window without an error; locum must say why instead of "succeeding" with no effect.
"""

from __future__ import annotations

import sys

import pytest

from locum.backends.windows import integrity
from locum.backends.windows._win32api import INPUT, INPUT_KEYBOARD, INPUT_MOUSE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_MOVE
from locum.backends.windows.input import _InputMixin
from locum.errors import PermissionDenied


class FakeGuard:
    def __init__(self, levels):
        self.levels = levels  # hwnd -> (own, target) or None

    def block(self, hwnd, pid):
        return self.levels.get(hwnd)


class FakeApi:
    def GetForegroundWindow(self):
        return 100

    def GetCursorPos(self, ref):
        return True

    def WindowFromPoint(self, pt):
        return 200

    def GetAncestor(self, h, flag):
        return h


class Dummy(_InputMixin):
    def __init__(self, levels):
        self._api = FakeApi()
        self._uipi = FakeGuard(levels)

    def _pid(self, hwnd):
        return hwnd + 1

    def _process_name(self, pid):
        return "regedit.exe"


def key_input():
    i = INPUT()
    i.type = INPUT_KEYBOARD
    return i


def mouse_input(flags):
    i = INPUT()
    i.type = INPUT_MOUSE
    i.u.mi.dwFlags = flags
    return i


def test_keyboard_to_an_elevated_foreground_window_raises_with_levels():
    d = Dummy({100: (0x2000, 0x3000)})
    with pytest.raises(PermissionDenied) as e:
        d._uipi_check([key_input()])
    msg = str(e.value)
    assert "regedit.exe" in msg and "High integrity" in msg and "Medium" in msg and "UIPI" in msg


def test_clicks_check_the_window_under_the_cursor_and_moves_are_free():
    d = Dummy({200: (0x2000, 0x3000)})
    d._uipi_check([mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)])  # hover only: allowed
    with pytest.raises(PermissionDenied):
        d._uipi_check([mouse_input(0x0002)])  # LEFTDOWN


def test_same_or_lower_level_and_unreadable_levels_never_block():
    Dummy({})._uipi_check([key_input(), mouse_input(0x0002)])


def test_guard_compares_levels_and_fails_open():
    g = integrity.UipiGuard(api=object())
    g._own, g._own_read = 0x2000, True
    g.window_level = lambda h, p: 0x3000
    assert g.block(1, 2) == (0x2000, 0x3000)
    g.window_level = lambda h, p: 0x2000
    assert g.block(1, 2) is None
    g.window_level = lambda h, p: None
    assert g.block(1, 2) is None
    assert integrity.UipiGuard(api=object()).block(1, 2) is None  # no API: never blocks


def test_level_names():
    assert [integrity.level_name(r) for r in (0x4000, 0x3000, 0x2100, 0x2000, 0x1000, 0)] == \
        ["System", "High", "Medium", "Medium", "Low", "Untrusted"]


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 token query")
def test_real_own_integrity_is_readable():
    from locum.backends.windows._win32api import _win32

    assert integrity.own_integrity(_win32()) is not None
