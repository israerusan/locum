"""Keyboard helpers: key resolution (``KeySpec``, ``resolve_key``), keyboard-layout maps
(US-ANSI and the active Carbon/TIS layout) and the UTF-16 chunking used for typing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ...errors import NotSupported
from ...keys import SHIFTED_CHARS, normalize_key
from .constants import (
    FLAG_ALTERNATE,
    FLAG_COMMAND,
    FLAG_CONTROL,
    FLAG_NUMERIC_PAD,
    FLAG_SECONDARY_FN,
    FLAG_SHIFT,
    _NX_LALT,
    _NX_LCMD,
    _NX_LCTL,
    _NX_LSHIFT,
    _NX_RALT,
    _NX_RCMD,
    _NX_RCTL,
    _NX_RSHIFT,
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
