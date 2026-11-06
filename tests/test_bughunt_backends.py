"""Regression tests for backend bugs found in the bug hunt (mock, macOS and X11 fakes only)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from locum.backends.mock import MockBackend
from locum.types import MouseButton, Rect

from test_macos_backend import FakeQuartz, _rec, _window_setup, make_backend


# ------------------------------------------------------------------ mock: windows
def test_mock_move_window_leaves_the_maximized_state():
    be = MockBackend()
    w = be.add_window("Doc", (100, 100, 400, 300))
    be.set_window_state(w.handle, "maximized")
    be.move_window(w.handle, Rect(50, 60, 500, 350))
    info = [i for i in be.list_windows() if i.handle == w.handle][0]
    assert info.is_maximized is False
    be.set_window_state(w.handle, "normal")
    assert w.rect == Rect(50, 60, 500, 350)  # the explicit move is kept


def test_mock_maximize_restore_still_round_trips():
    be = MockBackend()
    w = be.add_window("Doc", (100, 100, 400, 300))
    be.set_window_state(w.handle, "maximized")
    assert be.list_windows()[0].is_maximized is True
    be.set_window_state(w.handle, "maximized")
    be.set_window_state(w.handle, "normal")
    assert w.rect == Rect(100, 100, 400, 300) and w.maximized is False


def test_mock_maximizes_onto_the_windows_own_monitor():
    be = MockBackend(monitors=[(0, 0, 1920, 1080), (1920, 0, 1280, 1024)])
    w = be.add_window("Doc", (2000, 100, 600, 400))
    be.set_window_state(w.handle, "maximized")
    assert w.rect == Rect(1920, 0, 1280, 1024)
    be.set_window_state(w.handle, "normal")
    assert w.rect == Rect(2000, 100, 600, 400)
    # a window on the primary monitor still maximizes there
    p = be.add_window("Primary", (100, 100, 400, 300))
    be.set_window_state(p.handle, "maximized")
    assert p.rect == Rect(0, 0, 1920, 1080)


# ------------------------------------------------------------------ macOS: cursor lag
class LaggingQuartz(FakeQuartz):
    """WindowServer stand-in: posted mouse events are queued and applied in order later."""

    def __init__(self):
        super().__init__()
        self.queue = []

    def CGEventPost(self, tap, ev):
        self.posted.append(ev)
        if ev.kind == "mouse":
            self.queue.append(ev.pt)

    def apply_one(self):
        self.pos = self.queue.pop(0)


def test_mac_click_after_partially_applied_moves_goes_to_the_target():
    be, _, _ = make_backend()
    q = LaggingQuartz()
    be._q = q
    be.mouse_move(200, 100)  # A = (100, 50) pts
    be.mouse_move(400, 300)  # (200, 150) pts
    be.mouse_move(600, 400)  # B = (300, 200) pts, the target
    q.apply_one()  # the server has applied A only
    be.mouse_down(MouseButton.LEFT)
    assert q.posted[-1].pt == (300.0, 200.0)
    q.apply_one()  # now the second point
    be.mouse_up(MouseButton.LEFT)
    assert q.posted[-1].pt == (300.0, 200.0)


def test_mac_foreign_move_during_a_burst_still_wins():
    be, _, _ = make_backend()
    q = LaggingQuartz()
    be._q = q
    be.mouse_move(200, 100)
    be.mouse_move(600, 400)
    q.pos = (500.0, 500.0)  # the user moved the mouse somewhere off the posted path
    be.mouse_down(MouseButton.LEFT)
    assert q.posted[-1].pt == (500.0, 500.0)


# ------------------------------------------------------------------ macOS: windows
def test_mac_normal_after_maximize_and_move_keeps_the_move():
    be, q, appkit, ax, win = _window_setup()
    win.attrs["AXMinimized"] = False
    be.set_window_state(7, "maximized")
    q.windows = [_rec(7, 100, 0, 25, 1440, 875, "Doc", owner="TextEdit")]
    be.move_window(7, Rect(400, 300, 600, 400))
    q.windows = [_rec(7, 100, 200, 150, 300, 200, "Doc", owner="TextEdit")]
    be.set_window_state(7, "normal")
    assert win.attrs["AXPosition"] == (200.0, 150.0)
    assert win.attrs["AXSize"] == (300.0, 200.0)


# ------------------------------------------------------------------ X11: monitor order
def test_x11_monitors_are_ordered_left_to_right_after_the_primary():
    from locum.backends.x11 import X11Backend

    be = object.__new__(X11Backend)
    be._state = SimpleNamespace(desynced=False, closed=False)
    be._lock = threading.RLock()
    be._randr = True
    be._xft_scale = lambda: 1.0
    be._randr_monitors = lambda: [
        (Rect(1920, 0, 1920, 1080), True, "DP-1"),
        (Rect(3840, 0, 1920, 1080), False, "DP-2"),
        (Rect(0, 200, 1920, 1080), False, "HDMI-1"),
    ]
    mons = be.monitors()
    assert [m.name for m in mons] == ["DP-1", "HDMI-1", "DP-2"]
    assert [m.index for m in mons] == [0, 1, 2] and mons[0].is_primary
