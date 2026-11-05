"""Quartz mouse-event helpers: multi-click counting (``ClickTracker``) and the button tables.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, Optional, Tuple

from ...types import MouseButton
from .constants import (
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventLeftMouseUp,
    kCGEventOtherMouseDown,
    kCGEventOtherMouseDragged,
    kCGEventOtherMouseUp,
    kCGEventRightMouseDown,
    kCGEventRightMouseDragged,
    kCGEventRightMouseUp,
)


# =============================================================================
# Multi-click tracking
# =============================================================================


class ClickTracker:
    """Computes the ``kCGMouseEventClickState`` value for synthetic clicks.

    macOS does not derive double/triple clicks from timing on its own for
    posted events: the click count must be written into every mouse-down and
    mouse-up event. A press continues a click sequence when it uses the same
    button, happens within ``interval`` seconds of the previous press and lies
    within ``max_distance`` (points) of it; otherwise the count restarts at 1.
    """

    def __init__(self, interval: float = 0.5, max_distance: float = 4.0,
                 clock: Callable[[], float] = time.monotonic):
        self.interval = float(interval)
        self.max_distance = float(max_distance)
        self.clock = clock
        self._last_button: Optional[str] = None
        self._last_time: Optional[float] = None
        self._last_pos: Tuple[float, float] = (0.0, 0.0)
        self._count = 0
        self._down_counts: Dict[str, int] = {}

    def press(self, button: str, x: float, y: float, now: Optional[float] = None) -> int:
        """Register a button press at ``(x, y)`` and return its click count (>= 1)."""
        now = self.clock() if now is None else now
        same = (
            self._last_button == button
            and self._last_time is not None
            and 0 <= now - self._last_time <= self.interval
            and math.hypot(x - self._last_pos[0], y - self._last_pos[1]) <= self.max_distance
        )
        self._count = self._count + 1 if same else 1
        self._last_button = button
        self._last_time = now
        self._last_pos = (x, y)
        self._down_counts[button] = self._count
        return self._count

    def release(self, button: str) -> int:
        """Click count for the matching release (same as its press; 1 if unknown)."""
        return self._down_counts.pop(button, 1)

    def reset(self) -> None:
        self._last_button = None
        self._last_time = None
        self._count = 0
        self._down_counts.clear()


_BUTTON_EVENTS: Dict[MouseButton, Tuple[int, int, int, int]] = {
    # (down, up, dragged, CGMouseButton number)
    MouseButton.LEFT: (kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventLeftMouseDragged, 0),
    MouseButton.RIGHT: (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGEventRightMouseDragged, 1),
    MouseButton.MIDDLE: (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGEventOtherMouseDragged, 2),
    MouseButton.X1: (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGEventOtherMouseDragged, 3),
    MouseButton.X2: (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGEventOtherMouseDragged, 4),
}


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)
