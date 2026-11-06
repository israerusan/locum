"""Native constants (stable ABI values) and help texts shared by the macOS backend.

They are hard-coded so the pure helpers can be used and tested without pyobjc.
"""

from __future__ import annotations

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
kCGWindowImageBoundsIgnoreFraming = 1 << 0  # no window shadow
kCGWindowImageBestResolution = 1 << 3  # full backing (Retina) resolution

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
