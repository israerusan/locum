"""Exception hierarchy. Every pypilot error derives from :class:`PypilotError`."""

from __future__ import annotations


class PypilotError(Exception):
    """Base class for all pypilot errors."""


class BackendError(PypilotError):
    """The platform backend failed to perform an operation."""


class BackendUnavailable(BackendError):
    """No usable backend for this platform/session (e.g. no $DISPLAY, missing dependency)."""


class NotSupported(BackendError):
    """The active backend does not implement this capability."""


class FailSafeTriggered(PypilotError):
    """The user slammed the mouse into a fail-safe corner or hit the kill hotkey."""


class ActionTimeout(PypilotError, TimeoutError):
    """A wait/retry deadline elapsed."""


class ElementNotFound(PypilotError, LookupError):
    """No UI element matched a selector before the timeout."""


class AmbiguousElement(PypilotError, LookupError):
    """A selector that required a unique match matched several elements."""


class ImageNotFound(PypilotError, LookupError):
    """A template image / text / color could not be located on screen."""


class WindowNotFound(PypilotError, LookupError):
    """No window matched the query."""


class SelectorSyntaxError(PypilotError, ValueError):
    """A selector string could not be parsed."""


class PolicyViolation(PypilotError, PermissionError):
    """An action was blocked by the safety policy (LLM guardrails, allow/deny lists, dry-run...)."""


class WorkflowError(PypilotError):
    """A workflow step failed after exhausting its retries."""
