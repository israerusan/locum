"""Regression tests for bug-hunt round 2 (input: mouse, keys, motion, screen)."""

import pytest
from PIL import Image

from locum.backends.mock import MockBackend
from locum.errors import FailSafeTriggered
from locum.failsafe import FailSafe
from locum.keyboard import Keyboard
from locum.keys import normalize_key, parse_hotkey
from locum.motion import Humanize, plan_path
from locum.mouse import Mouse
from locum.screen import Screen


# ---------------------------------------------------------------- drag at a corner
def _mouse(be):
    return Mouse(be, FailSafe(be), sleep=lambda s: None)


def test_drag_from_failsafe_corner_does_not_self_trigger():
    be = MockBackend(width=1280, height=800)
    m = _mouse(be)
    be.mouse_move(500, 500)
    be.clear_events()
    assert m.drag((0, 0), (300, 300), duration=0) == (300, 300)
    assert be.events == [("move", 0, 0), ("down", "left", 0, 0), ("move", 300, 300), ("up", "left", 300, 300)]


def test_drag_from_corner_with_motion_does_not_self_trigger():
    be = MockBackend(width=1280, height=800)
    m = _mouse(be)
    be.mouse_move(500, 500)
    m.drag((0, 0), (300, 300), duration=0.1)
    assert be.mouse_position() == (300, 300)


def test_drag_none_start_at_user_corner_still_trips():
    be = MockBackend(width=1280, height=800)
    m = _mouse(be)
    be.mouse_move(0, 0)  # the user parked the cursor in the corner
    be.clear_events()
    with pytest.raises(FailSafeTriggered):
        m.drag(None, (300, 300), duration=0)
    assert not any(e[0] == "down" for e in be.events)


# ------------------------------------------------------- incomplete '+' combos
@pytest.mark.parametrize("bad", ["ctrl+", "+ctrl", "ctrl+shift+", "shift+"])
def test_incomplete_plus_combo_rejected(bad):
    with pytest.raises(ValueError):
        parse_hotkey(bad)


def test_incomplete_plus_combo_sends_nothing():
    be = MockBackend()
    with pytest.raises(ValueError):
        Keyboard(be, sleep=lambda s: None).hotkey("ctrl+")
    assert not [e for e in be.events if e[0].startswith("key")]


@pytest.mark.parametrize(
    "spec, want",
    [
        ("ctrl++", ["ctrl", "+"]),
        ("ctrl+shift++", ["ctrl", "shift", "+"]),
        ("++", ["+"]),
        ("+", ["+"]),
        ("ctrl+plus", ["ctrl", "+"]),
        ("ctrl+ ", ["ctrl", "space"]),
        ("ctrl+\n", ["ctrl", "enter"]),
    ],
)
def test_plus_key_spellings_still_work(spec, want):
    assert parse_hotkey(spec) == want


# ---------------------------------------------------------- spaced key names
@pytest.mark.parametrize("name", ["Left Shift", "Right Ctrl", "Left Alt", "Right Shift", "Play Pause", "Back Space"])
def test_spaced_names_agree_with_normalize_key(name):
    assert parse_hotkey(name) == [normalize_key(name)]


def test_press_left_shift_sends_only_shift():
    be = MockBackend()
    Keyboard(be, sleep=lambda s: None).press("Left Shift")
    assert [e for e in be.events if e[0].startswith("key")] == [("key_down", "shift"), ("key_up", "shift")]


@pytest.mark.parametrize(
    "spec, want",
    [
        ("ctrl alt del", ["ctrl", "alt", "delete"]),
        ("shift tab", ["shift", "tab"]),
        ("shift left", ["shift", "left"]),  # a leading modifier stays a chord
        ("ctrl right", ["ctrl", "right"]),
        ("Page Down", ["pagedown"]),
    ],
)
def test_spaced_combos_still_split(spec, want):
    assert parse_hotkey(spec) == want


# ------------------------------------------------------------- plan_path start
@pytest.mark.parametrize("kw", [{}, {"tween": "ease_in_cubic"}, {"humanize": Humanize(seed=1)}])
def test_plan_path_excludes_start_point(kw):
    path = plan_path((0, 0), (100, 0), duration=1.0, **kw)
    assert path[0][0] != (0, 0)
    assert path[-1] == ((100, 0), 1.0)


def test_plan_path_short_move_keeps_end():
    assert plan_path((0, 0), (1, 0), duration=1.0)[-1] == ((1, 0), 1.0)


# ------------------------------------------------------------ Screen.save names
@pytest.mark.parametrize("name", ["shot_10.30.45", "invoice.v2"])
def test_save_non_image_suffix_is_png(tmp_path, name):
    s = Screen(MockBackend())
    out = s.save(tmp_path / name, region=(0, 0, 10, 10))
    with Image.open(out) as im:
        assert im.format == "PNG"


def test_save_known_extension_uses_it(tmp_path):
    s = Screen(MockBackend())
    out = s.save(tmp_path / "shot.bmp", region=(0, 0, 10, 10))
    with Image.open(out) as im:
        assert im.format == "BMP"
