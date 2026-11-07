"""A drop-in subset of the PyAutoGUI API, implemented on top of :class:`locum.Desktop`.

Existing PyAutoGUI scripts can switch with a one-line change::

    import locum.compat.pyautogui as pyautogui

    pyautogui.PAUSE = 0.25
    pyautogui.moveTo(100, 200, duration=0.5, tween=pyautogui.easeInOutQuad)
    pyautogui.click(clicks=2, interval=0.1, button="left")
    pyautogui.write("Hello!\\n", interval=0.05)
    pyautogui.hotkey("ctrl", "s")
    box = pyautogui.locateOnScreen("button.png", confidence=0.9)
    pyautogui.click(pyautogui.center(box))

(or call :func:`locum.compat.install_pyautogui_shim` so that a plain
``import pyautogui`` gets this module).

Semantics follow PyAutoGUI 0.9.5x:

* every input function checks the fail-safe first (cursor on one of
  :data:`FAILSAFE_POINTS` while :data:`FAILSAFE` is true raises
  :class:`FailSafeException`) and sleeps :data:`PAUSE` seconds afterwards
  (pass ``_pause=False`` to skip);
* on a desktop handed in with :func:`set_desktop` those two settings never
  weaken its own fail-safe: its trigger points stay active and
  :data:`FAILSAFE_POINTS` are added to them (``FAILSAFE = False`` removes only
  the added points), and the desktop's fail-safe is restored when another
  desktop replaces it. They fully control only the desktop this module creates;
* ``x``/``y`` arguments accept numbers, ``None`` (keep the current coordinate),
  ``(x, y)`` tuples, 4-tuples / ``Box`` (their center) and image file names
  (located on screen first);
* moves shorter than :data:`MINIMUM_DURATION` are instant;
* ``locateOnScreen`` and friends return ``Box(left, top, width, height)`` /
  ``Point(x, y)`` namedtuples, and raise :class:`ImageNotFoundException` when
  nothing is found while :data:`USE_IMAGE_NOT_FOUND_EXCEPTION` is true (the
  default since PyAutoGUI 0.9.41); set it to ``False`` to get ``None``;
* unknown key names are silently ignored by :func:`press`/:func:`keyDown`/
  :func:`keyUp`/:func:`hotkey`, exactly like PyAutoGUI (use :func:`isValidKey`);
* :data:`LOG_SCREENSHOTS` (or ``logScreenshot=True`` on one call) saves a
  screenshot before every click, drag, scroll, key and ``write`` call to the
  current directory as ``2026-11-06_14-3-7-123_click_left,1,100,200.png``,
  keeping the newest :data:`LOG_SCREENSHOTS_LIMIT` (default 10) files.
  ``moveTo``/``moveRel`` log only with ``logScreenshot=True``, as in PyAutoGUI.
  The arguments are part of the file name -- ``write`` puts the typed text
  there (a :mod:`locum.rpa.credentials` password is replaced by ``***``) -- so
  leave it off around other sensitive text.

Improvements that come for free: template matching is grayscale/color
normalized cross-correlation with alpha masks and multi-monitor support, typing
is layout independent (unicode injection) where the backend supports it,
and the abort flag / kill hotkey of :mod:`locum.failsafe` stop scripts too.
"""

from __future__ import annotations

import collections
import contextlib
import datetime as _dt
import functools
import inspect
import logging
import math
import numbers
import os
import pathlib
import re
import threading
import time
from typing import Any, Callable, Generator, Iterator, List, Optional, Sequence, Union

from ..errors import FailSafeTriggered, ImageNotFound, LocumError
from ..keys import normalize_key
from ..types import Match, Rect

__all__ = [
    # settings
    "FAILSAFE", "FAILSAFE_POINTS", "PAUSE", "MINIMUM_DURATION", "MINIMUM_SLEEP", "DARWIN_CATCH_UP_TIME",
    "USE_IMAGE_NOT_FOUND_EXCEPTION", "LOG_SCREENSHOTS", "LOG_SCREENSHOTS_LIMIT", "G_LOG_SCREENSHOTS_FILENAMES",
    "KEYBOARD_KEYS", "KEY_NAMES",
    "LEFT", "MIDDLE", "RIGHT", "PRIMARY", "SECONDARY", "QWERTY", "QWERTZ",
    # exceptions & types
    "PyAutoGUIException", "FailSafeException", "ImageNotFoundException", "Point", "Size", "Box",
    # screen
    "size", "resolution", "position", "onScreen", "screenshot", "pixel", "pixelMatchesColor",
    "locate", "locateAll", "locateOnScreen", "locateAllOnScreen", "locateCenterOnScreen", "locateOnWindow", "center",
    "useImageNotFoundException",
    # mouse
    "moveTo", "moveRel", "move", "dragTo", "dragRel", "drag", "click", "leftClick", "rightClick", "middleClick",
    "doubleClick", "tripleClick", "mouseDown", "mouseUp", "scroll", "hscroll", "vscroll",
    # keyboard
    "write", "typewrite", "press", "keyDown", "keyUp", "hotkey", "hold", "isValidKey", "isShiftCharacter",
    # windows
    "getActiveWindow", "getActiveWindowTitle", "getAllWindows", "getAllTitles", "getWindowsWithTitle", "getWindowsAt",
    # misc
    "sleep", "countdown", "failSafeCheck", "get_desktop", "set_desktop",
    # tweens
    "linear", "getPointOnLine",
    "easeInQuad", "easeOutQuad", "easeInOutQuad", "easeInCubic", "easeOutCubic", "easeInOutCubic",
    "easeInQuart", "easeOutQuart", "easeInOutQuart", "easeInQuint", "easeOutQuint", "easeInOutQuint",
    "easeInSine", "easeOutSine", "easeInOutSine", "easeInExpo", "easeOutExpo", "easeInOutExpo",
    "easeInCirc", "easeOutCirc", "easeInOutCirc", "easeInElastic", "easeOutElastic", "easeInOutElastic",
    "easeInBack", "easeOutBack", "easeInOutBack", "easeInBounce", "easeOutBounce", "easeInOutBounce",
]

# =============================================================== settings
#: Raise :class:`FailSafeException` when the cursor is on a :data:`FAILSAFE_POINTS` point.
FAILSAFE = True
#: Fail-safe trigger points (PyAutoGUI default: the top-left corner only).
FAILSAFE_POINTS: List[tuple] = [(0, 0)]
#: Seconds to sleep after every input function call.
PAUSE = 0.1
#: Moves with a shorter ``duration`` happen instantly.
MINIMUM_DURATION = 0.1
#: Accepted for compatibility (PyAutoGUI's minimum sleep granularity).
MINIMUM_SLEEP = 0.05
#: Accepted for compatibility (unused).
DARWIN_CATCH_UP_TIME = 0.01
#: Save a screenshot before every click, drag, scroll, key press and ``write`` (PyAutoGUI's
#: debugging aid): ``YYYY-MM-DD_H-M-S-mmm_<function>_<args>.png`` in the current directory.
#: A function's ``logScreenshot=True``/``False`` argument overrides it for that call.
LOG_SCREENSHOTS = False
#: Keep at most this many logged screenshots, deleting the oldest (``None`` = keep all).
LOG_SCREENSHOTS_LIMIT: Optional[int] = 10
#: File names of the logged screenshots still on disk, oldest first.
G_LOG_SCREENSHOTS_FILENAMES: List[str] = []
#: Raise :class:`ImageNotFoundException` instead of returning ``None`` from the locate functions.
USE_IMAGE_NOT_FOUND_EXCEPTION = True

LEFT = "left"
MIDDLE = "middle"
RIGHT = "right"
PRIMARY = "primary"
SECONDARY = "secondary"

QWERTY = r"""`1234567890-=qwertyuiop[]\asdfghjkl;'zxcvbnm,./~!@#$%^&*()_+QWERTYUIOP{}|ASDFGHJKL:"ZXCVBNM<>?"""
QWERTZ = r"""=1234567890/0qwertzuiop89-asdfghjkl,\yxcvbnm,.7+!@#$%^&*()?)QWERTZUIOP*(_ASDFGHJKL<|YXCVBNM;:&"""

# ============================================================ exceptions
#: Base class of the PyAutoGUI-named exceptions (alias of :class:`locum.LocumError`).
PyAutoGUIException = LocumError
#: Raised by the fail-safe (alias of :class:`locum.FailSafeTriggered`).
FailSafeException = FailSafeTriggered
#: Raised when an image cannot be found on screen (alias of :class:`locum.ImageNotFound`).
ImageNotFoundException = ImageNotFound

# ================================================================= types
Point = collections.namedtuple("Point", "x y")
Size = collections.namedtuple("Size", "width height")
Box = collections.namedtuple("Box", "left top width height")

# ================================================================ desktop
_desktop: Any = None
_desktop_lock = threading.Lock()
#: Whether :data:`_desktop` was created by this module (``False``: handed in with :func:`set_desktop`).
_desktop_owned = False
#: The fail-safe a :func:`set_desktop` desktop came with: ``(enabled, explicit corners, effective corners)``.
_user_failsafe: Any = None
#: Sleep function used for :data:`PAUSE` (replace in tests).
_SLEEP: Callable[[float], Any] = time.sleep


def get_desktop() -> Any:
    """The :class:`locum.Desktop` driving this module (created on first use over the default backend)."""
    global _desktop, _desktop_owned
    if _desktop is None:
        with _desktop_lock:
            if _desktop is None:
                from ..desktop import Desktop

                _desktop = Desktop(failsafe=True, pause=0.0, humanize=False, default_timeout=0.0)
                _desktop_owned = True
    return _desktop


def set_desktop(desktop: Any) -> Any:
    """Use ``desktop`` (a :class:`locum.Desktop`) for every call; ``None`` = recreate lazily.

    The desktop's own ``pause`` should be 0: this module applies :data:`PAUSE` itself.
    :data:`FAILSAFE` / :data:`FAILSAFE_POINTS` only add trigger points to its own
    fail-safe while it is in use, and its fail-safe is restored as it was when it is
    replaced (by another :func:`set_desktop` call).
    Returns the previous desktop.
    """
    global _desktop, _desktop_owned, _user_failsafe
    with _desktop_lock:
        old = _desktop
        if old is not None and not _desktop_owned and _user_failsafe is not None:
            fs = old.failsafe
            fs.enabled = _user_failsafe[0]
            fs.corners = _user_failsafe[1]
        _desktop, _desktop_owned, _user_failsafe = desktop, False, None
        if desktop is not None:
            fs = desktop.failsafe
            explicit = getattr(fs, "_explicit", None)
            _user_failsafe = (bool(fs.enabled), None if explicit is None else list(explicit),
                              [tuple(c) for c in fs.corners] if fs.enabled else [])
    return old


def _d() -> Any:
    """The desktop with its fail-safe synchronized to :data:`FAILSAFE` / :data:`FAILSAFE_POINTS`.

    On a desktop this module created the two settings are its fail-safe. On one handed
    in with :func:`set_desktop` they never weaken what its owner configured: its own
    trigger points stay active (``FAILSAFE = False`` turns off only the compat points).
    """
    d = get_desktop()
    fs = d.failsafe
    points = [tuple(p) for p in FAILSAFE_POINTS] if FAILSAFE else []
    with _desktop_lock:
        user = _user_failsafe if (d is _desktop and not _desktop_owned) else None
    if user is not None:
        base_enabled, base_explicit, base_points = user
        if not FAILSAFE:
            if fs.enabled != base_enabled or getattr(fs, "_explicit", None) != base_explicit:
                fs.enabled = base_enabled
                fs.corners = base_explicit
            return d
        points = list(base_points) + [p for p in points if p not in base_points]
    fs.enabled = bool(FAILSAFE) or (user is not None and user[0])
    if [tuple(c) for c in fs.corners] != points:
        fs.corners = points
    return d


def failSafeCheck() -> None:
    """Raise :class:`FailSafeException` if :data:`FAILSAFE` is on and the cursor is on a fail-safe point."""
    _d().failsafe.check()


_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_log_lock = threading.Lock()


def _logScreenshot(logScreenshot: Any, funcName: str, funcArgs: str, folder: str = ".") -> Optional[str]:
    """PyAutoGUI's ``_logScreenshot``: save a screenshot named after the call, before it acts.

    Taken when ``logScreenshot`` is true, or when it is ``None`` and :data:`LOG_SCREENSHOTS`
    is on. The oldest file is deleted once :data:`LOG_SCREENSHOTS_LIMIT` are kept.
    Returns the path written (``None``: no screenshot).
    """
    if logScreenshot is False or (logScreenshot is None and not LOG_SCREENSHOTS):
        return None
    from .._secret import scrub

    funcArgs = scrub(str(funcArgs)) or ""  # a credential (locum.rpa.credentials) never lands in a file name
    if len(funcArgs) > 128:  # keep the file name under the 255-character limit
        funcArgs = funcArgs[:128] + "..."
    now = _dt.datetime.now()
    filename = "%s-%s-%s_%s-%s-%s-%s_%s_%s.png" % (
        now.year, str(now.month).rjust(2, "0"), str(now.day).rjust(2, "0"),
        now.hour, now.minute, now.second, str(now.microsecond)[:3], funcName, funcArgs,
    )
    # PyAutoGUI writes the arguments verbatim; characters no file system accepts are replaced.
    filename = _BAD_FILENAME_CHARS.sub("_", filename)
    with _log_lock:
        limit = LOG_SCREENSHOTS_LIMIT
        while limit is not None and G_LOG_SCREENSHOTS_FILENAMES and len(G_LOG_SCREENSHOTS_FILENAMES) >= int(limit):
            oldest = G_LOG_SCREENSHOTS_FILENAMES.pop(0)
            try:
                os.unlink(os.path.join(folder, oldest))
            except FileNotFoundError:
                pass
        if limit is not None and int(limit) <= 0:
            return None
        path = os.path.join(folder, filename)
        try:
            screenshot(path)
        except OSError as exc:  # a debugging aid must not fail the action (path too long, disk full...)
            logging.getLogger("locum.compat.pyautogui").warning("LOG_SCREENSHOTS: could not save %s: %s", path, exc)
            return None
        G_LOG_SCREENSHOTS_FILENAMES.append(filename)
    return path


def _api(pausing: bool = True) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator reproducing ``pyautogui._genericPyAutoGUIChecks``: fail-safe check before, PAUSE after."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        has_pause = "_pause" in sig.parameters

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            failSafeCheck()
            result = fn(*args, **kwargs)
            if pausing and has_pause:
                bound = sig.bind_partial(*args, **kwargs)
                do_pause = bound.arguments.get("_pause", True)
            else:
                do_pause = pausing
            if do_pause and PAUSE:
                _SLEEP(PAUSE)
            return result

        return wrapper

    return decorate


# ================================================================ tweens
def linear(n: float) -> float:
    """Constant speed."""
    return n


def easeInQuad(n: float) -> float:
    return n * n


def easeOutQuad(n: float) -> float:
    return -n * (n - 2)


def easeInOutQuad(n: float) -> float:
    if n < 0.5:
        return 2 * n * n
    n = n * 2 - 1
    return -0.5 * (n * (n - 2) - 1)


def easeInCubic(n: float) -> float:
    return n ** 3


def easeOutCubic(n: float) -> float:
    n -= 1
    return n ** 3 + 1


def easeInOutCubic(n: float) -> float:
    n *= 2
    if n < 1:
        return 0.5 * n ** 3
    n -= 2
    return 0.5 * (n ** 3 + 2)


def easeInQuart(n: float) -> float:
    return n ** 4


def easeOutQuart(n: float) -> float:
    n -= 1
    return -(n ** 4 - 1)


def easeInOutQuart(n: float) -> float:
    n *= 2
    if n < 1:
        return 0.5 * n ** 4
    n -= 2
    return -0.5 * (n ** 4 - 2)


def easeInQuint(n: float) -> float:
    return n ** 5


def easeOutQuint(n: float) -> float:
    n -= 1
    return n ** 5 + 1


def easeInOutQuint(n: float) -> float:
    n *= 2
    if n < 1:
        return 0.5 * n ** 5
    n -= 2
    return 0.5 * (n ** 5 + 2)


def easeInSine(n: float) -> float:
    return -1 * math.cos(n * math.pi / 2) + 1


def easeOutSine(n: float) -> float:
    return math.sin(n * math.pi / 2)


def easeInOutSine(n: float) -> float:
    return -0.5 * (math.cos(math.pi * n) - 1)


def easeInExpo(n: float) -> float:
    return 0.0 if n == 0 else 2 ** (10 * (n - 1))


def easeOutExpo(n: float) -> float:
    return 1.0 if n == 1 else -(2 ** (-10 * n)) + 1


def easeInOutExpo(n: float) -> float:
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0
    n *= 2
    if n < 1:
        return 0.5 * 2 ** (10 * (n - 1))
    n -= 1
    return 0.5 * (-1 * (2 ** (-10 * n)) + 2)


def easeInCirc(n: float) -> float:
    return -1 * (math.sqrt(max(0.0, 1 - n * n)) - 1)


def easeOutCirc(n: float) -> float:
    n -= 1
    return math.sqrt(max(0.0, 1 - n * n))


def easeInOutCirc(n: float) -> float:
    n *= 2
    if n < 1:
        return -0.5 * (math.sqrt(max(0.0, 1 - n ** 2)) - 1)
    n -= 2
    return 0.5 * (math.sqrt(max(0.0, 1 - n ** 2)) + 1)


def easeInElastic(n: float, amplitude: float = 1, period: float = 0.3) -> float:
    return 1 - easeOutElastic(1 - n, amplitude=amplitude, period=period)


def easeOutElastic(n: float, amplitude: float = 1, period: float = 0.3) -> float:
    if n in (0, 1):
        return float(n)
    if amplitude < 1:
        amplitude = 1
        s = period / 4
    else:
        s = period / (2 * math.pi) * math.asin(1 / amplitude)
    return amplitude * 2 ** (-10 * n) * math.sin((n - s) * (2 * math.pi / period)) + 1


def easeInOutElastic(n: float, amplitude: float = 1, period: float = 0.5) -> float:
    n *= 2
    if n < 1:
        return easeInElastic(n, amplitude=amplitude, period=period) / 2
    return easeOutElastic(n - 1, amplitude=amplitude, period=period) / 2 + 0.5


def easeInBack(n: float, s: float = 1.70158) -> float:
    return n * n * ((s + 1) * n - s)


def easeOutBack(n: float, s: float = 1.70158) -> float:
    n -= 1
    return n * n * ((s + 1) * n + s) + 1


def easeInOutBack(n: float, s: float = 1.70158) -> float:
    n *= 2
    s *= 1.525
    if n < 1:
        return 0.5 * (n * n * ((s + 1) * n - s))
    n -= 2
    return 0.5 * (n * n * ((s + 1) * n + s) + 2)


def easeOutBounce(n: float) -> float:
    if n < 1 / 2.75:
        return 7.5625 * n * n
    if n < 2 / 2.75:
        n -= 1.5 / 2.75
        return 7.5625 * n * n + 0.75
    if n < 2.5 / 2.75:
        n -= 2.25 / 2.75
        return 7.5625 * n * n + 0.9375
    n -= 2.625 / 2.75
    return 7.5625 * n * n + 0.984375


def easeInBounce(n: float) -> float:
    return 1 - easeOutBounce(1 - n)


def easeInOutBounce(n: float) -> float:
    if n < 0.5:
        return easeInBounce(n * 2) * 0.5
    return easeOutBounce(n * 2 - 1) * 0.5 + 0.5


def getPointOnLine(x1: float, y1: float, x2: float, y2: float, n: float) -> tuple:
    """The point ``n`` (0..1) of the way from ``(x1, y1)`` to ``(x2, y2)``."""
    return ((x2 - x1) * n + x1, (y2 - y1) * n + y1)


# ================================================================== keys
KEYBOARD_KEYS: List[str] = [
    "\t", "\n", "\r", " ", "!", '"', "#", "$", "%", "&", "'", "(", ")", "*", "+", ",", "-", ".", "/",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ":", ";", "<", "=", ">", "?", "@", "[", "\\", "]", "^", "_", "`",
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v",
    "w", "x", "y", "z", "{", "|", "}", "~",
    "accept", "add", "alt", "altleft", "altright", "apps", "backspace", "browserback", "browserfavorites",
    "browserforward", "browserhome", "browserrefresh", "browsersearch", "browserstop", "capslock", "clear",
    "convert", "ctrl", "ctrlleft", "ctrlright", "decimal", "del", "delete", "divide", "down", "end", "enter",
    "esc", "escape", "execute",
    *(f"f{i}" for i in range(1, 25)),
    "final", "fn", "hanguel", "hangul", "hanja", "help", "home", "insert", "junja", "kana", "kanji",
    "launchapp1", "launchapp2", "launchmail", "launchmediaselect", "left", "modechange", "multiply",
    "nexttrack", "nonconvert",
    *(f"num{i}" for i in range(10)),
    "numlock", "pagedown", "pageup", "pause", "pgdn", "pgup", "playpause", "prevtrack", "print", "printscreen",
    "prntscrn", "prtsc", "prtscr", "return", "right", "scrolllock", "select", "separator", "shift",
    "shiftleft", "shiftright", "sleep", "space", "stop", "subtract", "tab", "up", "volumedown", "volumemute",
    "volumeup", "win", "winleft", "winright", "yen", "command", "option", "optionleft", "optionright",
]
KEY_NAMES = KEYBOARD_KEYS

# PyAutoGUI names that locum spells differently.
_KEY_MAP = {
    "browserback": "browser_back",
    "browserforward": "browser_forward",
    "browserrefresh": "browser_refresh",
    "prntscrn": "printscreen",
    "optionleft": "alt",
    "optionright": "alt_r",
    "altright": "alt_r",
    "ctrlright": "ctrl_r",
    "shiftright": "shift_r",
    "winright": "cmd_r",
}


def isValidKey(key: str) -> bool:
    """Whether ``key`` is a PyAutoGUI key name (see :data:`KEYBOARD_KEYS`)."""
    return isinstance(key, str) and (key in KEYBOARD_KEYS or (len(key) > 1 and key.lower() in KEYBOARD_KEYS))


def isShiftCharacter(character: str) -> bool:
    """Whether typing ``character`` on a US keyboard needs shift."""
    return character.isupper() or character in set('~!@#$%^&*()_+{}|:"<>?')


def _key(key: Any) -> Optional[str]:
    """PyAutoGUI key name -> locum canonical key, or ``None`` if the backend has no such key."""
    if not isinstance(key, str) or key == "":
        return None
    if len(key) == 1 and key.isprintable() and not key.isspace():
        # A printable character types itself in PyAutoGUI ("^" is a caret, "+" a
        # plus sign): never let a modifier-symbol alias turn it into a modifier.
        return key
    k = key if len(key) == 1 else key.lower()
    k = _KEY_MAP.get(k, k)
    try:
        return normalize_key(k)
    except ValueError:
        return None


def _keys(keys: Union[str, Sequence[str]]) -> List[str]:
    if isinstance(keys, str):
        keys = [keys]
    out = []
    for k in keys:
        c = _key(k)
        if c is not None:
            out.append(c)
    return out


# ============================================================ coordinates
def _is_image_arg(value: Any) -> bool:
    if isinstance(value, (str, pathlib.PurePath, bytes, bytearray)):
        return True
    mod = type(value).__module__ or ""
    return mod.startswith("PIL.") or (mod.startswith("numpy") and len(getattr(value, "shape", ())) >= 2)


def _xy(x: Any = None, y: Any = None) -> Point:
    """PyAutoGUI's ``_normalizeXYArgs`` + filling ``None`` with the current cursor coordinate."""
    if x is not None and y is None:
        if _is_image_arg(x):
            loc = locateCenterOnScreen(x)
            if loc is None:  # USE_IMAGE_NOT_FOUND_EXCEPTION is False
                raise ImageNotFoundException(f"Could not locate the image {x!r} on the screen")
            return Point(int(loc.x), int(loc.y))
        if isinstance(x, Match):
            c = x.center
            return Point(int(c.x), int(c.y))
        if isinstance(x, Rect):
            c = x.center
            return Point(int(c.x), int(c.y))
        if isinstance(x, (tuple, list)):
            if len(x) == 2:
                return Point(int(x[0]), int(x[1]))
            if len(x) == 4:
                c = center(x)
                return Point(int(c.x), int(c.y))
            raise PyAutoGUIException(f"The x argument must be a 2- or 4-item sequence, got {len(x)} items")
        if not isinstance(x, numbers.Real):  # numpy scalars / Fraction too, like int(firstArg)
            from ..types import coerce_point

            p = coerce_point(x)
            return Point(p.x, p.y)
    cur = _d().mouse.position()
    return Point(
        int(round(x)) if x is not None else int(cur.x),
        int(round(y)) if y is not None else int(cur.y),
    )


def _duration(duration: Optional[float]) -> float:
    d = float(duration or 0.0)
    return d if d >= MINIMUM_DURATION else 0.0


def _button(button: Any) -> str:
    if isinstance(button, int) and not isinstance(button, bool):
        mapping = {1: "left", 2: "middle", 3: "right", 4: "x1", 5: "x2"}
        if button not in mapping:
            raise PyAutoGUIException(f"button argument must be 1-5 or a button name, not {button!r}")
        return mapping[button]
    b = str(button).lower()
    if b not in ("left", "middle", "right", "primary", "secondary", "x1", "x2", "back", "forward"):
        raise PyAutoGUIException(
            "button argument must be one of ('left', 'middle', 'right', 'primary', 'secondary', 1, 2, 3), not %r" % (button,)
        )
    return b


def _move_to(x: Any, y: Any, duration: Any, tween: Any) -> None:
    p = _xy(x, y)
    _d().mouse.move(p.x, p.y, duration=_duration(duration), tween=tween or linear, humanize=False)


# ================================================================= screen
def size() -> Size:
    """``Size(width, height)`` of the primary monitor."""
    w, h = _d().size()
    return Size(int(w), int(h))


resolution = size


def position(x: Optional[float] = None, y: Optional[float] = None) -> Point:
    """Current cursor position; a given ``x``/``y`` overrides that coordinate in the result."""
    p = _d().mouse.position()
    return Point(int(x) if x is not None else int(p.x), int(y) if y is not None else int(p.y))


def onScreen(x: Any, y: Any = None) -> bool:
    """Whether the point lies on a monitor."""
    if y is None:
        x, y = x[0], x[1]
    return _d().screen.on_screen(int(x), int(y))


def screenshot(imageFilename: Optional[str] = None, region: Optional[Sequence[int]] = None, allScreens: bool = False) -> Any:
    """Screenshot as a PIL image (primary monitor, ``region=(l, t, w, h)`` or every monitor with ``allScreens``).

    Saves to ``imageFilename`` when given.
    """
    d = _d()
    if region is not None:
        img = d.screen.capture(Rect.coerce(tuple(region)))
    elif allScreens:
        img = d.screen.capture()
    else:
        img = d.screen.capture(d.screen.primary_monitor().rect)
    if imageFilename is not None:
        img.save(str(imageFilename))
    return img


def pixel(x: Any, y: Any = None) -> tuple:
    """``(r, g, b)`` at a screen point."""
    if y is None:
        x, y = x[0], x[1]
    r, g, b = _d().screen.pixel(int(x), int(y))
    return (r, g, b)


def pixelMatchesColor(x: int, y: int, expectedRGBColor: Sequence[int], tolerance: int = 0) -> bool:
    """Whether the pixel at ``(x, y)`` is within ``tolerance`` of ``expectedRGBColor`` (RGB or RGBA)."""
    actual = pixel(x, y)
    expected = tuple(int(c) for c in expectedRGBColor)
    if len(expected) == 4:
        actual = actual + (255,)
    elif len(expected) != 3:
        raise PyAutoGUIException("expectedRGBColor must be a 3- or 4-integer tuple")
    return all(abs(a - e) <= int(tolerance) for a, e in zip(actual, expected))


def _load(image: Any) -> Any:
    from .. import vision

    if isinstance(image, (str, pathlib.PurePath)):
        image = str(pathlib.Path(image).expanduser())
    return vision.load_image(image)


def _box(m: Match) -> Box:
    r = m.rect
    return Box(int(r.left), int(r.top), int(r.width), int(r.height))


def _not_found(image: Any) -> None:
    if USE_IMAGE_NOT_FOUND_EXCEPTION:
        raise ImageNotFoundException(f"Could not locate the image {image if isinstance(image, str) else type(image).__name__!r}")
    return None


def _confidence(kwargs: dict) -> float:
    c = kwargs.get("confidence")
    return 0.999 if c is None else float(c)


def _region(kwargs: dict) -> Optional[Rect]:
    r = kwargs.get("region")
    return None if r is None else Rect.coerce(tuple(r))


def useImageNotFoundException(value: Optional[bool] = None) -> None:
    """Make the locate functions raise :class:`ImageNotFoundException` (default) or return ``None`` (``False``)."""
    global USE_IMAGE_NOT_FOUND_EXCEPTION
    USE_IMAGE_NOT_FOUND_EXCEPTION = True if value is None else bool(value)


def locate(needleImage: Any, haystackImage: Any, **kwargs: Any) -> Optional[Box]:
    """Find ``needleImage`` inside ``haystackImage`` (both paths / PIL images). Returns a :class:`Box`.

    Keyword arguments: ``confidence`` (default 0.999), ``grayscale``, ``region``.
    """
    from .. import vision

    m = vision.locate(_load(needleImage), _load(haystackImage), confidence=_confidence(kwargs),
                      grayscale=bool(kwargs.get("grayscale")), region=_region(kwargs))
    if m is None:
        return _not_found(needleImage)
    return _box(m)


def locateAll(needleImage: Any, haystackImage: Any, **kwargs: Any) -> Generator[Box, None, None]:
    """Yield every :class:`Box` where ``needleImage`` occurs in ``haystackImage`` (reading order)."""
    from .. import vision

    limit = kwargs.get("limit")
    matches = vision.locate_all(_load(needleImage), _load(haystackImage), confidence=_confidence(kwargs),
                                grayscale=bool(kwargs.get("grayscale")), region=_region(kwargs),
                                limit=int(limit) if limit else 1000)
    for m in sorted(matches, key=lambda m: (m.rect.top, m.rect.left)):
        yield _box(m)


def locateOnScreen(image: Any, minSearchTime: float = 0, **kwargs: Any) -> Optional[Box]:
    """Find ``image`` on screen, retrying for up to ``minSearchTime`` seconds.

    Keyword arguments: ``confidence`` (default 0.999), ``grayscale``, ``region=(l, t, w, h)``.
    Returns a :class:`Box`; raises :class:`ImageNotFoundException` (or returns ``None`` when
    :data:`USE_IMAGE_NOT_FOUND_EXCEPTION` is false).
    """
    d = _d()
    m = d.locate(_load(image), region=_region(kwargs), confidence=_confidence(kwargs),
                 grayscale=bool(kwargs.get("grayscale")), timeout=max(0.0, float(minSearchTime or 0)))
    if m is None:
        return _not_found(image)
    return _box(m)


def locateAllOnScreen(image: Any, **kwargs: Any) -> Generator[Box, None, None]:
    """Yield every :class:`Box` where ``image`` appears on screen (reading order)."""
    d = _d()
    limit = kwargs.get("limit")
    matches = d.locate_all(_load(image), region=_region(kwargs), confidence=_confidence(kwargs),
                           grayscale=bool(kwargs.get("grayscale")), limit=int(limit) if limit else 1000)
    for m in sorted(matches, key=lambda m: (m.rect.top, m.rect.left)):
        yield _box(m)


def locateCenterOnScreen(image: Any, *, minSearchTime: float = 0, **kwargs: Any) -> Optional[Point]:
    """Center :class:`Point` of ``image`` on screen (see :func:`locateOnScreen`)."""
    box = locateOnScreen(image, minSearchTime, **kwargs)
    return None if box is None else center(box)


def locateOnWindow(image: Any, title: str, **kwargs: Any) -> Optional[Box]:
    """Like :func:`locateOnScreen` but restricted to the first window whose title contains ``title``."""
    wins = getWindowsWithTitle(title)
    if not wins:
        raise PyAutoGUIException(f"Could not find a window with {title!r} in the title")
    w = wins[0]
    w.activate()
    kwargs["region"] = (w.left, w.top, w.width, w.height)
    return locateOnScreen(image, **kwargs)


def center(coords: Sequence[int]) -> Point:
    """Center :class:`Point` of a ``(left, top, width, height)`` box."""
    left, top, width, height = (coords.left, coords.top, coords.width, coords.height) if isinstance(coords, Rect) else coords
    return Point(int(left + int(width / 2)), int(top + int(height / 2)))


# ================================================================== mouse
@_api()
def moveTo(x: Any = None, y: Any = None, duration: float = 0.0, tween: Callable[[float], float] = linear,
           logScreenshot: bool = False, _pause: bool = True) -> None:
    """Move the cursor to ``(x, y)`` (``None`` keeps that coordinate) over ``duration`` seconds."""
    p = _xy(x, y)
    _logScreenshot(logScreenshot, "moveTo", "%s,%s" % (p.x, p.y))
    _move_to(p, None, duration, tween)


@_api()
def moveRel(xOffset: Any = None, yOffset: Any = None, duration: float = 0.0, tween: Callable[[float], float] = linear,
            logScreenshot: bool = False, _pause: bool = True) -> None:
    """Move the cursor relative to its current position."""
    if xOffset is not None and yOffset is None and isinstance(xOffset, (tuple, list)):
        xOffset, yOffset = xOffset[0], xOffset[1]
    _logScreenshot(logScreenshot, "moveRel", "%s,%s" % (xOffset, yOffset))
    cur = _d().mouse.position()
    _move_to(cur.x + int(xOffset or 0), cur.y + int(yOffset or 0), duration, tween)


move = moveRel


def _drag_to(x: Any, y: Any, duration: Any, tween: Any, button: Any, mouseDownUp: bool) -> None:
    p = _xy(x, y)
    d = _d()
    btn = _button(button)
    if not mouseDownUp:
        d.mouse.move(p.x, p.y, duration=_duration(duration), tween=tween or linear, humanize=False)
        return
    d.mouse.drag(None, (p.x, p.y), button=btn, duration=_duration(duration), tween=tween or linear, humanize=False, hold=0.0)


@_api()
def dragTo(x: Any = None, y: Any = None, duration: float = 0.0, tween: Callable[[float], float] = linear,
           button: str = PRIMARY, logScreenshot: Any = None, _pause: bool = True, mouseDownUp: bool = True) -> None:
    """Press ``button``, move to ``(x, y)`` over ``duration`` seconds and release (``mouseDownUp=False``: just move)."""
    p = _xy(x, y)
    _logScreenshot(logScreenshot, "dragTo", "%s,%s" % (p.x, p.y))
    _drag_to(p, None, duration, tween, button, mouseDownUp)


@_api()
def dragRel(xOffset: Any = 0, yOffset: Any = 0, duration: float = 0.0, tween: Callable[[float], float] = linear,
            button: str = PRIMARY, logScreenshot: Any = None, _pause: bool = True, mouseDownUp: bool = True) -> None:
    """Drag relative to the current position."""
    if isinstance(xOffset, (tuple, list)):
        xOffset, yOffset = xOffset[0], xOffset[1]
    _logScreenshot(logScreenshot, "dragRel", "%s,%s" % (xOffset, yOffset))
    cur = _d().mouse.position()
    _drag_to(cur.x + int(xOffset or 0), cur.y + int(yOffset or 0), duration, tween, button, mouseDownUp)


drag = dragRel


def _click(x: Any, y: Any, clicks: int, interval: float, button: Any, duration: float, tween: Any,
           logScreenshot: Any = None) -> None:
    btn = _button(button)
    p = _xy(x, y)
    shown = {"primary": LEFT, "secondary": RIGHT}.get(btn, btn)  # PyAutoGUI logs the normalized name
    _logScreenshot(logScreenshot, "click", "%s,%s,%s,%s" % (shown, clicks, p.x, p.y))
    _move_to(p, None, duration, tween)
    _d().mouse.click(None, button=btn, clicks=int(clicks), interval=float(interval or 0.0), humanize=False)


@_api()
def click(x: Any = None, y: Any = None, clicks: int = 1, interval: float = 0.0, button: str = PRIMARY,
          duration: float = 0.0, tween: Callable[[float], float] = linear, logScreenshot: Any = None,
          _pause: bool = True) -> None:
    """Move to ``(x, y)`` (if given) and click ``clicks`` times, ``interval`` seconds apart."""
    _click(x, y, clicks, interval, button, duration, tween, logScreenshot)


@_api()
def leftClick(x: Any = None, y: Any = None, interval: float = 0.0, duration: float = 0.0,
              tween: Callable[[float], float] = linear, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Left click."""
    _click(x, y, 1, interval, LEFT, duration, tween, logScreenshot)


@_api()
def rightClick(x: Any = None, y: Any = None, interval: float = 0.0, duration: float = 0.0,
               tween: Callable[[float], float] = linear, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Right click."""
    _click(x, y, 1, interval, RIGHT, duration, tween, logScreenshot)


@_api()
def middleClick(x: Any = None, y: Any = None, interval: float = 0.0, duration: float = 0.0,
                tween: Callable[[float], float] = linear, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Middle click."""
    _click(x, y, 1, interval, MIDDLE, duration, tween, logScreenshot)


@_api()
def doubleClick(x: Any = None, y: Any = None, interval: float = 0.0, button: str = LEFT, duration: float = 0.0,
                tween: Callable[[float], float] = linear, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Double click."""
    _click(x, y, 2, interval, button, duration, tween, logScreenshot)


@_api()
def tripleClick(x: Any = None, y: Any = None, interval: float = 0.0, button: str = LEFT, duration: float = 0.0,
                tween: Callable[[float], float] = linear, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Triple click."""
    _click(x, y, 3, interval, button, duration, tween, logScreenshot)


@_api()
def mouseDown(x: Any = None, y: Any = None, button: str = PRIMARY, duration: float = 0.0,
              tween: Callable[[float], float] = linear, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Move to ``(x, y)`` (if given) and press ``button``."""
    btn = _button(button)
    _logScreenshot(logScreenshot, "mouseDown", "%s,%s" % (x, y))
    if x is not None or y is not None:
        _move_to(x, y, duration, tween)
    _d().mouse.down(btn)


@_api()
def mouseUp(x: Any = None, y: Any = None, button: str = PRIMARY, duration: float = 0.0,
            tween: Callable[[float], float] = linear, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Move to ``(x, y)`` (if given) and release ``button``."""
    btn = _button(button)
    _logScreenshot(logScreenshot, "mouseUp", "%s,%s" % (x, y))
    if x is not None or y is not None:
        _move_to(x, y, duration, tween)
    _d().mouse.up(btn)


def _scroll(clicks: Any, x: Any, y: Any, horizontal: bool, logScreenshot: Any = None, name: str = "scroll") -> None:
    _logScreenshot(logScreenshot, name, "%s,%s,%s" % (clicks, x, y))
    if x is not None or y is not None:
        _move_to(x, y, 0.0, linear)
    n = int(clicks)
    if horizontal:
        _d().mouse.scroll(0, n)
    else:
        _d().mouse.scroll(n, 0)


@_api()
def scroll(clicks: float, x: Any = None, y: Any = None, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Scroll the wheel ``clicks`` notches (positive = up), optionally at ``(x, y)``."""
    _scroll(clicks, x, y, horizontal=False, logScreenshot=logScreenshot, name="scroll")


@_api()
def vscroll(clicks: float, x: Any = None, y: Any = None, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Vertical scroll (same as :func:`scroll`)."""
    _scroll(clicks, x, y, horizontal=False, logScreenshot=logScreenshot, name="vscroll")


@_api()
def hscroll(clicks: float, x: Any = None, y: Any = None, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Horizontal scroll (positive = right)."""
    _scroll(clicks, x, y, horizontal=True, logScreenshot=logScreenshot, name="hscroll")


# =============================================================== keyboard
@_api()
def keyDown(key: str, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Press and hold ``key`` (unknown names are ignored)."""
    k = _key(key)
    if k is not None:
        _logScreenshot(logScreenshot, "keyDown", key)
        _d().keyboard.down(k)


@_api()
def keyUp(key: str, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Release ``key`` (unknown names are ignored)."""
    k = _key(key)
    if k is not None:
        _logScreenshot(logScreenshot, "keyUp", key)
        _d().keyboard.up(k)


@_api()
def press(keys: Union[str, Sequence[str]], presses: int = 1, interval: float = 0.0, logScreenshot: Any = None,
          _pause: bool = True) -> None:
    """Tap ``keys`` (a name or a list of names) ``presses`` times, ``interval`` seconds apart."""
    names = _keys(keys)
    if not names or int(presses) <= 0:
        return
    _logScreenshot(logScreenshot, "press", keys if isinstance(keys, str) else ",".join(map(str, keys)))
    d = _d()
    for i in range(int(presses)):
        for j, k in enumerate(names):
            if (i or j) and interval:
                _SLEEP(float(interval))
            d.keyboard.press(k)


def hotkey(*args: Any, **kwargs: Any) -> None:
    """Press keys in order and release them in reverse: ``hotkey("ctrl", "shift", "esc")``.

    Keyword arguments: ``interval`` (seconds between presses), ``logScreenshot``
    (see :data:`LOG_SCREENSHOTS`), ``_pause``. Keys are always released, even if pressing one fails.
    """
    failSafeCheck()
    pause = kwargs.pop("_pause", True)
    log_shot = kwargs.pop("logScreenshot", None)
    interval = float(kwargs.pop("interval", 0.0) or 0.0)
    if kwargs:
        raise TypeError(f"hotkey() got unexpected keyword arguments: {', '.join(sorted(kwargs))}")
    flat: List[str] = []
    for a in args:
        if isinstance(a, (list, tuple)):
            flat.extend(a)
        else:
            flat.append(a)
    names = _keys(flat)
    if names:
        _logScreenshot(log_shot, "hotkey", ",".join(map(str, flat)))
        _d().keyboard.hotkey(names, interval=interval)
    if pause and PAUSE:
        _SLEEP(PAUSE)


@contextlib.contextmanager
def hold(keys: Union[str, Sequence[str]], logScreenshot: Any = None, _pause: bool = True) -> Iterator[None]:
    """Hold ``keys`` down for the duration of a ``with`` block."""
    failSafeCheck()
    names = _keys(keys)
    d = _d()
    if not names:
        yield
    else:
        with d.keyboard.hold(names):
            yield
    if _pause and PAUSE:
        _SLEEP(PAUSE)


@_api()
def write(message: Union[str, Sequence[str]], interval: float = 0.0, logScreenshot: Any = None, _pause: bool = True) -> None:
    """Type ``message``: a string (``\\n`` = Enter) or a list of key names pressed in turn."""
    d = _d()
    if message:
        _logScreenshot(logScreenshot, "write", message if isinstance(message, str) else ",".join(map(str, message)))
    if isinstance(message, str):
        if message:
            d.keyboard.type(message, interval=float(interval or 0.0), mode=d.typing_mode)
        return
    for i, k in enumerate(message):
        if i and interval:
            _SLEEP(float(interval))
        name = _key(k)
        if name is not None:
            d.keyboard.press(name)


typewrite = write


# ================================================================ windows
class Win:
    """A minimal PyGetWindow-style window object wrapping a :class:`locum.Window`."""

    def __init__(self, window: Any):
        self._window = window

    def _info(self) -> Any:
        try:
            self._window.refresh()
        except LocumError:
            pass
        return self._window

    @property
    def title(self) -> str:
        return self._info().title

    @property
    def left(self) -> int:
        return self._info().rect.left

    @property
    def top(self) -> int:
        return self._info().rect.top

    @property
    def width(self) -> int:
        return self._info().rect.width

    @property
    def height(self) -> int:
        return self._info().rect.height

    @property
    def right(self) -> int:
        return self._info().rect.right

    @property
    def bottom(self) -> int:
        return self._info().rect.bottom

    @property
    def box(self) -> Box:
        r = self._info().rect
        return Box(r.left, r.top, r.width, r.height)

    @property
    def size(self) -> Size:
        r = self._info().rect
        return Size(r.width, r.height)

    @property
    def topleft(self) -> Point:
        r = self._info().rect
        return Point(r.left, r.top)

    @property
    def center(self) -> Point:
        c = self._info().rect.center
        return Point(c.x, c.y)

    @property
    def isActive(self) -> bool:
        return self._info().is_active

    @property
    def isMinimized(self) -> bool:
        return self._info().is_minimized

    @property
    def isMaximized(self) -> bool:
        return self._info().is_maximized

    @property
    def visible(self) -> bool:
        return self._info().is_visible

    def activate(self) -> None:
        self._window.activate()

    def minimize(self) -> None:
        self._window.minimize()

    def maximize(self) -> None:
        self._window.maximize()

    def restore(self) -> None:
        self._window.restore()

    def close(self) -> None:
        self._window.close()

    def moveTo(self, newLeft: int, newTop: int) -> None:
        self._window.move(newLeft, newTop)

    def moveRel(self, xOffset: int = 0, yOffset: int = 0) -> None:
        r = self._info().rect
        self._window.move(r.left + int(xOffset), r.top + int(yOffset))

    def resizeTo(self, newWidth: int, newHeight: int) -> None:
        self._window.resize(newWidth, newHeight)

    def resizeRel(self, widthOffset: int = 0, heightOffset: int = 0) -> None:
        r = self._info().rect
        self._window.resize(r.width + int(widthOffset), r.height + int(heightOffset))

    @property
    def window(self) -> Any:
        """The underlying :class:`locum.Window`."""
        return self._window

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Win) and other._window == self._window

    def __hash__(self) -> int:
        return hash(self._window)

    def __repr__(self) -> str:
        r = self._window.rect
        return f"Win(left={r.left}, top={r.top}, width={r.width}, height={r.height}, title={self._window.title!r})"


def getActiveWindow() -> Optional[Win]:
    """The foreground window, or ``None``."""
    w = _d().active_window()
    return None if w is None else Win(w)


def getActiveWindowTitle() -> Optional[str]:
    """Title of the foreground window, or ``None``."""
    w = _d().active_window()
    return None if w is None else w.title


def getAllWindows() -> List[Win]:
    """Every top-level window (front-most first)."""
    return [Win(w) for w in _d().windows()]


def getAllTitles() -> List[str]:
    """Titles of every top-level window."""
    return [w.title for w in _d().windows()]


def getWindowsWithTitle(title: str) -> List[Win]:
    """Windows whose title contains ``title`` (case-insensitive, like PyGetWindow)."""
    t = str(title).upper()
    return [Win(w) for w in _d().windows() if t in w.title.upper()]


def getWindowsAt(x: int, y: int) -> List[Win]:
    """Windows whose rectangle contains ``(x, y)`` (front-most first)."""
    return [Win(w) for w in _d().windows() if w.rect.contains((int(x), int(y)))]


# =================================================================== misc
def sleep(seconds: float) -> None:
    """``time.sleep`` (provided for API compatibility)."""
    time.sleep(seconds)


def countdown(seconds: int) -> None:
    """Print a countdown, one number per second."""
    for i in range(int(seconds), 0, -1):
        print(str(i), end=" ", flush=True)
        time.sleep(1)
    print()
