"""Canonical key names, aliases and hotkey parsing.

Every backend receives *canonical* key names (the values of :data:`KEYS` /
single printable characters) so that platform code never has to deal with
user spelling variations like ``"Ctrl"``, ``"control"``, ``"⌘"`` or ``"Return"``.
"""

from __future__ import annotations

import sys
from typing import Iterable, List, Sequence, Union

# Canonical names. Backends MUST support every name in this set (mapping to the
# closest native key) plus any single printable character.
KEYS = frozenset(
    {
        # modifiers
        "ctrl", "shift", "alt", "cmd", "ctrl_r", "shift_r", "alt_r", "cmd_r", "altgr", "fn",
        # editing / navigation
        "enter", "tab", "space", "backspace", "delete", "escape", "insert",
        "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
        # locks / system
        "capslock", "numlock", "scrolllock", "printscreen", "pause", "menu",
        # function keys
        *(f"f{i}" for i in range(1, 25)),
        # numpad
        *(f"num{i}" for i in range(10)),
        "num_add", "num_subtract", "num_multiply", "num_divide", "num_decimal", "num_enter",
        # media
        "volumeup", "volumedown", "volumemute", "playpause", "nexttrack", "prevtrack", "stop",
        # browser
        "browser_back", "browser_forward", "browser_refresh",
    }
)

MODIFIERS = frozenset({"ctrl", "shift", "alt", "cmd", "ctrl_r", "shift_r", "alt_r", "cmd_r", "altgr", "fn"})

_ALIASES = {
    # modifiers
    "control": "ctrl", "ctl": "ctrl", "lctrl": "ctrl", "ctrlleft": "ctrl", "leftctrl": "ctrl", "^": "ctrl",
    "rctrl": "ctrl_r", "ctrlright": "ctrl_r", "rightctrl": "ctrl_r",
    "lshift": "shift", "shiftleft": "shift", "leftshift": "shift",
    "rshift": "shift_r", "shiftright": "shift_r", "rightshift": "shift_r",
    "option": "alt", "opt": "alt", "lalt": "alt", "altleft": "alt", "leftalt": "alt", "⌥": "alt",
    "ralt": "alt_r", "altright": "alt_r", "rightalt": "alt_r",
    "command": "cmd", "super": "cmd", "win": "cmd", "windows": "cmd", "meta": "cmd", "lwin": "cmd",
    "winleft": "cmd", "os": "cmd", "⌘": "cmd", "logo": "cmd",
    "rwin": "cmd_r", "winright": "cmd_r", "rcmd": "cmd_r", "cmdright": "cmd_r",
    # editing
    "return": "enter", "ret": "enter", "newline": "enter", "\n": "enter", "\r": "enter", "↵": "enter",
    "\t": "tab", " ": "space", "spacebar": "space",
    "back": "backspace", "bksp": "backspace", "bs": "backspace", "\b": "backspace", "⌫": "backspace",
    "del": "delete", "forwarddelete": "delete", "⌦": "delete",
    "esc": "escape", "\x1b": "escape",
    "ins": "insert",
    "pgup": "pageup", "page_up": "pageup", "prior": "pageup",
    "pgdn": "pagedown", "pgdown": "pagedown", "page_down": "pagedown", "next": "pagedown",
    "arrowup": "up", "uparrow": "up", "↑": "up",
    "arrowdown": "down", "downarrow": "down", "↓": "down",
    "arrowleft": "left", "leftarrow": "left", "←": "left",
    "arrowright": "right", "rightarrow": "right", "→": "right",
    "caps": "capslock", "caps_lock": "capslock",
    "num_lock": "numlock", "scroll_lock": "scrolllock",
    "prtsc": "printscreen", "prtscr": "printscreen", "print": "printscreen", "print_screen": "printscreen",
    "snapshot": "printscreen",
    "break": "pause", "apps": "menu", "contextmenu": "menu", "application": "menu",
    # numpad
    **{f"numpad{i}": f"num{i}" for i in range(10)},
    **{f"kp{i}": f"num{i}" for i in range(10)},
    "add": "num_add", "subtract": "num_subtract", "multiply": "num_multiply", "divide": "num_divide",
    "decimal": "num_decimal", "kp_enter": "num_enter", "numpadenter": "num_enter",
    # media
    "volume_up": "volumeup", "volume_down": "volumedown", "volume_mute": "volumemute", "mute": "volumemute",
    "play": "playpause", "play_pause": "playpause", "next_track": "nexttrack", "prev_track": "prevtrack",
    "previoustrack": "prevtrack",
}


def normalize_key(key: str) -> str:
    """Return the canonical name for ``key``.

    Single printable characters are returned unchanged (case preserved, so the
    backend can decide whether shift is needed). Raises ``ValueError`` for
    unknown multi-character names.
    """
    if not isinstance(key, str) or key == "":
        raise ValueError(f"Invalid key: {key!r}")
    if key in _ALIASES:  # raw control characters / symbols first, case sensitive
        return _ALIASES[key]
    if len(key) == 1:
        return key
    k = key.strip().lower().replace("-", "_").replace(" ", "")
    if k in KEYS:
        return k
    if k in _ALIASES:
        return _ALIASES[k]
    k2 = k.replace("_", "")
    if k2 in KEYS:
        return k2
    if k2 in _ALIASES:
        return _ALIASES[k2]
    raise ValueError(f"Unknown key name: {key!r}")


def is_modifier(key: str) -> bool:
    return normalize_key(key) in MODIFIERS


def parse_hotkey(combo: Union[str, Sequence[str]]) -> List[str]:
    """Parse ``"ctrl+shift+s"`` / ``"Ctrl-Alt-Del"`` / ``["cmd", "c"]`` into canonical keys.

    ``+`` itself can be written as ``"ctrl++"`` or ``"plus"``.
    The special token ``"primary"`` / ``"mod"`` maps to ``cmd`` on macOS and ``ctrl`` elsewhere,
    which makes LLM-authored shortcuts portable (``"mod+c"`` copies everywhere).
    """
    if isinstance(combo, str):
        if combo.strip() == "":
            if combo:
                return ["space"]
            raise ValueError("Empty hotkey")
        s = combo.strip()
        if len(s) == 1:
            parts = [s]
        elif "+" in s:
            raw = combo.lstrip().split("+")
            parts = []
            for i, p in enumerate(raw):
                if p == " " and i == len(raw) - 1 and i > 0 and raw[i - 1].strip():
                    # "ctrl+ " -> ctrl+space (the trailing space *is* the key)
                    parts.append(" ")
                elif p.strip() == "":
                    # "ctrl++" -> ['ctrl', '', ''] ; collapse to a single '+'
                    if not parts or parts[-1] != "+":
                        parts.append("+")
                else:
                    parts.append(p.strip())
        elif " " in s or ("-" in s and not s.startswith("-") and not s.endswith("-")):
            # "ctrl alt del" / "Ctrl-Alt-Del" are combos, but "Page Down" /
            # "caps-lock" are single multi-word key names: split first and fall
            # back to the whole string when a piece is not a key on its own.
            parts = s.split() if " " in s else s.split("-")
            try:
                for p in parts:
                    if p.strip().lower() not in ("primary", "mod", "cmdorctrl", "commandorcontrol", "plus", "minus"):
                        normalize_key(p)
            except ValueError:
                try:
                    return [normalize_key(s)]
                except ValueError:
                    pass  # report the unknown piece below
        else:
            parts = [s]
    else:
        parts = list(combo)
    out: List[str] = []
    for p in parts:
        lp = p.strip().lower() if len(p) > 1 else p
        if lp in ("primary", "mod", "cmdorctrl", "commandorcontrol"):
            out.append("cmd" if sys.platform == "darwin" else "ctrl")
        elif lp == "plus":
            out.append("+")
        elif lp == "minus":
            out.append("-")
        else:
            out.append(normalize_key(p if len(p) == 1 else p.strip()))
    return out


# US-layout characters that require shift. Backends that synthesize via
# virtual keys (not unicode injection) use this to decide on shift.
SHIFTED_CHARS = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'",
    "<": ",", ">": ".", "?": "/",
}


def char_needs_shift(ch: str) -> bool:
    return ch in SHIFTED_CHARS or (len(ch) == 1 and ch.isalpha() and ch.isupper() and ch.lower() != ch)


def split_text_tokens(text: str) -> Iterable[str]:
    """Yield characters, mapping newlines/tabs to canonical key names (``enter``/``tab``)."""
    for ch in text.replace("\r\n", "\n"):
        if ch in ("\n", "\r"):
            yield "enter"
        elif ch == "\t":
            yield "tab"
        else:
            yield ch
