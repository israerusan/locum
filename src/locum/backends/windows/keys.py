"""Keyboard and input-event helpers for the Windows backend: the virtual-key
tables (:data:`VK_CODES`, :data:`EXTENDED_KEYS`), key lookup and decoding helpers,
absolute-coordinate normalization and the ``INPUT`` builders (:func:`mouse_input`,
:func:`key_input`, :func:`unicode_inputs`).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from ...keys import KEYS
from ...types import MouseButton, Rect
from ._win32api import (
    INJECTED_EXTRA_INFO,
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYBDINPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_XDOWN,
    MOUSEEVENTF_XUP,
    MOUSEINPUT,
    XBUTTON1,
    XBUTTON2,
)


# ------------------------------------------------------------------ virtual-key codes
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_TAB = 0x09

#: Canonical key name -> Windows virtual-key code. Covers every name in :data:`locum.keys.KEYS`.
VK_CODES: Dict[str, int] = {
    # modifiers (left/right specific codes are accepted by SendInput)
    "shift": 0xA0,  # VK_LSHIFT
    "shift_r": 0xA1,  # VK_RSHIFT
    "ctrl": 0xA2,  # VK_LCONTROL
    "ctrl_r": 0xA3,  # VK_RCONTROL
    "alt": 0xA4,  # VK_LMENU
    "alt_r": 0xA5,  # VK_RMENU
    "altgr": 0xA5,  # AltGr is the right Alt key (Windows synthesises Ctrl+Alt for it)
    "cmd": 0x5B,  # VK_LWIN
    "cmd_r": 0x5C,  # VK_RWIN
    # The Fn key is handled in keyboard firmware and has no virtual-key code;
    # 0xFF ("no VK mapping") is what keyboards that do report Fn send.
    "fn": 0xFF,
    # editing / navigation
    "enter": 0x0D,  # VK_RETURN
    "tab": 0x09,  # VK_TAB
    "space": 0x20,  # VK_SPACE
    "backspace": 0x08,  # VK_BACK
    "delete": 0x2E,  # VK_DELETE
    "escape": 0x1B,  # VK_ESCAPE
    "insert": 0x2D,  # VK_INSERT
    "home": 0x24,  # VK_HOME
    "end": 0x23,  # VK_END
    "pageup": 0x21,  # VK_PRIOR
    "pagedown": 0x22,  # VK_NEXT
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    # locks / system
    "capslock": 0x14,  # VK_CAPITAL
    "numlock": 0x90,  # VK_NUMLOCK
    "scrolllock": 0x91,  # VK_SCROLL
    "printscreen": 0x2C,  # VK_SNAPSHOT
    "pause": 0x13,  # VK_PAUSE
    "menu": 0x5D,  # VK_APPS
    # function keys F1..F24
    **{f"f{i}": 0x70 + (i - 1) for i in range(1, 25)},
    # numpad
    **{f"num{i}": 0x60 + i for i in range(10)},  # VK_NUMPAD0..9
    "num_multiply": 0x6A,
    "num_add": 0x6B,
    "num_subtract": 0x6D,
    "num_decimal": 0x6E,
    "num_divide": 0x6F,
    "num_enter": 0x0D,  # VK_RETURN + extended flag
    # media
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "nexttrack": 0xB0,
    "prevtrack": 0xB1,
    "stop": 0xB2,  # VK_MEDIA_STOP
    "playpause": 0xB3,
    # browser
    "browser_back": 0xA6,
    "browser_forward": 0xA7,
    "browser_refresh": 0xA8,
}

#: Keys whose hardware scan code carries the ``E0`` prefix and therefore need
#: ``KEYEVENTF_EXTENDEDKEY`` (right Ctrl/Alt, Win keys, the navigation cluster,
#: arrows, NumLock, PrintScreen, numpad divide/enter, media/browser keys).
EXTENDED_KEYS = frozenset(
    {
        "ctrl_r", "alt_r", "altgr", "cmd", "cmd_r", "menu",
        "insert", "delete", "home", "end", "pageup", "pagedown",
        "up", "down", "left", "right",
        "numlock", "printscreen", "num_divide", "num_enter",
        "volumemute", "volumedown", "volumeup", "nexttrack", "prevtrack", "stop", "playpause",
        "browser_back", "browser_forward", "browser_refresh",
    }
)

#: Canonical modifier name -> VK used when ``VkKeyScanExW`` reports a shift state.
_SHIFT_STATE_MODIFIERS = ((0x01, "shift"), (0x02, "ctrl"), (0x04, "alt"))

#: Upper bound on INPUT structures passed to a single SendInput call.
_SENDINPUT_CHUNK = 256


# =========================================================================== pure helpers
def vk_for_key(key: str) -> Tuple[int, bool]:
    """Return ``(virtual_key, extended)`` for a canonical key name.

    Raises:
        KeyError: ``key`` is not a canonical multi-character key name.
    """
    return VK_CODES[key], key in EXTENDED_KEYS


def iter_vk_table() -> Iterable[Tuple[str, int, bool]]:
    """Yield ``(canonical_name, vk, extended)`` for every name in :data:`locum.keys.KEYS`."""
    for name in sorted(KEYS):
        vk, ext = vk_for_key(name)
        yield name, vk, ext


def _norm_axis(value: int, origin: int, size: int) -> int:
    if size <= 1:
        return 0
    v = min(max(int(value), origin), origin + size - 1) - origin
    # Aim at the centre of the pixel's bucket in the 0..65535 space so the
    # result is exact whether Windows truncates or rounds when de-normalizing.
    return min(65535, ((2 * v + 1) * 32768) // size)


def normalize_absolute(x: int, y: int, virtual: Rect) -> Tuple[int, int]:
    """Map virtual-desktop pixel ``(x, y)`` to ``MOUSEEVENTF_ABSOLUTE|VIRTUALDESK`` units (0..65535).

    ``virtual`` is the virtual desktop rectangle (``SM_XVIRTUALSCREEN`` ...),
    whose origin may be negative when a monitor sits left of/above the
    primary one. Points outside it are clamped to its edge.
    """
    return _norm_axis(x, virtual.left, virtual.width), _norm_axis(y, virtual.top, virtual.height)


def denormalize_absolute(nx: int, ny: int, virtual: Rect) -> Tuple[int, int]:
    """Inverse of :func:`normalize_absolute` as Windows computes it (``origin + n * size / 65536``)."""
    return (
        virtual.left + (int(nx) * virtual.width) // 65536,
        virtual.top + (int(ny) * virtual.height) // 65536,
    )


def utf16_units(text: str) -> List[int]:
    """Split ``text`` into UTF-16 code units (non-BMP characters become surrogate pairs)."""
    data = text.encode("utf-16-le", "surrogatepass")
    return [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]


def decode_vk_scan(result: int) -> Optional[Tuple[int, int]]:
    """Decode a ``VkKeyScan(Ex)W`` result into ``(vk, shift_state)`` or None if unmappable.

    ``shift_state`` bits: 1 = Shift, 2 = Ctrl, 4 = Alt (bits 8/16/32 are
    Hankaku/reserved and make the result unusable for synthesis).
    """
    result = int(result)
    if result == -1 or result == 0xFFFF:
        return None
    result &= 0xFFFF
    vk, state = result & 0xFF, (result >> 8) & 0xFF
    if vk == 0xFF or state & 0xF8:
        return None
    return vk, state


_BUTTON_FLAGS: Dict[MouseButton, Tuple[int, int, int]] = {
    MouseButton.LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    MouseButton.RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    MouseButton.MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    MouseButton.X1: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    MouseButton.X2: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}


def mouse_input(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> INPUT:
    """Build a mouse ``INPUT``. ``data`` may be negative (wheel); it is stored as a 32-bit DWORD."""
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(int(dx), int(dy), int(data) & 0xFFFFFFFF, int(flags), 0, INJECTED_EXTRA_INFO)
    return inp


def key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    """Build a keyboard ``INPUT``."""
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(int(vk) & 0xFFFF, int(scan) & 0xFFFF, int(flags), 0, INJECTED_EXTRA_INFO)
    return inp


def unicode_inputs(text: str) -> List[INPUT]:
    """Down/up ``KEYEVENTF_UNICODE`` pairs for every UTF-16 code unit of ``text``."""
    out: List[INPUT] = []
    for unit in utf16_units(text):
        out.append(key_input(0, unit, KEYEVENTF_UNICODE))
        out.append(key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return out
