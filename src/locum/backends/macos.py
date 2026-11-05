"""macOS backend built on pyobjc (Quartz / AppKit / ApplicationServices).

Everything platform specific is imported lazily, so this module imports cleanly
on Linux and Windows; only :class:`MacOSBackend` itself refuses to start off
macOS (raising :class:`~locum.errors.BackendUnavailable`).

Install the native dependencies with::

    pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices

Coordinate model (points vs. pixels)
------------------------------------
Quartz uses *global display coordinates in points*: the primary display's
top-left corner is ``(0, 0)``, ``y`` grows downwards, and on a Retina display
one point is 2x2 physical pixels. Screenshots, on the other hand, are captured
at the backing (pixel) resolution.

The locum backend contract says that **every coordinate is a physical pixel
of the virtual desktop**, so that a point found in a screenshot can be clicked
directly. This backend therefore keeps a :class:`DisplayLayout` that assigns
every display a rectangle in a *virtual pixel space*:

* the primary display's pixel rect is ``(0, 0, width_pt * scale, height_pt * scale)``;
* every other display is attached to an already placed neighbour so that the
  shared edge is preserved (a display to the right of the primary starts at the
  primary's right pixel edge, etc.). Offsets along the shared edge are measured
  in the neighbour's scale. Displays that touch nothing are placed relative to
  the nearest placed display, scaled by that display's factor.
* with mixed scales that rule can make two pixel rects overlap (an offset
  measured in one neighbour's scale lands on another display), so every new rect
  is checked against the displays already placed: the other edge neighbours are
  tried, then the display slides along its shared edge, and as a last resort it
  is pushed outward (away from the primary). Pixel rects are always disjoint, so
  every pixel maps back to exactly one display.

With identical scale factors this degenerates to "pixels = points * scale"
everywhere. With mixed scales (a 2x laptop next to a 1x monitor) each display
keeps its native pixel resolution, so screenshots are never resampled.

All conversions go through the display that contains the coordinate (or the
nearest display when it lies outside every display):

* ``mouse_move``/``mouse_position``: pixels <-> points of the containing display;
* window rects and accessibility bounds: the rect is mapped with the display that
  contains its centre;
* ``screenshot(region)``: the region is split per display, each part is captured
  in points and resized (if needed) to its exact pixel size, then stitched.

Keyboard layouts
----------------
Keyboard events carry *virtual keycodes* (physical key positions); macOS turns
them into characters through the active input source. Characters are therefore
mapped to keycodes with the live layout (Text Input Sources + ``UCKeyTranslate``,
re-read when the input source changes), so ``hotkey("cmd", "a")`` presses the key
that types "a" on AZERTY instead of the US "a" position (which is "q" there).
Characters the layout cannot type with at most Shift are injected as unicode,
except while Command/Control/Option is held: shortcuts then use the layout's
Command table, else the US-ANSI key position (a unicode event would carry
keycode 0, which apps read as Cmd+A on US and Cmd+Q on AZERTY).

The layout can only be read on the main thread. A backend created and used
only on other threads never learns it; it then types characters as unicode text
(with a :class:`RuntimeWarning`) and refuses character shortcuts with a
:class:`~locum.errors.BackendError` rather than guessing US-ANSI positions.

Permissions
-----------
macOS silently drops synthetic input and hides the accessibility tree unless the
process running Python has been granted **Accessibility** permission (System
Settings > Privacy & Security > Accessibility). Input and accessibility calls
check :func:`AXIsProcessTrusted` and raise a :class:`~locum.errors.BackendError`
explaining how to grant it. Screenshots and window titles additionally need
**Screen Recording** permission (macOS 10.15+); without it macOS returns only the
wallpaper/menu bar and omits window titles. :meth:`MacOSBackend.permissions`
reports both, :meth:`MacOSBackend.request_permissions` shows the system prompts.
"""

from __future__ import annotations

import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound
from ..keys import SHIFTED_CHARS, normalize_key
from ..types import Monitor, MouseButton, Point, Rect, WindowInfo
from .base import (
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOWS,
    AccessibleNode,
    Backend,
)

__all__ = [
    "ACCESSIBILITY_HELP",
    "AXNode",
    "ClickTracker",
    "DisplayInfo",
    "DisplayLayout",
    "KeySpec",
    "KeyboardLayoutMap",
    "MacOSBackend",
    "capture_plan",
    "chunk_utf16",
    "map_ax_role",
    "match_window_record",
    "pair_ax_windows",
    "resolve_key",
    "split_typing_segments",
    "utf16_len",
]

# =============================================================================
# Native constants (stable ABI values; hard-coded so the pure helpers below can
# be used and tested without pyobjc).
# =============================================================================

# CGEventType
kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventRightMouseDown = 3
kCGEventRightMouseUp = 4
kCGEventMouseMoved = 5
kCGEventLeftMouseDragged = 6
kCGEventRightMouseDragged = 7
kCGEventKeyDown = 10
kCGEventKeyUp = 11
kCGEventFlagsChanged = 12
kCGEventScrollWheel = 22
kCGEventOtherMouseDown = 25
kCGEventOtherMouseUp = 26
kCGEventOtherMouseDragged = 27

# CGEventField
kCGMouseEventClickState = 1
kCGMouseEventButtonNumber = 3
kCGMouseEventDeltaX = 4
kCGMouseEventDeltaY = 5

# Misc Quartz
kCGHIDEventTap = 0
kCGEventSourceStateHIDSystemState = 1
kCGScrollEventUnitLine = 1
kCGWindowListOptionAll = 0
kCGWindowListOptionOnScreenOnly = 1
kCGWindowListOptionIncludingWindow = 8
kCGWindowListExcludeDesktopElements = 16
kCGNullWindowID = 0
kCGWindowImageDefault = 0

# CGEventFlags (device independent)
FLAG_ALPHA_SHIFT = 0x00010000
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_ALTERNATE = 0x00080000
FLAG_COMMAND = 0x00100000
FLAG_NUMERIC_PAD = 0x00200000
FLAG_SECONDARY_FN = 0x00800000
# NX device-dependent bits (left/right side of a modifier)
_NX_LCTL = 0x00000001
_NX_LSHIFT = 0x00000002
_NX_RSHIFT = 0x00000004
_NX_LCMD = 0x00000008
_NX_RCMD = 0x00000010
_NX_LALT = 0x00000020
_NX_RALT = 0x00000040
_NX_RCTL = 0x00002000

# AppKit
_NS_SYSTEM_DEFINED = 14
_NS_ACTIVATE_ALL_WINDOWS = 1 << 0
_NS_ACTIVATE_IGNORING_OTHER_APPS = 1 << 1
_NS_APPLICATION_ACTIVATION_POLICY_REGULAR = 0

# Accessibility (HIServices)
kAXErrorSuccess = 0
kAXValueCGPointType = 1
kAXValueCGSizeType = 2
kAXValueCGRectType = 3
kAXValueCFRangeType = 4
kAXValueAXErrorType = 5

ACCESSIBILITY_HELP = (
    "macOS has not granted Accessibility permission to this process ({exe}). "
    "Open System Settings > Privacy & Security > Accessibility and enable the application "
    "that runs Python (Terminal, iTerm2, VS Code, PyCharm... or the python binary itself), "
    "then restart that application. Without this permission macOS silently drops synthetic "
    "mouse/keyboard events and hides the accessibility tree. "
    "MacOSBackend().request_permissions() opens the system prompt."
)

SCREEN_RECORDING_HELP = (
    "macOS has not granted Screen Recording permission to this process; screenshots will only "
    "show the wallpaper and menu bar and window titles are hidden. Enable it in System Settings > "
    "Privacy & Security > Screen Recording, then restart the application running Python."
)

_INSTALL_HINT = (
    "pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices"
)

# =============================================================================
# Keyboard: canonical key -> macOS virtual keycode (kVK_*, ANSI layout)
# =============================================================================

#: Printable characters of the US ANSI layout that can be typed without shift.
CHAR_KEYCODES: Dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07,
    "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10,
    "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "=": 0x18,
    "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20,
    "[": 0x21, "i": 0x22, "p": 0x23, "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29,
    "\\": 0x2A, ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32, " ": 0x31,
}

#: Modifier keys (posted as ``kCGEventFlagsChanged`` events).
MODIFIER_KEYCODES: Dict[str, int] = {
    "cmd": 0x37, "shift": 0x38, "alt": 0x3A, "ctrl": 0x3B,
    "cmd_r": 0x36, "shift_r": 0x3C, "alt_r": 0x3D, "ctrl_r": 0x3E,
    "altgr": 0x3D,  # Option is the macOS equivalent of AltGr; use the right one
    "fn": 0x3F,
}

#: Event flags contributed by each held modifier.
MODIFIER_FLAGS: Dict[str, int] = {
    "shift": FLAG_SHIFT | _NX_LSHIFT,
    "shift_r": FLAG_SHIFT | _NX_RSHIFT,
    "ctrl": FLAG_CONTROL | _NX_LCTL,
    "ctrl_r": FLAG_CONTROL | _NX_RCTL,
    "alt": FLAG_ALTERNATE | _NX_LALT,
    "alt_r": FLAG_ALTERNATE | _NX_RALT,
    "altgr": FLAG_ALTERNATE | _NX_RALT,
    "cmd": FLAG_COMMAND | _NX_LCMD,
    "cmd_r": FLAG_COMMAND | _NX_RCMD,
    "fn": FLAG_SECONDARY_FN,
}

#: Non-printable named keys.
NAMED_KEYCODES: Dict[str, int] = {
    "enter": 0x24, "tab": 0x30, "space": 0x31, "backspace": 0x33, "delete": 0x75, "escape": 0x35,
    "insert": 0x72,  # Help key sits where PC keyboards have Insert
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    "up": 0x7E, "down": 0x7D, "left": 0x7B, "right": 0x7C,
    "capslock": 0x39,
    "numlock": 0x47,  # keypad Clear occupies the Num Lock position
    "printscreen": 0x69,  # F13 -- the conventional Mac stand-in for Print Screen
    "scrolllock": 0x6B,  # F14
    "pause": 0x71,  # F15
    "menu": 0x6E,  # kVK_ContextualMenu
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
    "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F, "f13": 0x69, "f14": 0x6B, "f15": 0x71,
    "f16": 0x6A, "f17": 0x40, "f18": 0x4F, "f19": 0x50, "f20": 0x5A,
    "num0": 0x52, "num1": 0x53, "num2": 0x54, "num3": 0x55, "num4": 0x56, "num5": 0x57,
    "num6": 0x58, "num7": 0x59, "num8": 0x5B, "num9": 0x5C,
    "num_add": 0x45, "num_subtract": 0x4E, "num_multiply": 0x43, "num_divide": 0x4B,
    "num_decimal": 0x41, "num_enter": 0x4C,
}

#: Media keys are not keyboard events on macOS but ``NSSystemDefined`` events
#: (subtype 8) carrying an ``NX_KEYTYPE_*`` code.
MEDIA_KEYS: Dict[str, int] = {
    "volumeup": 0,  # NX_KEYTYPE_SOUND_UP
    "volumedown": 1,  # NX_KEYTYPE_SOUND_DOWN
    "volumemute": 7,  # NX_KEYTYPE_MUTE
    "playpause": 16,  # NX_KEYTYPE_PLAY
    "nexttrack": 17,  # NX_KEYTYPE_NEXT
    "prevtrack": 18,  # NX_KEYTYPE_PREVIOUS
}

#: Keys that have no Mac hardware equivalent and are mapped to the standard
#: shortcut performing the same function (character, flags).
SHORTCUT_KEYS: Dict[str, Tuple[str, int]] = {
    "browser_back": ("[", FLAG_COMMAND | _NX_LCMD),
    "browser_forward": ("]", FLAG_COMMAND | _NX_LCMD),
    "browser_refresh": ("r", FLAG_COMMAND | _NX_LCMD),
    "stop": (".", FLAG_COMMAND | _NX_LCMD),  # Cmd+. = universal "stop/cancel" on macOS
}

#: Canonical keys macOS cannot generate at all (the OS only knows F1-F20).
UNSUPPORTED_KEYS = frozenset({"f21", "f22", "f23", "f24"})

_FN_FLAG_KEYS = frozenset(
    {"home", "end", "pageup", "pagedown", "delete", "insert", *(f"f{i}" for i in range(1, 21))}
)
_ARROW_KEYS = frozenset({"up", "down", "left", "right"})
_KEYPAD_KEYS = frozenset(
    {*(f"num{i}" for i in range(10)), "num_add", "num_subtract", "num_multiply", "num_divide",
     "num_decimal", "num_enter", "numlock"}
)


@dataclass(frozen=True)
class KeySpec:
    """How to synthesize one canonical key on macOS.

    ``kind`` is one of:

    * ``"key"`` -- a regular keyboard event for ``keycode`` with extra ``flags``
      (shift for shifted characters, fn/numeric-pad bits for navigation keys,
      cmd for shortcut-mapped keys);
    * ``"modifier"`` -- a ``kCGEventFlagsChanged`` event; ``flags`` is the mask the
      modifier contributes while held;
    * ``"media"`` -- an ``NSSystemDefined`` media-key event; ``keycode`` is the
      ``NX_KEYTYPE_*`` value;
    * ``"unicode"`` -- a character outside the US layout, injected with
      ``CGEventKeyboardSetUnicodeString`` (``char``).
    """

    kind: str
    keycode: int
    flags: int = 0
    char: str = ""


def _named_key_flags(key: str) -> int:
    flags = 0
    if key in _ARROW_KEYS:
        flags |= FLAG_NUMERIC_PAD | FLAG_SECONDARY_FN
    elif key in _FN_FLAG_KEYS:
        flags |= FLAG_SECONDARY_FN
    if key in _KEYPAD_KEYS:
        flags |= FLAG_NUMERIC_PAD
    return flags


def _us_char_spec(key: str) -> Optional[KeySpec]:
    """The US-ANSI key (plus Shift if needed) that types the character ``key``."""
    if key in CHAR_KEYCODES:
        return KeySpec("key", CHAR_KEYCODES[key], 0, key)
    lower = key.lower()
    if lower != key and len(lower) == 1 and lower in CHAR_KEYCODES:
        return KeySpec("key", CHAR_KEYCODES[lower], FLAG_SHIFT | _NX_LSHIFT, key)
    base = SHIFTED_CHARS.get(key)
    if base is not None and base in CHAR_KEYCODES:
        return KeySpec("key", CHAR_KEYCODES[base], FLAG_SHIFT | _NX_LSHIFT, key)
    return None


def resolve_key(key: str, keymap: Optional["KeyboardLayoutMap"] = None, *,
                command: bool = False, modified: bool = False) -> KeySpec:
    """Map a canonical key name (or a single character) to a :class:`KeySpec`.

    ``keymap`` is the active keyboard layout (:class:`KeyboardLayoutMap`). With
    it, characters are mapped to the keycode that produces them on that layout
    (so ``"a"`` is keycode ``0x0C`` on AZERTY), and characters the layout cannot
    type with at most Shift are injected as unicode. ``command`` says whether
    Command is held and ``modified`` whether Command, Control or Option is held;
    then the layout's Command table is consulted first (non-Latin layouts switch
    to Latin letters while Command is down, and Control/Option shortcuts match
    the same keys). A shortcut character the layout cannot type falls back to its
    US-ANSI key position (like :data:`SHORTCUT_KEYS`), never to unicode injection:
    a unicode event still carries a keycode (0, the "a" key on US and the "q" key
    on AZERTY) and apps match shortcuts against it. Without a keymap the static
    US-ANSI table is used.

    Raises :class:`NotSupported` for ``f21``-``f24`` (macOS has no such keys) and
    for a character with no key at all while a modifier is held, and
    ``ValueError`` for unknown names.
    """
    if not isinstance(key, str) or key == "":
        raise ValueError(f"Invalid key: {key!r}")
    modified = modified or command
    if len(key) == 1:
        if key in ("\n", "\r", "\t", "\b", "\x1b"):
            return resolve_key(normalize_key(key), keymap, command=command, modified=modified)
        spec: Optional[KeySpec] = None
        if keymap is not None:
            hit = keymap.lookup(key, command=modified)
            if hit is not None:
                code, shift = hit
                spec = KeySpec("key", code, (FLAG_SHIFT | _NX_LSHIFT) if shift else 0, key)
            elif modified:
                spec = _us_char_spec(key)
        else:
            spec = _us_char_spec(key)
        if spec is not None:
            return spec
        if modified:
            raise NotSupported(
                f"Cannot press {key!r} together with Command/Control/Option: no key types it "
                "on the active keyboard layout")
        return KeySpec("unicode", 0, 0, key)
    if key in MODIFIER_KEYCODES:
        return KeySpec("modifier", MODIFIER_KEYCODES[key], MODIFIER_FLAGS[key])
    if key in MEDIA_KEYS:
        return KeySpec("media", MEDIA_KEYS[key])
    if key in SHORTCUT_KEYS:
        ch, flags = SHORTCUT_KEYS[key]
        hit = keymap.lookup(ch, command=True) if keymap is not None else None
        if hit is not None:
            code, shift = hit
            return KeySpec("key", code, flags | ((FLAG_SHIFT | _NX_LSHIFT) if shift else 0), ch)
        # the layout cannot type the shortcut character directly (e.g. "[" on
        # AZERTY needs Option): use the US-ANSI position, which is what most
        # apps' key equivalents fall back to
        return KeySpec("key", CHAR_KEYCODES[ch], flags, ch)
    if key in NAMED_KEYCODES:
        return KeySpec("key", NAMED_KEYCODES[key], _named_key_flags(key))
    if key in UNSUPPORTED_KEYS:
        raise NotSupported(f"macOS has no {key.upper()} key (only F1-F20 exist)")
    canonical = normalize_key(key)  # raises ValueError for unknown names
    if canonical == key:
        raise ValueError(f"Unknown key name: {key!r}")
    return resolve_key(canonical, keymap, command=command, modified=modified)


# =============================================================================
# Keyboard layout (character -> keycode for the active input source)
# =============================================================================

#: ``UCKeyTranslate`` modifier states (``(EventModifiers >> 8) & 0xFF``).
UC_MOD_NONE = 0
UC_MOD_CMD = 0x01
UC_MOD_SHIFT = 0x02
UC_MOD_OPTION = 0x08

#: Keypad keycodes: only used for a character when no main-keyboard key types it.
_KEYPAD_KEYCODES = frozenset({0x41, 0x43, 0x45, 0x47, 0x4B, 0x4C, 0x4E, 0x51,
                              *range(0x52, 0x5A), 0x5B, 0x5C})


def _printable_char(ch: Optional[str]) -> bool:
    return bool(ch) and len(ch) == 1 and (ch == " " or ch.isprintable())  # type: ignore[arg-type]


class KeyboardLayoutMap:
    """Characters of one keyboard layout -> ``(keycode, needs_shift)``.

    Built by scanning virtual keycodes 0-127 through a ``translate(keycode,
    modifier_state)`` function (``UCKeyTranslate`` on a real Mac; any callable in
    tests). ``translate`` returns the character the key produces, or ``None``
    for keys that produce nothing printable or start a dead-key sequence.
    Main-keyboard keys win over the keypad, then unshifted keys over shifted ones.
    A separate table records what keys produce while Command is held; non-Latin
    layouts (Russian, Greek...) produce Latin letters there, which is what app
    shortcuts match against.
    """

    def __init__(self, chars: Dict[str, Tuple[int, bool]],
                 command_chars: Optional[Dict[str, int]] = None):
        self.chars: Dict[str, Tuple[int, bool]] = dict(chars)
        self.command_chars: Dict[str, int] = dict(command_chars or {})

    @classmethod
    def from_translator(cls, translate: Callable[[int, int], Optional[str]]) -> "KeyboardLayoutMap":
        main = [k for k in range(128) if k not in _KEYPAD_KEYCODES]
        keypad = sorted(k for k in _KEYPAD_KEYCODES if k < 128)
        order = main + keypad
        chars: Dict[str, Tuple[int, bool]] = {}
        for codes in (main, keypad):
            for state, shifted in ((UC_MOD_NONE, False), (UC_MOD_SHIFT, True)):
                for code in codes:
                    ch = translate(code, state)
                    if _printable_char(ch) and ch not in chars:
                        chars[ch] = (code, shifted)  # type: ignore[index]
        command: Dict[str, int] = {}
        for code in order:
            ch = translate(code, UC_MOD_CMD)
            if _printable_char(ch) and ch not in command:
                command[ch] = code  # type: ignore[index]
        return cls(chars, command)

    @classmethod
    def us_ansi(cls) -> "KeyboardLayoutMap":
        """The static US-ANSI table (used when the live layout cannot be read)."""
        chars: Dict[str, Tuple[int, bool]] = {ch: (code, False) for ch, code in CHAR_KEYCODES.items()}
        for ch, code in CHAR_KEYCODES.items():
            up = ch.upper()
            if up != ch and len(up) == 1:
                chars.setdefault(up, (code, True))
        for shifted, base in SHIFTED_CHARS.items():
            if base in CHAR_KEYCODES:
                chars.setdefault(shifted, (CHAR_KEYCODES[base], True))
        return cls(chars, dict(CHAR_KEYCODES))

    def lookup(self, ch: str, *, command: bool = False) -> Optional[Tuple[int, bool]]:
        """``(keycode, needs_shift)`` for ``ch``, or ``None`` if the layout cannot type it.

        With ``command`` the Command table is tried first: a letter is matched
        case-insensitively there (shortcuts ignore case; an uppercase letter
        adds Shift).
        """
        if command and self.command_chars:
            code = self.command_chars.get(ch)
            if code is not None:
                return (code, False)
            low = ch.lower()
            if low != ch and len(low) == 1 and low in self.command_chars:
                return (self.command_chars[low], True)
        return self.chars.get(ch)


class _CarbonKeyboardLayouts:
    """Reads the current keyboard layout with Carbon's Text Input Sources API.

    Uses ``ctypes`` (pyobjc does not wrap ``UCKeyTranslate``):
    ``TISCopyCurrentKeyboardLayoutInputSource`` -> ``kTISPropertyUnicodeKeyLayoutData``
    -> ``UCKeyTranslate`` for keycodes 0-127. The raw ``uchr`` data doubles as a
    fingerprint so the table is rebuilt only when the input source changes.
    """

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        self._ct = ctypes
        carbon_path = ctypes.util.find_library("Carbon")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not carbon_path or not cf_path:
            raise OSError("Carbon/CoreFoundation frameworks not found")
        carbon = ctypes.cdll.LoadLibrary(carbon_path)
        cf = ctypes.cdll.LoadLibrary(cf_path)
        vp = ctypes.c_void_p
        for name in ("TISCopyCurrentKeyboardLayoutInputSource",
                     "TISCopyCurrentASCIICapableKeyboardLayoutInputSource"):
            fn = getattr(carbon, name)
            fn.restype = vp
            fn.argtypes = []
        carbon.TISGetInputSourceProperty.restype = vp
        carbon.TISGetInputSourceProperty.argtypes = [vp, vp]
        carbon.LMGetKbdType.restype = ctypes.c_uint8
        carbon.LMGetKbdType.argtypes = []
        carbon.UCKeyTranslate.restype = ctypes.c_int32
        carbon.UCKeyTranslate.argtypes = [
            vp, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_uint16),
        ]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [vp]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [vp]
        self._carbon = carbon
        self._cf = cf
        self._prop = vp.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")

    def current(self, known: Optional[bytes]) -> Tuple[Optional[bytes], Optional[KeyboardLayoutMap]]:
        """``(fingerprint, map)``; ``map`` is ``None`` when the fingerprint equals
        ``known`` (layout unchanged) or the layout has no unicode data."""
        ct, carbon, cf = self._ct, self._carbon, self._cf
        for getter in (carbon.TISCopyCurrentKeyboardLayoutInputSource,
                       carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource):
            src = getter()
            if not src:
                continue
            try:
                data = carbon.TISGetInputSourceProperty(src, self._prop)
                if not data:
                    continue
                ptr = cf.CFDataGetBytePtr(data)
                length = cf.CFDataGetLength(data)
                if not ptr or length <= 0:
                    continue
                fingerprint = ct.string_at(ptr, length)
                if known is not None and fingerprint == known:
                    return fingerprint, None
                kbd_type = int(carbon.LMGetKbdType())
                dead = ct.c_uint32(0)
                n = ct.c_ulong(0)
                buf = (ct.c_uint16 * 4)()

                def translate(code: int, state: int) -> Optional[str]:
                    dead.value = 0
                    status = carbon.UCKeyTranslate(ptr, code, 0, state, kbd_type, 0,
                                                   ct.byref(dead), 4, ct.byref(n), buf)
                    if status != 0 or dead.value != 0 or n.value == 0:
                        return None  # error, or the key starts a dead-key sequence
                    text = ct.string_at(buf, 2 * n.value).decode("utf-16-le", "replace")
                    return text if len(text) == 1 else None

                return fingerprint, KeyboardLayoutMap.from_translator(translate)
            finally:
                cf.CFRelease(src)
        return None, None


# =============================================================================
# Text injection helpers
# =============================================================================


def utf16_len(text: str) -> int:
    """Number of UTF-16 code units in ``text`` (what Core Graphics counts)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def chunk_utf16(text: str, max_units: int = 20) -> List[str]:
    """Split ``text`` into chunks of at most ``max_units`` UTF-16 code units.

    ``CGEventKeyboardSetUnicodeString`` silently truncates strings longer than
    20 units, so typing is done in chunks. Surrogate pairs (emoji, astral
    characters) are never split across chunks.
    """
    if max_units < 2:
        raise ValueError("max_units must be >= 2 so surrogate pairs fit in one chunk")
    chunks: List[str] = []
    cur: List[str] = []
    units = 0
    for ch in text:
        n = 2 if ord(ch) > 0xFFFF else 1
        if cur and units + n > max_units:
            chunks.append("".join(cur))
            cur, units = [], 0
        cur.append(ch)
        units += n
    if cur:
        chunks.append("".join(cur))
    return chunks


def split_typing_segments(text: str, max_units: int = 20) -> List[Tuple[str, str]]:
    """Split ``text`` into ``("text", chunk)`` and ``("key", "enter"|"tab")`` segments.

    Newlines and tabs are sent as real Return/Tab key presses because most
    Cocoa text views ignore them inside an injected unicode string.
    """
    segments: List[Tuple[str, str]] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            segments.extend(("text", c) for c in chunk_utf16("".join(buf), max_units))
            buf.clear()

    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            flush()
            segments.append(("key", "enter"))
        elif ch == "\t":
            flush()
            segments.append(("key", "tab"))
        else:
            buf.append(ch)
    flush()
    return segments


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


# =============================================================================
# Display geometry: points <-> pixels
# =============================================================================


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


@dataclass(frozen=True)
class DisplayInfo:
    """One display. ``x, y, width, height`` are Quartz global *points*;
    ``pixel_left``/``pixel_top`` are assigned by :class:`DisplayLayout`.
    ``work_area`` is the visible frame (minus menu bar / Dock) in points."""

    display_id: int
    x: float
    y: float
    width: float
    height: float
    scale: float = 1.0
    is_primary: bool = False
    name: str = ""
    pixel_left: int = 0
    pixel_top: int = 0
    work_area: Optional[Tuple[float, float, float, float]] = None

    @property
    def pixel_width(self) -> int:
        return _round(self.width * self.scale)

    @property
    def pixel_height(self) -> int:
        return _round(self.height * self.scale)

    @property
    def pixel_rect(self) -> Rect:
        return Rect(self.pixel_left, self.pixel_top, self.pixel_width, self.pixel_height)

    def contains_points(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def contains_pixels(self, x: float, y: float) -> bool:
        return (self.pixel_left <= x < self.pixel_left + self.pixel_width
                and self.pixel_top <= y < self.pixel_top + self.pixel_height)

    def points_to_pixels(self, x: float, y: float) -> Tuple[float, float]:
        return (self.pixel_left + (x - self.x) * self.scale, self.pixel_top + (y - self.y) * self.scale)

    def pixels_to_points(self, x: float, y: float) -> Tuple[float, float]:
        return (self.x + (x - self.pixel_left) / self.scale, self.y + (y - self.pixel_top) / self.scale)

    def distance_points(self, x: float, y: float) -> float:
        dx = max(self.x - x, 0.0, x - (self.x + self.width))
        dy = max(self.y - y, 0.0, y - (self.y + self.height))
        return math.hypot(dx, dy)

    def distance_pixels(self, x: float, y: float) -> float:
        r = self.pixel_rect
        dx = max(r.left - x, 0.0, x - r.right)
        dy = max(r.top - y, 0.0, y - r.bottom)
        return math.hypot(dx, dy)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)


def _adjacent_origin(p: DisplayInfo, d: DisplayInfo, eps: float = 0.5) -> Optional[Tuple[int, int]]:
    """Pixel origin for ``d`` if it shares an edge with the placed display ``p``."""
    found = _adjacent_placement(p, d, eps)
    return None if found is None else found[0]


def _adjacent_placement(p: DisplayInfo, d: DisplayInfo,
                        eps: float = 0.5) -> Optional[Tuple[Tuple[int, int], str]]:
    """Like :func:`_adjacent_origin` but also reports which side of ``p`` ``d`` is on
    (``"right"``, ``"left"``, ``"below"`` or ``"above"``)."""
    if _overlap(d.y, d.y + d.height, p.y, p.y + p.height) > 0:
        top = p.pixel_top + _round((d.y - p.y) * p.scale)
        if abs(d.x - (p.x + p.width)) <= eps:  # d right of p
            return (p.pixel_left + p.pixel_width, top), "right"
        if abs((d.x + d.width) - p.x) <= eps:  # d left of p
            return (p.pixel_left - d.pixel_width, top), "left"
    if _overlap(d.x, d.x + d.width, p.x, p.x + p.width) > 0:
        left = p.pixel_left + _round((d.x - p.x) * p.scale)
        if abs(d.y - (p.y + p.height)) <= eps:  # d below p
            return (left, p.pixel_top + p.pixel_height), "below"
        if abs((d.y + d.height) - p.y) <= eps:  # d above p
            return (left, p.pixel_top - d.pixel_height), "above"
    return None


def _rects_intersect(a: Tuple[int, int, int, int], b: Rect) -> bool:
    """``a`` is ``(left, top, width, height)``; touching edges do not intersect."""
    return (a[0] < b.right and b.left < a[0] + a[2] and a[1] < b.bottom and b.top < a[1] + a[3])


def _is_free(rect: Tuple[int, int, int, int], placed: Sequence[DisplayInfo]) -> bool:
    return not any(_rects_intersect(rect, q.pixel_rect) for q in placed)


def _slide_along_edge(origin: Tuple[int, int], side: str, d: DisplayInfo, p: DisplayInfo,
                      placed: Sequence[DisplayInfo]) -> Optional[Tuple[int, int]]:
    """Slide ``d`` along its shared edge with ``p`` to the nearest free position.

    Positions that keep the two displays in contact are preferred; among those
    (and then among the remaining ones) the smallest shift wins.
    """
    w, h = d.pixel_width, d.pixel_height
    vertical_edge = side in ("left", "right")  # shared edge is vertical -> slide on y
    base = origin[1] if vertical_edge else origin[0]
    size = h if vertical_edge else w
    cands = set()
    for q in placed:
        r = q.pixel_rect
        lo, hi = (r.top, r.bottom) if vertical_edge else (r.left, r.right)
        cands.add(hi)          # just after q
        cands.add(lo - size)   # just before q
    p_lo, p_hi = (p.pixel_top, p.pixel_top + p.pixel_height) if vertical_edge else (
        p.pixel_left, p.pixel_left + p.pixel_width)
    best: Optional[Tuple[int, int, int]] = None  # (no_contact, shift, value)
    for v in cands:
        rect = (origin[0], v, w, h) if vertical_edge else (v, origin[1], w, h)
        if not _is_free(rect, placed):
            continue
        contact = min(v + size, p_hi) - max(v, p_lo) > 0
        key = (0 if contact else 1, abs(v - base), v)
        if best is None or key < best:
            best = key
    if best is None:
        return None
    v = best[2]
    return (origin[0], v) if vertical_edge else (v, origin[1])


def _push_outward(origin: Tuple[int, int], d: DisplayInfo, primary: DisplayInfo,
                  placed: Sequence[DisplayInfo]) -> Tuple[int, int]:
    """Last resort: move ``d`` away from the primary display until it overlaps nothing."""
    w, h = d.pixel_width, d.pixel_height
    # direction: where ``d`` lies relative to the primary in point space (falling
    # back to pixel space for a display centred on the primary)
    ox = (d.x + d.width / 2) - (primary.x + primary.width / 2)
    oy = (d.y + d.height / 2) - (primary.y + primary.height / 2)
    if not ox and not oy:
        ox = origin[0] + w / 2 - (primary.pixel_left + primary.pixel_width / 2)
        oy = origin[1] + h / 2 - (primary.pixel_top + primary.pixel_height / 2)
    horizontal = abs(ox) >= abs(oy)
    sign = 1 if (ox if horizontal else oy) >= 0 else -1
    left, top = origin
    for _ in range(len(placed) + 1):
        rect = (left, top, w, h)
        hits = [q.pixel_rect for q in placed if _rects_intersect(rect, q.pixel_rect)]
        if not hits:
            break
        if horizontal:
            left = max(r.right for r in hits) if sign > 0 else min(r.left for r in hits) - w
        else:
            top = max(r.bottom for r in hits) if sign > 0 else min(r.top for r in hits) - h
    else:  # still overlapping something: go beyond every placed display
        if horizontal:
            left = (max(q.pixel_rect.right for q in placed) if sign > 0
                    else min(q.pixel_rect.left for q in placed) - w)
        else:
            top = (max(q.pixel_rect.bottom for q in placed) if sign > 0
                   else min(q.pixel_rect.top for q in placed) - h)
    return (left, top)


class DisplayLayout:
    """Maps Quartz points to the backend's virtual *pixel* space and back.

    See the module docstring for the placement rules. ``displays`` is ordered
    with the primary display first (index 0 == primary monitor).
    """

    def __init__(self, displays: Sequence[DisplayInfo]):
        if not displays:
            raise BackendError("No active displays")
        primary_idx = next((i for i, d in enumerate(displays) if d.is_primary), None)
        if primary_idx is None:
            primary_idx = next((i for i, d in enumerate(displays) if d.contains_points(0, 0)), 0)
        ordered = [displays[primary_idx]] + [d for i, d in enumerate(displays) if i != primary_idx]
        first = ordered[0]
        placed = [replace(first, is_primary=True, pixel_left=_round(first.x * first.scale),
                          pixel_top=_round(first.y * first.scale))]
        pending = [replace(d, is_primary=False) for d in ordered[1:]]
        while pending:
            choice = self._choose_next(placed, pending)
            i, (left, top) = choice
            placed.append(replace(pending.pop(i), pixel_left=left, pixel_top=top))
        self.displays: List[DisplayInfo] = placed

    @staticmethod
    def _choose_next(placed: Sequence[DisplayInfo],
                     pending: Sequence[DisplayInfo]) -> Tuple[int, Tuple[int, int]]:
        """Pick the next display to place and its pixel origin.

        The result never overlaps an already placed display's pixel rect: with
        mixed scale factors, offsets along a shared edge are measured in the
        neighbour's scale, which can push the new rect onto another display. In
        that case the other adjacent neighbours are tried, then the display is
        slid along its shared edge, and as a last resort it is pushed outward
        (away from the primary) until it is free.
        """
        primary = placed[0]
        fallback: Optional[Tuple[int, Tuple[int, int], str, DisplayInfo]] = None
        for i, d in enumerate(pending):
            w, h = d.pixel_width, d.pixel_height
            adjacent = []
            for p in placed:
                found = _adjacent_placement(p, d)
                if found is not None:
                    adjacent.append((found[0], found[1], p))
            for origin, _side, _p in adjacent:
                if _is_free((origin[0], origin[1], w, h), placed):
                    return i, origin
            if adjacent and fallback is None:
                fallback = (i, adjacent[0][0], adjacent[0][1], adjacent[0][2])
        if fallback is not None:
            i, origin, side, p = fallback
            d = pending[i]
            for o2, s2, p2 in [(origin, side, p)] + [
                    (f[0], f[1], q) for q in placed if q is not p
                    for f in [_adjacent_placement(q, d)] if f is not None]:
                slid = _slide_along_edge(o2, s2, d, p2, placed)
                if slid is not None:
                    return i, slid
            return i, _push_outward(origin, d, primary, placed)
        # nothing touches: place relative to the nearest display
        best = None
        for i, d in enumerate(pending):
            cx, cy = d.x + d.width / 2, d.y + d.height / 2
            for p in placed:
                dist = p.distance_points(cx, cy)
                if best is None or dist < best[0]:
                    best = (dist, i, p)
        assert best is not None
        _, i, p = best
        d = pending[i]
        origin = (p.pixel_left + _round((d.x - p.x) * p.scale),
                  p.pixel_top + _round((d.y - p.y) * p.scale))
        if not _is_free((origin[0], origin[1], d.pixel_width, d.pixel_height), placed):
            origin = _push_outward(origin, d, primary, placed)
        return i, origin

    # -- lookup -------------------------------------------------------------------
    @property
    def primary(self) -> DisplayInfo:
        return self.displays[0]

    def display_at_points(self, x: float, y: float) -> DisplayInfo:
        """Display containing the point, else the nearest one."""
        for d in self.displays:
            if d.contains_points(x, y):
                return d
        return min(self.displays, key=lambda d: d.distance_points(x, y))

    def display_at_pixels(self, x: float, y: float) -> DisplayInfo:
        for d in self.displays:
            if d.contains_pixels(x, y):
                return d
        return min(self.displays, key=lambda d: d.distance_pixels(x, y))

    # -- conversions ---------------------------------------------------------------
    def point_to_pixel(self, x: float, y: float) -> Point:
        px, py = self.display_at_points(x, y).points_to_pixels(x, y)
        return Point(_round(px), _round(py))

    def pixel_to_point(self, x: float, y: float) -> Tuple[float, float]:
        return self.display_at_pixels(x, y).pixels_to_points(x, y)

    def points_rect_to_pixels(self, x: float, y: float, w: float, h: float) -> Rect:
        d = self.display_at_points(x + w / 2, y + h / 2)
        left, top = d.points_to_pixels(x, y)
        right, bottom = d.points_to_pixels(x + w, y + h)
        return Rect.from_ltrb(_round(left), _round(top), _round(right), _round(bottom))

    def pixels_rect_to_points(self, rect: Rect) -> Tuple[float, float, float, float]:
        d = self.display_at_pixels(rect.left + rect.width / 2, rect.top + rect.height / 2)
        x, y = d.pixels_to_points(rect.left, rect.top)
        return (x, y, rect.width / d.scale, rect.height / d.scale)

    @property
    def virtual_pixel_rect(self) -> Rect:
        r = self.displays[0].pixel_rect
        for d in self.displays[1:]:
            r = r.union(d.pixel_rect)
        return r

    def monitors(self) -> List[Monitor]:
        return [Monitor(index=i, rect=d.pixel_rect, scale=d.scale, is_primary=(i == 0), name=d.name)
                for i, d in enumerate(self.displays)]


def capture_plan(display: DisplayInfo, region: Rect) -> Tuple[Tuple[int, int, int, int], Tuple[int, int], Tuple[int, int, int, int]]:
    """Plan the capture of ``region`` (pixels, already clipped to ``display``).

    Returns ``(points_rect, expected_size, crop_box)``: grab ``points_rect``
    (``x, y, w, h`` integer global points enclosing the region), resize the
    result to ``expected_size`` pixels if the grabber returned another size, and
    crop ``crop_box`` (PIL ``ltrb``) to get exactly ``region``.
    """
    s = display.scale
    lx0 = region.left - display.pixel_left
    ly0 = region.top - display.pixel_top
    lx1 = region.right - display.pixel_left
    ly1 = region.bottom - display.pixel_top
    px0 = max(0, int(math.floor(lx0 / s + 1e-9)))
    py0 = max(0, int(math.floor(ly0 / s + 1e-9)))
    px1 = min(int(math.ceil(display.width)), int(math.ceil(lx1 / s - 1e-9)))
    py1 = min(int(math.ceil(display.height)), int(math.ceil(ly1 / s - 1e-9)))
    px1 = max(px1, px0 + 1)
    py1 = max(py1, py0 + 1)
    pts = (_round(display.x) + px0, _round(display.y) + py0, px1 - px0, py1 - py0)
    expected = (_round((px1 - px0) * s), _round((py1 - py0) * s))
    ox = lx0 - _round(px0 * s)
    oy = ly0 - _round(py0 * s)
    crop = (ox, oy, ox + region.width, oy + region.height)
    return pts, expected, crop


# =============================================================================
# Accessibility role mapping
# =============================================================================

_AX_ROLE_MAP: Dict[str, str] = {
    "AXApplication": "application", "AXSystemWide": "desktop", "AXWindow": "window",
    "AXSheet": "dialog", "AXPopover": "dialog", "AXDrawer": "pane",
    "AXGroup": "group", "AXSplitGroup": "group", "AXRadioGroup": "group", "AXLayoutItem": "group",
    "AXMatte": "group", "AXRuler": "group", "AXColumn": "group",
    "AXLayoutArea": "pane", "AXScrollArea": "pane",
    "AXToolbar": "toolbar", "AXMenuBar": "menubar", "AXMenuBarItem": "menuitem",
    "AXMenu": "menu", "AXMenuItem": "menuitem",
    "AXButton": "button", "AXMenuButton": "button", "AXDisclosureTriangle": "button",
    "AXColorWell": "button", "AXDockItem": "button",
    "AXCheckBox": "checkbox", "AXRadioButton": "radiobutton",
    "AXPopUpButton": "combobox", "AXComboBox": "combobox",
    "AXTextField": "edit", "AXTextArea": "edit", "AXDateField": "edit", "AXTimeField": "edit",
    "AXStaticText": "text", "AXHeading": "header", "AXLink": "link", "AXImage": "image",
    "AXList": "list", "AXOutline": "tree", "AXBrowser": "tree",
    "AXTable": "table", "AXGrid": "table", "AXRow": "row", "AXCell": "cell",
    "AXTabGroup": "tablist", "AXSlider": "slider", "AXValueIndicator": "slider",
    "AXIncrementor": "spinbutton", "AXStepper": "spinbutton",
    "AXProgressIndicator": "progressbar", "AXBusyIndicator": "progressbar",
    "AXLevelIndicator": "progressbar", "AXRelevanceIndicator": "progressbar",
    "AXScrollBar": "scrollbar", "AXSplitter": "separator", "AXHelpTag": "tooltip",
    "AXWebArea": "document", "AXGrowArea": "unknown", "AXUnknown": "unknown", "AXHandle": "unknown",
}

_AX_SUBROLE_MAP: Dict[str, str] = {
    "AXDialog": "dialog", "AXSystemDialog": "dialog", "AXApplicationDialog": "dialog",
    "AXApplicationAlertDialog": "dialog",
    "AXToggle": "togglebutton", "AXSwitch": "checkbox", "AXTabButton": "tab",
    "AXOutlineRow": "treeitem", "AXTableRow": "row",
    "AXSecureTextField": "edit", "AXSearchField": "edit",
    "AXContentSeparator": "separator", "AXApplicationStatus": "statusbar",
    "AXDocument": "document", "AXDocumentArticle": "document", "AXDescriptionList": "list",
}

_CHECKABLE_ROLES = frozenset({"AXCheckBox", "AXRadioButton"})
_EDIT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXDateField", "AXTimeField"})


def map_ax_role(ax_role: Optional[str], subrole: Optional[str] = "") -> str:
    """Map an ``AXRole`` (+ optional ``AXSubrole``) to locum's portable role names.

    Unknown roles are passed through lowercased without the ``AX`` prefix
    (``"AXFancyThing"`` -> ``"fancything"``).
    """
    if subrole and subrole in _AX_SUBROLE_MAP:
        mapped = _AX_SUBROLE_MAP[subrole]
        # a "dialog" subrole only makes sense on windows; a toggle subrole on buttons/checkboxes
        if mapped != "dialog" or ax_role in ("AXWindow", "AXSheet", "AXGroup", None, ""):
            return mapped
    if not ax_role:
        return "unknown"
    if ax_role in _AX_ROLE_MAP:
        return _AX_ROLE_MAP[ax_role]
    name = ax_role[2:] if ax_role.startswith("AX") else ax_role
    return name.lower() or "unknown"


# =============================================================================
# Window matching (CGWindowList record <-> AX window)
# =============================================================================


def _record_bounds(rec: Dict[str, Any]) -> Tuple[float, float, float, float]:
    b = rec.get("kCGWindowBounds") or {}
    try:
        return (float(b.get("X", 0)), float(b.get("Y", 0)), float(b.get("Width", 0)), float(b.get("Height", 0)))
    except AttributeError:  # pragma: no cover - CGRect-like
        return (float(b[0][0]), float(b[0][1]), float(b[1][0]), float(b[1][1]))


def match_window_record(records: Iterable[Dict[str, Any]], pid: Optional[int],
                        bounds: Optional[Tuple[float, float, float, float]], title: str = "",
                        tolerance: float = 2.0) -> Optional[int]:
    """Find the ``kCGWindowNumber`` of the CG window matching an AX window.

    Candidates must belong to ``pid`` and be on layer 0. Bounds must match
    within ``tolerance`` points; when several match, the one whose title equals
    ``title`` wins. Falls back to a unique title match, then to the pid's only
    window.
    """
    cands = [r for r in records
             if (pid is None or int(r.get("kCGWindowOwnerPID", -1)) == pid)
             and int(r.get("kCGWindowLayer", 0)) == 0]
    if not cands:
        return None

    def num(r: Dict[str, Any]) -> int:
        return int(r.get("kCGWindowNumber", 0))

    if bounds is not None:
        hits = [r for r in cands
                if all(abs(a - b) <= tolerance for a, b in zip(_record_bounds(r), bounds))]
        if len(hits) == 1:
            return num(hits[0])
        if hits:
            titled = [r for r in hits if title and (r.get("kCGWindowName") or "") == title]
            return num(titled[0] if titled else hits[0])
    if title:
        titled = [r for r in cands if (r.get("kCGWindowName") or "") == title]
        if len(titled) == 1:
            return num(titled[0])
    if len(cands) == 1:
        return num(cands[0])
    return None


def _plausible_record(rec: Dict[str, Any]) -> bool:
    """A layer-0, non-transparent, non-degenerate window (what a user would call a window)."""
    if int(rec.get("kCGWindowLayer", 0)) != 0:
        return False
    if float(rec.get("kCGWindowAlpha", 1.0) or 0.0) <= 0.0:
        return False
    if str(rec.get("kCGWindowOwnerName") or "") == "Window Server":
        return False
    _, _, w, h = _record_bounds(rec)
    return w > 1 and h > 1


def pair_ax_windows(records: Iterable[Dict[str, Any]],
                    ax_windows: Sequence[Tuple[Optional[Tuple[float, float, float, float]], str, Optional[int]]],
                    tolerance: float = 2.0) -> Dict[int, int]:
    """Pair one app's AX windows with its CG windows, one-to-one.

    ``records`` are the app's ``CGWindowList`` records; ``ax_windows`` holds
    ``(bounds_points, title, window_id)`` per AX window, where ``window_id`` is
    the exact ``CGWindowID`` when known (``_AXUIElementGetWindow``) or ``None``.
    Returns ``{ax_index: kCGWindowNumber}``.

    Exact ids win. The rest are paired in rounds, each accepting only pairs that
    are unique on *both* sides: equal frame and equal title; equal frame and no
    title conflict; equal frame (CG and AX titles can differ, e.g. Java apps);
    equal (non-empty) title; and finally a single leftover AX
    window with a single leftover record whose titles do not conflict. Windows
    that stay ambiguous (two untitled windows with the same frame) are left
    unpaired rather than guessed, so two AX windows never share a handle and an
    action never lands on the wrong window.
    """
    recs = [r for r in records if _plausible_record(r)]
    nums = [int(r.get("kCGWindowNumber", 0)) for r in recs]
    rtitles = [str(r.get("kCGWindowName") or "") for r in recs]
    rbounds = [_record_bounds(r) for r in recs]
    out: Dict[int, int] = {}
    used: Set[int] = set()
    for i, (_, _, wid) in enumerate(ax_windows):
        if wid is not None and wid in nums:
            j = nums.index(wid)
            if j not in used:
                out[i] = j
                used.add(j)

    def same_frame(i: int, j: int) -> bool:
        b = ax_windows[i][0]
        return b is not None and all(abs(p - q) <= tolerance for p, q in zip(b, rbounds[j]))

    def same_title(i: int, j: int) -> bool:
        return bool(ax_windows[i][1]) and ax_windows[i][1] == rtitles[j]

    def no_conflict(i: int, j: int) -> bool:
        return not ax_windows[i][1] or not rtitles[j] or ax_windows[i][1] == rtitles[j]

    rounds: List[Callable[[int, int], bool]] = [
        lambda i, j: same_frame(i, j) and same_title(i, j),
        lambda i, j: same_frame(i, j) and no_conflict(i, j),
        same_frame,
        same_title,
        no_conflict,
    ]
    for k, ok in enumerate(rounds):
        progress = True
        while progress:
            progress = False
            free_a = [i for i in range(len(ax_windows)) if i not in out]
            free_r = [j for j in range(len(recs)) if j not in used]
            if k == len(rounds) - 1 and (len(free_a) != 1 or len(free_r) != 1):
                break
            for i in free_a:
                cands = [j for j in free_r if ok(i, j)]
                if len(cands) == 1 and sum(1 for i2 in free_a if ok(i2, cands[0])) == 1:
                    out[i] = cands[0]
                    used.add(cands[0])
                    progress = True
                    break
    return {i: nums[j] for i, j in out.items()}


# =============================================================================
# Thin wrapper over the HIServices accessibility API
# =============================================================================

_AX_POINT_RE = re.compile(r"x:\s*(-?[\d.]+)\s+y:\s*(-?[\d.]+)")
_AX_SIZE_RE = re.compile(r"w:\s*(-?[\d.]+)\s+h:\s*(-?[\d.]+)")


class _AXMissing:
    """Marker for an attribute whose value is an AXError (absent/unsupported)."""


_MISSING = _AXMissing()


class _AXApi:
    """Every accessibility call goes through this object (makes the node logic testable)."""

    def __init__(self, mod: Any, quartz: Any = None):
        self.m = mod
        self.q = quartz
        self.system_wide = mod.AXUIElementCreateSystemWide()
        try:  # global timeout: a hung app must not hang the automation
            mod.AXUIElementSetMessagingTimeout(self.system_wide, 1.5)
        except Exception:
            pass

    # -- trust ------------------------------------------------------------------------
    def is_trusted(self) -> bool:
        try:
            return bool(self.m.AXIsProcessTrusted())
        except Exception:
            return False

    def prompt_trust(self) -> bool:
        try:
            key = getattr(self.m, "kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt")
            return bool(self.m.AXIsProcessTrustedWithOptions({key: True}))
        except Exception:
            return self.is_trusted()

    # -- elements -----------------------------------------------------------------------
    def application(self, pid: int) -> Any:
        return self.m.AXUIElementCreateApplication(int(pid))

    def element_at(self, x: float, y: float) -> Any:
        try:
            err, el = self.m.AXUIElementCopyElementAtPosition(self.system_wide, float(x), float(y), None)
        except Exception:
            return None
        return el if err == kAXErrorSuccess else None

    def pid(self, el: Any) -> Optional[int]:
        try:
            err, pid = self.m.AXUIElementGetPid(el, None)
        except Exception:
            return None
        return int(pid) if err == kAXErrorSuccess else None

    def same(self, a: Any, b: Any) -> bool:
        try:
            return bool(a == b)
        except Exception:
            return a is b

    def window_id(self, el: Any) -> Optional[int]:
        """The exact ``CGWindowID`` of an AX window (private but long-stable
        ``_AXUIElementGetWindow``), or ``None`` when unavailable."""
        fn = self._get_window
        if fn is None:
            return None
        try:
            import ctypes
            import objc  # type: ignore

            out = ctypes.c_uint32(0)
            if fn(ctypes.c_void_p(objc.pyobjc_id(el)), ctypes.byref(out)) != kAXErrorSuccess:
                return None
            return int(out.value) or None
        except Exception:
            return None

    @property
    def _get_window(self) -> Any:
        if not hasattr(self, "_get_window_fn"):
            self._get_window_fn = None
            try:
                import ctypes
                import ctypes.util

                path = ctypes.util.find_library("ApplicationServices")
                fn = getattr(ctypes.cdll.LoadLibrary(path), "_AXUIElementGetWindow") if path else None
                if fn is not None:
                    fn.restype = ctypes.c_int32
                    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
                    self._get_window_fn = fn
            except Exception:
                self._get_window_fn = None
        return self._get_window_fn

    # -- attributes ------------------------------------------------------------------------
    def _convert(self, val: Any) -> Any:
        if val is None:
            return None
        tname = type(val).__name__
        if "AXValue" in tname:
            try:
                typ = self.m.AXValueGetType(val)
            except Exception:
                typ = None
            if typ == kAXValueAXErrorType:
                return _MISSING
            if typ is not None:
                try:
                    ok, out = self.m.AXValueGetValue(val, typ, None)
                except Exception:
                    ok, out = False, None
                if ok:
                    if typ == kAXValueCGPointType:
                        return (float(out.x), float(out.y))
                    if typ == kAXValueCGSizeType:
                        return (float(out.width), float(out.height))
                    if typ == kAXValueCGRectType:
                        return (float(out.origin.x), float(out.origin.y),
                                float(out.size.width), float(out.size.height))
                    if typ == kAXValueCFRangeType:
                        return (int(out.location), int(out.length))
            text = str(val)
            m = _AX_POINT_RE.search(text)
            if m:
                return (float(m.group(1)), float(m.group(2)))
            m = _AX_SIZE_RE.search(text)
            if m:
                return (float(m.group(1)), float(m.group(2)))
            return None
        if isinstance(val, str):
            return str(val)
        return val

    def attr(self, el: Any, name: str, default: Any = None) -> Any:
        try:
            err, val = self.m.AXUIElementCopyAttributeValue(el, name, None)
        except Exception:
            return default
        if err != kAXErrorSuccess:
            return default
        out = self._convert(val)
        return default if out is None or out is _MISSING else out

    def attrs(self, el: Any, names: Sequence[str]) -> Dict[str, Any]:
        """Fetch several attributes in one IPC round-trip (missing ones are omitted)."""
        out: Dict[str, Any] = {}
        try:
            err, values = self.m.AXUIElementCopyMultipleAttributeValues(el, list(names), 0, None)
        except Exception:
            err, values = -1, None
        if err == kAXErrorSuccess and values is not None and len(values) == len(names):
            for name, val in zip(names, values):
                v = self._convert(val)
                if v is not None and v is not _MISSING:
                    out[name] = v
            return out
        for name in names:
            v = self.attr(el, name, _MISSING)
            if v is not _MISSING:
                out[name] = v
        return out

    def attr_names(self, el: Any) -> List[str]:
        try:
            err, names = self.m.AXUIElementCopyAttributeNames(el, None)
        except Exception:
            return []
        return [str(n) for n in names] if err == kAXErrorSuccess and names else []

    def settable(self, el: Any, name: str) -> bool:
        try:
            err, ok = self.m.AXUIElementIsAttributeSettable(el, name, None)
        except Exception:
            return False
        return err == kAXErrorSuccess and bool(ok)

    def set_attr(self, el: Any, name: str, value: Any) -> bool:
        try:
            return self.m.AXUIElementSetAttributeValue(el, name, value) == kAXErrorSuccess
        except Exception:
            return False

    def set_point(self, el: Any, name: str, x: float, y: float) -> bool:
        try:
            pt = self.q.CGPointMake(float(x), float(y)) if self.q is not None else (float(x), float(y))
            return self.set_attr(el, name, self.m.AXValueCreate(kAXValueCGPointType, pt))
        except Exception:
            return False

    def set_size(self, el: Any, name: str, w: float, h: float) -> bool:
        try:
            sz = self.q.CGSizeMake(float(w), float(h)) if self.q is not None else (float(w), float(h))
            return self.set_attr(el, name, self.m.AXValueCreate(kAXValueCGSizeType, sz))
        except Exception:
            return False

    # -- actions --------------------------------------------------------------------------
    def action_names(self, el: Any) -> List[str]:
        try:
            err, names = self.m.AXUIElementCopyActionNames(el, None)
        except Exception:
            return []
        return [str(n) for n in names] if err == kAXErrorSuccess and names else []

    def perform(self, el: Any, action: str) -> bool:
        try:
            return self.m.AXUIElementPerformAction(el, action) == kAXErrorSuccess
        except Exception:
            return False


# =============================================================================
# Accessible node
# =============================================================================

_UNSET: Any = object()

_BASIC_ATTRS = (
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXIdentifier",
    "AXPosition", "AXSize", "AXEnabled", "AXFocused", "AXSelected", "AXExpanded",
    "AXDisclosing", "AXMinimized", "AXHelp", "AXRoleDescription", "AXModal", "AXPlaceholderValue",
)

_ACTION_NAMES = {
    "AXPress": "invoke", "AXConfirm": "invoke", "AXPick": "select", "AXOpen": "invoke",
    "AXShowMenu": "show_menu", "AXIncrement": "increment", "AXDecrement": "decrement",
    "AXRaise": "raise", "AXScrollToVisible": "scroll_into_view", "AXCancel": "cancel",
    "AXShowAlternateUI": "show_alternate_ui", "AXShowDefaultUI": "show_default_ui",
}


def _value_to_str(v: Any) -> Optional[str]:
    if v is None or v is _MISSING:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(int(v))
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(float(v))
    if isinstance(v, str):
        return str(v)
    if isinstance(v, (tuple, list)):
        return None
    tname = type(v).__name__
    if "AXUIElement" in tname:
        return None
    try:
        return str(v)
    except Exception:
        return None


class AXNode(AccessibleNode):
    """An ``AXUIElementRef`` exposed through the portable :class:`AccessibleNode` API.

    Attribute values are fetched in one batched IPC call and cached for
    :attr:`CACHE_TTL` seconds; :meth:`refresh` drops the cache. ``bounds`` are
    converted from Quartz points to backend pixels.
    """

    CACHE_TTL = 0.5
    MAX_CHILDREN = 10000

    def __init__(self, element: Any, backend: "MacOSBackend", *, app: Optional[str] = None,
                 pid: Optional[int] = None, handle: Any = _UNSET, parent: Any = _UNSET):
        self._el = element
        self._be = backend
        self._ax: _AXApi = backend._ax  # type: ignore[assignment]
        self._app = app
        self._pid = pid
        self._handle = handle
        self._parent = parent
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time = 0.0

    # -- raw access ---------------------------------------------------------------------------
    @property
    def element(self) -> Any:
        """The underlying ``AXUIElementRef``."""
        return self._el

    def _attrs(self) -> Dict[str, Any]:
        now = time.monotonic()
        if self._cache is None or now - self._cache_time > self.CACHE_TTL:
            try:
                self._cache = self._ax.attrs(self._el, _BASIC_ATTRS)
            except Exception:
                self._cache = {}
            self._cache_time = now
        return self._cache

    def refresh(self) -> "AXNode":
        self._cache = None
        return self

    def get(self, attribute: str, default: Any = None) -> Any:
        """Read any raw AX attribute (``node.get("AXURL")``)."""
        if attribute in _BASIC_ATTRS:
            return self._attrs().get(attribute, default)
        return self._ax.attr(self._el, attribute, default)

    def perform(self, action: str) -> bool:
        """Perform a raw AX action (``node.perform("AXShowMenu")``)."""
        ok = self._ax.perform(self._el, action)
        self._cache = None
        return ok

    def _set(self, attribute: str, value: Any) -> bool:
        ok = self._ax.set_attr(self._el, attribute, value)
        self._cache = None
        return ok

    # -- identity -------------------------------------------------------------------------------
    @property
    def native_role(self) -> str:
        return str(self._attrs().get("AXRole") or "")

    @property
    def subrole(self) -> str:
        return str(self._attrs().get("AXSubrole") or "")

    @property
    def role(self) -> str:
        return map_ax_role(self.native_role, self.subrole)

    @property
    def name(self) -> str:
        a = self._attrs()
        for key in ("AXTitle", "AXDescription"):
            v = a.get(key)
            if isinstance(v, str) and v.strip():
                return v
        native = a.get("AXRole")
        if native == "AXStaticText":
            v = _value_to_str(a.get("AXValue"))
            if v:
                return v
        if native in _EDIT_ROLES or native in ("AXPopUpButton", "AXSlider", "AXCheckBox", "AXRadioButton"):
            label_el = self._ax.attr(self._el, "AXTitleUIElement")
            if label_el is not None:
                label = self._ax.attr(label_el, "AXValue") or self._ax.attr(label_el, "AXTitle")
                if isinstance(label, str) and label.strip():
                    return label
            ph = a.get("AXPlaceholderValue")
            if isinstance(ph, str) and ph:
                return ph
        return ""

    @property
    def value(self) -> Optional[str]:
        return _value_to_str(self._attrs().get("AXValue"))

    @property
    def automation_id(self) -> str:
        return str(self._attrs().get("AXIdentifier") or "")

    @property
    def class_name(self) -> str:
        a = self._attrs()
        return str(a.get("AXSubrole") or a.get("AXRole") or "")

    @property
    def description(self) -> str:
        a = self._attrs()
        help_text = a.get("AXHelp")
        if isinstance(help_text, str) and help_text:
            return help_text
        desc = a.get("AXDescription")
        if isinstance(desc, str) and desc and desc != self.name:
            return desc
        return ""

    @property
    def bounds(self) -> Optional[Rect]:
        a = self._attrs()
        pos, size = a.get("AXPosition"), a.get("AXSize")
        if not pos or not size:
            return None
        try:
            return self._be._points_rect_to_pixels(pos[0], pos[1], size[0], size[1])
        except Exception:
            return None

    @property
    def states(self) -> frozenset:
        a = self._attrs()
        native = a.get("AXRole")
        st: Set[str] = set()
        enabled = a.get("AXEnabled")
        if enabled is False:
            st.add("disabled")
        else:
            st.add("enabled")
        if a.get("AXFocused") is True:
            st.update(("focused", "focusable"))
        if a.get("AXSelected") is True:
            st.add("selected")
        if native in _CHECKABLE_ROLES or a.get("AXSubrole") in ("AXToggle", "AXSwitch"):
            st.add("checkable")
            v = a.get("AXValue")
            if v is not None and v is not _MISSING and not isinstance(v, str):
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    iv = 0
                if iv == 1:
                    st.add("checked")
                elif iv == 2:
                    st.add("mixed")
        for key in ("AXExpanded", "AXDisclosing"):
            if key in a:
                st.add("expanded" if a[key] else "collapsed")
                break
        if a.get("AXMinimized") is True:
            st.add("minimized")
        if a.get("AXModal") is True:
            st.add("modal")
        if native in _EDIT_ROLES:
            st.add("editable" if enabled is not False else "readonly")
        b = self.bounds
        if b is not None and not b.is_empty and self._be._rect_on_screen(b):
            st.add("visible")
        elif b is not None:
            st.add("offscreen")
        return frozenset(st)

    @property
    def pid(self) -> Optional[int]:
        if self._pid is None:
            self._pid = self._ax.pid(self._el)
        return self._pid

    @property
    def app(self) -> str:
        if self._app is None:
            pid = self.pid
            self._app = self._be._app_name(pid) if pid is not None else ""
        return self._app or ""

    @property
    def handle(self) -> Any:
        if self._handle is _UNSET:
            self._handle = None
            try:
                win = self._el if self.native_role == "AXWindow" else self._ax.attr(self._el, "AXWindow")
                if win is not None:
                    self._handle = self._be._handle_for_ax_window(win, self.pid)
            except Exception:
                self._handle = None
        return self._handle

    def attributes(self) -> Dict[str, Any]:
        a = self._attrs()
        out: Dict[str, Any] = {"app": self.app, "native_role": self.native_role}
        for key, alias in (("AXSubrole", "subrole"), ("AXRoleDescription", "role_description"),
                           ("AXIdentifier", "identifier"), ("AXHelp", "help"),
                           ("AXPlaceholderValue", "placeholder")):
            v = a.get(key)
            if isinstance(v, str) and v:
                out[alias] = v
        return out

    # -- tree ------------------------------------------------------------------------------------
    def _wrap(self, el: Any, parent: Any = _UNSET) -> "AXNode":
        return AXNode(el, self._be, app=self._app, pid=self._pid,
                      handle=self._handle if self.native_role != "AXApplication" else _UNSET,
                      parent=parent)

    def children(self) -> Sequence["AXNode"]:
        kids = self._ax.attr(self._el, "AXChildren") or []
        try:
            kids = list(kids)[: self.MAX_CHILDREN]
        except TypeError:
            return []
        return [self._wrap(k, parent=self) for k in kids if k is not None]

    @property
    def parent(self) -> Optional["AXNode"]:
        if self._parent is _UNSET:
            p = self._ax.attr(self._el, "AXParent")
            self._parent = AXNode(p, self._be, app=self._app, pid=self._pid) if p is not None else None
        return self._parent

    # -- actions ------------------------------------------------------------------------------------
    def actions(self) -> List[str]:
        out: List[str] = []
        native = self._ax.action_names(self._el)
        for n in native:
            mapped = _ACTION_NAMES.get(n)
            if mapped and mapped not in out:
                out.append(mapped)
        if self.native_role in _CHECKABLE_ROLES and "AXPress" in native and "toggle" not in out:
            out.append("toggle")
        if self._ax.settable(self._el, "AXValue"):
            out.append("set_value")
        if self._ax.settable(self._el, "AXFocused"):
            out.append("focus")
        a = self._attrs()
        if "AXExpanded" in a or "AXDisclosing" in a:
            out.extend(x for x in ("expand", "collapse") if x not in out)
        if "select" not in out and self._ax.settable(self._el, "AXSelected"):
            out.append("select")
        return out

    def invoke(self) -> bool:
        native = self._ax.action_names(self._el)
        for action in ("AXPress", "AXConfirm", "AXPick", "AXOpen"):
            if action in native:
                return self.perform(action)
        return False

    def set_value(self, text: str) -> bool:
        if self._set("AXValue", text):
            return True
        self._set("AXFocused", True)
        return self._set("AXValue", text)

    def focus(self) -> bool:
        if self.native_role == "AXWindow":
            pid = self.pid
            if pid is not None:
                self._be._activate_pid(pid)
            self._set("AXMain", True)
            raised = self.perform("AXRaise")
            return self._set("AXFocused", True) or raised
        return self._set("AXFocused", True)

    def toggle(self) -> bool:
        if self.native_role in _CHECKABLE_ROLES or self.subrole in ("AXToggle", "AXSwitch"):
            return self.perform("AXPress")
        return False

    def _set_expanded(self, expanded: bool) -> bool:
        a = self._attrs()
        if "AXExpanded" in a:
            if bool(a["AXExpanded"]) == expanded:
                return True
            if self._set("AXExpanded", expanded):
                return True
        if "AXDisclosing" in a:
            if bool(a["AXDisclosing"]) == expanded:
                return True
            if self._set("AXDisclosing", expanded):
                return True
        native = a.get("AXRole")
        if native == "AXDisclosureTriangle":
            current = a.get("AXValue")
            if current is not None and bool(current) == expanded:
                return True
            return self.perform("AXPress")
        if native in ("AXPopUpButton", "AXComboBox", "AXMenuButton"):
            if expanded:
                return self.perform("AXShowMenu") or self.perform("AXPress")
            return self.perform("AXCancel")
        return False

    def expand(self) -> bool:
        return self._set_expanded(True)

    def collapse(self) -> bool:
        return self._set_expanded(False)

    def select(self) -> bool:
        if self._set("AXSelected", True):
            return True
        native = self._ax.action_names(self._el)
        if "AXPick" in native:
            return self.perform("AXPick")
        if self.subrole == "AXTabButton" and "AXPress" in native:
            return self.perform("AXPress")
        if self.native_role == "AXRow":
            parent = self._ax.attr(self._el, "AXParent")
            if parent is not None and self._ax.set_attr(parent, "AXSelectedRows", [self._el]):
                self._cache = None
                return True
        return False

    def scroll_into_view(self) -> bool:
        return self.perform("AXScrollToVisible")

    def same_as(self, other: AccessibleNode) -> bool:
        if not isinstance(other, AXNode):
            return False
        return self is other or self._ax.same(self._el, other._el)


# =============================================================================
# The backend
# =============================================================================

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


class MacOSBackend(Backend):
    """Native macOS backend (Quartz events, CGWindowList, AXUIElement, NSPasteboard).

    All coordinates are physical pixels (see the module docstring); conversion
    to/from Quartz points happens per display.

    Args:
        check_permissions: verify Accessibility permission before posting input
            and raise a helpful :class:`BackendError` instead of letting macOS
            silently drop the events.
        double_click_interval: seconds between presses that still count as a
            multi-click (default: the user's system setting).
        layout_ttl: seconds the display layout is cached before re-querying.

    Raises:
        BackendUnavailable: not running on macOS, or pyobjc is not installed.
    """

    name = "macos"

    def __init__(self, *, check_permissions: bool = True, double_click_interval: Optional[float] = None,
                 layout_ttl: float = 1.0):
        if sys.platform != "darwin":
            raise BackendUnavailable(f"MacOSBackend requires macOS (this is {sys.platform!r})")
        try:
            import Quartz  # type: ignore
        except ImportError as exc:
            raise BackendUnavailable(f"pyobjc Quartz is not installed ({exc}); {_INSTALL_HINT}") from exc
        try:
            import AppKit  # type: ignore
        except ImportError:
            AppKit = None
        ax: Optional[_AXApi] = None
        try:
            import ApplicationServices  # type: ignore

            ax = _AXApi(ApplicationServices, Quartz)
        except Exception:
            ax = None
        if double_click_interval is None and AppKit is not None:
            try:
                double_click_interval = float(AppKit.NSEvent.doubleClickInterval())
            except Exception:
                double_click_interval = None
        try:
            kbd_layouts: Any = _CarbonKeyboardLayouts()
        except Exception:  # pragma: no cover - Carbon is always present on macOS
            kbd_layouts = None
        self._setup(Quartz, AppKit, ax, check_permissions=check_permissions,
                    double_click_interval=double_click_interval or 0.5, layout_ttl=layout_ttl,
                    keyboard_layouts=kbd_layouts)
        self._keyboard_map()  # read the layout now (we are most likely on the main thread)

    @classmethod
    def _from_parts(cls, quartz: Any, appkit: Any = None, ax: Any = None, *, layout: Optional[DisplayLayout] = None,
                    check_permissions: bool = False, double_click_interval: float = 0.5,
                    clock: Callable[[], float] = time.monotonic,
                    keyboard_layout: Optional[KeyboardLayoutMap] = None,
                    keyboard_layouts: Any = None) -> "MacOSBackend":
        """Build a backend from explicit native modules (used for tests and embedding).

        With ``layout`` given the display layout is fixed and never re-queried.
        ``keyboard_layout`` fixes the keyboard layout map (default: US-ANSI);
        ``keyboard_layouts`` is a provider with a ``current(known_fingerprint)``
        method (like :class:`_CarbonKeyboardLayouts`) that is polled for changes.
        """
        self = cls.__new__(cls)
        self._setup(quartz, appkit, ax, check_permissions=check_permissions,
                    double_click_interval=double_click_interval, layout_ttl=1.0, clock=clock,
                    keyboard_layouts=keyboard_layouts)
        if layout is not None:
            self._layout = layout
            self._layout_ttl = None
        if keyboard_layout is not None:
            self._kbd_map = keyboard_layout
            self._kbd_fixed = True
        return self

    def _setup(self, quartz: Any, appkit: Any, ax: Optional[_AXApi], *, check_permissions: bool,
               double_click_interval: float, layout_ttl: Optional[float],
               clock: Callable[[], float] = time.monotonic, keyboard_layouts: Any = None) -> None:
        self._q = quartz
        self._appkit = appkit
        self._ax = ax
        self._check_permissions = check_permissions
        self._lock = threading.RLock()
        self._clicks = ClickTracker(interval=double_click_interval, clock=clock)
        self._layout: Optional[DisplayLayout] = None
        self._layout_time = 0.0
        self._layout_ttl = layout_ttl
        self._held_buttons: List[MouseButton] = []
        self._held_mods: Dict[str, int] = {}
        self._pressed_keys: Dict[str, KeySpec] = {}
        self._app_names: Dict[int, str] = {}
        self._restore_rects: Dict[Any, Tuple[float, float, float, float]] = {}
        self._tls = threading.local()
        self._trusted = False
        self._warned_capture = False
        self._closed = False
        self._clock = clock
        self._kbd_provider = keyboard_layouts
        self._kbd_map: KeyboardLayoutMap = KeyboardLayoutMap.us_ansi()
        self._kbd_fingerprint: Optional[bytes] = None
        self._kbd_checked: Optional[float] = None
        self._warned_layout = False
        self._kbd_fixed = False
        try:
            self._source = quartz.CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
        except Exception:
            self._source = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def capabilities(self) -> frozenset:
        caps = {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_SCREENSHOT, CAP_WINDOWS, CAP_HSCROLL}
        if self._appkit is not None:
            caps.add(CAP_CLIPBOARD)
        if self._ax is not None:
            caps.add(CAP_ACCESSIBILITY)
        return frozenset(caps)

    def close(self) -> None:
        """Release any buttons/keys still held by this backend (never leave stuck input)."""
        if self._closed:
            return
        try:
            self.release_all()
        except Exception:
            pass
        sct = getattr(self._tls, "mss", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
            self._tls.mss = None
        self._closed = True

    def release_all(self) -> None:
        """Release every mouse button and key pressed through this backend."""
        for button in list(self._held_buttons):
            self.mouse_up(button)
        for key in list(self._pressed_keys):
            self.key_up(key)
        for key in list(self._held_mods):
            self.key_up(key)

    def __enter__(self) -> "MacOSBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- permissions
    def permissions(self) -> Dict[str, Optional[bool]]:
        """``{"accessibility": bool|None, "screen_recording": bool|None}`` (None = unknown)."""
        acc = self._ax.is_trusted() if self._ax is not None else None
        rec: Optional[bool] = None
        fn = getattr(self._q, "CGPreflightScreenCaptureAccess", None)
        if fn is not None:
            try:
                rec = bool(fn())
            except Exception:
                rec = None
        return {"accessibility": acc, "screen_recording": rec}

    def request_permissions(self) -> Dict[str, Optional[bool]]:
        """Show the macOS permission prompts for Accessibility and Screen Recording."""
        if self._ax is not None:
            self._ax.prompt_trust()
        fn = getattr(self._q, "CGRequestScreenCaptureAccess", None)
        if fn is not None:
            try:
                fn()
            except Exception:
                pass
        return self.permissions()

    def _require_trust(self) -> None:
        if self._trusted or not self._check_permissions or self._ax is None:
            return
        if self._ax.is_trusted():
            self._trusted = True
            return
        raise BackendError(ACCESSIBILITY_HELP.format(exe=sys.executable))

    def _require_ax(self) -> _AXApi:
        if self._ax is None:
            raise NotSupported(
                f"Accessibility API unavailable: pyobjc-framework-ApplicationServices is not installed; {_INSTALL_HINT}"
            )
        if not self._ax.is_trusted():
            raise BackendError(ACCESSIBILITY_HELP.format(exe=sys.executable))
        return self._ax

    # -------------------------------------------------------------------- displays
    def _query_displays(self) -> List[DisplayInfo]:
        q = self._q
        err, ids, count = q.CGGetActiveDisplayList(32, None, None)
        if err:
            raise BackendError(f"CGGetActiveDisplayList failed (error {err})")
        ids = list(ids or [])[: int(count or 0)]
        main = int(q.CGMainDisplayID())
        screens: Dict[int, Tuple[float, str, Optional[Tuple[float, float, float, float]]]] = {}
        if self._appkit is not None:
            try:
                ns_screens = list(self._appkit.NSScreen.screens() or [])
                primary_h = float(ns_screens[0].frame().size.height) if ns_screens else 0.0
                for s in ns_screens:
                    did = int(s.deviceDescription()["NSScreenNumber"])
                    name = str(s.localizedName()) if hasattr(s, "localizedName") else ""
                    vf = s.visibleFrame()
                    # Cocoa: bottom-left origin, y up -> Quartz: top-left origin, y down
                    work = (float(vf.origin.x), primary_h - float(vf.origin.y) - float(vf.size.height),
                            float(vf.size.width), float(vf.size.height))
                    screens[did] = (float(s.backingScaleFactor()), name, work)
            except Exception:
                screens = {}
        out: List[DisplayInfo] = []
        for did in ids:
            did = int(did)
            try:
                if q.CGDisplayMirrorsDisplay(did) not in (0, None):
                    continue  # mirror of another display: same pixels
            except Exception:
                pass
            b = q.CGDisplayBounds(did)
            x, y = float(b.origin.x), float(b.origin.y)
            w, h = float(b.size.width), float(b.size.height)
            scale, name, work = screens.get(did, (0.0, "", None))
            if not scale:
                scale = 1.0
                try:
                    mode = q.CGDisplayCopyDisplayMode(did)
                    pw = float(q.CGDisplayModeGetPixelWidth(mode))
                    if w > 0 and pw > 0:
                        scale = pw / w
                except Exception:
                    pass
            out.append(DisplayInfo(did, x, y, w, h, scale=scale, is_primary=(did == main),
                                   name=name or f"Display {did}", work_area=work))
        if not out:
            raise BackendError("No active displays")
        return out

    def _get_layout(self) -> DisplayLayout:
        now = time.monotonic()
        if self._layout is None or (self._layout_ttl is not None and now - self._layout_time > self._layout_ttl):
            self._layout = DisplayLayout(self._query_displays())
            self._layout_time = now
        return self._layout

    def _points_rect_to_pixels(self, x: float, y: float, w: float, h: float) -> Rect:
        return self._get_layout().points_rect_to_pixels(x, y, w, h)

    def _rect_on_screen(self, rect: Rect) -> bool:
        return any(d.pixel_rect.intersect(rect) is not None for d in self._get_layout().displays)

    def monitors(self) -> List[Monitor]:
        return self._get_layout().monitors()

    def to_points(self, x: float, y: float) -> Tuple[float, float]:
        """Convert backend pixel coordinates to Quartz global points."""
        return self._get_layout().pixel_to_point(x, y)

    def to_pixels(self, x: float, y: float) -> Point:
        """Convert Quartz global points to backend pixel coordinates."""
        return self._get_layout().point_to_pixel(x, y)

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

    # ---------------------------------------------------------------------- screen
    def _mss(self) -> Any:
        sct = getattr(self._tls, "mss", None)
        if sct is None:
            import mss  # hard dependency, but keep the import lazy

            sct = mss.mss()
            self._tls.mss = sct
        return sct

    def _grab_points(self, x: int, y: int, w: int, h: int) -> Any:
        from PIL import Image

        try:
            shot = self._mss().grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as mss_exc:
            try:
                return self._grab_points_cg(x, y, w, h)
            except Exception as cg_exc:
                raise BackendError(f"Screen capture failed (mss: {mss_exc}; Quartz: {cg_exc})") from cg_exc

    def _grab_points_cg(self, x: int, y: int, w: int, h: int) -> Any:
        from PIL import Image

        q = self._q
        img = q.CGWindowListCreateImage(q.CGRectMake(x, y, w, h), kCGWindowListOptionOnScreenOnly,
                                        kCGNullWindowID, kCGWindowImageDefault)
        if img is None:
            raise BackendError("CGWindowListCreateImage returned no image. " + SCREEN_RECORDING_HELP)
        width, height = int(q.CGImageGetWidth(img)), int(q.CGImageGetHeight(img))
        bpr = int(q.CGImageGetBytesPerRow(img))
        data = bytes(q.CGDataProviderCopyData(q.CGImageGetDataProvider(img)))
        return Image.frombuffer("RGB", (width, height), data, "raw", "BGRX", bpr, 1)

    def _warn_screen_recording(self) -> None:
        if self._warned_capture:
            return
        self._warned_capture = True
        if self.permissions().get("screen_recording") is False:
            warnings.warn(SCREEN_RECORDING_HELP, RuntimeWarning, stacklevel=3)

    def screenshot(self, region: Optional[Rect] = None) -> Any:
        """RGB screenshot of ``region`` (pixels; default: the whole virtual screen)."""
        from PIL import Image

        self._warn_screen_recording()
        layout = self._get_layout()
        region = layout.virtual_pixel_rect if region is None else Rect.coerce(region)
        if region.is_empty:
            raise ValueError(f"Empty screenshot region: {region}")
        out = Image.new("RGB", (region.width, region.height), (0, 0, 0))
        for d in layout.displays:
            part = d.pixel_rect.intersect(region)
            if part is None:
                continue
            pts, expected, crop = capture_plan(d, part)
            img = self._grab_points(*pts)
            if img.size != expected:
                img = img.resize(expected, Image.BILINEAR)
            out.paste(img.crop(crop), (part.left - region.left, part.top - region.top))
        return out

    # --------------------------------------------------------------------- windows
    def _window_records(self, options: int, relative: int = kCGNullWindowID) -> List[Dict[str, Any]]:
        recs = self._q.CGWindowListCopyWindowInfo(options, relative)
        return [dict(r) for r in (recs or [])]

    def _pump_appkit(self) -> bool:
        """Let AppKit process pending notifications so ``NSWorkspace`` and
        ``NSRunningApplication`` values are current.

        AppKit only refreshes them while the main run loop runs, which a plain
        Python script never does, so without this they stay a snapshot of the
        moment they were first read. Only possible on the main thread; returns
        whether the run loop was serviced."""
        appkit = self._appkit
        if appkit is None or threading.current_thread() is not threading.main_thread():
            return False
        try:
            run, mode = appkit.CFRunLoopRunInMode, appkit.kCFRunLoopDefaultMode
        except AttributeError:
            return False
        try:
            for _ in range(64):  # kCFRunLoopRunHandledSource == 4: more may be pending
                if run(mode, 0.0, True) != 4:
                    break
            return True
        except Exception:
            return False

    def _front_window_pid(self) -> Optional[int]:
        """Owner of the front-most layer-0 window (live window-server z-order)."""
        try:
            records = self._window_records(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements)
        except Exception:
            return None
        for rec in records:
            if _plausible_record(rec):
                return int(rec.get("kCGWindowOwnerPID", -1))
        return None

    def _frontmost_pid(self) -> Optional[int]:
        """Pid of the application that receives keyboard input.

        Read live: the system-wide AX ``AXFocusedApplication`` first. AppKit's
        ``frontmostApplication`` is only trusted after the run loop could be
        serviced (see :meth:`_pump_appkit`); otherwise the owner of the front-most
        window is used, and the possibly stale AppKit value only as a last resort."""
        ax = self._ax
        if ax is not None:
            try:
                el = ax.attr(ax.system_wide, "AXFocusedApplication")
                pid = ax.pid(el) if el is not None else None
                if pid is not None and pid > 0:
                    return int(pid)
            except Exception:
                pass
        if self._pump_appkit():
            pid = self._workspace_front_pid()
            if pid is not None:
                return pid
        pid = self._front_window_pid()
        if pid is not None:
            return pid
        return self._workspace_front_pid()

    def _workspace_front_pid(self) -> Optional[int]:
        if self._appkit is None:
            return None
        try:
            app = self._appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app is not None else None
        except Exception:
            return None

    def _app_name(self, pid: Optional[int]) -> str:
        if pid is None:
            return ""
        if pid in self._app_names:
            return self._app_names[pid]
        name = ""
        if self._appkit is not None:
            try:
                app = self._appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
                if app is not None:
                    name = str(app.localizedName() or "")
            except Exception:
                name = ""
        self._app_names[pid] = name
        return name

    def _activate_pid(self, pid: int) -> bool:
        if self._appkit is None:
            return False
        app = self._appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        if app is None:
            return False
        try:
            return bool(app.activateWithOptions_(_NS_ACTIVATE_ALL_WINDOWS | _NS_ACTIVATE_IGNORING_OTHER_APPS))
        except Exception:
            return False

    def _ax_windows(self, pid: int) -> List[Tuple[Any, Optional[Tuple[float, float, float, float]], str, bool,
                                                  Optional[int]]]:
        """``(element, bounds_points, title, minimized, window_id)`` for every AX window
        of ``pid`` (``window_id`` is the exact ``CGWindowID`` when it can be read)."""
        ax = self._ax
        if ax is None:
            return []
        get_id = getattr(ax, "window_id", None)
        out = []
        for el in ax.attr(ax.application(pid), "AXWindows") or []:
            a = ax.attrs(el, ("AXPosition", "AXSize", "AXTitle", "AXMinimized"))
            pos, size = a.get("AXPosition"), a.get("AXSize")
            bounds = (pos[0], pos[1], size[0], size[1]) if pos and size else None
            wid = get_id(el) if get_id is not None else None
            out.append((el, bounds, str(a.get("AXTitle") or ""), bool(a.get("AXMinimized")), wid))
        return out

    def _ax_pairing(self, pid: int, records: Optional[List[Dict[str, Any]]] = None) -> Tuple[list, Dict[int, int]]:
        """``(ax_windows, {ax_index: CGWindowID})`` for ``pid`` (see :func:`pair_ax_windows`)."""
        wins = self._ax_windows(pid)
        if records is None:
            records = self._window_records(kCGWindowListOptionAll)
        own = [r for r in records if int(r.get("kCGWindowOwnerPID", -1)) == pid]
        return wins, pair_ax_windows(own, [(b, t, wid) for _, b, t, _, wid in wins])

    def _handle_for_ax_window(self, win: Any, pid: Optional[int],
                              records: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
        ax = self._ax
        if ax is None:
            return None
        if pid is None:
            pid = ax.pid(win)
        if pid is None:
            return None
        if records is None:
            records = self._window_records(kCGWindowListOptionAll)
        wins, pairs = self._ax_pairing(pid, records)
        for i, w in enumerate(wins):
            if ax.same(w[0], win):
                return pairs.get(i)
        # not in AXWindows (e.g. a sheet): match it against the still unpaired windows
        a = ax.attrs(win, ("AXPosition", "AXSize", "AXTitle"))
        pos, size = a.get("AXPosition"), a.get("AXSize")
        bounds = (pos[0], pos[1], size[0], size[1]) if pos and size else None
        get_id = getattr(ax, "window_id", None)
        taken = set(pairs.values())
        rest = [r for r in records if int(r.get("kCGWindowOwnerPID", -1)) == pid
                and int(r.get("kCGWindowNumber", -1)) not in taken]
        return pair_ax_windows(rest, [(bounds, str(a.get("AXTitle") or ""),
                                       get_id(win) if get_id is not None else None)]).get(0)

    def _window_record(self, handle: Any) -> Dict[str, Any]:
        try:
            num = int(handle)
        except (TypeError, ValueError):
            raise WindowNotFound(f"Invalid macOS window handle: {handle!r}") from None
        for rec in self._window_records(kCGWindowListOptionIncludingWindow, num):
            if int(rec.get("kCGWindowNumber", -1)) == num:
                return rec
        raise WindowNotFound(f"No window with id {num}")

    def _ax_window(self, handle: Any) -> Any:
        """The AXUIElement of the CG window ``handle``.

        Raises :class:`WindowNotFound` when no AX window pairs with it one-to-one
        (e.g. two untitled windows with the same frame): acting on a guess could
        close or move the wrong document."""
        self._require_ax()
        rec = self._window_record(handle)
        num = int(rec.get("kCGWindowNumber", -1))
        pid = int(rec.get("kCGWindowOwnerPID", -1))
        records = [r for r in self._window_records(kCGWindowListOptionAll)
                   if int(r.get("kCGWindowNumber", -1)) != num]
        wins, pairs = self._ax_pairing(pid, [rec, *records])
        for i, n in pairs.items():
            if n == num:
                return wins[i][0]
        raise WindowNotFound(
            f"Cannot tell which accessibility window is window {handle!r} (pid {pid}); "
            "it has no unambiguous AX counterpart")

    def _ax_frame(self, win: Any) -> Optional[Tuple[float, float, float, float]]:
        a = self._ax.attrs(win, ("AXPosition", "AXSize")) if self._ax is not None else {}
        pos, size = a.get("AXPosition"), a.get("AXSize")
        return (pos[0], pos[1], size[0], size[1]) if pos and size else None

    def _work_area_for(self, bounds: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
        x, y, w, h = bounds
        d = self._get_layout().display_at_points(x + w / 2, y + h / 2)
        return d.work_area or (d.x, d.y, d.width, d.height)

    def _is_maximized(self, bounds: Tuple[float, float, float, float]) -> bool:
        work = self._work_area_for(bounds)
        return all(abs(a - b) <= 2.0 for a, b in zip(bounds, work))

    def list_windows(self) -> List[WindowInfo]:
        """Layer-0 windows: on-screen ones front-most first, then minimized ones
        (``is_minimized=True``, ``is_visible=False``; needs Accessibility permission).
        Handles are ``CGWindowID`` ints.

        Titles need Screen Recording permission; when missing they are filled in
        from the accessibility API if Accessibility permission was granted."""
        records = self._window_records(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements)
        layout = self._get_layout()
        front_pid = self._frontmost_pid()
        ax_ok = self._ax is not None and self._ax.is_trusted()
        all_records: List[Dict[str, Any]] = []
        if ax_ok:
            try:
                all_records = self._window_records(kCGWindowListOptionAll)
            except Exception:
                all_records = []
        ax_cache: Dict[int, Tuple[list, Dict[int, int]]] = {}

        def ax_info(pid: int, num: int) -> Optional[tuple]:
            if pid not in ax_cache:
                try:
                    ax_cache[pid] = self._ax_pairing(pid, all_records or records)
                except Exception:
                    ax_cache[pid] = ([], {})
            wins, pairs = ax_cache[pid]
            for i, n in pairs.items():
                if n == num:
                    return wins[i]
            return None

        out: List[WindowInfo] = []
        seen: Set[int] = set()
        active_seen = False
        for rec in records:
            if not _plausible_record(rec):
                continue
            bounds = _record_bounds(rec)
            num = int(rec.get("kCGWindowNumber", 0))
            pid = int(rec.get("kCGWindowOwnerPID", -1))
            owner = str(rec.get("kCGWindowOwnerName") or "")
            title = str(rec.get("kCGWindowName") or "")
            if not title and ax_ok:
                info = ax_info(pid, num)
                if info is not None:
                    title = info[2]
            is_active = not active_seen and front_pid is not None and pid == front_pid
            active_seen = active_seen or is_active
            seen.add(num)
            out.append(WindowInfo(
                handle=num,
                title=title,
                rect=layout.points_rect_to_pixels(*bounds),
                app=owner or self._app_name(pid),
                pid=pid,
                is_active=is_active,
                is_minimized=False,
                is_maximized=self._is_maximized(bounds),
                is_visible=True,
                class_name="",
            ))
        # minimized windows are not on screen; the AX tree says which off-screen
        # windows are minimized (rather than on another Space or merely hidden)
        regular: Optional[Set[int]] = None
        for rec in all_records:
            num = int(rec.get("kCGWindowNumber", 0))
            if num in seen or not _plausible_record(rec):
                continue
            pid = int(rec.get("kCGWindowOwnerPID", -1))
            if regular is None:  # only ask apps with a Dock icon (agents never minimize)
                regular = {p for p, _ in self._regular_apps()} if self._appkit is not None else set()
            if self._appkit is not None and pid not in regular:
                continue
            info = ax_info(pid, num)
            if info is None or not info[3]:
                continue
            seen.add(num)
            owner = str(rec.get("kCGWindowOwnerName") or "")
            out.append(WindowInfo(
                handle=num,
                title=str(rec.get("kCGWindowName") or "") or info[2],
                rect=layout.points_rect_to_pixels(*_record_bounds(rec)),
                app=owner or self._app_name(pid),
                pid=pid,
                is_active=False,
                is_minimized=True,
                is_maximized=False,
                is_visible=False,
                class_name="",
            ))
        return out

    #: Seconds :meth:`activate_window` waits for the app to become front-most.
    ACTIVATE_TIMEOUT = 1.0

    def _wait_frontmost(self, pid: int, timeout: float) -> Optional[bool]:
        """Whether ``pid`` became front-most within ``timeout`` (``None``: unknown)."""
        deadline = time.monotonic() + timeout
        while True:
            front = self._frontmost_pid()
            if front is None or front == pid:
                return None if front is None else True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def activate_window(self, handle: Any) -> None:
        """Bring the window to the front and give its app keyboard focus.

        Raises :class:`BackendError` when the app could not be made front-most
        (``AXRaise`` alone only reorders windows inside an app; typing would still
        go to the previous app)."""
        rec = self._window_record(handle)
        pid = int(rec.get("kCGWindowOwnerPID", -1))
        activated = self._activate_pid(pid)
        ax = self._ax
        if ax is not None and ax.is_trusted():
            try:
                win = self._ax_window(handle)
            except WindowNotFound:
                win = None
            if win is not None:
                if ax.attr(win, "AXMinimized"):
                    ax.set_attr(win, "AXMinimized", False)
                ax.perform(win, "AXRaise")
                ax.set_attr(win, "AXMain", True)
            # works where activateWithOptions_ is declined (cooperative activation)
            try:
                activated = ax.set_attr(ax.application(pid), "AXFrontmost", True) or activated
            except Exception:
                pass
        if not activated:
            raise BackendError(f"Could not activate window {handle!r} (pid {pid})")
        if self._wait_frontmost(pid, self.ACTIVATE_TIMEOUT) is False:
            raise BackendError(
                f"Window {handle!r} (pid {pid}) did not become the front-most app; "
                f"{self._app_name(self._frontmost_pid()) or 'another app'} still has keyboard focus")

    def move_window(self, handle: Any, rect: Rect) -> None:
        """Move/resize to the pixel ``rect`` (converted to points of the target display)."""
        ax = self._require_ax()
        win = self._ax_window(handle)
        x, y, w, h = self._get_layout().pixels_rect_to_points(Rect.coerce(rect))
        ok = ax.set_point(win, "AXPosition", x, y)
        ok = ax.set_size(win, "AXSize", w, h) and ok
        ax.set_point(win, "AXPosition", x, y)  # size changes can be constrained by the screen edge
        if not ok:
            raise BackendError(f"The application refused to move/resize window {handle!r}")

    def set_window_state(self, handle: Any, state: str) -> None:
        ax = self._require_ax()
        state = str(state).lower()
        win = self._ax_window(handle)
        if state in ("minimized", "minimize", "min"):
            if not ax.set_attr(win, "AXMinimized", True):
                raise BackendError(f"Could not minimize window {handle!r}")
            return
        if state in ("normal", "restore", "restored"):
            if ax.attr(win, "AXMinimized"):
                ax.set_attr(win, "AXMinimized", False)
            if ax.attr(win, "AXFullScreen"):
                ax.set_attr(win, "AXFullScreen", False)
            saved = self._restore_rects.pop(handle, None)
            if saved is not None:
                ax.set_point(win, "AXPosition", saved[0], saved[1])
                ax.set_size(win, "AXSize", saved[2], saved[3])
                return
            if self._is_maximized(self._ax_frame(win) or (0.0, 0.0, 0.0, 0.0)):
                # zoomed by the user (green button / double-click on the title bar):
                # zoom back to the user size, never toggle full screen instead
                button = ax.attr(win, "AXZoomButton")
                if button is not None:
                    if "AXZoomWindow" in ax.action_names(button):
                        ax.perform(button, "AXZoomWindow")
                    elif ax.attr(button, "AXSubrole") != "AXFullScreenButton":
                        ax.perform(button, "AXPress")
                deadline = time.monotonic() + 0.5
                while self._is_maximized(self._ax_frame(win) or (0.0, 0.0, 0.0, 0.0)):
                    if time.monotonic() >= deadline:
                        raise BackendError(f"Could not restore window {handle!r} from its maximized size")
                    time.sleep(0.02)
            return
        if state in ("maximized", "maximize", "max"):
            if ax.attr(win, "AXMinimized"):
                ax.set_attr(win, "AXMinimized", False)
            a = ax.attrs(win, ("AXPosition", "AXSize"))
            pos, size = a.get("AXPosition"), a.get("AXSize")
            if pos and size:
                current = (pos[0], pos[1], size[0], size[1])
                if not self._is_maximized(current):
                    self._restore_rects[handle] = current
                work = self._work_area_for(current)
            else:
                d = self._get_layout().primary
                work = d.work_area or (d.x, d.y, d.width, d.height)
            ok = ax.set_point(win, "AXPosition", work[0], work[1])
            ok = ax.set_size(win, "AXSize", work[2], work[3]) and ok
            if not ok:
                raise BackendError(f"Could not maximize window {handle!r}")
            return
        raise ValueError(f"Unknown window state {state!r}; expected minimized, maximized or normal")

    def close_window(self, handle: Any) -> None:
        ax = self._require_ax()
        win = self._ax_window(handle)
        button = ax.attr(win, "AXCloseButton")
        if button is None or not ax.perform(button, "AXPress"):
            raise BackendError(f"Window {handle!r} has no usable close button")

    def launch(self, command: Any, **kwargs: Any) -> Any:
        """Start an application. App names (``"Safari"``, ``"Microsoft Excel"``),
        ``"/Applications/Safari.app"`` and bundle paths are opened with ``open -a``;
        anything else runs via :class:`subprocess.Popen`. A string is treated as an
        app name when its first word is not a command on ``PATH`` and it contains
        no ``/`` or quotes."""
        if isinstance(command, str):
            stripped = command.strip()
            if stripped.endswith(".app") or stripped.endswith(".app/"):
                return subprocess.Popen(["open", "-a", stripped], **kwargs)
            try:
                parts = shlex.split(stripped)
            except ValueError:
                parts = []
            if (parts and "/" not in stripped and not any(c in stripped for c in "\"'\\")
                    and shutil.which(parts[0]) is None):
                return subprocess.Popen(["open", "-a", " ".join(parts)], **kwargs)
            if parts and parts[0].endswith(".app") and os.path.isdir(parts[0]):
                return subprocess.Popen(["open", "-a", parts[0], "--args", *parts[1:]], **kwargs)
            if parts:
                # Pass the POSIX-split argv on: the base class splits strings by the
                # *host* platform's rules, which differ when this runs off macOS (tests).
                return super().launch(parts, **kwargs)
        return super().launch(command, **kwargs)

    # ------------------------------------------------------------------- clipboard
    def _pasteboard(self) -> Any:
        if self._appkit is None:
            raise NotSupported(f"Clipboard needs pyobjc-framework-Cocoa; {_INSTALL_HINT}")
        return self._appkit.NSPasteboard.generalPasteboard()

    def _string_type(self) -> Any:
        return getattr(self._appkit, "NSPasteboardTypeString", "public.utf8-plain-text")

    def get_clipboard(self) -> str:
        value = self._pasteboard().stringForType_(self._string_type())
        return str(value) if value is not None else ""

    def set_clipboard(self, text: str) -> None:
        pb = self._pasteboard()
        pb.clearContents()
        if not pb.setString_forType_(str(text), self._string_type()):
            raise BackendError("NSPasteboard refused the clipboard text")

    # --------------------------------------------------------------- accessibility
    def _regular_apps(self) -> List[Tuple[int, str]]:
        """``(pid, name)`` of running apps with a Dock icon.

        ``NSWorkspace.runningApplications`` is a snapshot unless the run loop runs
        (see :meth:`_pump_appkit`), so owners of on-screen windows that are not in
        it (apps launched since) are added from the live window list."""
        appkit = self._appkit
        if appkit is None:
            return []
        self._pump_appkit()
        out = []
        seen: Set[int] = set()
        for app in appkit.NSWorkspace.sharedWorkspace().runningApplications() or []:
            try:
                pid = int(app.processIdentifier())
                seen.add(pid)
                if int(app.activationPolicy()) != _NS_APPLICATION_ACTIVATION_POLICY_REGULAR:
                    continue
                if app.isTerminated():
                    continue
                name = str(app.localizedName() or "")
            except Exception:
                continue
            self._app_names[pid] = name
            out.append((pid, name))
        try:
            records = self._window_records(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements)
        except Exception:
            records = []
        for rec in records:
            pid = int(rec.get("kCGWindowOwnerPID", -1))
            if pid in seen or not _plausible_record(rec):
                continue
            seen.add(pid)
            try:
                app = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app is None or int(app.activationPolicy()) != _NS_APPLICATION_ACTIVATION_POLICY_REGULAR:
                    continue
                name = str(app.localizedName() or rec.get("kCGWindowOwnerName") or "")
            except Exception:
                continue
            self._app_names[pid] = name
            out.append((pid, name))
        return out

    def accessibility_roots(self) -> List[AccessibleNode]:
        """Top-level AX windows of every regular application, front-most first.

        Each root's ``handle`` is the matching ``CGWindowID`` (so it pairs with
        :meth:`list_windows`), or ``None`` when no CG window pairs with it
        unambiguously; two roots never share a handle."""
        ax = self._require_ax()
        records = self._window_records(kCGWindowListOptionAll)
        z_order = {int(r.get("kCGWindowNumber", 0)): i for i, r in enumerate(
            self._window_records(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements))}
        roots: List[Tuple[int, int, AXNode]] = []
        for app_index, (pid, name) in enumerate(self._regular_apps()):
            app_el = ax.application(pid)
            wins, pairs = self._ax_pairing(pid, records)
            for i, w in enumerate(wins):
                handle = pairs.get(i)
                parent = AXNode(app_el, self, app=name, pid=pid, handle=None, parent=None)
                node = AXNode(w[0], self, app=name, pid=pid, handle=handle, parent=parent)
                roots.append((z_order.get(handle, len(z_order) + app_index) if handle is not None
                              else len(z_order) + 10000 + app_index, len(roots), node))
        roots.sort(key=lambda t: (t[0], t[1]))
        return [n for _, _, n in roots]

    def application_node(self, pid: int) -> AXNode:
        """The AX application element of ``pid`` (includes its menu bar)."""
        ax = self._require_ax()
        return AXNode(ax.application(pid), self, app=self._app_name(pid), pid=pid, handle=None, parent=None)

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        """Deepest element at pixel ``(x, y)`` via ``AXUIElementCopyElementAtPosition``."""
        ax = self._require_ax()
        px, py = self._get_layout().pixel_to_point(x, y)
        el = ax.element_at(px, py)
        if el is None:
            return None
        pid = ax.pid(el)
        return AXNode(el, self, app=self._app_name(pid), pid=pid)

    def focused_element(self) -> Optional[AccessibleNode]:
        ax = self._require_ax()
        el = ax.attr(ax.system_wide, "AXFocusedUIElement")
        if el is None:
            pid = self._frontmost_pid()
            if pid is None:
                return None
            el = ax.attr(ax.application(pid), "AXFocusedUIElement")
            if el is None:
                return None
        pid = ax.pid(el)
        return AXNode(el, self, app=self._app_name(pid), pid=pid)
