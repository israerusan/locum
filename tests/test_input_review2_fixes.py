"""Regression tests for keyboard / hotkey / fail-safe fixes (review round 2)."""

from __future__ import annotations

import pytest

from pypilot import Desktop
from pypilot import failsafe as fs_mod
from pypilot.backends.mock import MockBackend
from pypilot.errors import BackendError
from pypilot.failsafe import FailSafe, reset_abort
from pypilot.keyboard import Keyboard
from pypilot.keys import normalize_key, parse_hotkey
from pypilot.types import Point, Rect


@pytest.fixture(autouse=True)
def _clean_abort():
    reset_abort()
    yield
    reset_abort()


def _keyev(be):
    return [e for e in be.events if e[0] in ("key_down", "key_up")]


def _form():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("Bank", (100, 100, 500, 300))
    paid = []
    btn = w.add_button("Release payment", (20, 200, 150, 30), on_click=lambda wd: paid.append(1))
    memo = w.add_edit("Memo", (20, 50, 200, 30))
    return be, btn, memo, paid


# --------------------------------------------------------------- \n / \r / \t
@pytest.mark.parametrize("raw, key", [("\n", "enter"), ("\r", "enter"), ("\t", "tab"), (" ", "space"), ("   ", "space")])
def test_whitespace_keys_are_not_space(raw, key):
    assert parse_hotkey(raw) == [key]


def test_whitespace_inside_combos():
    assert parse_hotkey("ctrl+\n") == ["ctrl", "enter"]
    assert parse_hotkey("alt+\t") == ["alt", "tab"]
    assert parse_hotkey("ctrl+ ") == ["ctrl", "space"]
    assert parse_hotkey("ctrl++") == ["ctrl", "+"]
    with pytest.raises(ValueError):
        parse_hotkey("")
    with pytest.raises(ValueError):
        parse_hotkey("\r\n")


def test_press_tab_moves_focus_instead_of_activating_button():
    be, btn, memo, paid = _form()
    d = Desktop(be, failsafe=False)
    be._set_focus(btn)
    d.press("\t")
    assert _keyev(be)[-2:] == [("key_down", "tab"), ("key_up", "tab")]
    assert paid == []
    d.keyboard.hotkey("ctrl", "\n")
    assert _keyev(be)[-4:] == [("key_down", "ctrl"), ("key_down", "enter"), ("key_up", "enter"), ("key_up", "ctrl")]


# --------------------------------------------------------------- Ctrl+S is not Ctrl+Shift+S
@pytest.mark.parametrize("combo", ["Ctrl+S", "CTRL-S", "Ctrl S", ["Ctrl", "S"]])
def test_uppercase_letter_in_combo_does_not_add_shift(combo):
    assert parse_hotkey(combo) == ["ctrl", "s"]


def test_hotkey_with_uppercase_letter_args():
    be = MockBackend()
    kb = Keyboard(be, sleep=lambda s: None)
    kb.hotkey("ctrl", "W")
    kb.press("Ctrl+W")
    downs = [e[1] for e in _keyev(be) if e[0] == "key_down"]
    assert downs == ["ctrl", "w", "ctrl", "w"]
    assert parse_hotkey("ctrl+shift+S") == ["ctrl", "shift", "s"]
    # a lone letter keeps its case (press("A") types "A")
    assert parse_hotkey("A") == ["A"]


# --------------------------------------------------------------- characters go to the layout-aware backend
def test_press_and_keys_mode_pass_characters_to_backend():
    be, _btn, memo, _paid = _form()
    be._set_focus(memo)
    kb = Keyboard(be, sleep=lambda s: None)
    kb.press("@", "#", "A")
    kb.type("É@", mode="keys")
    downs = [e[1] for e in _keyev(be) if e[0] == "key_down"]
    assert downs == ["@", "#", "A", "É", "@"]  # no US-layout shift+2 / shift+3 synthesis
    assert memo.value == "@#AÉ@"


# --------------------------------------------------------------- caret
def test_caret_is_a_character():
    assert normalize_key("^") == "^"
    assert parse_hotkey("^") == ["^"]
    assert normalize_key("⌃") == "ctrl"
    be, _btn, memo, _paid = _form()
    be._set_focus(memo)
    kb = Keyboard(be, sleep=lambda s: None)
    kb.press("2", "^", "3")
    assert memo.value == "2^3"


# --------------------------------------------------------------- ctrl+minus
def test_ctrl_minus_dash_form():
    assert parse_hotkey("ctrl--") == ["ctrl", "-"]
    assert parse_hotkey("alt--") == ["alt", "-"]
    assert parse_hotkey("Ctrl-Shift--") == ["ctrl", "shift", "-"]
    assert parse_hotkey("ctrl+-") == ["ctrl", "-"]
    assert parse_hotkey("-") == ["-"]
    for bad in ("shift-", "ctrl-", "ctrl_", "-ctrl"):
        with pytest.raises(ValueError):
            parse_hotkey(bad)
    with pytest.raises(ValueError):
        normalize_key("shift-")
    be = MockBackend()
    Keyboard(be, sleep=lambda s: None).hotkey("ctrl--")
    assert _keyev(be) == [("key_down", "ctrl"), ("key_down", "-"), ("key_up", "-"), ("key_up", "ctrl")]


# --------------------------------------------------------------- kill hotkey
@pytest.mark.parametrize("combo", ["ctrl+shift+1", "ctrl+alt+shift+2", "ctrl+shift+-", "altgr+q"])
def test_kill_hotkey_rejects_combos_that_change_the_character(combo):
    with pytest.raises(ValueError):
        fs_mod._pynput_combo(combo)
    with pytest.raises(ValueError):
        fs_mod.install_kill_hotkey(combo)


def test_kill_hotkey_rejects_vt_switch_on_linux(monkeypatch):
    monkeypatch.setattr(fs_mod.sys, "platform", "linux")
    with pytest.raises(ValueError):
        fs_mod._pynput_combo("ctrl+alt+f12")
    assert fs_mod._pynput_combo("ctrl+f12") == "<ctrl>+<f12>"
    assert fs_mod._pynput_combo("ctrl+alt+shift+q") == "<ctrl>+<alt>+<shift>+q"
    assert fs_mod._pynput_combo("ctrl+shift+X") == "<ctrl>+<shift>+x"
    assert fs_mod._pynput_combo("ctrl+alt+pause") == "<ctrl>+<alt>+<pause>"
    monkeypatch.setattr(fs_mod.sys, "platform", "win32")
    assert fs_mod._pynput_combo("ctrl+alt+f12") == "<ctrl>+<alt>+<f12>"


# --------------------------------------------------------------- fail-safe corners
def test_default_corners_are_reachable_on_uneven_monitors():
    be = MockBackend(monitors=[(0, 0, 1920, 1080), (1920, -360, 2560, 1440)])
    corners = FailSafe(be).corners
    rects = [m.rect for m in be.monitors()]
    for c in corners:
        assert any(r.left <= c.x < r.right and r.top <= c.y < r.bottom for r in rects), c
    # the bounding-box corner (0, -360) is on no monitor and must not be used
    assert Point(0, -360) not in corners
    assert set(corners) == {Point(0, 0), Point(0, 1079), Point(1920, -360), Point(4479, -360), Point(4479, 1079)}
    # the shared edge between the monitors is not a trigger
    assert Point(1919, 0) not in corners and Point(1920, 1079) not in corners


def test_default_corners_side_by_side_and_fallback():
    be = MockBackend(monitors=[(0, 0, 1920, 1080), (-1280, 0, 1280, 1024)])
    corners = FailSafe(be).corners
    assert Point(-1280, 0) in corners and Point(1919, 1079) in corners
    assert Point(0, 0) not in corners and Point(-1, 0) not in corners  # pass-through points

    class NoMonitors(MockBackend):
        broken = False

        def monitors(self):
            if self.broken:
                raise BackendError("no monitor info")
            return super().monitors()

    nm = NoMonitors(width=800, height=600)
    nm.broken = True
    nm.virtual_screen = lambda: Rect(0, 0, 800, 600)  # type: ignore[method-assign]
    assert FailSafe(nm).corners == [Point(0, 0), Point(799, 0), Point(0, 599), Point(799, 599)]


# --------------------------------------------------------------- release bookkeeping
class _FlakyUp(MockBackend):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fail_up = set()

    def key_up(self, key):
        if key in self.fail_up:
            self.fail_up.discard(key)
            raise BackendError("SendInput failed (transient)")
        super().key_up(key)


def test_failed_release_is_retried_by_release_all():
    be = _FlakyUp()
    kb = Keyboard(be, sleep=lambda s: None)
    kb.down("shift")
    be.fail_up.add("shift")
    with pytest.raises(BackendError):
        kb.up("shift")
    assert kb.pressed == ["shift"] and be.pressed_keys == {"shift"}
    assert kb.release_all() == ["shift"]
    assert kb.pressed == [] and be.pressed_keys == set()


def test_hotkey_release_failure_keeps_key_and_release_all_raises():
    be = _FlakyUp()
    kb = Keyboard(be, sleep=lambda s: None)
    be.fail_up.add("c")
    with pytest.raises(BackendError):
        kb.hotkey("ctrl+c")
    assert kb.pressed == ["c"] and be.pressed_keys == {"c"}  # ctrl was still released
    kb.down("alt")
    be.fail_up.add("alt")
    with pytest.raises(BackendError):
        kb.release_all()  # error reported, but every key was attempted
    assert kb.pressed == ["alt"] and be.pressed_keys == {"alt"}
    assert kb.release_all() == ["alt"]
    assert be.pressed_keys == set()
