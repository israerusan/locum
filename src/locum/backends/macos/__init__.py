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

from ...errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound
from ...keys import SHIFTED_CHARS, normalize_key
from ...types import Monitor, MouseButton, Point, Rect, WindowInfo
from ..base import (
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
from .constants import (
    ACCESSIBILITY_HELP,
    FLAG_ALPHA_SHIFT,
    FLAG_ALTERNATE,
    FLAG_COMMAND,
    FLAG_CONTROL,
    FLAG_NUMERIC_PAD,
    FLAG_SECONDARY_FN,
    FLAG_SHIFT,
    SCREEN_RECORDING_HELP,
    _INSTALL_HINT,
    _NS_ACTIVATE_ALL_WINDOWS,
    _NS_ACTIVATE_IGNORING_OTHER_APPS,
    _NS_APPLICATION_ACTIVATION_POLICY_REGULAR,
    _NS_SYSTEM_DEFINED,
    _NX_LALT,
    _NX_LCMD,
    _NX_LCTL,
    _NX_LSHIFT,
    _NX_RALT,
    _NX_RCMD,
    _NX_RCTL,
    _NX_RSHIFT,
    kAXErrorSuccess,
    kAXValueAXErrorType,
    kAXValueCFRangeType,
    kAXValueCGPointType,
    kAXValueCGRectType,
    kAXValueCGSizeType,
    kCGEventFlagsChanged,
    kCGEventKeyDown,
    kCGEventKeyUp,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventOtherMouseDown,
    kCGEventOtherMouseDragged,
    kCGEventOtherMouseUp,
    kCGEventRightMouseDown,
    kCGEventRightMouseDragged,
    kCGEventRightMouseUp,
    kCGEventScrollWheel,
    kCGEventSourceStateHIDSystemState,
    kCGHIDEventTap,
    kCGMouseEventButtonNumber,
    kCGMouseEventClickState,
    kCGMouseEventDeltaX,
    kCGMouseEventDeltaY,
    kCGNullWindowID,
    kCGScrollEventUnitLine,
    kCGWindowImageDefault,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionAll,
    kCGWindowListOptionIncludingWindow,
    kCGWindowListOptionOnScreenOnly,
)
from .keys import (
    CHAR_KEYCODES,
    KeySpec,
    KeyboardLayoutMap,
    MEDIA_KEYS,
    MODIFIER_FLAGS,
    MODIFIER_KEYCODES,
    NAMED_KEYCODES,
    SHORTCUT_KEYS,
    UC_MOD_CMD,
    UC_MOD_NONE,
    UC_MOD_OPTION,
    UC_MOD_SHIFT,
    UNSUPPORTED_KEYS,
    _ARROW_KEYS,
    _CarbonKeyboardLayouts,
    _FN_FLAG_KEYS,
    _KEYPAD_KEYCODES,
    _KEYPAD_KEYS,
    _named_key_flags,
    _printable_char,
    _us_char_spec,
    chunk_utf16,
    resolve_key,
    split_typing_segments,
    utf16_len,
)
from .events import ClickTracker, _BUTTON_EVENTS, _sign
from .displays import (
    DisplayInfo,
    DisplayLayout,
    _adjacent_origin,
    _adjacent_placement,
    _is_free,
    _overlap,
    _push_outward,
    _rects_intersect,
    _round,
    _slide_along_edge,
    capture_plan,
)
from .ax import (
    AXNode,
    _ACTION_NAMES,
    _AXApi,
    _AXMissing,
    _AX_POINT_RE,
    _AX_ROLE_MAP,
    _AX_SIZE_RE,
    _AX_SUBROLE_MAP,
    _BASIC_ATTRS,
    _CHECKABLE_ROLES,
    _EDIT_ROLES,
    _MISSING,
    _UNSET,
    _value_to_str,
    map_ax_role,
)
from .windowing import _plausible_record, _record_bounds, match_window_record, pair_ax_windows
from .backend import MacOSBackend

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
