"""Keysym and pointer-button tables of the X11 backend.

Numeric keysyms for every canonical key name, character -> keysym conversion and
the small keysym predicates used by the keyboard code; pure Python, python-xlib is
not needed.
"""

from __future__ import annotations

from typing import Dict, Iterator, Tuple

from ...keys import KEYS
from ...types import MouseButton


# --------------------------------------------------------------------------- keysyms
# Numeric keysym values (from X11/keysymdef.h and XF86keysym.h). They are
# hard-coded instead of looked up with ``XK.string_to_keysym`` because
# python-xlib only loads the latin1/miscellany groups by default, so names like
# ``ISO_Level3_Shift`` or the XF86 media keys would resolve to ``NoSymbol``.
KEYSYMS: Dict[str, int] = {
    # modifiers
    "shift": 0xFFE1,  # Shift_L
    "shift_r": 0xFFE2,  # Shift_R
    "ctrl": 0xFFE3,  # Control_L
    "ctrl_r": 0xFFE4,  # Control_R
    "alt": 0xFFE9,  # Alt_L
    "alt_r": 0xFFEA,  # Alt_R
    "cmd": 0xFFEB,  # Super_L
    "cmd_r": 0xFFEC,  # Super_R
    "altgr": 0xFE03,  # ISO_Level3_Shift
    # There is no X keysym for the laptop Fn key (it is handled in firmware);
    # XF86WakeUp is what many laptops report for Fn on its own.
    "fn": 0x1008FF2B,  # XF86WakeUp
    # editing / navigation
    "enter": 0xFF0D,  # Return
    "tab": 0xFF09,  # Tab
    "space": 0x0020,  # space
    "backspace": 0xFF08,  # BackSpace
    "delete": 0xFFFF,  # Delete
    "escape": 0xFF1B,  # Escape
    "insert": 0xFF63,  # Insert
    "home": 0xFF50,  # Home
    "end": 0xFF57,  # End
    "pageup": 0xFF55,  # Prior
    "pagedown": 0xFF56,  # Next
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    # locks / system
    "capslock": 0xFFE5,  # Caps_Lock
    "numlock": 0xFF7F,  # Num_Lock
    "scrolllock": 0xFF14,  # Scroll_Lock
    "printscreen": 0xFF61,  # Print
    "pause": 0xFF13,  # Pause
    "menu": 0xFF67,  # Menu
    # function keys F1..F24
    **{f"f{i}": 0xFFBE + (i - 1) for i in range(1, 25)},
    # numpad
    **{f"num{i}": 0xFFB0 + i for i in range(10)},  # KP_0..KP_9
    "num_add": 0xFFAB,
    "num_subtract": 0xFFAD,
    "num_multiply": 0xFFAA,
    "num_divide": 0xFFAF,
    "num_decimal": 0xFFAE,
    "num_enter": 0xFF8D,
    # media (XF86)
    "volumeup": 0x1008FF13,  # XF86AudioRaiseVolume
    "volumedown": 0x1008FF11,  # XF86AudioLowerVolume
    "volumemute": 0x1008FF12,  # XF86AudioMute
    "playpause": 0x1008FF14,  # XF86AudioPlay
    "nexttrack": 0x1008FF17,  # XF86AudioNext
    "prevtrack": 0x1008FF16,  # XF86AudioPrev
    "stop": 0x1008FF15,  # XF86AudioStop
    # browser
    "browser_back": 0x1008FF26,  # XF86Back
    "browser_forward": 0x1008FF27,  # XF86Forward
    "browser_refresh": 0x1008FF29,  # XF86Refresh
}


_CONTROL_CHAR_KEYS = {"\b": "backspace", "\x1b": "escape", "\x7f": "delete", "\n": "enter", "\r": "enter", "\t": "tab"}


_KEYSYM_SHIFT_L = 0xFFE1


_KEYSYM_NUM_LOCK = 0xFF7F


_KEYSYM_CAPS_LOCK = 0xFFE5


BUTTONS: Dict[MouseButton, int] = {
    MouseButton.LEFT: 1,
    MouseButton.MIDDLE: 2,
    MouseButton.RIGHT: 3,
    MouseButton.X1: 8,
    MouseButton.X2: 9,
}


_WHEEL_UP, _WHEEL_DOWN, _WHEEL_LEFT, _WHEEL_RIGHT = 4, 5, 6, 7


def char_to_keysym(ch: str) -> int:
    """Return the X keysym for a single character.

    Latin-1 characters map to their code point (X keysyms 0x20-0x7e, 0xa0-0xff);
    everything else uses the Unicode keysym range ``0x01000000 + codepoint``,
    which every modern X server and toolkit understands.
    """
    if not isinstance(ch, str) or len(ch) != 1:
        raise ValueError(f"Expected a single character, got {ch!r}")
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp


def key_to_keysym(key: str) -> int:
    """Keysym for a canonical key name or a single character."""
    if key in KEYSYMS:
        return KEYSYMS[key]
    if len(key) == 1:
        if key in _CONTROL_CHAR_KEYS:
            return KEYSYMS[_CONTROL_CHAR_KEYS[key]]
        return char_to_keysym(key)
    raise ValueError(f"Unknown key {key!r} (expected a canonical name from locum.keys.KEYS or one character)")


def _is_keypad_keysym(ks: int) -> bool:
    return 0xFF80 <= ks <= 0xFFBD


def _is_char_keysym(ks: int) -> bool:
    """True for keysyms of printable characters (Latin-1 or the Unicode keysym range),
    the only keysyms whose key differs between keyboard layouts (XKB groups)."""
    return 0x20 <= ks <= 0x7E or 0xA0 <= ks <= 0xFF or ks & 0xFF000000 == 0x01000000


def _lower_upper(ks: int) -> Tuple[int, int]:
    """(lowercase, uppercase) keysym pair for Latin-1/Unicode letters, else (ks, ks)."""
    if ks < 0x100:
        ch = chr(ks)
    elif ks & 0xFF000000 == 0x01000000:
        ch = chr(ks & 0x00FFFFFF)
    else:
        return ks, ks
    lo, up = ch.lower(), ch.upper()
    if len(lo) != 1 or len(up) != 1:
        return ks, ks
    return char_to_keysym(lo), char_to_keysym(up)


#: Held modifiers that turn a character tap into a shortcut (CapsLock is then ignored).
_COMMAND_MODIFIERS = ("ctrl", "ctrl_r", "alt", "alt_r", "cmd", "cmd_r")


def iter_keysym_table() -> Iterator[Tuple[str, int]]:
    """Yield ``(canonical_name, keysym)`` for every name in :data:`locum.keys.KEYS`."""
    for name in sorted(KEYS):
        yield name, KEYSYMS[name]
