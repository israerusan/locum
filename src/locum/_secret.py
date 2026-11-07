"""Secret values: a wrapper that never shows its value, and a process-wide scrub list.

:class:`Secret` holds a password (from :mod:`locum.rpa.credentials`, or wrapped
by hand). ``str()``, ``repr()``, ``format()`` and f-strings give ``***``; the
value comes out only through :meth:`Secret.reveal`. Typing APIs
(:meth:`Desktop.type`, :meth:`Element.type`, :meth:`Element.set_text`,
:meth:`Keyboard.type`, ``clipboard_set`` and their guarded versions) accept a
``Secret`` directly and reveal it at the last moment.

Every value wrapped in a ``Secret`` is also *registered*: traces
(:class:`~locum.trace.Tracer`), the safety audit log
(:class:`~locum.llm.safety.AuditLog`, confirmation prompts), flow run reports
(:class:`~locum.rpa.report.RunReport`), flow log messages and CLI error messages
replace any registered value with ``***`` -- also when it reaches them as
plain text (an error message naming what was typed into a field). Values
shorter than :data:`MIN_SCRUB` characters are masked as arguments but not
scrubbed out of other text (that would garble every message).
"""

from __future__ import annotations

import contextlib
import functools
import hmac
import html
import json
import threading
from typing import Any, Callable, Iterator, List, Optional, Tuple

__all__ = ["Secret", "MASK", "MIN_SCRUB", "register", "known_secrets", "reveal", "is_secret", "scrub",
           "scrub_data", "contains_secret", "scrubbing_errors", "accepts_secret", "forget_all"]

#: Replacement text for a secret.
MASK = "***"
#: Shorter secrets are not scrubbed out of free text.
MIN_SCRUB = 3

_lock = threading.Lock()
_known: List[str] = []  # longest first
_known_set: set = set()


def register(value: str) -> None:
    """Scrub ``value`` from every trace, audit log, run report and CLI error from now on."""
    if not isinstance(value, str) or not value:
        return
    with _lock:
        if value in _known_set:
            return
        _known_set.add(value)
        _known.append(value)
        _known.sort(key=len, reverse=True)


def known_secrets() -> Tuple[str, ...]:
    """The registered values (longest first)."""
    with _lock:
        return tuple(_known)


def forget_all() -> None:
    """Clear the scrub list (tests)."""
    with _lock:
        _known.clear()
        _known_set.clear()


class Secret:
    """A string that never shows itself: ``str``/``repr``/``format`` give ``***``.

    ``Secret("hunter2").reveal() == "hunter2"``; comparing two secrets is
    constant-time. A secret cannot be pickled (it would land in a file in clear).
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        if isinstance(value, Secret):
            value = value._value
        if not isinstance(value, str):
            raise TypeError(f"Secret() takes a str, not {type(value).__name__}")
        object.__setattr__(self, "_value", value)
        register(value)

    def reveal(self) -> str:
        """The value, for the one place that needs it (typing it)."""
        return self._value

    get_secret_value = reveal  # pydantic's spelling

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Secret is immutable")

    def __repr__(self) -> str:
        return f"Secret({MASK!r})"

    def __str__(self) -> str:
        return MASK

    def __format__(self, spec: str) -> str:
        return format(MASK, spec)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return hmac.compare_digest(self._value.encode("utf-8"), other._value.encode("utf-8"))
        return NotImplemented

    def __hash__(self) -> int:
        return hash((Secret, self._value))

    def __copy__(self) -> "Secret":
        return self

    def __deepcopy__(self, memo: Any) -> "Secret":
        return self

    def __reduce__(self) -> Any:
        raise TypeError("a Secret cannot be pickled")


def is_secret(value: Any) -> bool:
    return isinstance(value, Secret)


def reveal(value: Any) -> Any:
    """``value.reveal()`` for a :class:`Secret`, ``value`` otherwise."""
    return value.reveal() if isinstance(value, Secret) else value


def _forms(s: str) -> Tuple[str, ...]:
    """The ways a secret can appear in text: as is, quoted by repr/JSON, HTML-escaped."""
    out = [s]
    for form in (repr(s)[1:-1], json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1], html.escape(s)):
        if form not in out:
            out.append(form)
    return tuple(out)


def contains_secret(text: Any) -> bool:
    """Whether ``text`` (a string) contains a registered secret."""
    if isinstance(text, Secret):
        return True
    if not isinstance(text, str) or not text:
        return False
    return any(len(s) >= MIN_SCRUB and any(f in text for f in _forms(s)) for s in known_secrets())


def scrub(text: Optional[str], extra: Any = ()) -> Optional[str]:
    """``text`` with every registered secret (and every string in ``extra``) replaced by ``***``."""
    if not isinstance(text, str) or not text:
        return text
    secrets = list(known_secrets())
    if extra:
        secrets = sorted(set(secrets) | {str(s) for s in extra if s}, key=len, reverse=True)
    for s in secrets:
        if len(s) < MIN_SCRUB:
            continue
        for form in _forms(s):
            if form in text:
                text = text.replace(form, MASK)
    return text


def scrub_data(value: Any, _depth: int = 0) -> Any:
    """A copy of JSON-like ``value`` (dicts, lists, strings) with secrets scrubbed; :class:`Secret` -> ``***``."""
    if isinstance(value, Secret):
        return MASK
    if isinstance(value, str):
        return scrub(value)
    if _depth > 32:
        return value
    if isinstance(value, dict):
        return {k: scrub_data(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_data(v, _depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_data(v, _depth + 1) for v in value)
    return value


def accepts_secret(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Method decorator: a :class:`Secret` first argument (``text``) is revealed for the call,
    and the message of an error the call raises is scrubbed."""

    @functools.wraps(fn)
    def wrapper(self: Any, text: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(text, Secret):
            with scrubbing_errors():
                return fn(self, text.reveal(), *args, **kwargs)
        return fn(self, text, *args, **kwargs)

    return wrapper


@contextlib.contextmanager
def scrubbing_errors(active: bool = True) -> Iterator[None]:
    """Scrub registered secrets out of the message of an exception leaving the block.

    Used around typing a :class:`Secret`: a verification error names what the field
    holds, which is the password."""
    if not active:
        yield
        return
    try:
        yield
    except BaseException as exc:
        scrub_exception(exc)
        raise


def scrub_exception(exc: BaseException) -> BaseException:
    """Scrub registered secrets out of ``exc`` and its ``__cause__`` / ``__context__`` chain, in place.

    String ``args`` are rewritten; an exception whose ``str()`` still shows a secret
    (a custom ``__str__``) gets a scrubbed ``__str__`` through a same-named subclass,
    so ``isinstance`` checks and ``except`` clauses keep working."""
    seen: set = set()
    stack: List[Optional[BaseException]] = [exc]
    while stack and len(seen) < 64:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        try:
            _scrub_one(cur)
        except Exception:  # pragma: no cover - exotic exception types
            pass
        stack.extend((cur.__cause__, cur.__context__))
    return exc


def _scrub_one(exc: BaseException) -> None:
    if any(isinstance(a, str) and contains_secret(a) for a in exc.args):
        exc.args = tuple(scrub(a) if isinstance(a, str) else a for a in exc.args)
    try:
        text = str(exc)
    except Exception:
        return
    if not contains_secret(text):
        return
    masked = scrub(text) or ""
    cls = type(exc)
    sub = type(cls.__name__, (cls,), {"__str__": lambda self, _m=masked: _m,
                                      "__module__": cls.__module__, "__qualname__": cls.__qualname__})
    exc.__class__ = sub
