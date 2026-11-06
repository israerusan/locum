"""Step-by-step traces of what a :class:`~locum.desktop.Desktop` did.

A :class:`Tracer` records every action and lookup made through one ``Desktop``
instance -- the method, its arguments, the resolved target (selector, element
description and screen bounds), the result or error, the log records the
``locum`` loggers emitted meanwhile (self-healed selectors from ``locum.heal``
show up as step annotations) and a downscaled screenshot after the step::

    from locum import Desktop
    from locum.trace import Tracer, trace

    d = Desktop()
    with Tracer(d) as t:
        d.click('button "OK"')
        d.find('edit "Name"').type("Ada")
    t.save_html("trace.html")              # one self-contained file

    with trace(d, "run.html"):            # saved on exit, also when it raises
        d.window("Invoice").activate()

The tracer wraps the desktop's public methods **on the instance** (the class is
untouched); :meth:`Tracer.stop` restores them exactly. Elements and windows
returned by traced lookups are instrumented the same way, so
``d.find(...).click()`` is a step too. Calls a traced method makes internally
(``click`` -> ``find``) are not separate steps: they fill in the outer step's
target instead.

The HTML page (:meth:`Tracer.save_html`) has inline CSS/JS and base64
screenshots, every piece of user content HTML-escaped: a timeline of steps
colored by status; clicking one shows its screenshot large with the target
highlighted, the selector, arguments, result or error and annotations.

Secrets: text typed into a password field (:meth:`Desktop.type` with a
password field focused, :meth:`Element.type`/``set_text`` on one), text put on
or read from the clipboard (``clipboard_set``/``clipboard_get``), text a call on
a password field returns and the text value of a ``wait_until`` predicate are
recorded as ``***``, and that text is also scrubbed from errors and log
messages. A password field's value never appears in element descriptions. Pass ``redact=[...]`` for other strings that must
never appear in a trace. Screenshots are taken as they are: a password field
shows its bullets, but other sensitive content on screen is captured -- use
``screenshots=False`` (or ``"errors"``) where that matters.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import functools
import html
import inspect
import io
import json
import logging
import os
import pathlib
import threading
import time
import traceback as _traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

__all__ = ["Tracer", "TraceStep", "trace", "DESKTOP_METHODS", "ELEMENT_METHODS", "WINDOW_METHODS", "MASK"]

PathLike = Union[str, "os.PathLike[str]"]

#: Replacement for secret text in a trace.
MASK = "***"

#: :class:`~locum.desktop.Desktop` methods a tracer records (those the instance has).
DESKTOP_METHODS = (
    "move", "move_rel", "click", "double_click", "triple_click", "right_click", "middle_click",
    "mouse_down", "mouse_up", "drag", "scroll",
    "type", "press", "hotkey", "key_down", "key_up",
    "find", "find_all", "exists", "wait", "wait_until", "element_at", "focused",
    "window", "windows", "active_window", "launch",
    "locate", "locate_all", "wait_image", "click_image", "find_text", "click_text", "read_text",
    "clipboard_get", "clipboard_set",
)

#: :class:`~locum.element.Element` methods recorded on elements returned by traced lookups.
ELEMENT_METHODS = (
    "click", "double_click", "right_click", "hover", "invoke", "focus",
    "set_text", "type", "clear", "press", "toggle", "check", "uncheck", "select",
    "expand", "collapse", "scroll", "scroll_into_view", "wait_until", "find", "find_all",
)

#: :class:`~locum.window.Window` methods recorded on windows returned by traced lookups.
WINDOW_METHODS = (
    "activate", "minimize", "maximize", "restore", "close", "set_rect", "move", "resize",
    "wait_closed", "find", "find_all",
)

# Calls whose result designates a target (used to fill the outer step's target).
_LOOKUPS = frozenset({
    "find", "find_all", "wait", "exists", "element_at", "focused", "window", "windows", "active_window",
    "launch", "locate", "locate_all", "wait_image", "find_text",
})
# Parameters that hold a selector / "where" argument, in priority order.
_TARGET_PARAMS = ("selector", "target", "start", "image", "title", "wait_window", "at", "text")
# (kind, method) -> parameter holding text that may be secret.
_TEXT_PARAMS = {("desktop", "type"): "text", ("element", "type"): "text", ("element", "set_text"): "text",
                ("desktop", "clipboard_set"): "text"}
# Calls whose text result is never recorded: the clipboard may hold anything (a
# password copied from a manager...), and a wait_until predicate's value is
# arbitrary user data (``d.wait_until(lambda: field.value)``).
_SECRET_RESULTS = frozenset({"clipboard_get", "wait_until"})
_MIN_SCRUB = 3  # shorter secrets are masked in arguments but not scrubbed out of other text
_MAX_REPR = 300

_ACTIVE: Dict[int, "Tracer"] = {}  # id(desktop) -> its active tracer
_ACTIVE_LOCK = threading.Lock()


# ------------------------------------------------------------------- helpers

def _short(text: str, limit: int = _MAX_REPR) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _rect_list(rect: Any) -> Optional[List[int]]:
    try:
        return [int(rect.left), int(rect.top), int(rect.width), int(rect.height)]
    except Exception:
        return None


class _NoValue:
    """Node proxy hiding the value of a password field from :func:`describe_node`."""

    def __init__(self, node: Any):
        self._node = node

    value = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._node, name)


def _describe_node(node: Any) -> str:
    from ._nodeinfo import is_password
    from .selectors import describe_node

    try:
        return describe_node(_NoValue(node) if is_password(node) else node)
    except Exception as exc:  # a vanished node must not break the trace
        return f"<element: {type(exc).__name__}>"


def _is_element(value: Any) -> bool:
    from .element import Element

    return isinstance(value, Element)


def _is_window(value: Any) -> bool:
    from .window import Window

    return isinstance(value, Window)


def _describe_window(w: Any) -> str:
    try:
        info = w.info
        r = info.rect
        app = f" app={info.app!r}" if info.app else ""
        return f"window {info.title!r}{app} ({r.left},{r.top} {r.width}x{r.height})"
    except Exception as exc:
        return f"<window: {type(exc).__name__}>"


def _fmt(value: Any) -> str:
    """Short, human readable text for an argument or result."""
    from .types import Match, Point, Rect

    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        return _short(repr(value))
    if _is_element(value):
        return _describe_node(value.node)
    if _is_window(value):
        return _describe_window(value)
    if isinstance(value, Point):
        return f"({value.x}, {value.y})"
    if isinstance(value, Rect):
        return f"rect ({value.left},{value.top} {value.width}x{value.height})"
    if isinstance(value, Match):
        r = value.rect
        text = f" text={value.text!r}" if value.text else ""
        return f"match ({r.left},{r.top} {r.width}x{r.height}) confidence={value.confidence:.3f}{text}"
    if isinstance(value, pathlib.PurePath):
        return repr(str(value))
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]" if isinstance(value, list) else "()"
        head = ", ".join(_fmt(v) for v in list(value)[:3])
        more = f", ... ({len(value)} items)" if len(value) > 3 else ""
        return _short(f"[{head}{more}]")
    size = getattr(value, "size", None)
    if isinstance(size, tuple) and len(size) == 2 and hasattr(value, "mode"):
        return f"<image {size[0]}x{size[1]}>"
    shape = getattr(value, "shape", None)
    if shape is not None and not isinstance(value, dict):
        return f"<array {'x'.join(str(s) for s in shape)}>"
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if callable(value):
        return f"<{getattr(value, '__qualname__', None) or type(value).__name__}>"
    try:
        return _short(repr(value))
    except Exception:
        return f"<{type(value).__name__}>"


# ---------------------------------------------------------------------- model

@dataclass
class TraceStep:
    """One recorded call.

    Attributes:
        index: Position in the trace (0-based, in start order).
        method: ``"click"``, ``"find"``... -- ``"element.click"`` / ``"window.close"``
            for calls on a traced element or window.
        args: Argument name -> short text (secrets masked as :data:`MASK`).
        started_at: Wall-clock epoch seconds.
        duration: Seconds the call took (screenshots excluded).
        status: ``"passed"`` or ``"failed"``.
        target: What the call acted on: ``selector`` (text), ``element``
            (description), ``bounds`` (``[left, top, width, height]``), ``point``
            (``[x, y]``), ``window`` -- whichever are known.
        result: Short text of the return value.
        error: ``{"type", "message", "traceback"}`` when the call raised.
        annotations: Log records emitted during the call (``{"time", "level",
            "logger", "message"}``), e.g. ``locum.heal`` self-healing notes.
        screenshot / screenshot_before: Data URL of the (downscaled) screen after
            / before the call, or ``None``.
        screen: ``[left, top, width, height]`` of the captured screen (to place ``target`` on it).
        screenshot_error: Why a screenshot could not be taken.
    """

    index: int
    method: str
    args: Dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0
    duration: float = 0.0
    status: str = "passed"
    target: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    error: Optional[Dict[str, str]] = None
    annotations: List[Dict[str, Any]] = field(default_factory=list)
    screenshot: Optional[str] = None
    screenshot_before: Optional[str] = None
    screen: Optional[List[int]] = None
    screenshot_error: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def title(self) -> str:
        """One-line label: ``click 'button "OK"'``."""
        sel = self.target.get("selector")
        if sel:
            return f"{self.method} {sel}"
        first = next(iter(self.args.values()), "")
        return f"{self.method} {first}".rstrip()

    def to_dict(self, *, screenshots: bool = True) -> Dict[str, Any]:
        d = {
            "index": self.index,
            "method": self.method,
            "title": self.title,
            "args": dict(self.args),
            "started_at": self.started_at,
            "started": _iso(self.started_at),
            "duration": round(float(self.duration), 6),
            "status": self.status,
            "target": dict(self.target),
            "result": self.result,
            "error": dict(self.error) if self.error else None,
            "annotations": [dict(a) for a in self.annotations],
            "screen": self.screen,
            "screenshot_error": self.screenshot_error,
        }
        if screenshots:
            d["screenshot"] = self.screenshot
            d["screenshot_before"] = self.screenshot_before
        return d


class _LogHandler(logging.Handler):
    def __init__(self, tracer: "Tracer"):
        super().__init__(logging.NOTSET)
        self._tracer = tracer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._tracer._on_log(record)
        except Exception:  # pragma: no cover - logging must never break automation
            pass


class Tracer:
    """Record every action and lookup made through ``desktop``.

    Args:
        desktop: The :class:`~locum.desktop.Desktop` (or any object with the same
            methods, e.g. a ``GuardedDesktop``) to trace.
        screenshots: ``True`` -- a screenshot after every step; ``"errors"`` --
            only after a failed step; ``False`` -- none.
        before: Also take a screenshot before each step (doubles the cost).
        max_width: Screenshots are downscaled to at most this many pixels wide.
        quality: JPEG quality of the embedded screenshots (1-95).
        redact: Strings that must never appear in the trace (replaced by :data:`MASK`
            in arguments, results, errors and log messages).
        log_level: Minimum level of ``locum.*`` log records captured as
            annotations. While tracing, the ``locum`` logger's level is lowered to
            it if needed (and restored by :meth:`stop`).
        title: Title of the HTML report.

    Use as a context manager (``with Tracer(d) as t:``) or call :meth:`start` /
    :meth:`stop`. Only one tracer can be active on a desktop at a time.
    """

    def __init__(
        self,
        desktop: Any,
        *,
        screenshots: Union[bool, str] = True,
        before: bool = False,
        max_width: int = 1280,
        quality: int = 80,
        redact: Sequence[str] = (),
        log_level: int = logging.INFO,
        title: Optional[str] = None,
    ):
        if isinstance(screenshots, str):
            mode = screenshots.strip().lower()
            if mode in ("on", "all", "true", "always"):
                screenshots = True
            elif mode in ("off", "none", "false", "never"):
                screenshots = False
            elif mode in ("errors", "error", "failed", "failures"):
                screenshots = "errors"
            else:
                raise ValueError(f"screenshots must be True, False or 'errors', got {screenshots!r}")
        if int(max_width) < 16:
            raise ValueError(f"max_width must be >= 16, got {max_width!r}")
        self.desktop = desktop
        self.screenshots: Union[bool, str] = screenshots if screenshots == "errors" else bool(screenshots)
        self.before = bool(before)
        self.max_width = int(max_width)
        self.quality = max(1, min(95, int(quality)))
        self.log_level = int(log_level)
        self.title = title or "locum trace"
        self.steps: List[TraceStep] = []
        #: Log records emitted outside any step (``"after_step"`` = index of the previous step).
        self.logs: List[Dict[str, Any]] = []
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        #: Screenshot taken by :meth:`capture_final` (e.g. when a test fails after its last step).
        self.final_screenshot: Optional[str] = None
        self.final_screen: Optional[List[int]] = None
        self.final_screenshot_error: Optional[str] = None
        self._secrets: set = {str(s) for s in redact if s}
        self._lock = threading.RLock()
        self._local = threading.local()
        self._active = False
        self._patched: List[Tuple[Any, str, bool, Any]] = []
        self._patched_ids: set = set()
        self._handler: Optional[_LogHandler] = None
        self._logger_level: Optional[int] = None
        self._sig_cache: Dict[Any, Optional[inspect.Signature]] = {}

    # ------------------------------------------------------------ lifecycle
    @property
    def active(self) -> bool:
        """Whether the tracer is recording."""
        return self._active

    @property
    def failed(self) -> bool:
        """Whether any recorded step failed."""
        return any(s.failed for s in self.steps)

    def start(self) -> "Tracer":
        """Instrument the desktop and start recording (no-op if already started)."""
        if self._active:
            return self
        with _ACTIVE_LOCK:
            other = _ACTIVE.get(id(self.desktop))
            if other is not None and other is not self:
                raise RuntimeError("Another Tracer is already active on this desktop; stop it first")
            _ACTIVE[id(self.desktop)] = self
        try:
            self._instrument(self.desktop, "desktop", DESKTOP_METHODS)
            logger = logging.getLogger("locum")
            self._handler = _LogHandler(self)
            self._handler.setLevel(self.log_level)
            logger.addHandler(self._handler)
            if logger.getEffectiveLevel() > self.log_level:
                self._logger_level = logger.level
                logger.setLevel(self.log_level)
        except BaseException:
            self._restore()
            raise
        self.started_at = time.time()
        self.finished_at = None
        self._active = True
        return self

    def stop(self) -> "Tracer":
        """Stop recording and restore every instrumented method exactly. Idempotent."""
        if not self._active and not self._patched and self._handler is None:
            return self
        self._active = False
        self.finished_at = time.time()
        self._restore()
        return self

    def _restore(self) -> None:
        for obj, name, had, old in reversed(self._patched):
            try:
                if had:
                    setattr(obj, name, old)
                else:
                    delattr(obj, name)
            except Exception:  # pragma: no cover - an object that refuses is left as is
                pass
        self._patched.clear()
        self._patched_ids.clear()
        logger = logging.getLogger("locum")
        if self._handler is not None:
            logger.removeHandler(self._handler)
            self._handler = None
        if self._logger_level is not None:
            logger.setLevel(self._logger_level)
            self._logger_level = None
        with _ACTIVE_LOCK:
            if _ACTIVE.get(id(self.desktop)) is self:
                del _ACTIVE[id(self.desktop)]

    def __enter__(self) -> "Tracer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -------------------------------------------------------- instrumenting
    def _instrument(self, obj: Any, kind: str, names: Sequence[str]) -> None:
        key = id(obj)
        if key in self._patched_ids:
            return
        self._patched_ids.add(key)
        own = vars(obj) if hasattr(obj, "__dict__") else {}
        for name in names:
            orig = getattr(obj, name, None)
            if not callable(orig):
                continue
            had = name in own
            previous = own.get(name) if had else None
            try:
                setattr(obj, name, self._wrap(obj, kind, name, orig))
            except Exception:
                continue
            self._patched.append((obj, name, had, previous))

    def _wrap(self, owner: Any, kind: str, name: str, orig: Callable[..., Any]) -> Callable[..., Any]:
        tracer = self

        @functools.wraps(orig)
        def traced(*args: Any, **kwargs: Any) -> Any:
            if not tracer._active:
                return orig(*args, **kwargs)
            return tracer._call(owner, kind, name, orig, args, kwargs)

        return traced

    def _instrument_result(self, value: Any) -> None:
        if not self._active:
            return
        if isinstance(value, (list, tuple)):
            for v in value:
                self._instrument_result(v)
        elif _is_element(value):
            self._instrument(value, "element", ELEMENT_METHODS)
        elif _is_window(value):
            self._instrument(value, "window", WINDOW_METHODS)

    # -------------------------------------------------------------- calling
    def _stack(self) -> List[TraceStep]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def _bind(self, orig: Callable[..., Any], args: tuple, kwargs: dict) -> Dict[str, Any]:
        func = getattr(orig, "__func__", orig)
        if func not in self._sig_cache:
            try:
                self._sig_cache[func] = inspect.signature(orig)
            except (TypeError, ValueError):
                self._sig_cache[func] = None
        sig = self._sig_cache[func]
        if sig is not None:
            try:
                bound = sig.bind_partial(*args, **kwargs)
                out: Dict[str, Any] = {}
                for k, v in bound.arguments.items():
                    p = sig.parameters[k]
                    if p.kind is inspect.Parameter.VAR_KEYWORD:
                        out.update(v)
                    else:
                        out[k] = v
                return out
            except TypeError:
                pass
        out = {f"arg{i}": v for i, v in enumerate(args)}
        out.update(kwargs)
        return out

    def _secret_text(self, owner: Any, kind: str, name: str, params: Dict[str, Any]) -> bool:
        """Whether the text argument of this call is a secret (typed into a password field)."""
        if name == "clipboard_set":
            return True
        from ._nodeinfo import is_password

        try:
            if kind == "element":
                return bool(is_password(owner.node))
            if kind == "desktop" and name == "type":
                backend = getattr(owner, "backend", None)
                node = backend.focused_element() if backend is not None else None
                return bool(is_password(node))
        except Exception:
            return True  # cannot tell: err on the side of not recording it
        return False

    def _secret_result(self, owner: Any, kind: str, name: str, params: Dict[str, Any], result: Any) -> bool:
        """Whether the text this call returned is a secret: what was on the clipboard,
        the value of a password field, or whatever a ``wait_until`` predicate returned
        (arbitrary user code, typically reading a field's value)."""
        if not isinstance(result, str) or not result:
            return False
        if name in _SECRET_RESULTS:
            return True
        from ._nodeinfo import is_password

        try:
            if kind == "element" and is_password(owner.node):
                return True
            return any(_is_element(v) and is_password(v.node) for v in params.values())
        except Exception:
            return True  # cannot tell: err on the side of not recording it

    def _scrub(self, text: Optional[str]) -> Optional[str]:
        if not text or not self._secrets:
            return text
        for s in sorted(self._secrets, key=len, reverse=True):
            if len(s) >= _MIN_SCRUB and s in text:
                text = text.replace(s, MASK)
                for form in (repr(s), json.dumps(s)):
                    text = text.replace(form, repr(MASK))
        return text

    def _call(self, owner: Any, kind: str, name: str, orig: Callable[..., Any], args: tuple, kwargs: dict) -> Any:
        stack = self._stack()
        if stack:  # an internal call of the step in progress: it only fills in the target
            result = orig(*args, **kwargs)
            if name in _LOOKUPS:
                self._note_target(stack[0], result)
            return result

        params = self._bind(orig, args, kwargs)
        text_param = _TEXT_PARAMS.get((kind, name))
        if text_param and isinstance(params.get(text_param), str) and self._secret_text(owner, kind, name, params):
            secret = params[text_param]
            if secret:
                with self._lock:
                    self._secrets.add(secret)
            params = dict(params)
            params[text_param] = _Masked(len(secret))
        method = name if kind == "desktop" else f"{kind}.{name}"
        step = TraceStep(index=0, method=method, started_at=time.time())
        step.args = {k: self._scrub(_fmt(v)) or "" for k, v in params.items()}
        if kind != "desktop":
            self._note_target(step, owner)
        for p in _TARGET_PARAMS:
            v = params.get(p)
            if p == "text" and name not in ("find_text", "click_text"):
                continue
            if isinstance(v, str) and not isinstance(v, _Masked):
                step.target["selector"] = self._scrub(v)
                break
        for v in params.values():
            if _is_element(v) or _is_window(v):
                self._note_target(step, v)
                break
        with self._lock:
            step.index = len(self.steps)
            self.steps.append(step)
        if self.before and self.screenshots is True:
            step.screenshot_before = self._capture(step)
        stack.append(step)
        t0 = time.perf_counter()
        try:
            result = orig(*args, **kwargs)
        except BaseException as exc:
            step.duration = time.perf_counter() - t0
            step.status = "failed"
            tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__)[-12:])
            step.error = {"type": type(exc).__name__, "message": self._scrub(_short(str(exc), 2000)) or "",
                          "traceback": self._scrub(_short(tb, 8000)) or ""}
            raise
        else:
            step.duration = time.perf_counter() - t0
            if result is not owner:
                self._note_target(step, result)
            if self._secret_result(owner, kind, name, params, result):
                if len(result) >= _MIN_SCRUB:
                    with self._lock:
                        self._secrets.add(result)
                step.result = MASK
            else:
                step.result = self._scrub(_fmt(result))
            self._instrument_result(result)
            return result
        finally:
            stack.pop()
            if self.screenshots is True or (self.screenshots == "errors" and step.failed):
                step.screenshot = self._capture(step)

    def _note_target(self, step: TraceStep, value: Any) -> None:
        """Record what ``value`` (an element, window, match, point...) designates as the step's target."""
        from .types import Match, Point, Rect

        try:
            if isinstance(value, Point):
                step.target["point"] = [int(value.x), int(value.y)]
            elif isinstance(value, (list, tuple)):
                if value:
                    self._note_target(step, value[0])
            elif _is_element(value):
                step.target["element"] = _describe_node(value.node)
                b = None
                try:
                    b = value.bounds
                except Exception:
                    pass
                if b is not None:
                    step.target["bounds"] = _rect_list(b)
                if value.selector is not None and "selector" not in step.target:
                    step.target["selector"] = self._scrub(str(value.selector))
            elif _is_window(value):
                step.target["window"] = _describe_window(value)
                step.target["bounds"] = _rect_list(value.info.rect)
            elif isinstance(value, Match):
                step.target["bounds"] = _rect_list(value.rect)
            elif isinstance(value, Rect):
                step.target["bounds"] = _rect_list(value)
        except Exception:  # describing a target must never break the call
            pass

    # ---------------------------------------------------------- screenshots
    def capture_final(self) -> Optional[str]:
        """Take one screenshot now, outside any step, as :attr:`final_screenshot`.

        The pytest plugin calls it when a test fails, so a trace recorded with
        ``screenshots="errors"`` also shows the screen at the moment of a failure
        that no step raised (an ``assert`` in the test). Works also after
        :meth:`stop`. Returns the data URL, or ``None`` when no screenshot could be taken.
        """
        holder = TraceStep(index=-1, method="final")
        self.final_screenshot = self._capture(holder)
        self.final_screen = holder.screen
        self.final_screenshot_error = holder.screenshot_error
        return self.final_screenshot

    def _capture(self, step: TraceStep) -> Optional[str]:
        screen = getattr(self.desktop, "screen", None)
        if screen is None:
            return None
        try:
            from PIL import Image

            vr = screen.virtual_rect()
            img = screen.capture(None)
            step.screen = _rect_list(vr)
            if img.mode != "RGB":
                img = img.convert("RGB")
            w, h = img.size
            if w > self.max_width:
                img = img.resize((self.max_width, max(1, round(h * self.max_width / w))), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.quality)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:
            step.screenshot_error = f"{type(exc).__name__}: {_short(str(exc), 200)}"
            return None

    # -------------------------------------------------------------- logging
    def _on_log(self, record: logging.LogRecord) -> None:
        if not self._active:
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        entry = {
            "time": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": self._scrub(_short(message, 2000)),
        }
        stack = getattr(self._local, "stack", None)
        if stack:
            stack[0].annotations.append(entry)
        else:
            with self._lock:
                entry["after_step"] = len(self.steps) - 1
                self.logs.append(entry)

    # -------------------------------------------------------- serialization
    def to_dict(self, *, screenshots: bool = True) -> Dict[str, Any]:
        """The trace as JSON-able data (``screenshots=False`` drops the image data URLs)."""
        steps = list(self.steps)
        end = self.finished_at or time.time()
        final: Dict[str, Any] = {"final_screen": self.final_screen, "final_screenshot_error": self.final_screenshot_error}
        if screenshots:
            final["final_screenshot"] = self.final_screenshot
        return {
            "title": self.title,
            "started_at": self.started_at,
            "started": _iso(self.started_at),
            "finished_at": self.finished_at,
            "duration": round(max(0.0, end - (self.started_at or end)), 6),
            "backend": _backend_name(self.desktop),
            "ok": not any(s.failed for s in steps),
            "counts": {"total": len(steps), "passed": sum(1 for s in steps if not s.failed),
                       "failed": sum(1 for s in steps if s.failed)},
            "steps": [s.to_dict(screenshots=screenshots) for s in steps],
            "logs": [dict(r) for r in self.logs],
            **final,
        }

    def to_json(self, *, indent: Optional[int] = 2, screenshots: bool = True) -> str:
        return json.dumps(self.to_dict(screenshots=screenshots), indent=indent, ensure_ascii=False, default=str)

    def save_json(self, path: PathLike, *, screenshots: bool = True) -> str:
        """Write :meth:`to_dict` as JSON (parent directories are created). Returns the path."""
        return _write(path, self.to_json(screenshots=screenshots))

    def to_html(self) -> str:
        """The trace as one self-contained HTML page (see the module docstring)."""
        return _render_html(self)

    def save_html(self, path: PathLike) -> str:
        """Write :meth:`to_html` to ``path`` (parent directories are created). Returns the path."""
        return _write(path, self.to_html())

    def save(self, path: PathLike) -> str:
        """:meth:`save_json` for a ``.json`` path, else :meth:`save_html`. Returns the path."""
        if str(path).lower().endswith(".json"):
            return self.save_json(path)
        return self.save_html(path)

    def __repr__(self) -> str:
        state = "active" if self._active else "stopped"
        failed = sum(1 for s in self.steps if s.failed)
        return f"<Tracer {state} steps={len(self.steps)} failed={failed}>"


class _Masked(str):
    """A masked secret argument (formats as ``***``)."""

    def __new__(cls, length: int) -> "_Masked":
        return super().__new__(cls, MASK)

    def __repr__(self) -> str:
        return MASK


@contextlib.contextmanager
def trace(desktop: Any, path: PathLike, **kw: Any) -> Iterator[Tracer]:
    """Trace ``desktop`` for the ``with`` block and save the trace to ``path`` on exit.

    The trace is saved also when the block raises (that is when it is most
    useful); ``.json`` paths get JSON, anything else HTML. Keyword arguments go
    to :class:`Tracer`::

        with trace(d, "traces/login.html", screenshots="errors"):
            d.click('button "Sign in"')
    """
    tracer = Tracer(desktop, **kw)
    tracer.start()
    failed = False
    try:
        yield tracer
    except BaseException:
        failed = True
        raise
    finally:
        tracer.stop()
        try:
            tracer.save(path)
        except Exception:
            if not failed:
                raise
            # Never hide the error that ended the block behind a save error.
            logging.getLogger("locum.trace").warning("could not save the trace to %s", path, exc_info=True)


def _backend_name(desktop: Any) -> str:
    backend = getattr(desktop, "backend", None)
    return str(getattr(backend, "name", None) or type(backend).__name__)


def _write(path: PathLike, text: str) -> str:
    p = pathlib.Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, p)
    return str(p)


# ----------------------------------------------------------------------- HTML

_CSS = """
:root{--bg:#f6f7f9;--fg:#1d2330;--muted:#667085;--card:#fff;--line:#e4e7ec;--sel:#eef4ff;
--ok:#12805c;--okbg:#e3f6ee;--bad:#c4320a;--badbg:#fdece6;--warn:#b54708;--warnbg:#fef3e2;--hl:#ff2d6f}
@media (prefers-color-scheme:dark){:root{--bg:#11141a;--fg:#e6e9ef;--muted:#98a2b3;--card:#1a1f29;--line:#2b3240;
--sel:#1d2a44;--okbg:#0f2e23;--badbg:#3a1a10;--warnbg:#3a2a12}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{padding:16px 16px 8px;max-width:1500px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 8px}h3{font-size:13px;margin:14px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.meta{color:var(--muted);font-size:13px}.meta code{color:var(--fg)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px;letter-spacing:.02em;text-transform:uppercase}
.passed{color:var(--ok);background:var(--okbg)}.failed{color:var(--bad);background:var(--badbg)}.note{color:var(--warn);background:var(--warnbg)}
.wrap{display:grid;grid-template-columns:minmax(260px,380px) 1fr;gap:16px;padding:8px 16px 40px;max-width:1500px;margin:0 auto}
@media (max-width:800px){.wrap{grid-template-columns:1fr}}
ol.steps{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:auto;max-height:calc(100vh - 120px);position:sticky;top:8px}
ol.steps li{border-bottom:1px solid var(--line)}ol.steps li:last-child{border-bottom:0}
ol.steps a{display:block;padding:7px 10px;color:inherit;text-decoration:none;border-left:4px solid var(--ok)}
ol.steps li.failed a{border-left-color:var(--bad)}ol.steps a:hover{background:var(--bg)}ol.steps li.sel a{background:var(--sel)}
.idx{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:6px}.dur{float:right;color:var(--muted);font-size:12px;margin-left:8px}
.sub{color:var(--muted);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{display:inline-block;font-size:11px;padding:0 6px;border-radius:999px;margin-left:4px}
section.detail{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px}
table.kv{border-collapse:collapse;width:100%}table.kv th,table.kv td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
table.kv th{width:150px;color:var(--muted);font-weight:600;font-size:12px}table.kv tr:last-child>*{border-bottom:0}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;word-break:break-word}
.err{color:var(--bad);white-space:pre-wrap;word-break:break-word}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;font-size:12px;margin:6px 0 0}
details summary{cursor:pointer;color:var(--muted);font-size:12px}
.shot{position:relative;display:inline-block;max-width:100%;margin-top:6px;border:1px solid var(--line);border-radius:6px;overflow:hidden;line-height:0}
.shot img{display:block;max-width:100%;height:auto}
.box{position:absolute;border:2px solid var(--hl);box-shadow:0 0 0 2000px rgba(0,0,0,.18);border-radius:2px;pointer-events:none}
.dot{position:absolute;width:14px;height:14px;margin:-7px 0 0 -7px;border:2px solid var(--hl);border-radius:50%;background:rgba(255,45,111,.35);pointer-events:none}
ul.ann{margin:0;padding-left:18px}.lvl-ERROR,.lvl-CRITICAL{color:var(--bad)}.lvl-WARNING{color:var(--warn)}.lvl-DEBUG{color:var(--muted)}
footer{color:var(--muted);font-size:12px;padding:0 16px 24px;max-width:1500px;margin:0 auto}
"""

_JS = """
(function(){
  var items=[].slice.call(document.querySelectorAll('ol.steps li'));
  var details=[].slice.call(document.querySelectorAll('section.detail[id^="step-"]'));
  if(!items.length)return;
  function show(i){
    i=Math.max(0,Math.min(items.length-1,i));
    details.forEach(function(d){d.hidden=d.getAttribute('data-i')!==String(i);});
    items.forEach(function(li,j){li.className=li.className.replace(/ ?sel/g,'')+(j===i?' sel':'');});
    items[i].scrollIntoView({block:'nearest'});cur=i;
    if(history.replaceState)history.replaceState(null,'','#step-'+i);
  }
  var cur=0;
  items.forEach(function(li,j){li.querySelector('a').addEventListener('click',function(e){e.preventDefault();show(j);});});
  document.addEventListener('keydown',function(e){
    if(e.key==='ArrowDown'||e.key==='j'){e.preventDefault();show(cur+1);}
    else if(e.key==='ArrowUp'||e.key==='k'){e.preventDefault();show(cur-1);}
  });
  var m=/^#step-(\\d+)$/.exec(location.hash||'');
  var first=items.findIndex?items.findIndex(function(li){return /failed/.test(li.className);}):-1;
  show(m?parseInt(m[1],10):(first>=0?first:0));
})();
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 120:
        return f"{seconds:.2f} s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s:02d}s"


def _pct(v: float, total: float) -> str:
    return f"{100.0 * v / total:.3f}%" if total else "0%"


def _shot_html(step: TraceStep, data_url: str, label: str) -> str:
    overlay = ""
    scr = step.screen
    if scr and scr[2] > 0 and scr[3] > 0:
        b = step.target.get("bounds")
        if b:
            overlay += (f'<div class="box" style="left:{_pct(b[0] - scr[0], scr[2])};top:{_pct(b[1] - scr[1], scr[3])};'
                        f'width:{_pct(b[2], scr[2])};height:{_pct(b[3], scr[3])}"></div>')
        p = step.target.get("point")
        if p:
            overlay += f'<div class="dot" style="left:{_pct(p[0] - scr[0], scr[2])};top:{_pct(p[1] - scr[1], scr[3])}"></div>'
    return (f'<h3>{_esc(label)}</h3><div class="shot"><img alt="{_esc(label + " of step " + str(step.index))}" '
            f'src="{_esc(data_url)}">{overlay}</div>')


def _render_step(step: TraceStep) -> str:
    rows = [
        f'<tr><th>Status</th><td><span class="badge {_esc(step.status)}">{_esc(step.status)}</span></td></tr>',
        f"<tr><th>Started</th><td>{_esc(_iso(step.started_at))}</td></tr>",
        f"<tr><th>Duration</th><td>{_esc(_fmt_duration(step.duration))}</td></tr>",
    ]
    t = step.target
    for key, label in (("selector", "Selector"), ("element", "Element"), ("window", "Window")):
        if t.get(key):
            rows.append(f"<tr><th>{label}</th><td><code>{_esc(t[key])}</code></td></tr>")
    if t.get("bounds"):
        b = t["bounds"]
        rows.append(f"<tr><th>Bounds</th><td><code>({_esc(b[0])},{_esc(b[1])} {_esc(b[2])}x{_esc(b[3])})</code></td></tr>")
    if t.get("point"):
        rows.append(f"<tr><th>Point</th><td><code>({_esc(t['point'][0])}, {_esc(t['point'][1])})</code></td></tr>")
    for k, v in step.args.items():
        rows.append(f"<tr><th>{_esc(k)}</th><td><code>{_esc(v)}</code></td></tr>")
    if step.result is not None and not step.failed:
        rows.append(f"<tr><th>Result</th><td><code>{_esc(step.result)}</code></td></tr>")
    parts = [f'<section class="detail" id="step-{step.index}" data-i="{step.index}">',
             f'<h2><span class="idx">#{step.index}</span><code>{_esc(step.method)}</code></h2>',
             f'<table class="kv"><tbody>{"".join(rows)}</tbody></table>']
    if step.error:
        parts.append(f'<h3>Error</h3><div class="err">{_esc(step.error.get("type"))}: {_esc(step.error.get("message"))}</div>')
        if step.error.get("traceback"):
            parts.append(f"<details><summary>Traceback</summary><pre>{_esc(step.error['traceback'])}</pre></details>")
    if step.annotations:
        lis = "".join(
            f'<li class="lvl-{_esc(a.get("level"))}"><span class="mono">{_esc(a.get("logger"))}</span> '
            f'{_esc(a.get("level"))}: {_esc(a.get("message"))}</li>'
            for a in step.annotations
        )
        parts.append(f'<h3>Annotations</h3><ul class="ann">{lis}</ul>')
    if step.screenshot:
        parts.append(_shot_html(step, step.screenshot, "Screen after"))
    if step.screenshot_before:
        parts.append(_shot_html(step, step.screenshot_before, "Screen before"))
    if step.screenshot_error:
        parts.append(f'<div class="meta">screenshot failed: {_esc(step.screenshot_error)}</div>')
    parts.append("</section>")
    return "".join(parts)


def _render_html(tracer: Tracer) -> str:
    data = tracer.to_dict(screenshots=False)
    steps = list(tracer.steps)
    ok = data["ok"]
    items = []
    for s in steps:
        chips = ""
        heals = [a for a in s.annotations if str(a.get("logger", "")).startswith("locum.heal")]
        if heals:
            chips += '<span class="chip note">healed</span>'
        elif s.annotations:
            chips += f'<span class="chip note">{len(s.annotations)} note(s)</span>'
        sub = s.target.get("element") or s.target.get("window") or s.result or ""
        if s.error:
            sub = f"{s.error.get('type')}: {s.error.get('message')}"
        items.append(
            f'<li class="{_esc(s.status)}"><a href="#step-{s.index}">'
            f'<span class="dur">{_esc(_fmt_duration(s.duration))}</span>'
            f'<span class="idx">#{s.index}</span><strong>{_esc(_short(s.title, 120))}</strong>{chips}'
            f'<div class="sub">{_esc(_short(str(sub), 160))}</div></a></li>'
        )
    loose = ""
    if tracer.logs:
        lis = "".join(
            f'<li class="lvl-{_esc(r.get("level"))}">after #{_esc(r.get("after_step"))} '
            f'<span class="mono">{_esc(r.get("logger"))}</span> {_esc(r.get("level"))}: {_esc(r.get("message"))}</li>'
            for r in tracer.logs
        )
        loose = f'<section class="detail" data-i="log"><h2>Log outside steps</h2><ul class="ann">{lis}</ul></section>'
    if tracer.final_screenshot or tracer.final_screenshot_error:
        holder = TraceStep(index=len(steps), method="final", screen=tracer.final_screen)
        body = (_shot_html(holder, tracer.final_screenshot, "Screen at failure") if tracer.final_screenshot
                else f'<div class="meta">screenshot failed: {_esc(tracer.final_screenshot_error)}</div>')
        loose = f'<section class="detail" data-i="final"><h2>Screen at failure</h2>{body}</section>' + loose
    c = data["counts"]
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(tracer.title)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f'<header><h1>{_esc(tracer.title)} <span class="badge {"passed" if ok else "failed"}">'
        f'{"passed" if ok else "failed"}</span></h1>',
        f'<div class="meta">backend <code>{_esc(data["backend"])}</code> &middot; started {_esc(data["started"] or "-")}'
        f' &middot; {c["total"]} steps, {c["failed"]} failed &middot; {_esc(_fmt_duration(data["duration"]))}'
        " &middot; use &uarr;/&darr; to move between steps</div></header>",
        '<div class="wrap">',
        f'<nav><ol class="steps">{"".join(items)}</ol></nav>' if items else '<nav><p class="meta">No steps were recorded.</p></nav>',
        "<main>" + "".join(_render_step(s) for s in steps) + loose + "</main>",
        "</div>",
        '<footer>Generated by locum.trace</footer>',
        f"<script>{_JS}</script>",
        "</body></html>",
    ]
    return "\n".join(parts)
