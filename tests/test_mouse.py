from __future__ import annotations

import pytest

from locum.backends.mock import MockBackend
from locum.errors import FailSafeTriggered
from locum.failsafe import FailSafe, abort, reset_abort
from locum.motion import Humanize
from locum.mouse import Mouse
from locum.types import MouseButton, Point, Rect


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += max(0.0, s)

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _clean_abort():
    reset_abort()
    yield
    reset_abort()


@pytest.fixture
def be():
    return MockBackend(width=1280, height=800)


@pytest.fixture
def mouse(be):
    return Mouse(be, FailSafe(be), sleep=lambda s: None)


def moves(be):
    return [e for e in be.events if e[0] == "move"]


def test_position_and_instant_move(be, mouse):
    assert mouse.position() == Point(640, 400)
    assert mouse.move(100, 200) == Point(100, 200)
    assert be.events == [("move", 100, 200)]
    assert mouse.move((300, 301)) == Point(300, 301)
    assert mouse.move(Rect(0, 0, 50, 50)) == Point(25, 25)
    assert mouse.position() == Point(25, 25)


def test_move_same_point_emits_nothing(be, mouse):
    mouse.move(640, 400)
    assert be.events == []


def test_timed_move_tween(be):
    clock = FakeClock()
    m = Mouse(be, sleep=clock.sleep, clock=clock, rate=100)
    m.move(740, 400, duration=0.5, tween="linear")
    mv = moves(be)
    assert mv[-1] == ("move", 740, 400)
    assert len(mv) == 50
    assert clock.now == pytest.approx(0.5)
    xs = [e[1] for e in mv]
    assert xs == sorted(xs)


def test_humanized_move_uses_fitts_and_is_deterministic():
    results = []
    for _ in range(2):
        be = MockBackend(width=1280, height=800)
        clock = FakeClock()
        m = Mouse(be, sleep=clock.sleep, clock=clock, humanize=Humanize(seed=5))
        m.move(100, 100)
        results.append((list(be.events), clock.now))
    assert results[0] == results[1]
    events, elapsed = results[0]
    assert events[-1] == ("move", 100, 100)
    assert len(events) > 10
    assert 0.08 <= elapsed <= 1.2


def test_humanize_bool_overrides(be):
    m = Mouse(be, sleep=lambda s: None, humanize=Humanize(seed=1))
    m.move(100, 100, humanize=False)
    assert be.events == [("move", 100, 100)]
    be.clear_events()
    m2 = Mouse(be, sleep=lambda s: None)
    m2.move(900, 700, humanize=True)
    assert len(moves(be)) > 5 and be.events[-1] == ("move", 900, 700)
    with pytest.raises(TypeError):
        m2.move(1, 1, humanize="yes")


def test_move_rel(be, mouse):
    mouse.move(100, 100)
    assert mouse.move_rel(10, -20) == Point(110, 80)
    assert mouse.position() == Point(110, 80)


def test_click_presses_widget(be, mouse):
    w = be.add_window("T", (100, 100, 400, 300))
    hits = []
    b = w.add_button("OK", (10, 10, 80, 30), on_click=lambda wd: hits.append(1))
    pt = mouse.click(b.screen_rect.center)
    assert pt == b.screen_rect.center
    assert hits == [1] and b.clicks == 1
    assert be.events[-2:] == [("down", "left", pt.x, pt.y), ("up", "left", pt.x, pt.y)]


def test_multi_click_same_spot(be, mouse):
    mouse.move(10, 10)
    be.clear_events()
    mouse.click(200, 200, clicks=3, interval=0.1)
    assert [e[0] for e in be.events] == ["move", "down", "up", "down", "up", "down", "up"]
    assert all(e[-2:] == (200, 200) for e in be.events)


def test_click_current_position_and_variants(be, mouse):
    mouse.move(50, 60)
    be.clear_events()
    mouse.click()
    mouse.double_click()
    mouse.triple_click()
    mouse.right_click()
    mouse.middle_click()
    assert not moves(be)
    downs = [e[1] for e in be.events if e[0] == "down"]
    assert downs == ["left"] * 6 + ["right", "middle"]


def test_click_interval_sleeps():
    be = MockBackend()
    sleeps = []
    m = Mouse(be, sleep=sleeps.append)
    m.click(5, 5, clicks=2, interval=0.25)
    assert sleeps == [0.25]
    with pytest.raises(ValueError):
        m.click(clicks=-1)


def test_humanized_click_holds_button_briefly(be):
    sleeps = []
    m = Mouse(be, sleep=sleeps.append, humanize=Humanize(seed=1))
    m.click(300, 300)
    assert be.events[-1][0] == "up" and be.events[-1][2:] == (300, 300)
    assert any(0.03 <= s <= 0.08 for s in sleeps)


def test_down_up(be, mouse):
    mouse.down("right")
    assert MouseButton.RIGHT in be.pressed_buttons
    mouse.up("secondary")
    assert not be.pressed_buttons


def test_drag(be, mouse):
    mouse.drag((100, 100), (300, 200), duration=0.2)
    kinds = [e[0] for e in be.events]
    assert kinds[0] == "move" and be.events[0] == ("move", 100, 100)
    assert kinds.count("down") == 1 and kinds[-1] == "up"
    down_i = kinds.index("down")
    assert be.events[down_i][2:] == (100, 100)
    assert len(moves(be)) > 5  # intermediate motion while held
    assert be.events[-1] == ("up", "left", 300, 200)
    assert not be.pressed_buttons


def test_drag_from_current_position(be, mouse):
    mouse.move(10, 10)
    be.clear_events()
    mouse.drag(None, (20, 20), duration=0, button="right")
    assert be.events == [("down", "right", 10, 10), ("move", 20, 20), ("up", "right", 20, 20)]


def test_drag_releases_on_failsafe(be):
    """A user yanking the mouse into a corner mid-drag aborts AND releases the button."""
    fs = FailSafe(be)
    clock = FakeClock()
    calls = {"n": 0}

    def sleep(s):
        clock.sleep(s)
        calls["n"] += 1
        if calls["n"] == 5:
            be.mouse_move(0, 0)  # the human intervenes

    m = Mouse(be, fs, sleep=sleep, clock=clock)
    with pytest.raises(FailSafeTriggered):
        m.drag((100, 100), (900, 600), duration=0.5)
    assert not be.pressed_buttons
    assert be.events[-1][0] == "up"


def test_drag_releases_on_abort(be):
    clock = FakeClock()
    n = {"i": 0}

    def sleep(s):
        clock.sleep(s)
        n["i"] += 1
        if n["i"] == 3:
            abort("test")

    m = Mouse(be, FailSafe(be), sleep=sleep, clock=clock)
    with pytest.raises(FailSafeTriggered):
        m.drag((100, 100), (900, 600), duration=0.5)
    assert not be.pressed_buttons


def test_failsafe_before_move(be, mouse):
    be.mouse_move(0, 0)
    be.clear_events()
    with pytest.raises(FailSafeTriggered):
        mouse.move(100, 100)
    with pytest.raises(FailSafeTriggered):
        mouse.click()
    with pytest.raises(FailSafeTriggered):
        mouse.down()
    mouse.up()  # releasing is always allowed
    assert [e[0] for e in be.events] == ["up"]


def test_path_through_corner_does_not_self_trigger(be, mouse):
    # moving *to* a corner (the program's own motion) succeeds
    mouse.move(0, 0, duration=0.1)
    assert mouse.position() == Point(0, 0)
    # ...but the next action starting from a corner trips, like PyAutoGUI
    with pytest.raises(FailSafeTriggered):
        mouse.move(10, 10)


def test_scroll(be, mouse):
    mouse.scroll(3)
    assert be.events[-1] == ("scroll", 0, 3, 640, 400)
    mouse.scroll(-2, dx=1, at=(200, 250))
    assert be.events[-2:] == [("move", 200, 250), ("scroll", 1, -2, 200, 250)]
    n = len(be.events)
    mouse.scroll(0)
    assert len(be.events) == n


def test_pause_after_actions(be):
    sleeps = []
    m = Mouse(be, pause=0.1, sleep=sleeps.append)
    m.move(1, 1)
    m.click()
    m.scroll(1)
    assert sleeps == [0.1, 0.1, 0.1]
