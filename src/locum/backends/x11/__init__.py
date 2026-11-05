"""X11 backend (Linux / BSD desktops, XWayland) built on ``python-xlib``.

Features
--------
* **Input** through the XTEST extension: absolute pointer motion, buttons
  1/2/3/8/9, wheel buttons 4/5 (vertical) and 6/7 (horizontal), and key
  events for every canonical key name in :data:`locum.keys.KEYS`.
* **Layout-independent unicode typing** (:data:`CAP_UNICODE_TYPING`): characters
  that the current keymap cannot produce with at most Shift are bound to a
  *spare* keycode (one with no keysyms) on the fly, exactly like ``xdotool``
  does. Bindings are kept in a small LRU pool, re-used across calls and
  restored when the backend is closed (or garbage collected). Keycodes are
  claimed under a server grab and listed in the ``_LOCUM_SCRATCH_KEYCODES``
  root property, so several backends (in one or more processes) never share
  one. Characters follow the active XKB group (second layout) like a user's
  keystrokes would.
* **Screenshots** via ``mss`` (fast SHM path) with an ``XGetImage`` fallback,
  always returned as ``RGB`` PIL images; regions partially outside the screen
  are padded with black so pixel (0, 0) is always the region's top-left.
* **Monitors** via RandR 1.5 ``GetMonitors`` (falling back to RandR CRTCs, then
  the whole root window). The DPI scale is read from ``Xft.dpi``.
* **Windows** via EWMH (``_NET_CLIENT_LIST_STACKING``, ``_NET_ACTIVE_WINDOW``,
  ``_NET_WM_STATE``, ``_NET_MOVERESIZE_WINDOW``, ``_NET_CLOSE_WINDOW``...), with
  an ICCCM / bare-X fallback when no window manager is running (e.g. Xvfb).
* **Clipboard** through ``xclip`` / ``xsel`` / ``wl-copy`` when installed, else
  natively (:mod:`._x11clip`: the backend owns ``CLIPBOARD`` and serves it,
  large texts with the ``INCR`` protocol).
* **Accessibility** through AT-SPI (``gi.repository.Atspi`` / ``pyatspi``) when
  importable, exposed as :class:`AtspiNode` with portable role names.

The X connection is shared by all threads and guarded by a re-entrant lock;
every injected event is followed by ``XSync`` so the server has processed it
before the primitive returns. A lost X connection raises
:class:`~locum.errors.BackendError`.

Under a pure Wayland session XTEST only reaches XWayland clients; use the
backend inside an X11 session for full-desktop control.
"""

from __future__ import annotations

# Every name the former single-module x11.py had at top level stays importable from here.
import collections  # noqa: F401
import functools  # noqa: F401
import os  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import weakref  # noqa: F401
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple  # noqa: F401

from ...errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound  # noqa: F401
from ...keys import KEYS, split_text_tokens  # noqa: F401
from ...types import Monitor, MouseButton, Point, Rect, WindowInfo  # noqa: F401
from ..base import (  # noqa: F401
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

from .keysyms import (  # noqa: F401
    BUTTONS,
    KEYSYMS,
    _COMMAND_MODIFIERS,
    _CONTROL_CHAR_KEYS,
    _KEYSYM_CAPS_LOCK,
    _KEYSYM_NUM_LOCK,
    _KEYSYM_SHIFT_L,
    _WHEEL_DOWN,
    _WHEEL_LEFT,
    _WHEEL_RIGHT,
    _WHEEL_UP,
    _is_char_keysym,
    _is_keypad_keysym,
    _lower_upper,
    char_to_keysym,
    iter_keysym_table,
    key_to_keysym,
)
from ._xcore import (  # noqa: F401
    _ErrorSink,
    _XLIB,
    _XlibModules,
    _abandon_display,
    _close_quietly,
    _make_error_handler,
    _x_errors,
    _xlib,
)
from .atspi import (  # noqa: F401
    AtspiNode,
    _ATSPI_ROLE_MAP,
    _COLLAPSE_ACTIONS,
    _EXPAND_ACTIONS,
    _INVOKE_ACTIONS,
    _iface,
    _proc_comm,
    load_atspi,
    map_atspi_role,
)
from .input import (  # noqa: F401
    _InputState,
    _SCRATCH_PROP,
    _cleanup,
    _scratch_marks,
    _set_scratch_marks,
)
from .screen import (  # noqa: F401
    _MssHolder,
)
from .backend import (  # noqa: F401
    X11Backend,
)

__all__ = [
    "AtspiNode",
    "BUTTONS",
    "KEYSYMS",
    "X11Backend",
    "char_to_keysym",
    "iter_keysym_table",
    "key_to_keysym",
    "load_atspi",
    "map_atspi_role",
]
