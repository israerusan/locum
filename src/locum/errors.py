"""Exception hierarchy. Every locum error derives from :class:`LocumError`.

Transient vs. fatal
-------------------

Every locum wait (``Desktop.find``, ``Desktop.window``, ``Desktop.locate``,
``Desktop.wait``, scoped ``Element.find``...) polls a probe until it succeeds.
An exception raised by the probe is classified by :func:`is_transient`:

* **Fatal** -- raised on the *first* probe, unchanged (never wrapped in an
  :class:`ActionTimeout`), because waiting cannot fix it:

  - stop requests: :class:`FailSafeTriggered`, :class:`PolicyViolation`;
  - configuration problems: :class:`BackendUnavailable`, :class:`NotSupported`,
    :class:`PermissionDenied` and the built-in :class:`PermissionError`;
  - invalid input: :class:`SelectorSyntaxError` and any other :class:`ValueError`;
  - programming errors: :class:`TypeError`, :class:`AttributeError`,
    :class:`NameError`, :class:`AssertionError`, :class:`ImportError`;
  - **any exception type not listed as transient below** (``RuntimeError``,
    ``KeyError``, ``IndexError``, a third-party error...).

* **Transient** -- "not there yet": retried until the timeout, after which an
  :class:`ActionTimeout` naming the last such error is raised:

  - "not found" results: :class:`ElementNotFound`, :class:`WindowNotFound`,
    :class:`ImageNotFound`, :class:`AmbiguousElement`;
  - :class:`ActionTimeout` from an inner wait;
  - :class:`BackendError` (other than the fatal subclasses above): a stale
    element, a vanished window, a busy UI, a failed screen capture;
  - :class:`OSError` (other than :class:`PermissionError`), including
    :class:`TimeoutError` and connection errors;
  - errors of the accessibility / IPC bridges the backends sit on, matched by
    module so none of them has to be importable: ``COMError`` (comtypes /
    ``_ctypes``, Windows UI Automation), and anything from ``Xlib``, ``gi``
    (``GLib.Error``), ``pyatspi``, ``dbus``, ``objc`` (pyobjc) and ``mss``
    (``ScreenShotError``, a failed screen capture) that is not
    itself a programming error such as a ``TypeError`` subclass.

An exception can override the rule with a boolean ``transient`` attribute --
``raise BackendError("UI busy", transient=True)`` or ``err.transient = False``
-- except that stop requests (:class:`FailSafeTriggered`,
:class:`PolicyViolation`) are always fatal.
"""

from __future__ import annotations

from typing import Optional, Tuple, Type

__all__ = [
    "LocumError",
    "BackendError",
    "BackendUnavailable",
    "NotSupported",
    "PermissionDenied",
    "FailSafeTriggered",
    "ActionTimeout",
    "ElementNotFound",
    "AmbiguousElement",
    "ImageNotFound",
    "WindowNotFound",
    "SelectorSyntaxError",
    "PolicyViolation",
    "WorkflowError",
    "STOP_ERRORS",
    "FATAL_ERRORS",
    "TRANSIENT_ERRORS",
    "is_transient",
]


class LocumError(Exception):
    """Base class for all locum errors.

    ``transient`` (keyword-only) marks this error as worth retrying (``True``)
    or not (``False``) inside locum's polling waits; ``None`` (the default)
    applies the type-based rule of :func:`is_transient`.
    """

    #: Retry classification override; ``None`` = decide by type (see :func:`is_transient`).
    transient: Optional[bool] = None

    def __init__(self, *args: object, transient: Optional[bool] = None) -> None:
        super().__init__(*args)
        if transient is not None:
            self.transient = bool(transient)


class BackendError(LocumError):
    """The platform backend failed to perform an operation."""


class BackendUnavailable(BackendError):
    """No usable backend for this platform/session (e.g. no $DISPLAY, missing dependency)."""


class NotSupported(BackendError):
    """The active backend does not implement this capability."""


class PermissionDenied(BackendError, PermissionError):
    """The operating system refused access (e.g. macOS Accessibility or Screen
    Recording permission not granted, a UIPI-protected elevated window).

    Fatal in every wait: retrying cannot grant the permission.
    """


class FailSafeTriggered(LocumError):
    """The user slammed the mouse into a fail-safe corner or hit the kill hotkey."""


class ActionTimeout(LocumError, TimeoutError):
    """A wait/retry deadline elapsed."""


class ElementNotFound(LocumError, LookupError):
    """No UI element matched a selector before the timeout."""


class AmbiguousElement(LocumError, LookupError):
    """A selector that required a unique match matched several elements."""


class ImageNotFound(LocumError, LookupError):
    """A template image / text / color could not be located on screen."""


class WindowNotFound(LocumError, LookupError):
    """No window matched the query."""


class SelectorSyntaxError(LocumError, ValueError):
    """A selector string could not be parsed."""


class PolicyViolation(LocumError, PermissionError):
    """An action was blocked by the safety policy (LLM guardrails, allow/deny lists, dry-run...)."""


class WorkflowError(LocumError):
    """A workflow step failed after exhausting its retries."""


# ------------------------------------------------------------ classification
#: Stop requests: fatal even when marked ``transient=True``.
STOP_ERRORS: Tuple[Type[BaseException], ...] = (FailSafeTriggered, PolicyViolation)

#: Always fatal (checked before :data:`TRANSIENT_ERRORS`, so these win over
#: their transient base classes: ``PermissionError`` is an ``OSError``,
#: ``NotSupported`` a ``BackendError``...).
FATAL_ERRORS: Tuple[Type[BaseException], ...] = STOP_ERRORS + (
    BackendUnavailable,
    NotSupported,
    PermissionDenied,
    PermissionError,
    SelectorSyntaxError,
    ValueError,
    TypeError,
    AttributeError,
    NameError,
    AssertionError,
    ImportError,
)

#: Retried until the timeout ("not there yet").
TRANSIENT_ERRORS: Tuple[Type[BaseException], ...] = (
    ElementNotFound,
    WindowNotFound,
    ImageNotFound,
    AmbiguousElement,
    ActionTimeout,
    BackendError,
    OSError,
)

# Third-party accessibility / IPC bridges whose errors mean "stale element, busy
# or vanished UI" (matched by the top-level package of the exception class).
# ``mss`` raises a bare ``ScreenShotError(Exception)`` for a failed capture (locked
# workstation, disconnected RDP session); the Windows backend re-raises it as is in
# explicit ``screenshot_method="mss"`` mode.
_TRANSIENT_MODULES = frozenset({"comtypes", "Xlib", "gi", "pyatspi", "dbus", "objc", "mss"})
# ``_ctypes`` also defines programming errors (ctypes.ArgumentError): only COMError.
_TRANSIENT_NAMES = frozenset({("_ctypes", "COMError")})


def _bridge_error(exc: BaseException) -> bool:
    for cls in type(exc).__mro__:
        root = (getattr(cls, "__module__", "") or "").split(".")[0]
        if root in _TRANSIENT_MODULES or (root, cls.__name__) in _TRANSIENT_NAMES:
            return True
    return False


def is_transient(exc: BaseException) -> bool:
    """Whether a polling wait should retry after ``exc`` (``False`` = raise it at once).

    See the module docstring for the full rule.
    """
    if isinstance(exc, STOP_ERRORS):
        return False
    flag = getattr(exc, "transient", None)
    if isinstance(flag, bool):
        return flag
    if isinstance(exc, FATAL_ERRORS):
        return False
    if isinstance(exc, TRANSIENT_ERRORS):
        return True
    return _bridge_error(exc)
