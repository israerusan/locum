"""Mouse and keyboard input for the Windows backend: the :class:`_Held` key
record and the :class:`_InputMixin` (``SendInput`` mouse, wheel and keyboard).
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, List, Sequence, Tuple

from ...errors import BackendError
from ...keys import normalize_key, split_text_tokens
from ...types import MouseButton, Point, Rect
from ._win32api import (
    ERROR_ACCESS_DENIED,
    INPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MAPVK_VK_TO_CHAR,
    MAPVK_VK_TO_VSC,
    MAPVK_VK_TO_VSC_EX,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_MOVE_NOCOALESCE,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    POINT,
    SM_CXSCREEN,
    SM_CXVIRTUALSCREEN,
    SM_CYSCREEN,
    SM_CYVIRTUALSCREEN,
    SM_SWAPBUTTON,
    SM_XVIRTUALSCREEN,
    SM_YVIRTUALSCREEN,
    WHEEL_DELTA,
)
from .keys import (
    VK_CODES,
    VK_RETURN,
    _BUTTON_FLAGS,
    _SENDINPUT_CHUNK,
    _SHIFT_STATE_MODIFIERS,
    decode_vk_scan,
    key_input,
    mouse_input,
    normalize_absolute,
    unicode_inputs,
    utf16_units,
    vk_for_key,
)


class _Held:
    """A key pressed through :meth:`WindowsBackend.key_down` (for symmetric release)."""

    __slots__ = ("events", "release")

    def __init__(self, events: List[INPUT], release: List[INPUT]) -> None:
        self.events = events
        self.release = release


class _InputMixin:
    """Mouse and keyboard input of :class:`WindowsBackend` (``SendInput``)."""

    def _send(self, inputs: Sequence[INPUT]) -> None:
        """SendInput in chunks; raise if Windows rejects the events."""
        self._check()
        for start in range(0, len(inputs), _SENDINPUT_CHUNK):
            chunk = inputs[start:start + _SENDINPUT_CHUNK]
            arr = (INPUT * len(chunk))(*chunk)
            sent = int(self._api.SendInput(len(chunk), arr, ctypes.sizeof(INPUT)))
            if sent != len(chunk):
                code = self._api.get_last_error()
                hint = ""
                if code == ERROR_ACCESS_DENIED:
                    hint = " (input is blocked: the secure desktop/UAC prompt or a locked workstation is active)"
                raise BackendError(
                    f"SendInput injected {sent}/{len(chunk)} events: {self._api.format_error(code)}{hint}"
                )

    # ==================================================================== mouse
    def mouse_position(self) -> Point:
        self._check()
        pt = POINT()
        if not self._api.GetCursorPos(ctypes.byref(pt)):
            raise self._error("GetCursorPos")
        return Point(int(pt.x), int(pt.y))

    def _virtual_metrics(self) -> Rect:
        a = self._api
        w = int(a.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        h = int(a.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if w <= 0 or h <= 0:
            return Rect(0, 0, int(a.GetSystemMetrics(SM_CXSCREEN)), int(a.GetSystemMetrics(SM_CYSCREEN)))
        return Rect(int(a.GetSystemMetrics(SM_XVIRTUALSCREEN)), int(a.GetSystemMetrics(SM_YVIRTUALSCREEN)), w, h)

    def mouse_move(self, x: int, y: int) -> None:
        """Move with an absolute SendInput (real WM_MOUSEMOVE / hover), then verify the position."""
        self._check()
        x, y = int(x), int(y)
        with self._lock:
            vs = self._virtual_metrics()
            nx, ny = normalize_absolute(x, y, vs)
            flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE_NOCOALESCE
            self._send([mouse_input(nx, ny, 0, flags)])
            tx = min(max(x, vs.left), vs.right - 1)
            ty = min(max(y, vs.top), vs.bottom - 1)
            try:
                pos = self.mouse_position()
            except BackendError:
                pos = None
            if pos != (tx, ty):
                # Rounding in exotic configurations, or the move is still queued:
                # SetCursorPos lands exactly on the requested physical pixel.
                self._api.SetCursorPos(tx, ty)

    def _physical_button(self, button: Any) -> MouseButton:
        b = MouseButton.coerce(button)
        if self._respect_swap and b in (MouseButton.LEFT, MouseButton.RIGHT):
            if self._api.GetSystemMetrics(SM_SWAPBUTTON):
                b = MouseButton.RIGHT if b == MouseButton.LEFT else MouseButton.LEFT
        return b

    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._check()
        logical = MouseButton.coerce(button)
        phys = self._physical_button(logical)
        down, up, data = _BUTTON_FLAGS[phys]
        with self._lock:
            self._send([mouse_input(0, 0, data, down)])
            self._buttons_down[logical] = mouse_input(0, 0, data, up)

    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._check()
        logical = MouseButton.coerce(button)
        with self._lock:
            ev = self._buttons_down.get(logical)
            if ev is None:
                phys = self._physical_button(logical)
                _down, up, data = _BUTTON_FLAGS[phys]
                ev = mouse_input(0, 0, data, up)
            self._send([ev])
            self._last_button_up = time.monotonic()
            # Forgotten only once released: if Windows rejected the release (secure
            # desktop, locked workstation) release_all()/close() can retry it.
            self._buttons_down.pop(logical, None)

    def _settle_after_click(self) -> None:
        """Before the first keyboard event after a click, let the app finish handling the click."""
        last, self._last_button_up = self._last_button_up, None
        if last is None or self._click_key_settle <= 0:
            return
        remaining = self._click_key_settle - (time.monotonic() - last)
        if remaining > 0:
            self._sleep(remaining)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Wheel notches: ``dy > 0`` scrolls up, ``dx > 0`` scrolls right (one event per notch)."""
        self._check()
        dx, dy = int(dx), int(dy)
        events: List[INPUT] = []
        step = WHEEL_DELTA if dy > 0 else -WHEEL_DELTA
        events += [mouse_input(0, 0, step, MOUSEEVENTF_WHEEL) for _ in range(abs(dy))]
        step = WHEEL_DELTA if dx > 0 else -WHEEL_DELTA
        events += [mouse_input(0, 0, step, MOUSEEVENTF_HWHEEL) for _ in range(abs(dx))]
        if events:
            with self._lock:
                self._send(events)

    # ================================================================= keyboard
    def _scan(self, vk: int) -> Tuple[int, bool]:
        """Hardware scan code for ``vk`` and whether Windows reports it as E0-extended."""
        try:
            sc = int(self._api.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX))
        except Exception:
            sc = 0
        if sc == 0:
            try:
                sc = int(self._api.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC))
            except Exception:
                sc = 0
        return sc & 0xFF, (sc >> 8) == 0xE0

    def _vk_events(self, vk: int, extended: bool) -> Tuple[INPUT, INPUT]:
        scan, ext2 = self._scan(vk)
        # num_enter shares VK_RETURN, so the caller's ``extended`` decides; for
        # other keys MapVirtualKey's E0 prefix is honoured as well.
        flags = KEYEVENTF_EXTENDEDKEY if (extended or (ext2 and vk != VK_RETURN)) else 0
        return key_input(vk, scan, flags), key_input(vk, scan, flags | KEYEVENTF_KEYUP)

    def _layout(self) -> Any:
        a = self._api
        try:
            fg = a.GetForegroundWindow()
            tid = a.GetWindowThreadProcessId(fg, None) if fg else 0
            return a.GetKeyboardLayout(tid)
        except Exception:
            return None

    def _char_plan(self, ch: str) -> Tuple[List[INPUT], List[INPUT]]:
        """(press events, release events) for a single character."""
        if len(ch) == 1 and ord(ch) < 0x10000:
            hkl = self._layout()
            try:
                scanned = decode_vk_scan(self._api.VkKeyScanExW(ch, hkl))
            except Exception:
                scanned = None
            if scanned is not None and self._is_dead_key(scanned[0], hkl, scanned[1]):
                scanned = None  # the key would start a composition instead of typing ``ch``
            if scanned is not None:
                vk, state = scanned
                if state in (0, 1) and self._caps_flips(ch):
                    state ^= 1  # CapsLock is on: Shift now gives the other case
                press: List[INPUT] = []
                release: List[INPUT] = []
                for bit, mod in _SHIFT_STATE_MODIFIERS:
                    if state & bit and not self._modifier_held(mod):
                        d, u = self._vk_events(*vk_for_key(mod))
                        press.append(d)
                        release.insert(0, u)
                d, u = self._vk_events(vk, False)
                press.append(d)
                release.insert(0, u)
                return press, release
        units = utf16_units(ch)
        press = [key_input(0, unit, KEYEVENTF_UNICODE) for unit in units]
        release = [key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP) for unit in units]
        return press, release

    def _is_dead_key(self, vk: int, hkl: Any, state: int = 0) -> bool:
        """Whether ``vk`` pressed with shift state ``state`` (VkKeyScan bits) is a dead
        (diacritic) key on layout ``hkl`` (circumflex and grave on German/French,
        quote/tilde and Shift+6 on US-International). Such characters are
        typed as Unicode packets instead (unless Ctrl/Alt/Win is held, where the
        key itself is wanted for a shortcut)."""
        if any(self._modifier_held(m) for m in ("ctrl", "alt", "cmd")):
            return False
        to_unicode = getattr(self._api, "ToUnicodeEx", None)
        if state & 0x07 and to_unicode is not None:
            # MAPVK_VK_TO_CHAR only reports the *unshifted* character, so a dead
            # shifted/AltGr position is asked for with that exact key state.
            keys = (ctypes.c_ubyte * 256)()
            for bit, vks in ((0x01, (0x10, 0xA0)), (0x02, (0x11, 0xA2)), (0x04, (0x12, 0xA4))):
                if state & bit:
                    for k in vks:
                        keys[k] = 0x80
            buf = ctypes.create_unicode_buffer(8)
            try:
                scan = self._scan(vk)[0]
                # flag 0x4: leave the kernel keyboard state alone (Windows 10 1607+)
                n = int(to_unicode(vk, scan, keys, buf, len(buf), 0x4, hkl))
                if n < 0:  # older Windows keeps the dead key pending: flush it
                    to_unicode(vk, scan, keys, buf, len(buf), 0x4, hkl)
                return n < 0
            except Exception:
                pass
        try:
            # MAPVK_VK_TO_CHAR sets the top bit for dead keys.
            return bool(int(self._api.MapVirtualKeyExW(vk, MAPVK_VK_TO_CHAR, hkl)) & 0x80000000)
        except Exception:
            return False

    def _caps_flips(self, ch: str) -> bool:
        """Whether CapsLock is on and would change the case of letter ``ch`` typed as text
        (not while Ctrl/Alt/Win is held: shortcuts keep the key's own Shift state)."""
        lo, up = ch.lower(), ch.upper()
        if lo == up or len(lo) != 1 or len(up) != 1:
            return False
        if any(self._modifier_held(m) for m in ("ctrl", "alt", "cmd")):
            return False
        try:
            return bool(self._api.GetKeyState(0x14) & 1)  # VK_CAPITAL toggled
        except Exception:
            return False

    def _modifier_held(self, mod: str) -> bool:
        return mod in self._held or f"{mod}_r" in self._held

    def _plan(self, key: str) -> Tuple[str, List[INPUT], List[INPUT]]:
        if not isinstance(key, str) or key == "":
            raise ValueError(f"Invalid key: {key!r}")
        # Canonical names pass straight through; aliases ("Return", "\n", " ") are
        # normalized; unknown multi-character names raise ValueError.
        name = key if key in VK_CODES else normalize_key(key)
        if name in VK_CODES:
            d, u = self._vk_events(*vk_for_key(name))
            return name, [d], [u]
        press, release = self._char_plan(name)
        return name, press, release

    def key_down(self, key: str) -> None:
        """Press a canonical key name or a single character (adding Shift/AltGr as the layout needs)."""
        self._check()
        self._settle_after_click()
        self._pace_key()
        with self._lock:
            name, press, release = self._plan(key)
            self._send(press)
            self._held[name] = _Held(press, release)
            self._last_key_down = time.monotonic()

    def _pace_key(self) -> None:
        """Keep key presses at least ``type_char_delay`` apart.

        Key-by-key typing (``mode="keys"``) is a burst of presses just like a
        unicode burst; Windows 11 Notepad intermittently dropped whole runs of
        them (about 1 in 6 runs of a 30-symbol string on a real machine).
        """
        last = self._last_key_down
        if last is None or self._type_char_delay <= 0:
            return
        remaining = self._type_char_delay - (time.monotonic() - last)
        if remaining > 0:
            self._sleep(remaining)

    def key_up(self, key: str) -> None:
        self._check()
        with self._lock:
            name, _press, release = self._plan(key)
            held = self._held.get(name)
            self._send(held.release if held is not None else release)
            self._held.pop(name, None)  # only once released (see mouse_up)

    def type_text(self, text: str) -> None:
        """Type ``text`` layout-independently (``KEYEVENTF_UNICODE``); newlines/tabs as Enter/Tab keys.

        Characters are sent one at a time (a surrogate pair stays in one
        ``SendInput`` call) with ``type_char_delay`` seconds between them:
        modern apps such as Windows 11 Notepad drop or garble long bursts of
        ``KEYEVENTF_UNICODE`` input (verified on a real machine), while a
        10 ms pace is reliable and still ~100 characters per second. Use
        clipboard typing for very long text.
        """
        self._check()
        if not text:
            return
        self._settle_after_click()
        groups: List[List[INPUT]] = []
        for token in split_text_tokens(text):
            if token in ("enter", "tab"):
                d, u = self._vk_events(*vk_for_key(token))
                groups.append([d, u])
            else:
                groups.append(unicode_inputs(token))
        delay = self._type_char_delay
        for i, events in enumerate(groups):
            if i and delay > 0:
                self._sleep(delay)
            with self._lock:
                self._send(events)
