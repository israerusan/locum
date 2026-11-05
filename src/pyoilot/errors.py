"""Exception hierarchy. Every pyoilot error derives from :class:`PyoilotError`."""

from __future__ import annotations


class PyoilotError(Exception):
    """Base class for all pyoilot errors."""


class BackendError(PyoilotError):
    """The platform backend failed to perform an operation."""


class BackendUnavailable(BackendError):
    """No usable backend for this platform/session (e.g. no $DISPLAY, missing dependency)."""


class NotSupported(BackendError):
    """The active backend does not implement this capability."""


class FailSafeTriggered(PyoilotError):
    """The user slammed the mouse into a fail-safe corner or hit the kill hotkey."""


class ActionTimeout(PyoilotError, TimeoutError):
    """A wait/retry deadline elapsed."""


class ElementNotFound(PyoilotError, LookupError):
    """No UI element matched a selector before the timeout."""


class AmbiguousElement(PyoilotError, LookupError):
    """A selector that required a unique match matched several elements."""


class ImageNotFound(PyoilotError, LookupError):
    """A template image / text / color could not be located on screen."""


class WindowNotFound(PyoilotError, LookupError):
    """No window matched the query."""


class SelectorSyntaxError(PyoilotError, ValueError):
    """A selector string could not be parsed."""


class PolicyViolation(PyoilotError, PermissionError):
    """An action was blocked by the safety policy (LLM guardrails, allow/deny lists, dry-run...)."""


class WorkflowError(PyoilotError):
    """A workflow step failed after exhausting its retries."""
