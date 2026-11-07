"""UI elements: an accessibility node bound to a desktop.

:class:`Element` is what ``desktop.find(...)`` returns. It exposes the node's
properties, tree navigation, scoped lookups and *robust* actions:

* ``click(native="auto")`` clicks the element's center with the mouse when it
  is visible and actually hit-testable, and falls back to the native
  accessibility ``invoke`` when it is off-screen, minimized or covered by
  another window -- so automations keep working where coordinate-only tools
  (PyAutoGUI) silently click the wrong thing.
* ``set_text`` sets values natively (UIA ValuePattern, AT-SPI EditableText...)
  when possible, verifies the result and falls back to focus + select-all +
  typing.

Keystrokes are only sent once focus is *proven*
-----------------------------------------------
``type``, ``press`` and the typing path of ``set_text`` send keystrokes to
whatever control has keyboard focus, so before sending any they require
affirmative proof that the keystrokes will reach this element: the element
reports the ``focused`` state, or the backend's ``focused_element()`` is the
element, one of its descendants (the inner edit of a combo box) or -- only
after a click on the element -- an ancestor that owns its keyboard input (a
grid and its cells). A focused window, pane or web document that merely
*contains* the element is not proof before that click: UI Automation and macOS
report the container as focused when nothing inside it is, so the element is
focused (natively, then by click) instead of typing into the container. A
backend that cannot
tell -- it reports no states and no focused element, or reading them raised --
is **not** proof: the action raises :class:`~locum.errors.ElementNotFound`
instead of typing into whichever field (or window) already had focus. (Earlier
builds accepted "cannot tell" as success.)

For backends or apps that genuinely cannot report focus, opt out per call
with ``require_focus=False`` (``el.type("x", require_focus=False)``,
``el.press("enter", require_focus=False)``, ``el.set_text("x",
require_focus=False)``) or for a whole desktop with ``desktop.require_focus =
False``; the per-call keyword wins over the desktop attribute. Opted out, an
element that is *known* to lack focus (another control has it) is still
refused -- only "cannot tell" is let through. :meth:`Element.focus` sends no
keystrokes and keeps its best-effort behaviour.

The ``desktop`` argument only needs the parts of
:class:`locum.desktop.Desktop` used here: ``backend``, ``mouse``,
``keyboard``, ``default_timeout`` and optionally ``sleep``, ``typing_mode``,
``require_focus`` and ``find``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

from . import _inputlock, _target
from ._cancel import check as _cancel_check
from ._secret import MASK, accepts_secret, known_secrets, mask_value, scrub_for_model, typing_secret
from .backends.base import CAP_CLIPBOARD, AccessibleNode
from .errors import (
    ActionTimeout,
    ElementNotFound,
    FailSafeTriggered,
    NotSupported,
    PolicyViolation,
    SelectorSyntaxError,
    VerificationUnavailable,
    is_transient,
)
from .types import Point, Rect
from .wait import wait_until

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from .desktop import Desktop
    from .window import Window

__all__ = ["Element", "ELEMENT_STATES", "NATIVE_ACTIONS", "VERIFY_MODES", "VerifyResult", "add_action_listener",
           "remove_action_listener"]

#: Native (accessibility-API) actions reported to action listeners.
NATIVE_ACTIONS = ("invoke", "set_text", "toggle", "select", "expand", "collapse")

ActionListener = Callable[[str, "Element", Dict[str, Any]], Any]
# id(backend) -> (backend, listeners). Keyed by identity: backends need not be hashable.
_LISTENERS: Dict[int, Tuple[Any, List[ActionListener]]] = {}


def add_action_listener(backend: Any, listener: ActionListener) -> None:
    """Call ``listener(kind, element, data)`` after every successful *native* element
    action on ``backend`` (``kind`` in :data:`NATIVE_ACTIONS`).

    Native actions (UIA patterns, AT-SPI actions...) produce no mouse/keyboard
    input, so input hooks cannot see them; the recorder uses this to capture
    ``Element.set_text``/``invoke``/... too. ``data`` holds action details,
    e.g. ``{"text": ...}`` for ``set_text``. Listener errors are logged, never raised.
    """
    entry = _LISTENERS.get(id(backend))
    if entry is None or entry[0] is not backend:
        entry = (backend, [])
        _LISTENERS[id(backend)] = entry
    entry[1].append(listener)


def remove_action_listener(backend: Any, listener: ActionListener) -> None:
    """Undo :func:`add_action_listener` (no error if it was not registered)."""
    entry = _LISTENERS.get(id(backend))
    if entry is None or entry[0] is not backend:
        return
    try:
        entry[1].remove(listener)
    except ValueError:
        return
    if not entry[1]:
        del _LISTENERS[id(backend)]


def _notify(element: "Element", kind: str, **data: Any) -> None:
    entry = _LISTENERS.get(id(element.backend))
    if entry is None or entry[0] is not element.backend:
        return
    for fn in list(entry[1]):
        try:
            fn(kind, element, data)
        except Exception as exc:  # a listener must never break the automation
            import logging

            logging.getLogger("locum.element").warning("action listener %r failed: %s", fn, exc)

NativeMode = Union[bool, str]

#: States accepted by :meth:`Element.wait_until`.
ELEMENT_STATES = ("visible", "hidden", "enabled", "disabled", "focused", "checked", "unchecked", "selected", "gone", "exists")


def _native_mode(native: NativeMode) -> Union[bool, str]:
    if native is True or native is False:
        return native
    v = str(native).strip().lower()
    if v == "auto":
        return "auto"
    if v in ("true", "native", "invoke", "1", "yes"):
        return True
    if v in ("false", "mouse", "keyboard", "typing", "0", "no"):
        return False
    raise ValueError(f"native must be True, False or 'auto', got {native!r}")


def _bound_input(keyboard: bool, lock: bool = True) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Run an :class:`Element` input method with its input bound to the element's window
    (and, for keyboard methods, to the element's focus): see :mod:`locum._target`.

    With ``lock=True`` the whole method (focus, clicks, keys, read-back) holds the
    desktop input lock (:mod:`locum._inputlock`), so input from another thread
    or process cannot land between its steps. ``lock=False`` methods take it
    themselves around their input part only."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(self: "Element", *args: Any, **kwargs: Any) -> Any:
            with _target.bind(self._input_target(keyboard)):
                if not lock:
                    return fn(self, *args, **kwargs)
                with _inputlock.hold(self.desktop):
                    return fn(self, *args, **kwargs)

        return wrapper

    return deco


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _require_focus(desktop: Any, require_focus: Optional[bool]) -> bool:
    """Effective focus requirement: the per-call keyword, else ``desktop.require_focus``, else True."""
    if require_focus is not None:
        return bool(require_focus)
    try:
        v = getattr(desktop, "require_focus", True)
    except Exception:
        return True
    return True if v is None else bool(v)


def _same(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        return bool(a.same_as(b))
    except Exception:
        return False


def _parse_number(text: str, decimal: Optional[str] = None) -> "set":
    """Numeric readings of ``text`` (``"1,250.00"``, ``"1.250,00-"``, ``"00123"``); empty if not a number.

    Grouping separators are only accepted in groups of three digits, so
    ``"1,25"`` reads as 1.25 and never as 125. Currency symbols/codes
    (``$1,250.00``, ``1.250,00 EUR``) and accounting parentheses are ignored.

    Without ``decimal`` both conventions are tried, so ``"1,000"`` has two
    readings (1000 and 1.000). With ``decimal="."`` or ``","`` only that
    convention is used and the result has at most one element.
    """
    if decimal not in (None, ".", ","):
        raise ValueError(f"decimal must be '.' or ',', got {decimal!r}")
    import re
    from decimal import Decimal, InvalidOperation

    t = "".join(text.split()).replace("'", "").replace("\u00a0", "").replace("\u202f", "")
    t = re.sub(r"^[A-Z]{3}(?=[-+(\d])|(?<=[\d)\-])[A-Z]{3}$", "", t.strip("$€£¥₹"))  # "EUR", "$"
    sign = 1
    if t[:1] == "(" and t[-1:] == ")":  # accounting format: (1,250.00)
        sign, t = -1, t[1:-1]
    elif t[:1] and t[:1] in "+-":
        sign, t = (-1 if t[0] == "-" else 1), t[1:]
    elif t.endswith("-"):  # SAP writes negative amounts as "1.250,00-"
        sign, t = -1, t[:-1]
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", t):
        return set()
    out = set()
    for dec, grp in ((".", ","), (",", ".")):
        if decimal is not None and dec != decimal:
            continue
        whole, _, frac = t.partition(dec)
        if grp in frac or dec in frac:
            continue
        groups = whole.split(grp)
        if len(groups) > 1 and not (len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:])):
            continue
        try:
            out.add(sign * Decimal("".join(groups) + ("." + frac if frac else "")))
        except InvalidOperation:  # pragma: no cover - the regex only lets digits through
            continue
    return out


def _same_text(actual: str, expected: str, decimal: Optional[str] = None) -> bool:
    """Whether a field reading ``actual`` holds ``expected`` up to the app's normalisation.

    Fields commonly upper-case, collapse whitespace/line breaks, or reformat
    numbers (``1250`` -> ``1,250.00``); a truncated, refused or different value
    does not count.

    Numbers are compared by value only when that value is unambiguous: with a
    known ``decimal`` separator, or when neither side can be read two ways.
    ``"1,000"`` is 1000 in one locale and 1.000 in another, so without a
    separator it never "equals" ``"1"`` or ``"1.00"`` (a 1000x error).
    """
    if actual == expected:
        return True
    if " ".join(actual.split()).casefold() == " ".join(expected.split()).casefold():
        return True
    a, e = _parse_number(actual, decimal), _parse_number(expected, decimal)
    if len(a) != 1 or len(e) != 1:
        return False
    return a == e


def _same_text_lenient(actual: str, expected: str) -> bool:
    """The previous rule (any numeric reading overlaps): only used to decide whether a
    native set-value "took" when the caller asked for no verification at all."""
    if _same_text(actual, expected):
        return True
    a, e = _parse_number(actual), _parse_number(expected)
    return bool(a and e and a & e)


#: Read-back verification modes of :meth:`Element.set_text` / :meth:`Element.type`.
VERIFY_MODES = ("normalized", "exact", "amount")

VerifyMode = Union[bool, str, None]


def _verify_mode(verify: VerifyMode) -> Optional[str]:
    """``verify=`` -> a mode name, or ``None`` for no verification."""
    if verify is None or verify is False:
        return None
    if verify is True:
        return "normalized"
    if isinstance(verify, str) and verify.lower() in VERIFY_MODES:
        return verify.lower()
    raise ValueError(f"verify must be True, False or one of {', '.join(VERIFY_MODES)}, got {verify!r}")


def _matches(actual: str, expected: str, mode: str, decimal: Optional[str]) -> bool:
    """Whether a read-back ``actual`` satisfies ``expected`` under verification ``mode``."""
    if mode == "exact":
        return actual == expected
    if mode == "amount":
        a, e = _parse_number(actual, decimal), _parse_number(expected, decimal)
        return len(a) == 1 and a == e
    return _same_text(actual, expected, decimal)


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of the read-back of the last :meth:`Element.set_text` / :meth:`Element.type`.

    ``status`` is ``"verified"`` (the field was read back and matched),
    ``"unverified"`` (it could not be read back: no value pattern, a password
    field, text typed as Tab/Enter key presses -- the action is *not* proven)
    or ``"skipped"`` (``verify=False``). A mismatch never produces a result:
    it raises :class:`~locum.errors.ActionTimeout`.
    """

    status: str
    mode: Optional[str]
    expected: str
    actual: Optional[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        # Whether a Secret was being typed when this result was made (not a field: the
        # constructor signature is public API).
        object.__setattr__(self, "_secret", typing_secret())

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def _shown(self) -> Tuple[str, Optional[str]]:
        # A secret was typed: the read-back is (a near-copy of) the password, never shown.
        if getattr(self, "_secret", False) or _holds_secret(self.expected) or _holds_secret(self.actual) \
                or mask_value(self.actual) != self.actual:
            return MASK, (None if self.actual is None else MASK)
        return self.expected, self.actual

    def to_dict(self) -> Dict[str, Any]:
        expected, actual = self._shown()
        return {"status": self.status, "mode": self.mode, "expected": expected,
                "actual": actual, "reason": self.reason}

    def __repr__(self) -> str:
        expected, actual = self._shown()
        return (f"VerifyResult(status={self.status!r}, mode={self.mode!r}, expected={expected!r}, "
                f"actual={actual!r}, reason={self.reason!r})")


def _holds_secret(text: Optional[str]) -> bool:
    """Whether ``text`` (the expected text of a verification) holds a secret: the call is typing
    a :class:`~locum._secret.Secret` (tracked, see ``typing_secret``) or ``text`` *is* a
    registered value. Not a substring search: typing ``"Order 12345"`` with ``Secret("1234")``
    registered is not typing a secret."""
    if not isinstance(text, str) or not text:
        return False
    return typing_secret() or text in known_secrets()


def _secret_mismatch(actual: Optional[str], expected: str) -> str:
    """Where a read-back differs from a secret, without showing either: lengths and the first differing position."""
    if actual is None:
        return f"no readable value, expected {MASK!r} (a {len(expected)}-character secret)"
    pos = next((i for i, (a, e) in enumerate(zip(actual, expected)) if a != e), min(len(actual), len(expected)))
    return (f"a {len(actual)}-character value, expected {MASK!r} (a {len(expected)}-character secret; "
            f"first difference at position {pos})")


def _norm_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix(a: str, b: str, limit: int) -> int:
    n = min(len(a), len(b), limit)
    i = 0
    while i < n and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def _typed_into(before: str, after: str, text: str) -> bool:
    """Whether ``after`` is ``before`` with ``text`` typed at one place (replacing a selection or not)."""
    if len(after) < len(text):
        return False
    p = _common_prefix(before, after)
    s = _common_suffix(before, after, len(before))
    # after = before[:a] + text + before[len(before) - k:], a <= p, k <= s, a + k <= len(before)
    for a in range(min(p, len(after) - len(text)) + 1):
        k = len(after) - a - len(text)
        if 0 <= k <= s and a + k <= len(before) and after[a:a + len(text)] == text:
            return True
    return False


def _inserted_region(before: str, after: str) -> str:
    """The part of ``after`` that differs from ``before`` (what the app received from the keystrokes)."""
    p = _common_prefix(before, after)
    s = _common_suffix(before, after, min(len(before), len(after)) - p)
    return after[p:len(after) - s]


def _is_subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)


def _looks_reformatted(before: str, after: str, text: str) -> bool:
    """Whether every typed character arrived, in order, but the app added or reformatted around them.

    Autocomplete (``goo`` -> ``google.com``), input masks (``5551234`` ->
    ``(555) 123-4``) and case-forcing fields keep the typed characters as a
    subsequence of what changed; dropped or garbled keystrokes (``café`` ->
    ``éééé``) do not. Only the latter is repaired.
    """
    region = _inserted_region(before, after)
    return bool(text) and _is_subsequence(text.casefold(), region.casefold())


def _repair_candidates(before: str, after: str, text: str) -> set:
    """What the field should hold if ``text`` was meant to be *inserted* (no selection) but came out garbled.

    The insertion point must lie where ``before`` and ``after`` still agree
    around it; every position consistent with that yields a candidate. One
    distinct candidate = safe to repair; several = ambiguous; none = the
    edit was not a plain insertion (a selection was replaced, the app
    reformatted), which is not repaired.
    """
    p = _common_prefix(before, after)
    s = _common_suffix(before, after, len(before))
    lo, hi = max(0, len(before) - s), min(p, len(before))
    return {before[:k] + text + before[k:] for k in range(lo, hi + 1)}


def _desktop_sleep(desktop: Any) -> Callable[[float], Any]:
    fn = getattr(desktop, "sleep", None)
    return fn if callable(fn) else time.sleep


def _desktop_clock(desktop: Any) -> Callable[[], float]:
    """The desktop's injected clock (``Desktop(clock=...)``), else :func:`time.monotonic`."""
    fn = getattr(desktop, "_clock", None)
    return fn if callable(fn) else time.monotonic


def _desktop_interval(desktop: Any, default: float = 0.1) -> float:
    """The desktop's ``poll_interval`` (seconds between polls).

    ``poll_interval=0`` maps to a short positive pause: a zero interval would
    busy-spin the wait (and never advance an injected test clock).
    """
    try:
        v = float(getattr(desktop, "poll_interval", default))
    except (TypeError, ValueError):
        return default
    if v == 0:
        return 0.05
    return v if v > 0 else default


#: Errors that mean "searching again cannot help" (no accessibility API, invalid
#: selector, emergency stop, policy refusal): scoped lookups raise them at once,
#: like :meth:`locum.desktop.Desktop.find`, instead of waiting for the timeout.
#: Kept for backward compatibility; the full rule is :func:`locum.errors.is_transient`.
_FIND_FATAL = (NotSupported, SelectorSyntaxError, FailSafeTriggered, PolicyViolation)


def _poll_find(desktop: Any, probe: Callable[[], Any], timeout: float) -> Any:
    """Poll ``probe`` until truthy (the waiting behind ``Element.find``/``Window.find``).

    Transient errors (stale nodes, a busy UI) count as "not yet" and become the
    ``__cause__`` of the final :class:`ActionTimeout`; :data:`_FIND_FATAL`
    errors -- and every error :func:`locum.errors.is_transient` rejects --
    propagate immediately.
    """
    last: List[BaseException] = []

    def guarded() -> Any:
        try:
            return probe()
        except _FIND_FATAL:
            raise
        except Exception as exc:  # transient backend / stale tree errors
            if not is_transient(exc):
                raise
            last[:] = [exc]
            return None

    try:
        return wait_until(guarded, timeout, _desktop_interval(desktop), ignore_exceptions=False,
                          sleep=_desktop_sleep(desktop), clock=_desktop_clock(desktop))
    except ActionTimeout as exc:
        if last:
            raise ActionTimeout(f"{exc}; last error: {type(last[0]).__name__}: {last[0]}") from last[0]
        raise


_TEXT_ENTRY_ROLES = frozenset({"edit", "document", "combobox", "spinbutton"})

class Element:
    """A UI element (accessibility node) bound to a desktop.

    Args:
        node: The backend's :class:`AccessibleNode`.
        desktop: The owning :class:`~locum.desktop.Desktop`.
        selector: The selector that found the element (enables :meth:`refresh`).
        scope: The :class:`Element`/:class:`~locum.window.Window` the selector
            is relative to (``None`` = the whole desktop).
        index: Position of the element among the selector's matches when it
            came from ``find_all()``/``wait()`` (``None`` = the first match, as
            for ``find()``). :meth:`refresh` and the ``"gone"``/``"exists"``
            waits use it so the element never silently turns into another match.

    Attributes:
        healed: A :class:`~locum.heal.HealInfo` when the element was found by a
            self-repaired selector (its :attr:`selector` is then the new one),
            else ``None``.
        last_verification: The :class:`VerifyResult` of the last
            :meth:`set_text`/:meth:`type` on this element (``None`` before one).
    """

    #: Set by a healed lookup (see :mod:`locum.heal`); ``None`` for normal matches.
    healed: Any = None
    #: Outcome of the last :meth:`set_text` / :meth:`type` read-back.
    last_verification: Optional["VerifyResult"] = None

    def __init__(
        self,
        node: AccessibleNode,
        desktop: "Desktop",
        selector: Optional[Any] = None,
        *,
        scope: Any = None,
        index: Optional[int] = None,
    ):
        if node is None:
            raise ValueError("Element needs a node")
        if index is not None and int(index) < 0:
            raise ValueError("index must be >= 0")
        self._node = node
        self.desktop = desktop
        self.selector = selector
        self._scope = scope
        self._index = None if index is None else int(index)

    # ------------------------------------------------------------ properties
    @property
    def node(self) -> AccessibleNode:
        return self._node

    @property
    def backend(self) -> Any:
        return self.desktop.backend

    @property
    def role(self) -> str:
        return _safe(lambda: self._node.role, "") or ""

    @property
    def name(self) -> str:
        return _safe(lambda: self._node.name, "") or ""

    @property
    def value(self) -> Optional[str]:
        return _safe(lambda: self._node.value)

    @property
    def text(self) -> str:
        """The element's text content.

        For text-entry controls (``edit``, ``document``, ``combobox``,
        ``spinbutton`` or anything in the ``editable`` state) this is always
        the ``value`` -- an empty field gives ``""``, never its label, so
        ``el.text == ""`` reliably means "empty". For other elements it is the
        ``value`` if non-empty, else the ``name`` (a label's or button's text).
        """
        v = self.value
        if self.role in _TEXT_ENTRY_ROLES or "editable" in self.states:
            return v or ""
        return v if v not in (None, "") else self.name

    @property
    def automation_id(self) -> str:
        return _safe(lambda: self._node.automation_id, "") or ""

    @property
    def class_name(self) -> str:
        return _safe(lambda: self._node.class_name, "") or ""

    @property
    def description(self) -> str:
        return _safe(lambda: self._node.description, "") or ""

    @property
    def bounds(self) -> Optional[Rect]:
        return _safe(lambda: self._node.bounds)

    @property
    def center(self) -> Point:
        b = self.bounds
        if b is None:
            raise ElementNotFound(f"{self!r} has no screen bounds (hidden, minimized or stale)")
        return b.center

    @property
    def states(self) -> frozenset:
        return _safe(lambda: frozenset(self._node.states), frozenset()) or frozenset()

    @property
    def is_enabled(self) -> bool:
        return bool(_safe(lambda: self._node.is_enabled, False))

    @property
    def is_visible(self) -> bool:
        return bool(_safe(lambda: self._node.is_visible, False))

    @property
    def is_focused(self) -> bool:
        return "focused" in self.states

    @property
    def is_checked(self) -> bool:
        return "checked" in self.states

    @property
    def is_selected(self) -> bool:
        return "selected" in self.states

    @property
    def actions(self) -> List[str]:
        return list(_safe(lambda: self._node.actions(), []) or [])

    @property
    def window(self) -> Optional["Window"]:
        """The top-level :class:`~locum.window.Window` owning this element, if it can be determined."""
        from .window import Window

        from ._nodeinfo import owning_window

        info = owning_window(self.backend, self._node, guess=False)
        return Window(info, self.desktop) if info is not None else None

    # ------------------------------------------------------------------ tree
    def _wrap(self, node: AccessibleNode, selector: Any = None, scope: Any = None) -> "Element":
        return Element(node, self.desktop, selector, scope=scope if scope is not None else self._scope)

    @property
    def parent(self) -> Optional["Element"]:
        p = _safe(lambda: self._node.parent)
        return None if p is None else self._wrap(p)

    def children(self) -> List["Element"]:
        kids = _safe(lambda: list(self._node.children()), []) or []
        return [self._wrap(k) for k in kids]

    def find(self, selector: Any, timeout: Optional[float] = None, *, heal: Optional[bool] = None,
             fingerprint: Any = None) -> "Element":
        """First descendant matching ``selector``, polling up to ``timeout`` seconds
        (``None`` = ``desktop.default_timeout``). Raises :class:`ElementNotFound`.

        ``heal`` (default: the desktop's ``heal_selectors``) and ``fingerprint``
        enable self-repair inside this element as in :meth:`Desktop.find`."""
        from .heal import enabled as _heal_enabled
        from .selectors import parse

        sel = parse(selector)
        if timeout is None:
            timeout = float(getattr(self.desktop, "default_timeout", 10.0))
        try:
            node = _poll_find(self.desktop, lambda: sel.find_first([self._node], include_roots=False), timeout)
        except ActionTimeout as exc:
            shown = selector if isinstance(selector, str) else str(sel)
            msg = f"No element matching {shown!r} inside {self!r} within {timeout:g}s"
            default = getattr(self.desktop, "heal_selectors", False)
            if not _heal_enabled(heal, default):
                raise ElementNotFound(msg) from exc
            from .heal import HealOptions, heal as _heal, log_heal

            outcome = _heal(sel, [self._node], fingerprint=fingerprint, include_roots=False, selector_text=shown,
                            selector_root=self._node, log=False,
                            options=default if isinstance(default, HealOptions) else None)
            if not outcome:
                raise ElementNotFound(f"{msg} (self-healing: {outcome.reason})") from exc
            el = Element(outcome.node, self.desktop, outcome.info.new_selector, scope=self)
            el.healed = outcome.info
            log_heal(outcome.info)
            return el
        return Element(node, self.desktop, selector if isinstance(selector, str) else str(sel), scope=self)

    def find_all(self, selector: Any) -> List["Element"]:
        """All descendants matching ``selector`` (no waiting), in document order."""
        from .selectors import parse

        sel = parse(selector)
        text = selector if isinstance(selector, str) else str(sel)
        return [
            Element(n, self.desktop, text, scope=self, index=i)
            for i, n in enumerate(sel.find_all([self._node], include_roots=False))
        ]

    def exists(self, selector: Any, timeout: float = 0) -> bool:
        """Whether a descendant matching ``selector`` appears within ``timeout`` seconds (never heals)."""
        try:
            self.find(selector, timeout=timeout, heal=False)
            return True
        except ElementNotFound:
            return False

    # ----------------------------------------------------------- resolution
    @property
    def index(self) -> Optional[int]:
        """Position among the selector's matches (``None`` = first match / not from ``find_all``)."""
        return self._index

    def _matches(self) -> List[AccessibleNode]:
        """Current matches of the selector in its scope (``[]`` if the scope is gone)."""
        from .errors import WindowNotFound

        src = self._scope if self._scope is not None else self.desktop
        try:
            found = src.find_all(self.selector)
        except (ElementNotFound, WindowNotFound):
            return []
        return [getattr(f, "node", f) for f in found]

    def _pick(self, strict: bool = False) -> Optional[AccessibleNode]:
        """Choose this element among the selector's current matches.

        The element's own node wins if it still matches (so an element never
        jumps to another match while it exists). Otherwise -- e.g. a stale node
        that the toolkit re-created -- the match at :attr:`index` (the first
        match for ``find()`` results) is used, unless ``strict`` is set.
        """
        nodes = self._matches()
        for n in nodes:
            if _same(n, self._node):
                return n
        if strict:
            return None
        i = self._index or 0
        return nodes[i] if i < len(nodes) else None

    def _resolve(self, timeout: float = 0.0, strict: bool = False) -> Optional[AccessibleNode]:
        """Re-run the selector in its scope; ``None`` if it no longer matches this element."""
        if self.selector is None:
            return None
        if timeout <= 0:
            return self._pick(strict)
        try:
            hit = wait_until(
                lambda: (lambda n: (n,) if n is not None else None)(self._pick(strict)),
                timeout,
                _desktop_interval(self.desktop),
                sleep=_desktop_sleep(self.desktop),
                clock=_desktop_clock(self.desktop),
            )
        except ActionTimeout:
            return None
        return hit[0]

    def refresh(self, timeout: float = 0.0) -> "Element":
        """Re-resolve the element through its selector (in place) and return ``self``.

        The current node is kept while it still matches; otherwise the match at
        the element's :attr:`index` is used (the first match for ``find()``
        results). Elements without a selector keep their (live) node. Raises
        :class:`ElementNotFound` if the selector no longer matches.
        """
        if self.selector is None:
            return self
        node = self._resolve(timeout)
        if node is None:
            raise ElementNotFound(f"Element {self.selector!r} no longer exists")
        self._node = node
        return self

    # --------------------------------------------------------------- helpers
    def _ancestors(self, node: AccessibleNode, limit: int = 128) -> List[AccessibleNode]:
        out = []
        cur = _safe(lambda: node.parent)
        while cur is not None and len(out) < limit:
            out.append(cur)
            nxt = cur
            cur = _safe(lambda: nxt.parent)
        return out

    def _occluded(self, point: Point) -> bool:
        """True if hit-testing ``point`` lands on an unrelated element (e.g. a covering window)."""
        try:
            hit = self.backend.element_at(int(point.x), int(point.y))
        except Exception:
            return False
        if hit is None:
            return False
        if _same(hit, self._node):
            return False
        if any(_same(a, self._node) for a in self._ancestors(hit)):
            return False  # hit a child of ours
        if any(_same(a, hit) for a in self._ancestors(self._node)):
            return False  # hit-testing is coarser than our element
        return True

    def _input_target(self, keyboard: bool) -> Optional["_target.InputTarget"]:
        """The input target for this element's window (``None``: unknown window, or the same
        target is already bound by an enclosing call)."""
        cur = _target.current()
        if cur is not None and (not keyboard or cur.element is self):
            mine = _safe(lambda: self._node.handle)
            if cur.element is self or (mine is not None and mine == cur.handle):
                return None
        from ._nodeinfo import owning_window

        info = _safe(lambda: owning_window(self.backend, self._node, guess=False))
        if info is None or info.handle is None:
            return None
        if cur is not None and cur.handle == info.handle and (not keyboard or cur.element is self):
            return None
        # Lenient: focus was proven before typing; a backend that cannot tell where input
        # goes is not a reason to refuse here (explicit bindings -- the CLI's --window -- are strict).
        return _target.InputTarget(self.backend, info.handle, element=self if keyboard else None,
                                   title=info.title or "", strict=False)

    def _guard(self) -> None:
        """Honour the fail-safe (global abort flag + screen corners) before a native action.

        Native accessibility actions produce no mouse/keyboard input, so the
        checks in :class:`~locum.mouse.Mouse`/:class:`~locum.keyboard.Keyboard`
        never see them; every state-changing element action calls this first.
        """
        _cancel_check()
        fs = getattr(self.desktop, "failsafe", None)
        check = getattr(fs, "check", None)
        if callable(check):
            check()
            return
        from . import failsafe as _fs
        from .errors import FailSafeTriggered

        if _fs.is_aborted():
            reason = _fs.abort_reason()
            raise FailSafeTriggered("Automation aborted" + (f": {reason}" if reason else ""))

    def _on_screen(self, b: Rect) -> Optional[Rect]:
        """The largest part of ``b`` lying on a monitor (``None`` if entirely off-screen)."""
        screens: List[Rect] = []
        try:
            screens = [m.rect for m in self.backend.monitors()]
        except Exception:
            screens = []
        if not screens:
            try:
                screens = [self.backend.virtual_screen()]
            except Exception:
                return b  # cannot tell; assume visible
        best: Optional[Rect] = None
        for r in screens:
            part = b.intersect(r)
            if part is not None and (best is None or part.area > best.area):
                best = part
        return best

    def _on_any_screen(self, p: Point) -> bool:
        try:
            screens = [m.rect for m in self.backend.monitors()] or [self.backend.virtual_screen()]
        except Exception:
            return True
        return any(r.contains(p) for r in screens)

    def _click_point(self, offset: Tuple[int, int], *, require: bool) -> Optional[Point]:
        b = self.bounds
        if b is None or not self.is_visible:
            if not require:
                return None
            self._native(lambda: self._node.scroll_into_view(), False)
            b = self.bounds
            if b is None or b.is_empty or not self.is_visible:
                # Still hidden/offscreen: its (virtual) bounds may lie under another control.
                raise ElementNotFound(f"{self!r} is not visible on screen; cannot click it with the mouse")
        dx, dy = int(offset[0]), int(offset[1])
        part = self._on_screen(b)
        if part is None and require:
            # Entirely off-screen: ask the toolkit to scroll it into view once.
            self._native(lambda: self._node.scroll_into_view(), False)
            nb = self.bounds
            part = self._on_screen(nb) if nb is not None and not nb.is_empty else None
            if part is not None:
                b = nb  # type: ignore[assignment]
        if part is None:
            if not require:
                return None
            raise ElementNotFound(f"{self!r} is off-screen at {b}; cannot click it with the mouse")
        if (dx, dy) == (0, 0):
            # Center of the visible part: the OS would clamp an off-screen point to the
            # screen edge and the click would land on a neighbouring control.
            return part.center
        point = Point(b.center.x + dx, b.center.y + dy)
        if not self._on_any_screen(point):
            if not require:
                return None
            raise ElementNotFound(f"Click point ({point.x}, {point.y}) of {self!r} is off-screen")
        return point

    @staticmethod
    def _native(fn: Callable[[], Any], default: Any = None) -> Any:
        """Run a state-changing native action (invoke, toggle, set_value, focus...) best effort.

        The running call's cancel scope is checked right before the action -- after
        any wait that preceded it -- and *outside* the error swallowing, so a call
        that already timed out or was cancelled changes nothing.
        """
        _cancel_check()
        return _safe(fn, default)

    def _try_invoke(self) -> bool:
        self._guard()
        ok = bool(self._native(lambda: self._node.invoke(), False))
        if ok:
            _notify(self, "invoke")
        return ok

    def _activate_window(self) -> bool:
        """Bring the element's top-level window to the front; ``True`` if that was attempted."""
        from ._nodeinfo import owning_window

        info = owning_window(self.backend, self._node, guess=False)
        if info is None:
            return False
        _cancel_check()
        try:
            self.backend.activate_window(info.handle)
        except Exception:
            return False
        return True

    def _mouse_point(self, offset: Tuple[int, int] = (0, 0)) -> Point:
        """Screen point for a mouse action on this element that really hits it.

        If another element covers the point (typically another window), the
        element's window is brought to the front and hit-testing is repeated;
        if it is still covered, :class:`ElementNotFound` is raised instead of
        letting the click/keystrokes land on whatever is on top.
        """
        point = self._click_point(offset, require=True)
        assert point is not None
        if not self._occluded(point):
            return point
        activated = self._activate_window()
        point = self._click_point(offset, require=True)
        assert point is not None
        if activated:
            p = point
            try:  # give the window manager a moment to raise the window
                wait_until(lambda: not self._occluded(p), 1.0, _desktop_interval(self.desktop) or 0.05,
                           sleep=_desktop_sleep(self.desktop), clock=_desktop_clock(self.desktop))
            except ActionTimeout:
                pass
        if self._occluded(point):
            raise ElementNotFound(
                f"{self!r} is covered by another element at ({point.x}, {point.y}) and could not be "
                "brought to the front; refusing to click the wrong thing"
            )
        return point

    def _mouse_click(self, point: Point, button: str, clicks: int) -> None:
        self.desktop.mouse.click(point, button=button, clicks=clicks)
        self._last_click_point = point  # what Desktop.click reports as the clicked point

    # --------------------------------------------------------------- actions
    @_bound_input(keyboard=False)
    def click(self, button: str = "left", clicks: int = 1, *, offset: Tuple[int, int] = (0, 0), native: NativeMode = "auto") -> "Element":
        """Click the element.

        Args:
            button: ``"left"``, ``"right"``, ``"middle"``...
            clicks: Number of clicks (2 = double click).
            offset: Pixel offset from the center.
            native: ``"auto"`` -- mouse click at the center when the element is
                visible and not covered, otherwise the native ``invoke`` action
                (if the element is covered and cannot be invoked, its window is
                activated first); ``True`` -- native invoke only (raises
                :class:`NotSupported`); ``False`` -- mouse only.

        Mouse clicks are hit-tested first: if the element is hidden, or still
        covered by something else after its window was activated,
        :class:`ElementNotFound` is raised rather than clicking whatever is
        drawn at its position. A disabled element is waited for (up to the
        desktop's ``default_timeout``) to become enabled; if it stays disabled
        :class:`ElementNotFound` is raised instead of reporting a click that
        did nothing.
        """
        mode = _native_mode(native)
        self._last_click_point: Optional[Point] = None  # stays None for a native invoke
        self._guard()
        self._wait_enabled()
        simple = str(button).lower() == "left" and int(clicks) == 1 and tuple(offset) == (0, 0)
        if mode is True:
            if not simple:
                raise ValueError("native=True only supports a single left click without offset")
            return self.invoke()
        if mode == "auto":
            point = self._click_point(offset, require=False)
            if point is None or self._occluded(point):
                # Off-screen, hidden or covered: the native action needs no hit-testing.
                if simple and self._try_invoke():
                    return self
            else:
                self._mouse_click(point, button, int(clicks))
                return self
        self._mouse_click(self._mouse_point(offset), button, int(clicks))
        return self

    def double_click(self, button: str = "left", *, offset: Tuple[int, int] = (0, 0)) -> "Element":
        return self.click(button, 2, offset=offset, native=False)

    def right_click(self, *, offset: Tuple[int, int] = (0, 0)) -> "Element":
        return self.click("right", 1, offset=offset, native=False)

    def hover(self, *, offset: Tuple[int, int] = (0, 0)) -> "Element":
        self._guard()
        self.desktop.mouse.move(self._mouse_point(offset))
        return self

    def _wait_enabled(self) -> None:
        """Wait (up to ``default_timeout``) for a disabled element to become enabled; else raise."""
        if "disabled" not in self.states:
            return
        timeout = float(getattr(self.desktop, "default_timeout", 10.0))
        try:
            wait_until(lambda: "disabled" not in self.states, timeout, _desktop_interval(self.desktop) or 0.05,
                       sleep=_desktop_sleep(self.desktop), clock=_desktop_clock(self.desktop))
        except ActionTimeout:
            raise ElementNotFound(
                f"{self!r} is disabled; refusing to click it (the click would do nothing)"
            ) from None

    def invoke(self) -> "Element":
        """Perform the default action natively (no mouse). Raises :class:`NotSupported` if unavailable
        and :class:`ElementNotFound` if the element stays disabled (see :meth:`click`)."""
        self._guard()
        self._wait_enabled()
        if not self._try_invoke():
            raise NotSupported(f"{self!r} does not support native invoke")
        return self

    @_bound_input(keyboard=False)
    def focus(self) -> "Element":
        """Give keyboard focus natively; editable elements fall back to a mouse click.

        Raises :class:`ElementNotFound` if the element is disabled or the click
        did not give it focus (see :meth:`type`)."""
        self._guard()
        if self._native(lambda: self._node.focus(), False):
            return self
        if self._is_text_input():
            self._focus_by_click()
            return self
        raise NotSupported(f"{self!r} cannot be focused natively")

    def _is_text_input(self) -> bool:
        st = self.states
        return self.role in ("edit", "combobox", "document", "spinbutton") or "editable" in st

    def _read_states(self) -> Optional[frozenset]:
        """The node's states, or ``None`` if reading them raised (unknown, not "no states")."""
        try:
            st = self._node.states
            return frozenset(st) if st is not None else frozenset()
        except Exception:
            return None

    def _has_focus(self, allow_ancestor: bool = True) -> Optional[bool]:
        """Whether keystrokes would reach this element (``None`` = the backend cannot tell).

        ``True`` is affirmative proof: the element reports ``focused``, or the
        backend's focused element is the element, a descendant (the inner edit
        of a combo box) or -- only with ``allow_ancestor`` -- an ancestor (a
        grid that owns its cells' keyboard input). ``False`` means it is known
        not to have focus: another control has it (with ``allow_ancestor``
        off, that includes a containing window/pane/document), or the element
        reports states without ``focused`` while the backend reports no
        focused element. A backend error while reading the states or the
        focused element is never turned into evidence either way: without
        other proof the answer is ``None``.

        The ancestor rule is only meaningful *after a click* has physically
        targeted this element: UI Automation and macOS report the window or
        web document as focused when nothing inside has focus, so before that
        an ancestor being focused proves nothing about where keystrokes go.
        """
        states = self._read_states()
        if states is not None and "focused" in states:
            return True
        try:
            f = self.backend.focused_element()
            f_known = True
        except Exception:
            f, f_known = None, False
        if f is not None:
            if _same(f, self._node) or any(_same(a, self._node) for a in self._ancestors(f)):
                return True
            if allow_ancestor and any(_same(a, f) for a in self._ancestors(self._node)):
                return True
            return False
        if not f_known or not states:  # a read failed, or nothing was reported at all
            return None
        return False

    def _focus_refusal(self, state: Optional[bool]) -> ElementNotFound:
        if state is None:
            return ElementNotFound(
                f"{self!r}: could not confirm that it has keyboard focus (the backend reports neither "
                "its focus state nor a focused element, or reading them failed); refusing to send "
                "keystrokes that could land in another control. If this backend/app cannot report "
                "focus, pass require_focus=False to type()/press()/set_text() or set "
                "desktop.require_focus = False"
            )
        return ElementNotFound(
            f"{self!r} did not receive keyboard focus (another control has it); refusing to type into "
            "another control"
        )

    def _check_enabled(self) -> None:
        if "disabled" in self.states:
            raise ElementNotFound(f"{self!r} is disabled; refusing to send keystrokes to it")

    def _check_writable(self) -> None:
        """Refuse to enter text into a disabled or read-only edit field.

        Only edit/spin fields are checked for ``readonly``: a drop-down list
        combo box reports a read-only value yet selects an item when typed into.
        """
        self._check_enabled()
        if "readonly" in self.states and self.role in ("edit", "spinbutton"):
            raise ElementNotFound(f"{self!r} is read-only; refusing to enter text into it")

    def _focus_by_click(self, require: bool = False, *, before_click: Optional[Callable[[], Any]] = None) -> None:
        """Click the element to focus it and verify that it really got keyboard focus.

        Keystrokes typed after a click that did not focus the element would go
        to whichever control had focus before (another field, another window).
        With ``require=True`` focus must be proven (``_has_focus() is True``);
        otherwise only a known *lack* of focus is refused. ``before_click`` runs
        right before the click (a policy gate raises there to prevent it).
        """
        self._check_enabled()
        if before_click is not None:
            before_click()
        self.click(native=False)
        last: List[Optional[bool]] = [None]

        def ok() -> bool:
            last[0] = self._has_focus()
            return last[0] is True if require else last[0] is not False

        try:
            wait_until(ok, 1.0, _desktop_interval(self.desktop) or 0.05,
                       sleep=_desktop_sleep(self.desktop), clock=_desktop_clock(self.desktop))
        except ActionTimeout:
            raise self._focus_refusal(last[0]) from None

    def _ensure_focus(self, require_focus: Optional[bool] = None, *,
                      before_click: Optional[Callable[[], Any]] = None) -> None:
        """Make sure keystrokes go to this element; raise rather than type elsewhere.

        Focus must be proven (see the module docstring) unless ``require_focus``
        -- or, when it is ``None``, ``desktop.require_focus`` -- is False.
        ``before_click`` runs before a focusing click, if one is needed.
        """
        self._guard()
        require = _require_focus(self.desktop, require_focus)
        # Already focused (own "focused" state, read first and cheaply) or focus is
        # on a descendant (the inner edit of a combo box): do not refocus. A
        # focused *ancestor* (window, pane, web document) is no proof here --
        # UIA/macOS report the container when nothing inside is focused.
        if self._has_focus(allow_ancestor=False) is True:
            return
        self._check_enabled()
        if self._native(lambda: self._node.focus(), False):
            # Native focus targeted this element, but the focused element being
            # merely an ancestor still is not proof; that case goes to the click.
            state = self._has_focus(allow_ancestor=False)
            if state is True:
                return
            if state is None:
                if not require:
                    return  # opted out: the backend cannot tell, trust the native focus call
                # A click would not produce proof either (the backend cannot tell) and
                # may have side effects (pressing a button): refuse now.
                raise self._focus_refusal(None)
        self._focus_by_click(require, before_click=before_click)

    def _typing_mode(self, mode: Optional[str]) -> str:
        return mode or getattr(self.desktop, "typing_mode", None) or "auto"

    @accepts_secret
    @_bound_input(keyboard=True, lock=False)  # the native path needs no input lock: taken for typing only
    def set_text(self, text: str, *, native: NativeMode = "auto", clear: bool = True, verify: VerifyMode = True,
                 decimal: Optional[str] = None, require_focus: Optional[bool] = None,
                 _typing_mode: Optional[str] = None) -> "Element":
        """Replace (``clear=True``) or extend the element's text.

        ``native="auto"`` uses the accessibility set-value action and verifies
        the result, falling back to focus + ``mod+a`` + typing; ``True`` only
        tries the native path (raises :class:`NotSupported`); ``False`` always types.

        The typing path never turns ``\\n``/``\\t`` into Enter/Tab key presses
        (which would press a dialog's default button or move to the next field):
        text containing them is pasted.

        With ``verify=True`` the field is read back afterwards: a value that
        differs from the target beyond the app's normalisation (case, white
        space, number formatting) -- a read-only field, a refused or truncated
        input -- raises :class:`ActionTimeout` naming both values instead of
        reporting success. Password fields are not read back. Raises
        :class:`ElementNotFound` for a disabled or read-only edit field, and
        on the typing path (the native set-value needs no focus) when keyboard
        focus cannot be proven -- ``require_focus=False`` opts out, see the
        module docstring.

        Verification modes (``verify=``):

        * ``True`` / ``"normalized"``: the read-back may differ by case, white
          space and number formatting (``1250`` -> ``1,250.00``). Numbers are
          compared by value only when the value is unambiguous: ``"1,000"`` is
          1000 or 1.000 depending on the locale, so it matches ``"1"`` only
          with ``decimal=","`` (or ``Desktop.decimal_separator``).
        * ``"exact"``: the read-back must equal ``text`` character for
          character (document numbers, vendor ids: ``0001234`` is not ``1234``).
        * ``"amount"``: both sides are parsed with **one** decimal separator
          (``decimal=`` or ``Desktop.decimal_separator``; ``ValueError`` if
          neither is set) and compared as :class:`~decimal.Decimal` values;
          SAP's trailing minus and accounting parentheses are understood.
        * ``False``: no read-back.

        A field that cannot be read back (no value, a password field) is never
        reported as verified: ``"exact"``/``"amount"`` raise
        :class:`VerificationUnavailable`; ``"normalized"`` returns with
        :attr:`last_verification` ``.status == "unverified"``.
        """
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        mode = _native_mode(native)
        vmode = _verify_mode(verify)
        dec = self._decimal(decimal, vmode)
        self._guard()
        self._check_writable()
        target = text if clear else (self.value or "") + text
        typed = text
        if mode is not False:
            ok = bool(self._native(lambda: self._node.set_value(target), False))
            if ok:
                current = self._read_back()[0]
                if vmode is None:
                    took = current is None or _same_text_lenient(current, target)
                else:
                    took = current is None or _matches(current, target, vmode, dec)
                if took or mode is True:
                    if vmode is not None and current is not None and not took:
                        self._raise_mismatch(target, current, vmode)  # native only: nothing to fall back to
                    result = self._conclude(target, vmode, dec, current)
                    _notify(self, "set_text", text=target, verification=result)
                    return self
                # The app accepted the call but reads back something else (it ignored
                # the value, or the native set did not reach its model): the field may
                # already contain the appended text, so retype the whole target from
                # a cleared field instead of appending a second time.
                clear, typed = True, target
            elif mode is True:
                raise NotSupported(f"{self!r} does not support setting its value natively")
        kb = self.desktop.keyboard
        typing_mode = self._typing_mode(_typing_mode)
        if any(c in typed for c in "\r\n\t"):
            # Typed, these would be Enter/Tab key presses: paste them literally instead.
            if not _safe(lambda: self.backend.has(CAP_CLIPBOARD), False):
                raise NotSupported(
                    f"{self!r}: text with line breaks or tabs can only be entered literally via the "
                    "clipboard, which this backend does not support"
                )
            typing_mode = "clipboard"
        # Focus, select-all, delete, type and read back are one action: no other
        # thread or process may move the mouse or type in between.
        with _inputlock.hold(self.desktop):
            self._ensure_focus(require_focus)
            if clear:
                kb.hotkey("mod+a")
                kb.press("backspace")
            if typed:
                kb.type(typed, mode=typing_mode)
            if vmode is None:
                self.last_verification = VerifyResult("skipped", None, target, reason="verify=False")
            else:
                self._verify_text(target, vmode, dec)
        return self

    def _decimal(self, decimal: Optional[str], vmode: Optional[str]) -> Optional[str]:
        """The decimal separator for a read-back: the argument, else ``Desktop.decimal_separator``."""
        if decimal is None:
            decimal = _safe(lambda: getattr(self.desktop, "decimal_separator", None))
        if decimal not in (None, ".", ","):
            raise ValueError(f"decimal must be '.' or ',', got {decimal!r}")
        if vmode == "amount" and decimal is None:
            raise ValueError(
                "verify='amount' needs the field's decimal separator: pass decimal='.' or decimal=',' "
                "(or set Desktop(decimal_separator=...)); guessing it is how 1,000 passes for 1.00"
            )
        return decimal

    def _read_back(self) -> Tuple[Optional[str], str]:
        """``(value, reason)``: the field's value, or ``None`` and why it cannot be verified."""
        from ._nodeinfo import is_password

        if _safe(lambda: is_password(self._node), False):
            return None, "password field: the read-back is masked"
        v = self.value
        if v is None:
            return None, "the field exposes no readable value"
        return v, ""

    def _conclude(self, target: str, vmode: Optional[str], decimal: Optional[str], actual: Optional[str],
                  reason: str = "") -> VerifyResult:
        """Record (and for strict modes enforce) the outcome of a read-back that did not mismatch."""
        if vmode is None:
            result = VerifyResult("skipped", None, target, actual, "verify=False")
        elif actual is None:
            result = VerifyResult("unverified", vmode, target, None, reason or "the field exposes no readable value")
        else:
            result = VerifyResult("verified", vmode, target, actual)
        self.last_verification = result
        if result.status == "unverified":
            _notify(self, "verify", verification=result)
            if vmode != "normalized":
                raise VerificationUnavailable(
                    f"{self!r}: cannot verify {MASK if _holds_secret(target) else repr(target)} with verify={vmode!r} ({result.reason}); the input is "
                    "not proven. Use a readable field, or verify=True to accept an unverified result"
                )
        return result

    def _raise_mismatch(self, target: str, actual: Optional[str], vmode: str = "normalized") -> None:
        hint = "pass verify=False if the app legitimately reformats it"
        if _holds_secret(target):
            # A near-copy of the password (one keystroke dropped) would get past scrub(): never show it.
            raise ActionTimeout(
                f"{self._repr_without_value()} holds {_secret_mismatch(actual, target)} after set_text (verify={vmode!r}: the field "
                f"refused, truncated or changed the input); {hint}"
            )
        if vmode == "normalized" and actual is not None and len(_parse_number(actual)) > 1:
            hint = (f"{actual!r} reads as a different number per locale; pass decimal='.' or decimal=',' "
                    "(or verify='amount') to compare it by value")
        raise ActionTimeout(
            f"{self!r} holds {actual!r} after set_text, expected {target!r} (verify={vmode!r}: the field "
            f"refused, truncated or changed the input); {hint}"
        )

    def _verify_text(self, target: str, vmode: str = "normalized", decimal: Optional[str] = None) -> VerifyResult:
        """Wait briefly for the field to read ``target`` (typed keys may be processed asynchronously)."""
        first, reason = self._read_back()
        if first is None and reason.startswith("password"):
            return self._conclude(target, vmode, decimal, None, reason)  # masked: says nothing about the input
        last: List[Optional[str]] = [None]

        def holds() -> bool:
            v = self.value
            last[0] = v
            return v is None or _matches(v, target, vmode, decimal)

        try:
            wait_until(holds, 1.0, _desktop_interval(self.desktop) or 0.05,
                       sleep=_desktop_sleep(self.desktop), clock=_desktop_clock(self.desktop))
        except ActionTimeout:
            self._raise_mismatch(target, last[0], vmode)
        return self._conclude(target, vmode, decimal, last[0])

    @accepts_secret
    @_bound_input(keyboard=True)
    def type(self, text: str, *, verify: bool = True, require_focus: Optional[bool] = None,
             **kw: Any) -> "Element":
        """Focus the element (if needed) and type ``text`` (keyword args go to ``Keyboard.type``).

        With ``verify=True`` (the default) a text field (``edit``/``document``
        whose value is readable and not a password) is read before and after
        typing. The result must be the old text with ``text`` inserted at one
        place (or replacing a selection). Some apps drop or garble synthesized
        keystrokes while busy -- Windows 11 Notepad repeats a later character
        in place of earlier ones -- so a mismatch is **repaired** when the
        intended result is unambiguous (the field was empty, or there is only
        one place the text can have gone): the field is set to it with
        :meth:`set_text`, which is verified in turn. Otherwise
        :class:`ActionTimeout` is raised naming both values instead of
        reporting success. Fields that add to or reformat what was typed
        (autocomplete, input masks, upper-casing) are accepted as long as
        every typed character arrived in order; only lost or garbled
        keystrokes are repaired. Text containing ``\\t``/``\\n``
        is not verified unless pasted (``mode="clipboard"``): typed, those
        are Tab/Enter key presses whose effect is up to the app.

        Nothing is typed unless keyboard focus is proven: raises
        :class:`ElementNotFound` otherwise. ``require_focus=False`` (or
        ``desktop.require_focus = False``) accepts a backend that cannot
        report focus (a repair's ``set_text`` inherits it); see the module
        docstring.

        ``verify="exact"`` accepts only the old text with ``text`` inserted
        verbatim (no autocomplete, masks or re-casing); ``verify="amount"``
        (with ``decimal=``) needs an empty field and compares the result as a
        number. Both refuse to type at all (:class:`VerificationUnavailable`)
        into a field whose result cannot be read back. With ``verify=True``
        such a field is typed into and :attr:`last_verification` says
        ``"unverified"``.
        """
        decimal = kw.pop("decimal", None)
        vmode = _verify_mode(verify)
        dec = self._decimal(decimal, vmode)
        self._guard()
        self._check_writable()
        self._ensure_focus(require_focus)
        kw["mode"] = self._typing_mode(kw.get("mode"))
        before = self._verify_snapshot(text, kw["mode"]) if vmode else None
        if vmode is None:
            self.last_verification = VerifyResult("skipped", None, text, reason="verify=False")
        elif before is None:
            # Strict modes raise here, before a single key is sent.
            self._conclude(text, vmode, dec, None, "the field's result cannot be read back (not a readable text "
                           "field, a password, disabled/read-only, or Tab/Enter typed as key presses)")
        elif vmode == "amount" and before != "":
            raise ValueError(f"{self!r}: verify='amount' types into an empty field only (it holds {before!r}); "
                             "use set_text(..., verify='amount') to replace the value")
        self.desktop.keyboard.type(text, **kw)
        if before is not None:
            self._verify_typed(before, text, require_focus, vmode or "normalized", dec)
        return self

    def _verify_snapshot(self, text: str, mode: Optional[str]) -> Optional[str]:
        """The field's text before typing ``text``, or ``None`` when the result cannot be verified.

        Typed (not pasted), ``\\t`` and ``\\n`` are Tab/Enter *key presses* that
        may move focus, submit a form or be swallowed: the field is not expected
        to hold them, so such text is not verified (and never "repaired" into
        literal tabs/newlines). Disabled or read-only fields are not verified
        either (keystrokes there may be shortcuts, e.g. type-ahead find).
        """
        if not text:
            return None
        if mode != "clipboard" and any(c in text for c in "\t\r\n"):
            return None
        st = _safe(lambda: self.states, frozenset())
        if "disabled" in st or "readonly" in st:
            return None
        return self._value_for_verify()

    #: fields longer than this are not read back by :meth:`type` (cost of the round trip)
    TYPE_VERIFY_MAX_CHARS = 200_000

    def _value_for_verify(self) -> Optional[str]:
        """The field's current text if :meth:`type` can verify it, else ``None``."""
        from ._nodeinfo import is_password

        if self.role not in ("edit", "document") or _safe(lambda: is_password(self._node), True):
            return None
        v = _safe(lambda: self.value, None)
        if not isinstance(v, str) or len(v) > self.TYPE_VERIFY_MAX_CHARS:
            return None
        return v

    def _verify_typed(self, before: str, text: str, require_focus: Optional[bool] = None,
                      vmode: str = "normalized", decimal: Optional[str] = None) -> None:
        b, t = _norm_newlines(before), _norm_newlines(text)
        last: List[Optional[str]] = [None]

        def arrived(v: str) -> bool:
            if vmode == "amount":
                return _matches(v, text, "amount", decimal)
            return _typed_into(b, _norm_newlines(v), t)

        def holds() -> bool:
            v = _safe(lambda: self.value, None)
            last[0] = v
            return v is None or arrived(v)

        try:
            wait_until(holds, 1.0, _desktop_interval(self.desktop) or 0.05,
                       sleep=_desktop_sleep(self.desktop), clock=_desktop_clock(self.desktop))
            self._conclude(text, vmode, decimal, last[0])
            return
        except ActionTimeout:
            pass
        after = _norm_newlines(last[0] or "")
        if vmode == "normalized" and _looks_reformatted(b, after, t):
            # every keystroke arrived; the app completed/masked/re-cased them
            self._conclude(text, vmode, decimal, last[0])
            return
        if vmode == "amount":
            intended = {text}  # the field was empty: the whole value is the amount
        else:
            intended = _repair_candidates(b, after, t)
        if len(intended) != 1:
            if _holds_secret(text):
                raise ActionTimeout(
                    f"{self._repr_without_value()} holds {_secret_mismatch(last[0], b + t)} after typing a secret: the app dropped "
                    "or garbled keystrokes and the intended text is ambiguous, so it was not repaired; use "
                    "set_text(), mode='clipboard', or verify=False if the field reformats its input"
                )
            raise ActionTimeout(
                f"{self!r} holds {last[0]!r} after typing {text!r} into {before!r}: the app dropped or "
                "garbled keystrokes and the intended text is ambiguous, so it was not repaired; use "
                "set_text(), mode='clipboard', or verify=False if the field reformats its input"
            )
        # Native set-value first; if the app needs keystrokes, paste rather than retype
        # (the same keystroke path just failed).
        can_paste = bool(_safe(lambda: self.backend.has(CAP_CLIPBOARD), False))
        self.set_text(intended.pop(), clear=True, verify=vmode, decimal=decimal, require_focus=require_focus,
                      _typing_mode="clipboard" if can_paste else None)

    def clear(self) -> "Element":
        return self.set_text("", clear=True)

    @_bound_input(keyboard=True)
    def press(self, *keys: Any, require_focus: Optional[bool] = None, **kw: Any) -> "Element":
        """Focus the element (if needed) and press keys: ``el.press("ctrl+a", "delete")``.

        Raises :class:`ElementNotFound` unless keyboard focus is proven
        (``require_focus=False`` opts out; see the module docstring)."""
        self._ensure_focus(require_focus)
        self.desktop.keyboard.press(*keys, **kw)
        return self

    def toggle(self) -> "Element":
        """Toggle a checkbox / toggle button (native first, then click)."""
        self._guard()
        if self._native(lambda: self._node.toggle(), False):
            _notify(self, "toggle")
            return self
        return self.click()

    def _check_state(self) -> str:
        st = self.states
        return "checked" if "checked" in st else "mixed" if "mixed" in st else "unchecked"

    def _set_checked(self, want: str) -> "Element":
        """Toggle until the check state is ``want`` (a tri-state box may need two toggles)."""
        state = self._check_state()
        for _ in range(3):
            if state == want:
                return self
            self.toggle()
            before = state
            try:  # the toolkit may update the state asynchronously
                state = wait_until(lambda: (lambda s: s if s != before else None)(self._check_state()), 1.0,
                                   _desktop_interval(self.desktop) or 0.05,
                                   sleep=_desktop_sleep(self.desktop), clock=_desktop_clock(self.desktop))
            except ActionTimeout:
                break  # toggling had no visible effect (disabled, or the state is not reported)
        state = self._check_state()
        if state == want:
            return self
        raise ActionTimeout(f"{self!r} did not become {want!r} (state is {state!r})")

    def check(self) -> "Element":
        """Ensure the element is checked. Raises :class:`ActionTimeout` if it does not become checked."""
        return self._set_checked("checked")

    def uncheck(self) -> "Element":
        """Ensure the element is unchecked (neither checked nor mixed).
        Raises :class:`ActionTimeout` if it does not become unchecked."""
        return self._set_checked("unchecked")

    def select(self) -> "Element":
        """Select a list/tree/tab item (native select, then invoke, then click)."""
        self._guard()
        if self._native(lambda: self._node.select(), False):
            _notify(self, "select")
            return self
        return self.click()

    def expand(self) -> "Element":
        """Expand a tree item / combo box (native first, then the keyboard)."""
        self._guard()
        if "expanded" in self.states:
            return self
        if self._native(lambda: self._node.expand(), False):
            _notify(self, "expand")
            return self
        role = self.role
        if role == "treeitem":
            return self.press("right")
        if role == "combobox":
            return self.press("alt+down")
        raise NotSupported(f"{self!r} cannot be expanded")

    def collapse(self) -> "Element":
        """Collapse a tree item / combo box (native first, then the keyboard)."""
        self._guard()
        if "collapsed" in self.states:
            return self
        if self._native(lambda: self._node.collapse(), False):
            _notify(self, "collapse")
            return self
        role = self.role
        if role == "treeitem":
            return self.press("left")
        if role == "combobox":
            return self.press("escape")
        raise NotSupported(f"{self!r} cannot be collapsed")

    def scroll_into_view(self) -> "Element":
        """Ask the backend to scroll the element into view (best effort)."""
        self._guard()
        self._native(lambda: self._node.scroll_into_view(), False)
        return self

    @_bound_input(keyboard=False)
    def scroll(self, dy: int, dx: int = 0) -> "Element":
        """Scroll ``dy`` wheel notches (``>0`` = up) with the cursor over the element."""
        self._guard()
        self.desktop.mouse.scroll(dy, dx, at=self._mouse_point())
        return self

    def screenshot(self) -> "Image":
        """Capture the element's on-screen rectangle."""
        b = self.bounds
        if b is None or b.is_empty:
            raise ElementNotFound(f"{self!r} has no screen bounds")
        try:
            clipped = b.intersect(self.backend.virtual_screen())
        except Exception:
            clipped = b
        if clipped is None:
            raise ElementNotFound(f"{self!r} is entirely off-screen")
        return self.backend.screenshot(clipped)

    def highlight(self, duration: float = 1.0) -> "Element":
        """Flash a rectangle around the element if the backend can draw overlays (otherwise a no-op)."""
        b = self.bounds
        fn = getattr(self.backend, "highlight", None)
        if b is not None and callable(fn):
            try:
                fn(b, duration)
            except NotSupported:
                pass
        return self

    # ------------------------------------------------------------------ waits
    def _state_predicate(self, state: Any) -> Callable[[], bool]:
        if callable(state):
            return lambda: bool(state(self))
        s = str(state).lower()
        # Elements from find_all()/wait() (index set) are tracked by identity: another
        # node matching the same selector does not keep them "existing".
        strict = self._index is not None
        if s == "gone":
            if self.selector is not None:
                return lambda: self._resolve(0.0, strict) is None
            return lambda: not self.is_visible
        if s == "exists":
            if self.selector is not None:
                return lambda: self._resolve(0.0, strict) is not None
            return lambda: self.bounds is not None
        table: Dict[str, Callable[[], bool]] = {
            "visible": lambda: self.is_visible,
            "hidden": lambda: not self.is_visible,
            "enabled": lambda: self.is_enabled,
            "disabled": lambda: not self.is_enabled,
            "focused": lambda: self.is_focused,
            "checked": lambda: self.is_checked,
            "unchecked": lambda: not self.is_checked,
            "selected": lambda: self.is_selected,
        }
        if s not in table:
            raise ValueError(f"Unknown state {state!r}; expected one of {', '.join(ELEMENT_STATES)} or a callable")
        return table[s]

    def wait_until(self, state: Any = "visible", timeout: Optional[float] = None, interval: Optional[float] = None) -> "Element":
        """Wait until the element reaches ``state`` (a name from :data:`ELEMENT_STATES`
        or ``callable(element) -> bool``). Returns ``self``; raises :class:`ActionTimeout`.

        ``interval`` defaults to the desktop's ``poll_interval``; the desktop's
        injected ``sleep``/``clock`` are used for waiting. Only transient errors
        raised while checking the state are retried (see :mod:`locum.errors`):
        a ``TypeError``/``AttributeError`` in a ``callable`` state raises at once."""
        if interval is None:
            interval = _desktop_interval(self.desktop)
        elif interval == 0:
            interval = 0.05  # a zero interval busy-spins (and never advances an injected clock)
        pred = self._state_predicate(state)
        if timeout is None:
            timeout = float(getattr(self.desktop, "default_timeout", 10.0))
        label = getattr(state, "__name__", None) or str(state)
        sleep = _desktop_sleep(self.desktop)
        event_wait = getattr(self.desktop, "_event_wait", None)
        if callable(event_wait):  # wake on accessibility change events where available
            pred, sleep, interval = event_wait(pred, interval)
        wait_until(
            pred,
            timeout,
            interval,
            message=f"{self!r} did not become {label!r} within {timeout:g}s",
            ignore_exceptions="transient",
            sleep=sleep,
            clock=_desktop_clock(self.desktop),
        )
        return self

    # ------------------------------------------------------------------- misc
    def fingerprint(self) -> Any:
        """A :class:`~locum.heal.Fingerprint` of the element now (store it to help a later heal)."""
        from .heal import Fingerprint

        return Fingerprint.from_element(self)

    def to_dict(self) -> dict:
        b = self.bounds
        return {
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "bounds": b.to_dict() if b is not None else None,
            "states": sorted(self.states),
            "selector": self.selector if self.selector is None or isinstance(self.selector, str) else str(self.selector),
        }

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Element):
            return _same(self._node, other._node)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("element", self.role, self.automation_id))

    def __repr__(self) -> str:
        from .selectors import describe_node

        try:
            # Scrubbed before describe_node shortens them: a cut-off secret would no longer match.
            desc = describe_node(_ScrubbedNode(self._node))
        except Exception:  # pragma: no cover - defensive
            desc = self.role
        return f"<Element {scrub_for_model(desc)}>"

    def _repr_without_value(self) -> str:
        """``repr(self)`` without the field's value (it may hold a near-copy of a secret)."""
        from .selectors import describe_node

        try:
            return f"<Element {scrub_for_model(describe_node(_ScrubbedNode(self._node, hide_value=True)))}>"
        except Exception:  # pragma: no cover - defensive
            return f"<Element {self.role}>"


class _ScrubbedNode:
    """A node whose ``name`` / ``value`` have registered secrets masked (for :meth:`Element.__repr__`).

    The repr reaches the model (in tool errors) too, so it follows the observation rule: a
    value that is a secret is masked whole, only long secrets are scrubbed out of names."""

    __slots__ = ("_n", "_hide_value")

    def __init__(self, node: Any, hide_value: bool = False) -> None:
        self._n = node
        self._hide_value = hide_value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._n, name)

    @property
    def name(self) -> Any:
        return scrub_for_model(self._n.name)

    @property
    def value(self) -> Any:
        if self._hide_value:
            return None
        v = self._n.value
        return mask_value(v)
