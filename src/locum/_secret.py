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

Two strengths of scrubbing:

* For **humans** (traces, logs, run reports, CLI and exception messages):
  every registered value of :data:`MIN_SCRUB` or more characters is replaced.
  A 4-digit PIN also blanks ``1234`` out of an order number there -- a garbled
  digit in a log is better than a leaked PIN.
* For **the model** (observations, tool results): only what the model needs
  to stay correct about is left alone. A field value that *equals* a
  registered secret is masked whole; inside other text only secrets of
  :data:`MODEL_MIN_SCRUB` or more characters (unambiguous ones) are replaced,
  so a PIN never turns a coordinate, an amount or a window title into
  ``***``. Use :func:`scrub_for_model` / :func:`mask_value` there.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hmac
import html
import json
import re
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Pattern, Tuple

__all__ = ["Secret", "MASK", "MIN_SCRUB", "MODEL_MIN_SCRUB", "register", "known_secrets", "reveal", "is_secret",
           "scrub", "scrub_data", "scrub_for_model", "mask_value", "contains_secret", "typing_secret",
           "scrubbing_errors", "accepts_secret", "forget_all"]

#: Replacement text for a secret.
MASK = "***"
#: Shorter secrets are not scrubbed out of free text (traces, logs, reports, errors: for humans).
MIN_SCRUB = 3
#: Shorter secrets are not scrubbed out of free text the *model* reads (observations, tool
#: results): ``Secret("1234")`` must not turn ``(1234, 250)`` into ``(***, 250)``.
MODEL_MIN_SCRUB = 8

_lock = threading.Lock()
_known: List[str] = []  # longest first
_known_set: set = set()
#: min_len -> (registry snapshot, compiled alternation of every form, shortest form length)
_patterns: Dict[int, Tuple[Tuple[str, ...], Optional[Pattern[str]], int]] = {}
#: Set while a Secret is being typed (inside ``scrubbing_errors(True)`` / ``accepts_secret``):
#: verification code tells "the expected text was built from a Secret" from this, not by
#: searching the text for registered values.
_typing: contextvars.ContextVar = contextvars.ContextVar("locum_typing_secret", default=False)


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
        _patterns.clear()
    _forms.cache_clear()


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


@functools.lru_cache(maxsize=4096)
def _forms(s: str) -> Tuple[str, ...]:
    """The ways a secret can appear in text: as is, quoted by repr/JSON, HTML-escaped."""
    out = [s]
    for form in (repr(s)[1:-1], json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1], html.escape(s)):
        if form not in out:
            out.append(form)
    return tuple(out)


def _compile(secrets: Any, min_len: int, cached: bool = True) -> Tuple[Optional[Pattern[str]], int]:
    """One alternation of every form of every secret of ``min_len`` or more characters
    (longest first, so the longest match wins at a position) and the shortest form's length."""
    forms = sorted({f for s in secrets if len(s) >= min_len for f in _forms(s)}, key=len, reverse=True)
    if not forms:
        return None, 0
    pattern = "|".join(map(re.escape, forms))
    return (re.compile(pattern) if cached else _compile_uncached(pattern)), len(forms[-1])


def _compile_uncached(pattern: str) -> Pattern[str]:
    """``re.compile(pattern)`` without a copy in the ``re`` module's cache. The registry pattern
    is cached in ``_patterns``; every registration makes a new, longer alternation, and ``re``
    would keep hundreds of those (each holding the secrets) for the rest of the process."""
    try:
        from re import _compiler as sre  # type: ignore[attr-defined]  # Python 3.11+
    except ImportError:  # pragma: no cover - Python < 3.11
        import sre_compile as sre  # type: ignore[no-redef]
    try:
        return sre.compile(pattern, 0)
    except Exception:  # pragma: no cover - a changed private API
        return re.compile(pattern)


def _pattern(min_len: int) -> Tuple[Optional[Pattern[str]], int]:
    """The compiled scrub pattern for the current registry (rebuilt only when it changed)."""
    snap = known_secrets()
    cached = _patterns.get(min_len)
    if cached is not None and cached[0] == snap:
        return cached[1], cached[2]
    pat, shortest = _compile(snap, min_len, False)  # not kept by re: see _compile_uncached
    with _lock:
        _patterns[min_len] = (snap, pat, shortest)
    return pat, shortest


def contains_secret(text: Any) -> bool:
    """Whether ``text`` (a string) contains a registered secret (of :data:`MIN_SCRUB` or more characters)."""
    if isinstance(text, Secret):
        return True
    if not isinstance(text, str) or not text:
        return False
    pat, shortest = _pattern(MIN_SCRUB)
    return pat is not None and len(text) >= shortest and pat.search(text) is not None


def scrub(text: Optional[str], extra: Any = (), *, min_len: int = MIN_SCRUB) -> Optional[str]:
    """``text`` with every registered secret (and every string in ``extra``) of ``min_len`` or
    more characters replaced by ``***``.

    The default strength is for text humans read (traces, logs, reports, errors); text the
    model reads goes through :func:`scrub_for_model`."""
    if not isinstance(text, str) or not text:
        return text
    if extra:
        pat, shortest = _compile(set(known_secrets()) | {str(s) for s in extra if s}, min_len)
    else:
        pat, shortest = _pattern(min_len)
    if pat is None or len(text) < shortest:
        return text
    return pat.sub(MASK, text)


def scrub_for_model(text: Optional[str]) -> Optional[str]:
    """``text`` with only the unambiguous (:data:`MODEL_MIN_SCRUB`\\ +) secrets replaced: for
    observations and tool results, where a short PIN must not garble digits the model acts on."""
    return scrub(text, min_len=MODEL_MIN_SCRUB)


def mask_value(value: Any) -> Any:
    """A field value for the model: ``***`` when it *is* a registered secret (any length),
    else :func:`scrub_for_model`. Non-strings pass through (a :class:`Secret` gives ``***``)."""
    if isinstance(value, Secret):
        return MASK
    if not isinstance(value, str) or not value:
        return value
    if value in _known_set or _looks_like_a_secret(value):
        return MASK
    return scrub_for_model(value)


_SEPARATORS = str.maketrans("", "", " \t-._/")
#: Shortest value (separators removed) compared against the secrets that way: a short secret
#: (a 6-digit PIN) must not mask an amount that spells it with a separator ("4821.50").
_MIN_PARTIAL = MODEL_MIN_SCRUB


def _looks_like_a_secret(value: str) -> bool:
    """``value`` is a registered secret shown with separators (``4111 1111 1111 1111``).

    Only the whole secret counts. A prefix rule (for fields that cut a password short)
    masked ordinary values such as "Invoice" when "Invoice-2024-001" was a secret."""
    v = value.translate(_SEPARATORS)
    if len(v) < _MIN_PARTIAL:
        return False
    return any(v == known.translate(_SEPARATORS) for known in list(_known_set))


def scrub_data(value: Any, _depth: int = 0, *, min_len: int = MIN_SCRUB) -> Any:
    """A copy of JSON-like ``value`` (dicts, lists, strings) with secrets scrubbed; :class:`Secret` -> ``***``."""
    pat, shortest = _pattern(min_len)

    def sub(text: str) -> str:
        if pat is None or len(text) < shortest:
            return text
        return pat.sub(MASK, text)

    return _scrub_data(value, sub, _depth)


def _scrub_data(value: Any, sub: Callable[[str], str], depth: int) -> Any:
    if isinstance(value, Secret):
        return MASK
    if isinstance(value, str):
        return sub(value) if value else value
    if depth > 32:
        return value
    if isinstance(value, dict):
        return {k: _scrub_data(v, sub, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_data(v, sub, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_data(v, sub, depth + 1) for v in value)
    return value


def typing_secret() -> bool:
    """Whether the current call is typing a :class:`Secret` (inside :func:`scrubbing_errors`)."""
    return bool(_typing.get())


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
    holds, which is the password. While the block runs, :func:`typing_secret` is true."""
    if not active:
        yield
        return
    token = _typing.set(True)
    try:
        yield
    except BaseException as exc:
        scrub_exception(exc)
        raise
    finally:
        _typing.reset(token)


def scrub_exception(exc: BaseException) -> BaseException:
    """Scrub registered secrets out of ``exc`` and its ``__cause__`` / ``__context__`` chain, in place.

    String ``args`` are rewritten, and so are an ``OSError``'s ``filename``,
    ``filename2`` and ``strerror``. The exception's type is never changed (``type(e)
    is X``, pickling and ``except`` clauses keep working), so an exception with a
    custom ``__str__`` built from other attributes can still show a secret in
    ``str(e)``: the renderers (traces, run reports, robot logs, CLI errors) scrub
    ``str(e)`` and the traceback text themselves."""
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
    if isinstance(exc, OSError):  # str() is built from these, not from args
        for attr in ("filename", "filename2", "strerror"):
            v = getattr(exc, attr, None)
            if v is None or isinstance(v, (bytes, int)):
                continue
            text = v if isinstance(v, str) else _fspath(v)
            if text is not None and contains_secret(text):
                setattr(exc, attr, scrub(text))


def _fspath(v: Any) -> Optional[str]:
    import os

    try:
        p = os.fspath(v)
    except TypeError:
        return None
    return p if isinstance(p, str) else None
