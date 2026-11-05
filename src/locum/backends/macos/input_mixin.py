"""``MacOSBackend`` mouse and keyboard input (Quartz events).
"""

from __future__ import annotations

import threading
import warnings
from typing import Optional, Tuple

from ...errors import BackendError, NotSupported
from ...types import MouseButton, Point
from .constants import (
    FLAG_ALTERNATE,
    FLAG_COMMAND,
    FLAG_CONTROL,
    _INSTALL_HINT,
    _NS_SYSTEM_DEFINED,
    kCGEventFlagsChanged,
    kCGEventMouseMoved,
    kCGEventOtherMouseDown,
    kCGEventOtherMouseDragged,
    kCGEventOtherMouseUp,
    kCGHIDEventTap,
    kCGMouseEventButtonNumber,
    kCGMouseEventClickState,
    kCGMouseEventDeltaX,
    kCGMouseEventDeltaY,
    kCGScrollEventUnitLine,
)
from .keys import KeySpec, KeyboardLayoutMap, NAMED_KEYCODES, resolve_key, split_typing_segments, utf16_len
from .events import _BUTTON_EVENTS, _sign


class _InputMixin:
    # ----------------------------------------------------------------------- mouse
    def _cursor_points(self) -> Tuple[float, float]:
        ev = self._q.CGEventCreate(None)
        loc = self._q.CGEventGetLocation(ev)
        return (float(loc.x), float(loc.y))

    def _current_flags(self) -> int:
        flags = 0
        for f in self._held_mods.values():
            flags |= f
        return flags

    def _post_mouse(self, etype: int, pt: Tuple[float, float], cg_button: int, *,
                    click_state: Optional[int] = None, delta: Optional[Tuple[float, float]] = None) -> None:
        q = self._q
        ev = q.CGEventCreateMouseEvent(self._source, etype, pt, cg_button)
        if ev is None:
            raise BackendError("CGEventCreateMouseEvent failed")
        if click_state is not None:
            q.CGEventSetIntegerValueField(ev, kCGMouseEventClickState, int(click_state))
        if etype in (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGEventOtherMouseDragged):
            q.CGEventSetIntegerValueField(ev, kCGMouseEventButtonNumber, int(cg_button))
        if delta is not None:
            q.CGEventSetIntegerValueField(ev, kCGMouseEventDeltaX, int(round(delta[0])))
            q.CGEventSetIntegerValueField(ev, kCGMouseEventDeltaY, int(round(delta[1])))
        q.CGEventSetFlags(ev, self._current_flags())
        q.CGEventPost(kCGHIDEventTap, ev)

    def mouse_position(self) -> Point:
        x, y = self._cursor_points()
        return self._get_layout().point_to_pixel(x, y)

    def mouse_move(self, x: int, y: int) -> None:
        """Move the cursor to pixel ``(x, y)``. While a button is held a
        ``*MouseDragged`` event is posted so drag-and-drop works."""
        self._require_trust()
        pt = self._get_layout().pixel_to_point(x, y)
        with self._lock:
            try:
                old = self._cursor_points()
                delta: Optional[Tuple[float, float]] = (pt[0] - old[0], pt[1] - old[1])
            except Exception:
                delta = None
            if self._held_buttons:
                button = self._held_buttons[-1]
                _, _, drag_type, cg_button = _BUTTON_EVENTS[button]
                self._post_mouse(drag_type, pt, cg_button, delta=delta)
            else:
                self._post_mouse(kCGEventMouseMoved, pt, 0, delta=delta)

    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._require_trust()
        button = MouseButton.coerce(button)
        down_type, _, _, cg_button = _BUTTON_EVENTS[button]
        with self._lock:
            pt = self._cursor_points()
            count = self._clicks.press(button.value, pt[0], pt[1])
            self._post_mouse(down_type, pt, cg_button, click_state=count)
            if button in self._held_buttons:
                self._held_buttons.remove(button)
            self._held_buttons.append(button)

    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._require_trust()
        button = MouseButton.coerce(button)
        _, up_type, _, cg_button = _BUTTON_EVENTS[button]
        with self._lock:
            pt = self._cursor_points()
            count = self._clicks.release(button.value)
            self._post_mouse(up_type, pt, cg_button, click_state=count)
            if button in self._held_buttons:
                self._held_buttons.remove(button)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Scroll by wheel notches (line units). ``dy > 0`` scrolls up, ``dx > 0`` scrolls
        right (Quartz's horizontal axis is inverted: positive wheel2 means left)."""
        self._require_trust()
        dx, dy = int(dx), int(dy)
        q = self._q
        create2 = getattr(q, "CGEventCreateScrollWheelEvent2", None)
        with self._lock:
            steps = [(0, _sign(dy))] * abs(dy) + [(-_sign(dx), 0)] * abs(dx)
            for w2, w1 in steps:
                if create2 is not None:
                    ev = create2(self._source, kCGScrollEventUnitLine, 2, w1, w2, 0)
                else:
                    ev = q.CGEventCreateScrollWheelEvent(self._source, kCGScrollEventUnitLine, 2, w1, w2)
                if ev is None:
                    raise BackendError("CGEventCreateScrollWheelEvent failed")
                q.CGEventSetFlags(ev, self._current_flags())
                q.CGEventPost(kCGHIDEventTap, ev)

    # -------------------------------------------------------------------- keyboard
    #: Seconds between checks for an input-source change.
    KEYBOARD_LAYOUT_TTL = 0.5

    def _keyboard_map(self) -> KeyboardLayoutMap:
        """The active keyboard layout, re-read when the input source changes.

        The Text Input Sources API must be used from the main thread on recent
        macOS (it traps otherwise), so other threads use the last map read on the
        main thread (or US-ANSI if none was read yet; see :meth:`_layout_known`).
        """
        provider = self._kbd_provider
        if provider is None or threading.current_thread() is not threading.main_thread():
            return self._kbd_map
        now = self._clock()
        if self._kbd_checked is not None and now - self._kbd_checked < self.KEYBOARD_LAYOUT_TTL:
            return self._kbd_map
        self._kbd_checked = now
        try:
            fingerprint, fresh = provider.current(self._kbd_fingerprint)
        except Exception:
            return self._kbd_map
        if fresh is not None:
            self._kbd_map = fresh
            self._kbd_fingerprint = fingerprint
        return self._kbd_map

    def _layout_known(self) -> bool:
        """False while the live layout was never queried (backend only used off the
        main thread): the US-ANSI stand-in may then be wrong, and on AZERTY the US
        "a"/"z" keys are "q"/"w" (Cmd+A would quit the app, Cmd+Z close a window)."""
        return self._kbd_provider is None or self._kbd_checked is not None or self._kbd_fixed

    def _resolve(self, key: str) -> KeySpec:
        keymap = self._keyboard_map()
        flags = self._current_flags()
        modified = bool(flags & (FLAG_COMMAND | FLAG_CONTROL | FLAG_ALTERNATE))
        if (len(key) == 1 and key not in ("\n", "\r", "\t", "\b", "\x1b")
                and not self._layout_known()):
            if modified:
                raise BackendError(
                    f"Refusing to press {key!r} with Command/Control/Option: the keyboard layout "
                    "has not been read yet (macOS only allows that on the main thread), so the "
                    "key that types it is unknown and could trigger a different shortcut (on "
                    "AZERTY the US 'a' key is 'q'). Create the backend/Desktop on the main thread.")
            if not self._warned_layout:
                self._warned_layout = True
                warnings.warn(
                    "locum (macOS): the keyboard layout cannot be read off the main thread; "
                    "characters are injected as unicode text until the backend is used on the "
                    "main thread", RuntimeWarning, stacklevel=4)
            return KeySpec("unicode", 0, 0, key)
        return resolve_key(key, keymap, command=bool(flags & FLAG_COMMAND), modified=modified)

    def _post_key(self, keycode: int, down: bool, flags: int, *, flags_changed: bool = False,
                  text: Optional[str] = None) -> None:
        q = self._q
        ev = q.CGEventCreateKeyboardEvent(self._source, int(keycode), bool(down))
        if ev is None:
            raise BackendError("CGEventCreateKeyboardEvent failed")
        if text:
            q.CGEventKeyboardSetUnicodeString(ev, utf16_len(text), text)
        if flags_changed:
            q.CGEventSetType(ev, kCGEventFlagsChanged)
        q.CGEventSetFlags(ev, flags)
        q.CGEventPost(kCGHIDEventTap, ev)

    def _post_media(self, nx_key: int, down: bool) -> None:
        if self._appkit is None:
            raise NotSupported(f"Media keys need pyobjc-framework-Cocoa; {_INSTALL_HINT}")
        state = 0xA if down else 0xB
        ev = self._appkit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            _NS_SYSTEM_DEFINED, (0, 0), state << 8, 0, 0, None, 8, (nx_key << 16) | (state << 8), -1
        )
        self._q.CGEventPost(kCGHIDEventTap, ev.CGEvent())

    def key_down(self, key: str) -> None:
        self._require_trust()
        with self._lock:
            spec = self._resolve(key)
            if spec.kind == "modifier":
                self._held_mods[key] = spec.flags
                self._post_key(spec.keycode, True, self._current_flags(), flags_changed=True)
            elif spec.kind == "media":
                self._post_media(spec.keycode, True)
                self._pressed_keys[key] = spec
            else:
                text = spec.char if spec.kind == "unicode" else None
                self._post_key(spec.keycode, True, self._current_flags() | spec.flags, text=text)
                self._pressed_keys[key] = spec

    def key_up(self, key: str) -> None:
        self._require_trust()
        with self._lock:
            spec = self._pressed_keys.pop(key, None) or self._resolve(key)
            if spec.kind == "modifier":
                self._held_mods.pop(key, None)
                self._post_key(spec.keycode, False, self._current_flags(), flags_changed=True)
            elif spec.kind == "media":
                self._post_media(spec.keycode, False)
            else:
                text = spec.char if spec.kind == "unicode" else None
                self._post_key(spec.keycode, False, self._current_flags() | spec.flags, text=text)

    def type_text(self, text: str) -> None:
        """Inject ``text`` independent of the keyboard layout (chunks of <= 20 UTF-16
        units via ``CGEventKeyboardSetUnicodeString``; newlines/tabs as real keys).

        The unicode string decides what is typed; the event's nominal keycode is
        the key that types the chunk's first character on the active layout (0
        when the layout has no such key), so apps that peek at keycodes see a
        consistent key rather than the US position of "a".
        """
        self._require_trust()
        with self._lock:
            keymap = self._keyboard_map()
            for kind, payload in split_typing_segments(text):
                if kind == "key":
                    code = NAMED_KEYCODES[payload]
                    self._post_key(code, True, 0)
                    self._post_key(code, False, 0)
                else:
                    hit = keymap.lookup(payload[0])
                    code = hit[0] if hit is not None else 0
                    self._post_key(code, True, 0, text=payload)
                    self._post_key(code, False, 0, text=payload)
