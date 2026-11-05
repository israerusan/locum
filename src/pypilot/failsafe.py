"""Emergency stops: fail-safe screen corners, a global abort flag and a kill hotkey.

Like PyAutoGUI, slamming the mouse into a corner of the screen aborts the
automation with :class:`~pypilot.errors.FailSafeTriggered`. pypilot adds a
process-wide *abort* flag that any thread (a GUI "Stop" button, a signal
handler, an LLM safety monitor, the optional global kill hotkey) can set with
:func:`abort`; every input action checks it.

The abort flag is sticky: once set, every subsequent :meth:`FailSafe.check`
raises until :func:`reset_abort` is called. It is honoured even when the
corner check is disabled (``FailSafe.enabled = False``), because it is an
explicit request to stop.
"""

from __future__ import annotations

import threading
from typing import Any, List, Optional, Sequence

from .errors import FailSafeTriggered, PypilotError
from .types import Point, PointLike, coerce_point

_abort_event = threading.Event()
_abort_reason = ""
_state_lock = threading.Lock()
_hotkey_listener: Any = None


def abort(reason: str = "") -> None:
    """Request that all running automation stops at its next fail-safe check."""
    global _abort_reason
    with _state_lock:
        _abort_reason = reason or ""
        _abort_event.set()


def reset_abort() -> None:
    """Clear the global abort flag so automation can run again."""
    global _abort_reason
    with _state_lock:
        _abort_reason = ""
        _abort_event.clear()


def is_aborted() -> bool:
    """Whether :func:`abort` was called (and not yet reset)."""
    return _abort_event.is_set()


def abort_reason() -> str:
    """The reason passed to the last :func:`abort` call ("" if none / not aborted)."""
    return _abort_reason if _abort_event.is_set() else ""


def _pynput_combo(combo: str) -> str:
    """Convert ``"ctrl+alt+shift+q"`` to pynput's ``"<ctrl>+<alt>+<shift>+q"`` format."""
    from .keys import parse_hotkey

    names = {
        "ctrl": "<ctrl>", "ctrl_r": "<ctrl_r>", "shift": "<shift>", "shift_r": "<shift_r>",
        "alt": "<alt>", "alt_r": "<alt_r>", "altgr": "<alt_gr>", "cmd": "<cmd>", "cmd_r": "<cmd_r>",
        "enter": "<enter>", "escape": "<esc>", "space": "<space>", "tab": "<tab>", "backspace": "<backspace>",
        "delete": "<delete>", "pause": "<pause>", "home": "<home>", "end": "<end>",
        "pageup": "<page_up>", "pagedown": "<page_down>", "insert": "<insert>",
        "up": "<up>", "down": "<down>", "left": "<left>", "right": "<right>",
        "printscreen": "<print_screen>", "scrolllock": "<scroll_lock>",
    }
    parts = []
    for key in parse_hotkey(combo):
        if key in names:
            parts.append(names[key])
        elif key.startswith("f") and key[1:].isdigit():
            parts.append(f"<{key}>")
        elif len(key) == 1:
            parts.append(key.lower())
        else:
            raise ValueError(f"Key {key!r} is not supported in a kill hotkey")
    return "+".join(parts)


def install_kill_hotkey(combo: str = "ctrl+alt+shift+q") -> bool:
    """Install a global hotkey that calls :func:`abort` when pressed.

    Requires the optional ``pynput`` package (``pip install pypilot[record]``)
    and a session where global keyboard hooks are allowed. Returns ``True`` if
    the hotkey is active, ``False`` if it could not be installed. Calling it
    again replaces the previously installed hotkey.
    """
    global _hotkey_listener
    hotkey = _pynput_combo(combo)  # ValueError for unusable combos, before touching pynput
    try:
        from pynput import keyboard as pkb  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        listener = pkb.GlobalHotKeys({hotkey: lambda: abort(f"kill hotkey {combo} pressed")})
        listener.daemon = True
        listener.start()
    except Exception:
        return False
    with _state_lock:
        old, _hotkey_listener = _hotkey_listener, listener
    if old is not None:
        try:
            old.stop()
        except Exception:
            pass
    return True


def uninstall_kill_hotkey() -> None:
    """Stop the global kill hotkey listener, if any."""
    global _hotkey_listener
    with _state_lock:
        old, _hotkey_listener = _hotkey_listener, None
    if old is not None:
        try:
            old.stop()
        except Exception:
            pass


class FailSafe:
    """Fail-safe guard checked by :class:`~pypilot.mouse.Mouse` and :class:`~pypilot.keyboard.Keyboard`.

    Args:
        backend: The backend whose cursor position is inspected.
        enabled: Whether the corner check is active (the abort flag always is).
        corners: Explicit trigger points. Default: the four corners of the virtual screen.
        margin: Distance in pixels (Chebyshev) around each corner that also triggers.
    """

    def __init__(self, backend: Any, enabled: bool = True, corners: Optional[List[PointLike]] = None, margin: int = 0):
        if margin < 0:
            raise ValueError("margin must be >= 0")
        self.backend = backend
        self.enabled = bool(enabled)
        self.margin = int(margin)
        self._explicit: Optional[List[Point]] = [coerce_point(c) for c in corners] if corners is not None else None
        self._cached: Optional[List[Point]] = None

    @property
    def corners(self) -> List[Point]:
        """The trigger points (computed from the virtual screen on first use unless given explicitly)."""
        if self._explicit is not None:
            return list(self._explicit)
        if self._cached is None:
            vs = self.backend.virtual_screen()
            self._cached = [
                Point(vs.left, vs.top),
                Point(vs.right - 1, vs.top),
                Point(vs.left, vs.bottom - 1),
                Point(vs.right - 1, vs.bottom - 1),
            ]
        return list(self._cached)

    @corners.setter
    def corners(self, value: Optional[Sequence[PointLike]]) -> None:
        self._explicit = [coerce_point(c) for c in value] if value is not None else None
        self._cached = None

    def refresh(self) -> None:
        """Forget cached default corners (call after the monitor layout changed)."""
        self._cached = None

    def in_corner(self, point: PointLike) -> bool:
        """Whether ``point`` lies within :attr:`margin` of a trigger corner."""
        p = coerce_point(point)
        m = self.margin
        return any(abs(p.x - c.x) <= m and abs(p.y - c.y) <= m for c in self.corners)

    def check(self, ignore: Optional[PointLike] = None) -> None:
        """Raise :class:`FailSafeTriggered` if aborted or if the cursor is in a corner.

        Args:
            ignore: If the cursor is exactly at this point the corner test is
                skipped (the abort flag is still checked). Used during moves so a
                path that itself passes through a corner does not trip the guard,
                while a user yanking the mouse into a corner still does.
        """
        if _abort_event.is_set():
            reason = abort_reason()
            raise FailSafeTriggered("Automation aborted" + (f": {reason}" if reason else ""))
        if not self.enabled:
            return
        try:
            pos = self.backend.mouse_position()
        except PypilotError:
            return  # cannot read the cursor; nothing to check
        if ignore is not None and tuple(pos) == tuple(coerce_point(ignore)):
            return
        if self.in_corner(pos):
            raise FailSafeTriggered(
                f"Fail-safe triggered: mouse moved to screen corner ({pos[0]}, {pos[1]}). "
                "Disable with Desktop(failsafe=False) / FailSafe.enabled = False (not recommended)."
            )

    def __repr__(self) -> str:
        return f"<FailSafe enabled={self.enabled} margin={self.margin}>"


__all__ = [
    "FailSafe",
    "abort",
    "reset_abort",
    "is_aborted",
    "abort_reason",
    "install_kill_hotkey",
    "uninstall_kill_hotkey",
]
