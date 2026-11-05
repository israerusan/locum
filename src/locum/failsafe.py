"""Emergency stops: fail-safe screen corners, a global abort flag and a kill hotkey.

Like PyAutoGUI, slamming the mouse into a corner of the screen aborts the
automation with :class:`~locum.errors.FailSafeTriggered`. locum adds a
process-wide *abort* flag that any thread (a GUI "Stop" button, a signal
handler, an LLM safety monitor, the optional global kill hotkey) can set with
:func:`abort`; every input action checks it.

The abort flag is sticky: once set, every subsequent :meth:`FailSafe.check`
raises until :func:`reset_abort` is called. It is honoured even when the
corner check is disabled (``FailSafe.enabled = False``), because it is an
explicit request to stop.
"""

from __future__ import annotations

import math
import sys
import threading
import time
from typing import Any, Callable, List, Optional, Sequence

from .errors import FailSafeTriggered, LocumError
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


def _raise_if_aborted() -> None:
    if _abort_event.is_set():
        reason = abort_reason()
        raise FailSafeTriggered("Automation aborted" + (f": {reason}" if reason else ""))


#: Longest uninterrupted slice :func:`abortable_sleep` sleeps with a custom sleep function.
ABORT_POLL_INTERVAL = 0.1


def abortable_sleep(seconds: float, sleep: Callable[[float], Any] = time.sleep) -> bool:
    """Sleep ``seconds`` but wake up as soon as :func:`abort` is called. Returns :func:`is_aborted`.

    With the default ``time.sleep`` this waits on the abort event itself (the
    wake-up is immediate); a custom ``sleep`` function is called in slices of
    at most :data:`ABORT_POLL_INTERVAL` seconds with the flag checked between them.
    """
    seconds = float(seconds)
    if seconds <= 0 or _abort_event.is_set():
        return _abort_event.is_set()
    if sleep is time.sleep:
        return _abort_event.wait(seconds)
    slices = max(1, math.ceil(seconds / ABORT_POLL_INTERVAL - 1e-9))
    for _ in range(slices):  # equal slices: they add up to ``seconds``
        sleep(seconds / slices)
        if _abort_event.is_set():
            return True
    return False


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
    keys = parse_hotkey(combo)
    mods = set(keys)
    for key in keys:
        if len(key) != 1:
            continue
        # pynput matches the *character* a key event produces, and Shift/AltGr
        # change it ("shift+1" arrives as "!", "altgr+q" as "@" on German
        # layouts), so such a hotkey would be accepted but could never fire.
        if mods & {"altgr"} or (mods & {"shift", "shift_r"} and not key.isalpha()):
            raise ValueError(
                f"Kill hotkey {combo!r} cannot be detected reliably: Shift/AltGr change the character "
                f"of {key!r}; use a letter (e.g. 'ctrl+alt+shift+q') or a named key (e.g. 'ctrl+alt+pause')"
            )
    if (
        sys.platform.startswith("linux")
        and mods & {"ctrl", "ctrl_r"}
        and mods & {"alt", "alt_r"}
        and any(k.startswith("f") and k[1:].isdigit() for k in keys)
    ):
        raise ValueError(
            f"Kill hotkey {combo!r} cannot be detected on Linux: Ctrl+Alt+F<n> is the virtual-terminal switch "
            "(XF86Switch_VT_n); choose another combination"
        )
    parts = []
    for key in keys:
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

    Requires the optional ``pynput`` package (``pip install locum[record]``)
    and a session where global keyboard hooks are allowed. Returns ``True`` if
    the hotkey is active, ``False`` if it could not be installed or cannot
    receive key events (the listener died, e.g. an X server without the RECORD
    extension, or macOS without Accessibility/Input Monitoring permission).
    Calling it again replaces the previously installed hotkey.

    Raises ``ValueError`` for combinations that could never fire: unknown keys,
    Shift with a non-letter character (``ctrl+shift+1`` arrives as ``!``), AltGr
    with a character, and Ctrl+Alt+F<n> on Linux (the VT switch).
    """
    global _hotkey_listener
    hotkey = _pynput_combo(combo)  # ValueError for unusable combos, before touching pynput
    try:
        from pynput import keyboard as pkb  # type: ignore[import-not-found]
    except Exception:
        return False
    listener: Any = None
    try:
        listener = pkb.GlobalHotKeys({hotkey: lambda: abort(f"kill hotkey {combo} pressed")})
        listener.daemon = True
        listener.start()
        ok = _listener_works(listener)
    except Exception:
        ok = False
    if not ok:
        # No emergency stop must ever be reported when the hook cannot deliver events.
        # (pynput's stop() waits forever for a listener that never became ready.)
        if listener is not None and getattr(listener, "_ready", True):
            try:
                listener.stop()
            except Exception:
                pass
        return False
    with _state_lock:
        old, _hotkey_listener = _hotkey_listener, listener
    if old is not None:
        try:
            old.stop()
        except Exception:
            pass
    return True


def _listener_works(listener: Any, timeout: float = 2.0) -> bool:
    """Whether a started pynput listener really receives events.

    ``start()`` returns before the OS hook exists: the listener thread dies at once
    on an X server without the RECORD extension (common with VNC), and on macOS
    without Accessibility/Input Monitoring permission it gets no events
    (``IS_TRUSTED`` is False). pynput marks a listener ready once its hook is in place.
    """
    alive = getattr(listener, "is_alive", None)
    deadline = time.monotonic() + timeout
    while not getattr(listener, "_ready", True):
        if (alive is not None and not alive()) or time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    if alive is not None and not alive():
        return False
    return getattr(listener, "IS_TRUSTED", True) is not False


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
    """Fail-safe guard checked by :class:`~locum.mouse.Mouse` and :class:`~locum.keyboard.Keyboard`.

    Args:
        backend: The backend whose cursor position is inspected.
        enabled: Whether the corner check is active (the abort flag always is).
        corners: Explicit trigger points. Default: every monitor corner the cursor
            stops at when pushed outwards (the four corners of a single screen;
            corners shared with a neighbouring monitor are left out, and corners of
            the virtual-screen bounding box that lie on no monitor are never used).
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
        """The trigger points (computed from the monitor layout on first use unless given explicitly)."""
        if self._explicit is not None:
            return list(self._explicit)
        if self._cached is None:
            self._cached = self._default_corners()
        return list(self._cached)

    def _default_corners(self) -> List[Point]:
        rects: List[Any] = []
        try:
            rects = [m.rect for m in self.backend.monitors() if m.rect.width > 0 and m.rect.height > 0]
        except Exception:
            rects = []
        if not rects:
            vs = self.backend.virtual_screen()
            rects = [vs]

        def on_screen(x: int, y: int) -> bool:
            return any(r.left <= x < r.right and r.top <= y < r.bottom for r in rects)

        out: List[Point] = []
        for r in rects:
            for x, y, dx, dy in (
                (r.left, r.top, -1, -1),
                (r.right - 1, r.top, 1, -1),
                (r.left, r.bottom - 1, -1, 1),
                (r.right - 1, r.bottom - 1, 1, 1),
            ):
                # The cursor stops here only if it can go neither further
                # sideways nor further up/down (no neighbouring monitor).
                if on_screen(x + dx, y) or on_screen(x, y + dy):
                    continue
                p = Point(x, y)
                if p not in out:
                    out.append(p)
        return out

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
        except LocumError:
            return  # cannot read the cursor; nothing to check
        if ignore is not None and tuple(pos) == tuple(coerce_point(ignore)):
            return
        if self.in_corner(pos):
            raise FailSafeTriggered(
                f"Fail-safe triggered: mouse moved to screen corner ({pos[0]}, {pos[1]}). "
                "Move the mouse away from the corner to continue; every action is blocked while it "
                "stays there. Disable with Desktop(failsafe=False) / FailSafe.enabled = False (not recommended)."
            )

    def __repr__(self) -> str:
        return f"<FailSafe enabled={self.enabled} margin={self.margin}>"


__all__ = [
    "FailSafe",
    "abort",
    "reset_abort",
    "is_aborted",
    "abort_reason",
    "abortable_sleep",
    "install_kill_hotkey",
    "uninstall_kill_hotkey",
]
