"""A call that was stopped (MCP timeout / client cancel) must not send input afterwards.

Cancellation used to be checked only in tree walks and waits: a tool call that
timed out could still click, the model was told the call "was stopped" and
retried, and both clicks ran. Every synthesized input and state-changing native
action now checks the cancel scope right before it reaches the backend; releasing
a held button or key is never blocked.
"""

from __future__ import annotations

import pytest

from locum._cancel import CallCancelled, CancelScope
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.keyboard import Keyboard
from locum.mouse import Mouse
from locum.types import MouseButton


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


INPUT = ("move", "down", "up", "scroll", "key_down", "key_up", "type", "a11y_invoke", "a11y_set_value",
         "activate", "window_state", "close", "move_window", "launch")


def inputs(be):
    return [e for e in be.events if e[0] in INPUT]


@pytest.fixture
def be():
    return MockBackend(width=800, height=600)


# ----------------------------------------------------------------------- mouse
def test_expired_scope_blocks_a_click(be):
    clock = Clock()
    m = Mouse(be, sleep=lambda s: None)
    with CancelScope(5.0, clock=clock):
        clock.now = 6.0  # the call ran past its limit (e.g. a slow find before the click)
        with pytest.raises(CallCancelled):
            m.click(100, 100)
    assert inputs(be) == []


def test_cancel_mid_call_stops_the_next_click(be):
    m = Mouse(be, sleep=lambda s: None)
    scope = CancelScope()
    with scope:
        m.click(100, 100)
        scope.cancel("timed out")  # the server answered "was stopped" here
        with pytest.raises(CallCancelled):
            m.click(200, 200)
    assert [e[0] for e in be.events if e[0] in ("down", "up")] == ["down", "up"]
    assert be.mouse_position() == (100, 100)


def test_cancel_between_multi_clicks_stops_further_presses(be):
    scope = CancelScope()
    m = Mouse(be, sleep=lambda s: scope.cancel("timed out"))
    with scope, pytest.raises(CallCancelled):
        m.click(100, 100, clicks=3, interval=0.1)
    assert [e[0] for e in be.events if e[0] in ("down", "up")] == ["down", "up"]
    assert not be.pressed_buttons


def test_cancelled_scope_blocks_moves_downs_and_scrolls(be):
    m = Mouse(be, sleep=lambda s: None)
    scope = CancelScope()
    scope.cancel()
    with scope:
        for action in (lambda: m.move(50, 50), lambda: m.down(), lambda: m.scroll(3),
                       lambda: m.drag((10, 10), (300, 300), duration=0)):
            with pytest.raises(CallCancelled):
                action()
        assert m.pressed == []
    assert inputs(be) == []


def test_drag_cancelled_midway_still_releases_the_button(be):
    clock = Clock()
    scope = CancelScope(1.0, clock=clock)

    def sleep(s: float) -> None:
        clock.now += s
        if clock.now > 0.3:  # partway through the move: the deadline passes
            clock.now = 5.0

    m = Mouse(be, sleep=sleep, clock=clock)
    with scope, pytest.raises(CallCancelled):
        m.drag((10, 10), (400, 400), duration=1.0)
    kinds = [e[0] for e in be.events]
    assert "down" in kinds and kinds[-1] == "up"  # released after the cancel
    assert not be.pressed_buttons
    assert be.mouse_position() != (400, 400)  # the move really stopped midway
    assert kinds.index("down") < len(kinds) - 1


def test_release_is_never_blocked_by_cancellation(be):
    m = Mouse(be, sleep=lambda s: None)
    kb = Keyboard(be, sleep=lambda s: None)
    m.down()
    kb.down("shift")
    scope = CancelScope()
    scope.cancel()
    with scope:
        m.up()
        kb.up("shift")
        assert m.release_all() == [] and kb.release_all() == []
    assert not be.pressed_buttons and not be.pressed_keys


# -------------------------------------------------------------------- keyboard
def test_cancelled_scope_blocks_keys_and_typing(be):
    kb = Keyboard(be, sleep=lambda s: None)
    scope = CancelScope()
    scope.cancel()
    with scope:
        for action in (lambda: kb.press("a"), lambda: kb.down("ctrl"), lambda: kb.hotkey("ctrl+s"),
                       lambda: kb.type("hello", mode="unicode"), lambda: kb.type("hi", mode="keys")):
            with pytest.raises(CallCancelled):
                action()
    assert inputs(be) == []
    assert not be.pressed_keys


def test_hotkey_cancelled_between_keys_releases_the_held_modifier(be):
    scope = CancelScope()
    kb = Keyboard(be, sleep=lambda s: scope.cancel("timed out"))
    with scope, pytest.raises(CallCancelled):
        kb.hotkey("ctrl", "shift", "s", interval=0.1)
    assert [e for e in be.events if e[0] in ("key_down", "key_up")] == [("key_down", "ctrl"), ("key_up", "ctrl")]
    assert not be.pressed_keys and kb.pressed == []


def test_typing_cancelled_midway_stops_the_rest(be):
    scope = CancelScope()
    n = [0]

    def sleep(s: float) -> None:
        n[0] += 1
        if n[0] == 2:
            scope.cancel("timed out")

    kb = Keyboard(be, sleep=sleep)
    with scope, pytest.raises(CallCancelled):
        kb.type("abcdef", mode="keys", interval=0.01)
    typed = [e[1] for e in be.events if e[0] == "key_down"]
    assert typed == ["a", "b"]
    assert not be.pressed_keys


# ------------------------------------------------- native element / window actions
@pytest.fixture
def desk(be):
    win = be.add_window("Form", (100, 100, 500, 400), app="form.exe")
    win.add_button("OK", (20, 340, 80, 30), automation_id="ok")
    win.add_edit("Name", (20, 40, 200, 28), automation_id="name")
    win.add_checkbox("Agree", (20, 80, 120, 24), automation_id="agree")
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return d


def _cancelled() -> CancelScope:
    s = CancelScope()
    s.cancel("timed out")
    return s


@pytest.mark.parametrize("action", ["invoke", "click_native", "toggle", "check", "set_text_native", "focus",
                                    "select", "expand", "collapse", "click_mouse"])
def test_cancelled_scope_blocks_native_element_actions(be, desk, action):
    ok, name, agree = desk.find("#ok", timeout=0), desk.find("#name", timeout=0), desk.find("#agree", timeout=0)
    be.clear_events()
    before = (agree.is_checked, name.value, be.focused_widget)
    run = {
        "invoke": lambda: ok.invoke(),
        "click_native": lambda: ok.click(native=True),
        "toggle": lambda: agree.toggle(),
        "check": lambda: agree.check(),
        "set_text_native": lambda: name.set_text("x", native=True),
        "focus": lambda: name.focus(),
        "select": lambda: ok.select(),
        "expand": lambda: ok.expand(),
        "collapse": lambda: ok.collapse(),
        "click_mouse": lambda: ok.click(native=False),
    }[action]
    with _cancelled(), pytest.raises(CallCancelled):
        run()
    assert inputs(be) == []
    assert (agree.is_checked, name.value, be.focused_widget) == before


@pytest.mark.parametrize("action", ["activate", "minimize", "maximize", "restore", "close", "move", "resize"])
def test_cancelled_scope_blocks_window_actions(be, desk, action):
    w = desk.window("Form", timeout=0)
    be.clear_events()
    run = {
        "activate": lambda: w.activate(),
        "minimize": lambda: w.minimize(),
        "maximize": lambda: w.maximize(),
        "restore": lambda: w.restore(),
        "close": lambda: w.close(),
        "move": lambda: w.move(0, 0),
        "resize": lambda: w.resize(300, 200),
    }[action]
    with _cancelled(), pytest.raises(CallCancelled):
        run()
    assert inputs(be) == []
    assert w.exists


def test_cancelled_scope_blocks_launch(be, desk):
    with _cancelled(), pytest.raises(CallCancelled):
        desk.launch("notepad.exe")
    assert be.launched == []


def test_desktop_click_and_type_blocked_after_cancel(be, desk):
    with _cancelled():
        with pytest.raises(CallCancelled):
            desk.click(150, 150)
        with pytest.raises(CallCancelled):
            desk.type("secret")
        with pytest.raises(CallCancelled):
            desk.hotkey("ctrl+w")
    assert inputs(be) == []


def test_no_scope_means_no_change(be, desk):
    m = Mouse(be, sleep=lambda s: None)
    m.click(100, 100)
    desk.find("#ok", timeout=0).invoke()
    assert ("a11y_invoke", "OK") in be.events
    assert MouseButton.LEFT not in be.pressed_buttons
