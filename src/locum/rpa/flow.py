"""Declarative YAML/JSON workflows.

A flow is a document with a list of steps; each step is a mapping with exactly
one *action* key plus optional common keys::

    name: Enter invoice
    vars:
      customer: ACME
      amount: "12.50"
    settings:            # defaults for every step (all optional)
      timeout: 5         # seconds to wait for elements / images
      retries: 0         # retries of a failing *action* step (not of whole blocks)
      retry_delay: 0.5
      screenshot_on_error: true   # with a run/report dir (default)
    steps:
      - activate_window: "ERP*"
      - set_text: {target: 'edit "Customer"', text: "${customer}"}
      - click: 'button "Save"'
      - if_exists:
          target: 'dialog "Warning"'
          then:
            - click: 'dialog "Warning" button "OK"'
      - for_each:
          csv: lines.csv           # or items: [...] / items: "${list_var}"; strict: false
          as: line
          steps:
            - type: {text: "${line.sku}\\t${line.qty}\\n", into: 'edit "Lines"'}
      - try:
          steps: [{click: 'button "Post"'}]
          catch: [{log: "post failed: ${error.message}"}]

Step types (shorthand scalar form -> the key in brackets):

* mouse: ``click``/``double_click``/``right_click``/``middle_click`` [target]
  (selector, ``"x,y"``, ``[x, y]``, image path; or ``selector:``/``image:``/``x:``+``y:``
  with ``button``, ``clicks``, ``offset``, ``timeout``), ``move`` [target],
  ``drag`` {start, end, duration, button}, ``scroll`` [dy] {dy, dx, at}
* keyboard: ``type`` [text] {text, into, interval, mode}, ``press`` [keys]
  {keys, presses, interval}, ``hotkey`` [keys] (``"ctrl+s"`` or a list)
* elements: ``set_text`` {target, text, native, clear} (an ``"x,y"`` target clicks
  there, selects all and types), ``invoke``/``toggle``/
  ``select``/``expand``/``collapse``/``check``/``uncheck`` [target] (native
  accessibility action with click/keyboard fallback), ``wait`` [target]
  {target, state, timeout}, ``wait_gone`` [target], ``assert_exists`` [target]
  {target, timeout, equals, message}, ``read_value`` {target, var, attribute}
* vision: ``wait_image``/``click_image`` [image] {image, timeout, confidence,
  region, button, clicks, var}, ``click_text`` [text], ``screenshot`` [path] {path, region}
* system: ``launch`` [command] {command, wait_window, timeout},
  ``activate_window`` [title] {title, app, timeout}, ``sleep`` [seconds],
  ``clipboard_set`` [text], ``clipboard_get`` [var]
* data/logging: ``set_var`` {name: value, ...}, ``log`` [message] {message, level}, ``fail`` [message]
* control: ``if_exists`` {target|image, timeout, then, else}, ``for_each``
  {items|csv|json, as, steps, on_error}, ``try`` {steps, catch, finally},
  ``repeat`` {times, steps, as}, ``call`` [flow] {flow, vars, outputs}
  (a name under the document's ``flows:`` mapping or a YAML/JSON file).

A ``target`` that ends in an image extension (``warn.png``) or is a
``data:image/`` URL is a template image in every step (``click``, ``move``,
``wait``, ``wait_gone``, ``assert_exists``, ``if_exists``), relative to the
flow's directory; a missing file is an error, never a selector.

Common step keys: ``name``, ``retries``, ``retry_delay``, ``timeout``,
``continue_on_error``, ``description``. ``settings.retries``/``retry_delay``
apply to action steps only: a failing step inside ``for_each``/``try``/``call``...
is retried on its own, the block is never re-run (iterations that already
succeeded -- a payment -- are not repeated). An explicit ``retries:`` on a
control step re-runs the whole block. ``continue_on_error`` on a control step
also marks the failed steps inside it as handled.

Scalars are read as written: YAML values that look like numbers keep their
source text when typed (``00123`` stays ``00123``, ``1250.50`` stays
``1250.50``, ``12:30`` stays ``12:30``) and only ``true``/``false`` are booleans
(``NO``, ``yes``, ``off`` are strings). A CSV row with more or fewer fields than
the header is an error naming the line (``strict: false`` pads/ignores instead).

Interpolation: ``${var}``, nested paths ``${item.address.city}`` /
``${rows.0.name}``, environment ``${env.HOME}`` (a flow variable named ``env``
takes precedence; a bare ``${env}`` is never the whole environment), loop state ``${loop.index}``
(also ``number``, ``count``, ``first``, ``last``), the caught error in a
``catch`` block ``${error.message}`` / ``${error.type}`` / ``${error.step}``,
defaults ``${name:-fallback}`` and ``$${`` for a literal ``${``. A string
that is exactly one ``${...}`` keeps the value's type (lists stay lists).

Errors carry the step path, e.g. ``steps[3].for_each.steps[1]``; invalid
documents are rejected *before* anything runs, with every problem listed.

Safety policy (opt-in): ``run_flow(doc, desktop, policy=Policy.strict(allow=[...]),
confirm=..., audit=...)`` runs the flow on a
:class:`~locum.llm.safety.GuardedDesktop`, so every step that acts (clicks,
typing, ``set_text``, ``invoke``/``toggle``/..., keys, ``launch``,
``clipboard_set``, ``activate_window``) is checked against the policy first;
a refused or declined action fails its step with
:class:`~locum.errors.PolicyViolation` and is never retried (neither the
step nor an enclosing ``for_each``/``repeat``/``try``/subflow step with
``retries``). **Without a
policy, flows are not gated** (the default, as before). A flow document cannot
declare its own allow rules: authorization comes from the caller.
"""

from __future__ import annotations

import copy
import csv
import difflib
import json
import logging
import os
import pathlib
import re
import time
import traceback as _tb
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ..errors import FailSafeTriggered, PolicyViolation, WorkflowError
from ..types import Point, Rect
from .report import FAILED, PASSED, RunReport, StepResult

if TYPE_CHECKING:  # pragma: no cover
    from ..desktop import Desktop

__all__ = [
    "run_flow",
    "load_flow",
    "validate_flow",
    "interpolate",
    "FlowRunner",
    "FlowStepError",
    "FlowAssertionError",
    "STEP_TYPES",
    "CONTROL_STEPS",
]

PathLike = Union[str, "os.PathLike[str]"]

COMMON_KEYS = frozenset({"name", "retries", "retry_delay", "timeout", "continue_on_error", "description"})
CONTROL_STEPS = frozenset({"if_exists", "for_each", "try", "repeat", "call"})
_NESTED_KEYS = {"if_exists": ("then", "else"), "for_each": ("steps",), "try": ("steps", "catch", "finally"), "repeat": ("steps",), "call": ()}

_TARGET_KEYS = {"target", "selector", "image", "x", "y", "timeout", "confidence"}
# Element methods exposed as one-target steps.
_ELEMENT_ACTIONS = ("invoke", "toggle", "select", "expand", "collapse", "check", "uncheck")
# step type -> (shorthand key for a scalar value or None, allowed argument keys)
_SPEC: Dict[str, Tuple[Optional[str], frozenset]] = {
    "click": ("target", frozenset(_TARGET_KEYS | {"button", "clicks", "offset", "interval"})),
    "double_click": ("target", frozenset(_TARGET_KEYS | {"button", "offset"})),
    "right_click": ("target", frozenset(_TARGET_KEYS | {"offset"})),
    "middle_click": ("target", frozenset(_TARGET_KEYS | {"offset"})),
    "move": ("target", frozenset(_TARGET_KEYS | {"offset", "duration"})),
    "drag": (None, frozenset({"start", "end", "duration", "button", "timeout"})),
    "scroll": ("dy", frozenset({"dy", "dx", "at", "timeout"})),
    "type": ("text", frozenset({"text", "into", "target", "interval", "mode", "timeout"})),
    "press": ("keys", frozenset({"keys", "presses", "interval"})),
    "hotkey": ("keys", frozenset({"keys"})),
    "set_text": (None, frozenset({"target", "text", "native", "clear", "timeout"})),
    **{k: ("target", frozenset({"target", "timeout"})) for k in _ELEMENT_ACTIONS},
    "wait": ("target", frozenset({"target", "state", "timeout"})),
    "wait_gone": ("target", frozenset({"target", "timeout"})),
    "wait_image": ("image", frozenset({"image", "timeout", "confidence", "region", "grayscale", "var"})),
    "click_image": ("image", frozenset({"image", "timeout", "confidence", "region", "grayscale", "button", "clicks", "offset", "var"})),
    "click_text": ("text", frozenset({"text", "timeout", "region", "button", "clicks", "offset", "regex", "case_sensitive", "fuzzy", "var"})),
    "screenshot": ("path", frozenset({"path", "region"})),
    "launch": ("command", frozenset({"command", "wait_window", "timeout"})),
    "activate_window": ("title", frozenset({"title", "app", "timeout"})),
    "sleep": ("seconds", frozenset({"seconds"})),
    "assert_exists": ("target", frozenset({"target", "image", "timeout", "equals", "message", "confidence"})),
    "set_var": (None, frozenset()),  # free-form mapping
    "read_value": (None, frozenset({"target", "var", "attribute", "timeout"})),
    "clipboard_set": ("text", frozenset({"text"})),
    "clipboard_get": ("var", frozenset({"var"})),
    "log": ("message", frozenset({"message", "level"})),
    "fail": ("message", frozenset({"message"})),
    "if_exists": ("target", frozenset({"target", "selector", "image", "timeout", "confidence", "then", "else"})),
    "for_each": (None, frozenset({"items", "csv", "json", "as", "steps", "on_error", "encoding", "delimiter", "strict"})),
    "try": (None, frozenset({"steps", "catch", "finally"})),
    "repeat": (None, frozenset({"times", "steps", "as"})),
    "call": ("flow", frozenset({"flow", "vars", "outputs"})),
}
#: Every supported step type.
STEP_TYPES = frozenset(_SPEC)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")
_XY_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_VAR_RE = re.compile(r"\$\$\{|\$\{([^{}]*)\}")
_LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "warn": logging.WARNING,
               "error": logging.ERROR, "critical": logging.CRITICAL}
_MAX_CALL_DEPTH = 16


class FlowStepError(WorkflowError):
    """A flow step failed. ``path`` locates it (``steps[3].for_each.steps[1]``); ``original`` is the cause."""

    def __init__(self, message: str, *, path: str = "", step_type: str = "", original: Optional[BaseException] = None):
        super().__init__(message)
        self.path = path
        self.step_type = step_type
        self.original = original


def _is_refusal(exc: Optional[BaseException]) -> bool:
    """Whether ``exc`` is (or wraps, through nested :class:`FlowStepError` s) a :class:`PolicyViolation`.

    A refusal inside a container step (``for_each``, ``repeat``, ``try``, a group,
    a called subflow) reaches the container's retry loop wrapped in a
    :class:`FlowStepError`; it must not be retried either (the human would be
    asked again on every attempt).
    """
    seen = 0
    while exc is not None and seen < 64:
        if isinstance(exc, PolicyViolation):
            return True
        exc = exc.original if isinstance(exc, FlowStepError) else None
        seen += 1
    return False


class FlowAssertionError(WorkflowError, AssertionError):
    """An ``assert_exists`` step (or ``fail``) did not hold."""


class _Undefined(LookupError):
    pass


# ============================================================ interpolation

def _lookup(variables: Mapping[str, Any], expr: str) -> Any:
    parts = [p.strip() for p in expr.split(".")]
    if not parts or not parts[0]:
        raise _Undefined(f"empty variable reference ${{{expr}}}")
    head, rest = parts[0], parts[1:]
    if head == "env" and head not in variables:
        # A user variable named ``env`` (PROD/QA...) wins; a bare ``${env}`` never
        # expands to the whole process environment (it holds credentials).
        if not rest:
            raise _Undefined("${env} needs a variable name: ${env.NAME}")
        name = ".".join(rest)
        if name not in os.environ:
            raise _Undefined(f"environment variable {name!r} is not set")
        return os.environ[name]
    if head not in variables:
        known = ", ".join(sorted(k for k in variables if not k.startswith("_"))) or "none"
        hint = difflib.get_close_matches(head, list(variables), n=1)
        raise _Undefined(f"undefined variable {head!r}" + (f" (did you mean {hint[0]!r}?)" if hint else "") + f"; defined: {known}")
    value: Any = variables[head]
    walked = head
    for part in rest:
        if isinstance(value, Mapping):
            if part in value:
                value = value[part]
            else:
                keys = ", ".join(map(str, list(value)[:20])) or "none"
                raise _Undefined(f"{walked!r} has no key {part!r} (keys: {keys})")
        elif isinstance(value, (list, tuple)):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                raise _Undefined(f"{walked!r} has no index {part!r} (length {len(value)})") from None
        elif hasattr(value, "__getitem__") and not isinstance(value, (str, bytes)):
            try:
                value = value[part]
            except Exception:
                if hasattr(value, part):
                    value = getattr(value, part)
                else:
                    raise _Undefined(f"{walked!r} has no field {part!r}") from None
        elif not part.startswith("_") and hasattr(value, part):
            value = getattr(value, part)
        else:
            raise _Undefined(f"{walked!r} ({type(value).__name__}) has no field {part!r}")
        walked += "." + part
    return value


def _resolve_expr(expr: str, variables: Mapping[str, Any]) -> Any:
    if ":-" in expr:
        name, default = expr.split(":-", 1)
        try:
            return _lookup(variables, name.strip())
        except _Undefined:
            return default
    return _lookup(variables, expr.strip())


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def interpolate(value: Any, variables: Mapping[str, Any], *, path: str = "") -> Any:
    """Recursively substitute ``${...}`` references in strings inside ``value``.

    Raises :class:`WorkflowError` (mentioning ``path``) for undefined references.
    """
    if isinstance(value, str):
        if "$" not in value:
            return value
        whole = _VAR_RE.fullmatch(value)
        if whole and whole.group(0) != "$${":
            try:
                return _resolve_expr(whole.group(1), variables)
            except _Undefined as exc:
                raise WorkflowError(f"{path or 'flow'}: cannot resolve ${{{whole.group(1)}}}: {exc}") from None

        def sub(m: "re.Match[str]") -> str:
            if m.group(0) == "$${":
                return "${"
            try:
                return _to_text(_resolve_expr(m.group(1), variables))
            except _Undefined as exc:
                raise WorkflowError(f"{path or 'flow'}: cannot resolve ${{{m.group(1)}}}: {exc}") from None

        return _VAR_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: interpolate(v, variables, path=path) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, variables, path=path) for v in value]
    if isinstance(value, tuple):
        return tuple(interpolate(v, variables, path=path) for v in value)
    return value


# ============================================================ loading & validation

class _SourceFloat(float):
    """A float that remembers how it was written: ``str()`` gives ``1250.50``, not ``1250.5``.

    Flows type values into applications, so a document amount must reach the
    field exactly as written; arithmetic and comparisons still see the number.
    """

    def __new__(cls, value: float, text: str) -> "_SourceFloat":
        obj = super().__new__(cls, value)
        obj._text = text
        return obj

    def __str__(self) -> str:
        return self._text

    def __format__(self, spec: str) -> str:
        return self._text if not spec else float.__format__(self, spec)

    def __reduce__(self) -> Any:
        return (_SourceFloat, (float(self), self._text))


def _number_from_text(value: Any, text: str) -> Any:
    """``value`` when its canonical spelling is ``text``, else something that types as ``text``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if isinstance(value, int):
        # 00123 (YAML 1.1 octal = 83), 0x1F, 1_000, 12:30 (sexagesimal = 750), +5:
        # identifiers and times, not numbers -> keep the text.
        return value if str(value) == text else text
    if value != value or value in (float("inf"), float("-inf")):
        return value
    return value if repr(value) == text else _SourceFloat(value, text)


def _json_loads(text: str) -> Any:
    return json.loads(text, parse_float=lambda t: _number_from_text(float(t), t))


_YAML_LOADER: Any = None


def _yaml_loader() -> Any:
    """A ``yaml.SafeLoader`` that keeps scalars as written (see :func:`_number_from_text`)
    and, like YAML 1.2, only treats ``true``/``false`` as booleans (``NO``, ``on``,
    ``yes`` stay strings: a country code or a CSV flag must not become ``false``)."""
    global _YAML_LOADER
    if _YAML_LOADER is not None:
        return _YAML_LOADER
    import yaml  # type: ignore

    class FlowLoader(yaml.SafeLoader):
        pass

    bool_tag = "tag:yaml.org,2002:bool"
    FlowLoader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag != bool_tag]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    FlowLoader.add_implicit_resolver(bool_tag, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))

    def construct_int(loader: Any, node: Any) -> Any:
        return _number_from_text(yaml.SafeLoader.construct_yaml_int(loader, node), str(node.value))

    def construct_float(loader: Any, node: Any) -> Any:
        return _number_from_text(yaml.SafeLoader.construct_yaml_float(loader, node), str(node.value))

    FlowLoader.add_constructor("tag:yaml.org,2002:int", construct_int)
    FlowLoader.add_constructor("tag:yaml.org,2002:float", construct_float)
    _YAML_LOADER = FlowLoader
    return FlowLoader


def _parse_text(text: str, source: str) -> Any:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return _json_loads(text)
        except json.JSONDecodeError:
            pass  # might still be YAML flow style
    try:
        import yaml  # type: ignore
    except ImportError:
        if source.lower().endswith((".yaml", ".yml")) or not stripped.startswith(("{", "[")):
            raise ImportError(f"Reading YAML flows ({source}) requires PyYAML: pip install locum[yaml]") from None
        raise
    try:
        return yaml.load(text, Loader=_yaml_loader())  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{source}: invalid YAML: {exc}") from exc


def load_flow(source: Union[PathLike, Mapping[str, Any], Sequence[Any]]) -> Tuple[Dict[str, Any], Optional[pathlib.Path]]:
    """Load a flow document. Returns ``(document, file_path_or_None)``.

    ``source`` may be a path to a ``.yaml``/``.yml``/``.json`` file, a mapping,
    a bare list of steps, or YAML/JSON text (a string with a newline, or starting with
    ``{``/``[`` that is not the name of an existing file such as ``[Q4] close.yaml``).
    """
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source)), None
    if isinstance(source, (list, tuple)):
        return {"steps": copy.deepcopy(list(source))}, None
    text_like = isinstance(source, str) and (
        "\n" in source
        or (source.lstrip().startswith(("{", "[")) and not _is_existing_file(source))
    )
    if text_like:
        doc = _parse_text(str(source), "<flow text>")
        file_path = None
    else:
        file_path = pathlib.Path(os.fspath(source)).expanduser()  # type: ignore[arg-type]
        if not file_path.is_file():
            raise FileNotFoundError(f"Flow file not found: {file_path}")
        raw = file_path.read_text(encoding="utf-8")
        if file_path.suffix.lower() == ".json":
            try:
                doc = _json_loads(raw)
            except json.JSONDecodeError as exc:
                raise WorkflowError(f"{file_path}: invalid JSON: {exc}") from exc
        else:
            doc = _parse_text(raw, str(file_path))
    if isinstance(doc, list):
        doc = {"steps": doc}
    if not isinstance(doc, dict):
        raise WorkflowError(f"{file_path or 'flow'}: a flow must be a mapping with a 'steps' list, got {type(doc).__name__}")
    return doc, file_path


def _is_existing_file(source: str) -> bool:
    try:
        return pathlib.Path(source).expanduser().is_file()
    except (OSError, ValueError):  # e.g. a name too long for the file system
        return False


def _step_type(step: Mapping[str, Any]) -> List[str]:
    return [k for k in step if k not in COMMON_KEYS]


def _suggest(word: str, options: Sequence[str]) -> str:
    close = difflib.get_close_matches(str(word), list(options), n=1, cutoff=0.6)
    return f" (did you mean {close[0]!r}?)" if close else ""


def validate_flow(doc: Mapping[str, Any], *, prefix: str = "") -> List[str]:
    """Check a flow document's structure. Returns a list of problems (empty = valid)."""
    problems: List[str] = []
    if not isinstance(doc, Mapping):
        return [f"{prefix or 'flow'}: must be a mapping, got {type(doc).__name__}"]
    known_top = {"name", "description", "vars", "settings", "steps", "flows", "version"}
    for k in doc:
        if k not in known_top:
            problems.append(f"{prefix or 'flow'}: unknown top-level key {k!r}{_suggest(k, sorted(known_top))}")
    if "steps" not in doc:
        problems.append(f"{prefix or 'flow'}: missing 'steps' list")
    elif not isinstance(doc["steps"], list):
        problems.append(f"{prefix or 'flow'}: 'steps' must be a list, got {type(doc['steps']).__name__}")
    else:
        _validate_steps(doc["steps"], (prefix + "." if prefix else "") + "steps", problems)
    if "vars" in doc and doc["vars"] is not None and not isinstance(doc["vars"], Mapping):
        problems.append(f"{prefix or 'flow'}: 'vars' must be a mapping")
    if "settings" in doc and doc["settings"] is not None:
        if not isinstance(doc["settings"], Mapping):
            problems.append(f"{prefix or 'flow'}: 'settings' must be a mapping")
        else:
            allowed = {"timeout", "retries", "retry_delay", "continue_on_error", "screenshot_on_error"}
            for k in doc["settings"]:
                if k not in allowed:
                    problems.append(f"{prefix or 'flow'}: unknown setting {k!r}{_suggest(k, sorted(allowed))}")
            shot = doc["settings"].get("screenshot_on_error")
            if shot is not None and not isinstance(shot, bool) and str(shot).strip().lower() not in _TRUE_WORDS + _FALSE_WORDS:
                problems.append(f"{prefix or 'flow'}: settings.screenshot_on_error must be true or false")
    flows = doc.get("flows")
    if flows is not None:
        if not isinstance(flows, Mapping):
            problems.append(f"{prefix or 'flow'}: 'flows' must be a mapping of name -> sub-flow")
        else:
            for name, sub in flows.items():
                sub_doc = {"steps": sub} if isinstance(sub, list) else sub
                problems.extend(validate_flow(sub_doc, prefix=f"flows.{name}"))
    return problems


def _validate_steps(steps: Any, path: str, problems: List[str]) -> None:
    if not isinstance(steps, list):
        problems.append(f"{path}: must be a list of steps, got {type(steps).__name__}")
        return
    for i, step in enumerate(steps):
        _validate_step(step, f"{path}[{i}]", problems)


def _validate_step(step: Any, path: str, problems: List[str]) -> None:
    if not isinstance(step, Mapping):
        problems.append(f"{path}: a step must be a mapping like {{click: 'button \"OK\"'}}, got {type(step).__name__} {step!r}")
        return
    kinds = _step_type(step)
    if not kinds:
        problems.append(f"{path}: no action key (only {', '.join(map(repr, step))}); expected one of the step types")
        return
    if len(kinds) > 1:
        problems.append(f"{path}: a step must have exactly one action, found {', '.join(map(repr, kinds))}")
        return
    kind = kinds[0]
    if kind not in _SPEC:
        problems.append(f"{path}: unknown step type {kind!r}{_suggest(kind, sorted(_SPEC))}")
        return
    for key in ("retries",):
        if key in step and not _is_int_like(step[key]):
            problems.append(f"{path}: {key!r} must be an integer")
    for key in ("retry_delay", "timeout"):
        if key in step and not _is_number_like(step[key]):
            problems.append(f"{path}: {key!r} must be a number")
    value = step[kind]
    shorthand, allowed = _SPEC[kind]
    where = f"{path}.{kind}"
    if kind == "set_var":
        if not isinstance(value, Mapping) or not value:
            problems.append(f"{where}: expects a mapping of variable names to values")
        return
    if isinstance(value, Mapping):
        for k in value:
            if k not in allowed:
                problems.append(f"{where}: unknown argument {k!r}{_suggest(k, sorted(allowed))}")
    elif shorthand is None:
        problems.append(f"{where}: expects a mapping with {', '.join(sorted(allowed))}")
        return
    elif kind in CONTROL_STEPS and kind != "call":
        problems.append(f"{where}: expects a mapping")
        return
    if kind in CONTROL_STEPS:
        _validate_control(kind, value, where, problems)
        return
    if isinstance(value, Mapping):
        required = {
            "set_text": ("target", "text"),
            "drag": ("end",),
            "read_value": ("target", "var"),
        }.get(kind, ())
        for r in required:
            if r not in value:
                problems.append(f"{where}: missing required argument {r!r}")
        if shorthand and shorthand not in value and kind not in (
            "click", "double_click", "right_click", "middle_click", "move", "scroll", "assert_exists", "activate_window"
        ):
            problems.append(f"{where}: missing required argument {shorthand!r}")
        if kind == "activate_window" and "title" not in value and "app" not in value:
            problems.append(f"{where}: needs 'title' and/or 'app'")
        if kind in ("click", "double_click", "right_click", "middle_click", "move", "assert_exists"):
            if not any(k in value for k in ("target", "selector", "image", "x")):
                problems.append(f"{where}: needs 'target', 'selector', 'image' or 'x'/'y'")
            if ("x" in value) != ("y" in value):
                problems.append(f"{where}: 'x' and 'y' must be given together")


def _validate_control(kind: str, value: Any, where: str, problems: List[str]) -> None:
    if kind == "call":
        if isinstance(value, Mapping):
            if "flow" not in value:
                problems.append(f"{where}: missing required argument 'flow'")
            if "vars" in value and not isinstance(value["vars"], Mapping):
                problems.append(f"{where}.vars: must be a mapping")
            if "outputs" in value and not isinstance(value["outputs"], (list, Mapping, str)):
                problems.append(f"{where}.outputs: must be a list of names or a mapping child -> parent")
        elif not isinstance(value, str):
            problems.append(f"{where}: expects a flow name/path or a mapping")
        return
    if kind == "if_exists":
        if not any(k in value for k in ("target", "selector", "image")):
            problems.append(f"{where}: needs 'target' (selector) or 'image'")
        if "then" not in value and "else" not in value:
            problems.append(f"{where}: needs a 'then' and/or 'else' list")
    elif kind == "for_each":
        sources = [k for k in ("items", "csv", "json") if k in value]
        if len(sources) != 1:
            problems.append(f"{where}: needs exactly one of 'items', 'csv' or 'json'")
        if "steps" not in value:
            problems.append(f"{where}: missing 'steps' list")
        if "on_error" in value and value["on_error"] not in ("stop", "continue"):
            problems.append(f"{where}.on_error: must be 'stop' or 'continue'")
        if "as" in value and not (isinstance(value["as"], str) and value["as"].isidentifier()):
            problems.append(f"{where}.as: must be a variable name")
        elif value.get("as") == "loop":
            problems.append(f"{where}.as: 'loop' is reserved for the loop state; choose another name")
    elif kind == "try":
        if "steps" not in value:
            problems.append(f"{where}: missing 'steps' list")
        if "catch" not in value and "finally" not in value:
            problems.append(f"{where}: needs a 'catch' and/or 'finally' list")
    elif kind == "repeat":
        if "times" not in value:
            problems.append(f"{where}: missing 'times'")
        elif not isinstance(value["times"], str) and not _is_int_like(value["times"]):
            problems.append(f"{where}.times: must be an integer (or a ${{variable}})")
        if "steps" not in value:
            problems.append(f"{where}: missing 'steps' list")
        if value.get("as") == "loop":
            problems.append(f"{where}.as: 'loop' is reserved for the loop state; choose another name")
    for key in _NESTED_KEYS[kind]:
        if key in value and value[key] is not None:
            _validate_steps(value[key], f"{where}.{key}", problems)


def _is_int_like(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, str):
        return v.strip().lstrip("-").isdigit() or "${" in v
    return False


def _is_number_like(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        if "${" in v:
            return True
        try:
            float(v)
            return True
        except ValueError:
            return False
    return False


# ============================================================ runner

@dataclass
class _Scope:
    """Where a block of steps comes from (for paths and relative file resolution)."""

    base_dir: pathlib.Path
    flows: Mapping[str, Any]
    settings: Mapping[str, Any]
    call_depth: int = 0


class FlowRunner:
    """Executes flow documents against a :class:`~locum.desktop.Desktop`, recording a :class:`RunReport`.

    Most users call :func:`run_flow`; the runner is exposed for embedding
    (e.g. :class:`~locum.rpa.robot.Robot` tasks that run flow fragments).
    """

    def __init__(
        self,
        desktop: "Desktop",
        report: RunReport,
        variables: Optional[Dict[str, Any]] = None,
        *,
        run_dir: Optional[pathlib.Path] = None,
        screenshot_on_error: bool = False,
        logger: Optional[logging.Logger] = None,
        policy: Any = None,
        confirm: Any = None,
        audit: Any = None,
    ):
        if policy is not None:
            from ..llm.safety import GuardedDesktop

            desktop = GuardedDesktop(desktop, policy, confirm=confirm, audit=audit)
        #: The desktop steps act on (a :class:`~locum.llm.safety.GuardedDesktop` when a policy is given).
        self.desktop = desktop
        self.report = report
        self.vars: Dict[str, Any] = variables if variables is not None else {}
        self.run_dir = run_dir
        self.screenshot_on_error = screenshot_on_error
        self.logger = logger or logging.getLogger("locum.flow")
        self._contexts: List[str] = []  # human context (loop iterations) for error messages
        self._shots = 0

    # ---------------------------------------------------------------- helpers
    def log(self, message: str, level: str = "info", path: str = "", **fields: Any) -> None:
        lvl = _LOG_LEVELS.get(str(level).lower())
        if lvl is None:
            raise WorkflowError(f"{path}: unknown log level {level!r}; expected one of {', '.join(sorted(_LOG_LEVELS))}")
        record = {"time": time.time(), "level": logging.getLevelName(lvl).lower(), "message": str(message), "path": path, **fields}
        if lvl >= logging.INFO:
            self.report.logs.append(record)
        self.logger.log(lvl, "[%s] %s", path or self.report.name, message, extra={"locum": record})

    def _timeout(self, args: Mapping[str, Any], step: Mapping[str, Any], scope: _Scope, default: Optional[float] = None) -> Optional[float]:
        for source in (args.get("timeout"), step.get("timeout"), scope.settings.get("timeout")):
            if source is not None and source != "":
                try:
                    return float(source)
                except (TypeError, ValueError):
                    raise WorkflowError(f"timeout must be a number, got {source!r}") from None
        return default

    def _file(self, value: Any, scope: _Scope) -> pathlib.Path:
        p = pathlib.Path(str(value)).expanduser()
        return p if p.is_absolute() else scope.base_dir / p

    def _image_ref(self, value: Any, scope: _Scope) -> Any:
        """The image a string target names, else ``None``.

        A string ending in an image extension is an image path (relative to the
        flow's directory) and a missing file raises :class:`FileNotFoundError`:
        read as a selector (``warn.png`` = role ``warn``, class ``png``) it could
        never match, so ``if_exists``/``wait_gone`` would silently go the wrong way.
        """
        if isinstance(value, str):
            low = value.strip().lower()
            if low.startswith("data:image/") or low.endswith(_IMAGE_EXTS):
                return self._image(value, scope)
        return None

    def _image(self, value: Any, scope: _Scope) -> Any:
        if isinstance(value, str) and not value.strip().lower().startswith("data:image/"):
            p = self._file(value.strip(), scope)
            if not p.is_file():
                raise FileNotFoundError(f"Image file not found: {p}")
            return p
        return value

    def _point(self, value: Any) -> Optional[Point]:
        if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            return Point(int(round(value[0])), int(round(value[1])))
        if isinstance(value, Mapping) and set(value) >= {"x", "y"} and len(value) == 2:
            return Point(int(round(float(value["x"]))), int(round(float(value["y"]))))
        if isinstance(value, str):
            m = _XY_RE.match(value)
            if m:
                return Point(int(round(float(m.group(1)))), int(round(float(m.group(2)))))
        return None

    def _target(self, args: Mapping[str, Any], timeout: Optional[float], scope: _Scope, *, key: str = "target") -> Any:
        """Resolve a target argument into an Element, Match or Point."""
        d = self.desktop
        if "x" in args or "y" in args:
            if "x" not in args or "y" not in args:
                raise WorkflowError("'x' and 'y' must be given together")
            return Point(int(round(float(args["x"]))), int(round(float(args["y"]))))
        if args.get("selector") is not None:
            return d.find(args["selector"], timeout=timeout)
        if args.get("image") is not None:
            kw = {"confidence": float(args["confidence"])} if args.get("confidence") is not None else {}
            return d.wait_image(self._image(args["image"], scope), timeout=timeout, **kw)
        if key not in args or args[key] is None:
            raise WorkflowError(f"missing {key!r}")
        return self._resolve_any(args[key], timeout, scope)

    def _resolve_any(self, value: Any, timeout: Optional[float], scope: _Scope) -> Any:
        pt = self._point(value)
        if pt is not None:
            return pt
        if isinstance(value, Mapping):
            return self._target(value, timeout, scope)
        if isinstance(value, str):
            image = self._image_ref(value, scope)
            if image is not None:
                return self.desktop.wait_image(image, timeout=timeout)
            return self.desktop.find(value, timeout=timeout)
        raise WorkflowError(f"cannot use {value!r} as a target (expected a selector, 'x,y', [x, y], or an image path)")

    def _element(self, value: Any, timeout: Optional[float]) -> Any:
        if isinstance(value, Mapping) and "selector" in value:
            value = value["selector"]
        if not isinstance(value, (str, Mapping)):
            raise WorkflowError(f"expected a selector, got {value!r}")
        return self.desktop.find(value, timeout=timeout)

    def _keys(self, value: Any) -> List[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)) and value and all(isinstance(k, str) for k in value):
            return list(value)
        raise WorkflowError(f"keys must be a key name or a list of key names, got {value!r}")

    @staticmethod
    def _offset(args: Mapping[str, Any]) -> Tuple[int, int]:
        off = args.get("offset")
        if off is None:
            return (0, 0)
        if isinstance(off, (list, tuple)) and len(off) == 2:
            return (int(off[0]), int(off[1]))
        if isinstance(off, Mapping):
            return (int(off.get("x", 0)), int(off.get("y", 0)))
        raise WorkflowError(f"offset must be [dx, dy], got {off!r}")

    def _region(self, value: Any, scope: _Scope) -> Any:
        if value is None:
            return None
        if isinstance(value, (list, tuple)) and len(value) == 4:
            return Rect(*(int(v) for v in value))
        if isinstance(value, Mapping) and {"left", "top", "width", "height"} <= set(value):
            return Rect.coerce(dict(value))
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",")]
            if len(parts) == 4 and all(p.lstrip("-").isdigit() for p in parts):
                return Rect(*(int(p) for p in parts))
            return self.desktop.find(value, timeout=0)
        raise WorkflowError(f"region must be [left, top, width, height] or a selector, got {value!r}")

    # ---------------------------------------------------------------- leaf actions
    def _do_click(self, kind: str, args: Mapping[str, Any], step: Mapping[str, Any], scope: _Scope) -> Any:
        t = self._timeout(args, step, scope)
        target = self._target(args, t, scope)
        offset = self._offset(args)
        d = self.desktop
        if kind == "click":
            clicks = int(args.get("clicks", 1))
            point = d.click(target, button=str(args.get("button", "left")), clicks=clicks,
                            interval=float(args.get("interval", 0.0)), offset=offset)
        elif kind == "double_click":
            point = d.double_click(target, button=str(args.get("button", "left")), offset=offset)
        elif kind == "right_click":
            point = d.right_click(target, offset=offset)
        else:
            point = d.middle_click(target, offset=offset)
        return {"point": [point.x, point.y]} if isinstance(point, Point) else None

    def _act(self, kind: str, args: Dict[str, Any], step: Mapping[str, Any], scope: _Scope, path: str) -> Any:
        d = self.desktop
        if kind in ("click", "double_click", "right_click", "middle_click"):
            return self._do_click(kind, args, step, scope)
        if kind == "move":
            t = self._timeout(args, step, scope)
            target = self._target(args, t, scope)
            dur = args.get("duration")
            # Desktop.move hit-tests element targets (never hover what covers them).
            point = d.move(target, offset=self._offset(args), duration=None if dur is None else float(dur))
            return {"point": [point.x, point.y]}
        if kind == "drag":
            t = self._timeout(args, step, scope)
            start = None if args.get("start") is None else self._resolve_any(args["start"], t, scope)
            end = self._resolve_any(args["end"], t, scope)
            d.drag(start, end, duration=float(args.get("duration", 0.5)), button=str(args.get("button", "left")))
            return None
        if kind == "scroll":
            t = self._timeout(args, step, scope)
            at = None if args.get("at") is None else self._resolve_any(args["at"], t, scope)
            d.scroll(int(args.get("dy", 0) or 0), int(args.get("dx", 0) or 0), at=at)
            return None
        if kind == "type":
            text = _to_text(args.get("text", ""))
            into = args.get("into", args.get("target"))
            kw: Dict[str, Any] = {"interval": float(args.get("interval", 0.0))}
            if args.get("mode") is not None:
                kw["mode"] = str(args["mode"])
            if into is not None:
                self._element(into, self._timeout(args, step, scope)).type(text, **kw)
            else:
                d.type(text, **kw)
            return None
        if kind == "press":
            keys = self._keys(args.get("keys"))
            d.press(*keys, presses=int(args.get("presses", 1)), interval=float(args.get("interval", 0.0)))
            return None
        if kind == "hotkey":
            keys = self._keys(args.get("keys"))
            d.hotkey(*keys)
            return None
        if kind == "set_text":
            pt = self._point(args["target"])
            if pt is not None:
                # A point target (what the recorder writes for a field it found no selector for):
                # the same typing path as Element.set_text's fallback -- click, select all, type.
                d.click(pt)
                if _bool_arg(args.get("clear", True), "set_text.clear"):
                    d.hotkey("mod+a")
                d.type(_to_text(args.get("text")))
                return None
            el = self._element(args["target"], self._timeout(args, step, scope))
            native = args.get("native", "auto")
            el.set_text(_to_text(args.get("text")), native=native, clear=_bool_arg(args.get("clear", True), "set_text.clear"))
            return None
        if kind in _ELEMENT_ACTIONS:
            el = self._element(args["target"], self._timeout(args, step, scope))
            getattr(el, kind)()
            return None
        if kind == "wait":
            state = args.get("state", "visible")
            target = args["target"]
            image = self._image_ref(target, scope)
            found = d.wait(target if image is None else image, state=state, timeout=self._timeout(args, step, scope))
            if found is None:
                return None
            return {"match": found.to_dict()} if image is not None else {"element": found.to_dict()}
        if kind == "wait_gone":
            target = args["target"]
            image = self._image_ref(target, scope)
            d.wait(target if image is None else image, state="gone", timeout=self._timeout(args, step, scope))
            return None
        if kind in ("wait_image", "click_image"):
            kw = {}
            for k in ("confidence",):
                if args.get(k) is not None:
                    kw[k] = float(args[k])
            if args.get("grayscale") is not None:
                kw["grayscale"] = _bool_arg(args["grayscale"], "grayscale")
            if args.get("region") is not None:
                kw["region"] = self._region(args["region"], scope)
            image = self._image(args["image"], scope)
            t = self._timeout(args, step, scope)
            if kind == "wait_image":
                m = d.wait_image(image, timeout=t, **kw)
            else:
                m = d.click_image(image, timeout=t, button=str(args.get("button", "left")),
                                  clicks=int(args.get("clicks", 1)), offset=self._offset(args), **kw)
            if args.get("var"):
                self.vars[str(args["var"])] = m.to_dict()
            return {"match": m.to_dict()}
        if kind == "click_text":
            kw = {}
            for k in ("regex", "case_sensitive"):
                if args.get(k) is not None:
                    kw[k] = _bool_arg(args[k], k)
            if args.get("fuzzy") is not None:
                kw["fuzzy"] = float(args["fuzzy"])
            if args.get("region") is not None:
                kw["region"] = self._region(args["region"], scope)
            m = d.click_text(_to_text(args["text"]), timeout=self._timeout(args, step, scope),
                             button=str(args.get("button", "left")), clicks=int(args.get("clicks", 1)),
                             offset=self._offset(args), **kw)
            if args.get("var"):
                self.vars[str(args["var"])] = m.to_dict()
            return {"match": m.to_dict()}
        if kind == "screenshot":
            path_arg = args.get("path")
            if not path_arg:
                raise WorkflowError("screenshot needs a 'path'")
            out = self._file(path_arg, scope)
            d.screenshot(self._region(args.get("region"), scope), path=str(out))
            return {"path": str(out)}
        if kind == "launch":
            cmd = args.get("command")
            if not cmd:
                raise WorkflowError("launch needs a 'command'")
            result = d.launch(cmd, wait_window=args.get("wait_window"), timeout=self._timeout(args, step, scope))
            return {"window": result.title} if args.get("wait_window") is not None and result is not None else None
        if kind == "activate_window":
            title, app = args.get("title"), args.get("app")
            title = None if title is None or _to_text(title).strip() == "" else title
            app = None if app is None or _to_text(app).strip() == "" else app
            if title is None and app is None:
                # e.g. "${title}" from an empty CSV cell: activating "any window" would send
                # the following keystrokes to whatever application happens to be in front.
                raise WorkflowError("activate_window needs a non-empty 'title' or 'app' (after interpolation)")
            win = d.window(title, app=app, timeout=self._timeout(args, step, scope))
            win.activate()
            return {"window": win.title}
        if kind == "sleep":
            try:
                seconds = float(args.get("seconds", 0))
            except (TypeError, ValueError):
                raise WorkflowError(f"sleep needs a number of seconds, got {args.get('seconds')!r}") from None
            if seconds < 0:
                raise WorkflowError("sleep seconds must be >= 0")
            d.sleep(seconds)
            return None
        if kind == "assert_exists":
            t = self._timeout(args, step, scope, default=0.0)
            message = args.get("message")
            if args.get("image") is not None and args.get("target") is None:
                kw = {"confidence": float(args["confidence"])} if args.get("confidence") is not None else {}
                if d.locate(self._image(args["image"], scope), timeout=t or 0.0, **kw) is None:
                    raise FlowAssertionError(message or f"expected image {args['image']!r} on screen")
                return None
            target = args.get("target")
            image = self._image_ref(target, scope)
            if image is not None:
                if "equals" in args:
                    raise WorkflowError("assert_exists: 'equals' needs an element target, not an image")
                kw = {"confidence": float(args["confidence"])} if args.get("confidence") is not None else {}
                if d.locate(image, timeout=t or 0.0, **kw) is None:
                    raise FlowAssertionError(message or f"expected image {target!r} on screen within {t:g}s")
                return None
            try:
                el = d.find(target, timeout=t)
            except Exception as exc:
                if type(exc).__name__ != "ElementNotFound":
                    raise
                raise FlowAssertionError(message or f"expected an element matching {target!r} within {t:g}s") from exc
            if "equals" in args:
                expected = _to_text(args["equals"])
                actual = el.text
                if actual != expected:
                    raise FlowAssertionError(message or f"element {target!r} has text {actual!r}, expected {expected!r}")
            return None
        if kind == "set_var":
            if set(args) == {"name", "value"} and isinstance(args["name"], str):
                self.vars[args["name"]] = args["value"]
            else:
                for k, v in args.items():
                    self.vars[str(k)] = v
            return None
        if kind == "read_value":
            el = self._element(args["target"], self._timeout(args, step, scope))
            attribute = str(args.get("attribute", "text"))
            allowed = ("text", "value", "name", "role", "automation_id", "class_name", "is_checked", "is_enabled", "bounds")
            if attribute not in allowed:
                raise WorkflowError(f"read_value attribute must be one of {', '.join(allowed)}, got {attribute!r}")
            value = getattr(el, attribute)
            if attribute == "bounds" and value is not None:
                value = value.to_dict()
            self.vars[str(args["var"])] = value
            return {"value": value}
        if kind == "clipboard_set":
            d.clipboard_set(_to_text(args.get("text", "")))
            return None
        if kind == "clipboard_get":
            name = args.get("var")
            if not name:
                raise WorkflowError("clipboard_get needs a 'var' name")
            self.vars[str(name)] = d.clipboard_get()
            return None
        if kind == "log":
            self.log(_to_text(args.get("message", "")), level=str(args.get("level", "info")), path=path)
            return None
        if kind == "fail":
            raise FlowAssertionError(_to_text(args.get("message")) or "flow failed")
        raise WorkflowError(f"unsupported step type {kind!r}")  # pragma: no cover - validated earlier

    # ---------------------------------------------------------------- control flow
    def _control(self, kind: str, raw: Mapping[str, Any], step: Mapping[str, Any], scope: _Scope, path: str, depth: int) -> Any:
        nested = _NESTED_KEYS[kind]
        args = {k: v for k, v in raw.items() if k not in nested} if isinstance(raw, Mapping) else raw
        args = interpolate(args, self.vars, path=path)
        if kind == "if_exists":
            t = self._timeout(args, step, scope, default=0.0) or 0.0
            if args.get("image") is not None:
                kw = {"confidence": float(args["confidence"])} if args.get("confidence") is not None else {}
                found = self.desktop.locate(self._image(args["image"], scope), timeout=t, **kw) is not None
            else:
                sel = args.get("target", args.get("selector"))
                image = self._image_ref(sel, scope) if "target" in args else None
                if image is not None:
                    kw = {"confidence": float(args["confidence"])} if args.get("confidence") is not None else {}
                    found = self.desktop.locate(image, timeout=t, **kw) is not None
                else:
                    found = self.desktop.exists(sel, timeout=t)
            branch = "then" if found else "else"
            body = raw.get(branch) or []
            self.run_steps(body, f"{path}.if_exists.{branch}", depth + 1, scope)
            return {"exists": found, "branch": branch}
        if kind == "repeat":
            try:
                times = int(args["times"])
            except (TypeError, ValueError):
                raise WorkflowError(f"repeat.times must be an integer, got {args['times']!r}") from None
            if times < 0:
                raise WorkflowError("repeat.times must be >= 0")
            items = list(range(times))
            self._loop(items, args.get("as"), raw.get("steps") or [], f"{path}.repeat.steps", depth, scope, "repeat", on_error="stop")
            return {"iterations": times}
        if kind == "for_each":
            items = self._loop_items(args, scope)
            name = str(args.get("as", "item"))
            self._loop(items, name, raw.get("steps") or [], f"{path}.for_each.steps", depth, scope, "for_each",
                       on_error=str(args.get("on_error", "stop")))
            return {"iterations": len(items)}
        if kind == "try":
            return self._try(raw, path, depth, scope)
        if kind == "call":
            return self._call(args, path, depth, scope)
        raise WorkflowError(f"unsupported control step {kind!r}")  # pragma: no cover

    def _loop_items(self, args: Mapping[str, Any], scope: _Scope) -> List[Any]:
        if "csv" in args:
            p = self._file(args["csv"], scope)
            strict = _bool_arg(args.get("strict", True), "for_each.strict")
            rows: List[Dict[str, Any]] = []
            with open(p, "r", encoding=str(args.get("encoding", "utf-8-sig")), newline="") as fh:
                reader = csv.DictReader(fh, delimiter=str(args.get("delimiter", ",")))
                for row in reader:
                    # A row with more fields than the header (an unquoted "1,234.56") or
                    # fewer (a truncated line) would silently shift or blank values.
                    extra = row.get(None)  # type: ignore[call-overload]
                    missing = [k for k, v in row.items() if k is not None and v is None]
                    if strict and (extra or missing):
                        width = len(reader.fieldnames or ())
                        got = width + len(extra) if extra else width - len(missing)
                        raise WorkflowError(
                            f"{p}:{reader.line_num}: expected {width} field(s) like the header, got {got}"
                            + (" (quote values that contain the delimiter, e.g. \"1,234.56\")" if extra else "")
                            + "; set strict: false on the for_each to accept such rows"
                        )
                    rows.append({str(k).strip(): (v if v is not None else "") for k, v in row.items() if k is not None})
            return rows
        if "json" in args:
            p = self._file(args["json"], scope)
            with open(p, "r", encoding=str(args.get("encoding", "utf-8"))) as fh:
                data = _json_loads(fh.read())
            if isinstance(data, Mapping) and isinstance(data.get("items"), list):
                data = data["items"]
            if not isinstance(data, list):
                raise WorkflowError(f"{p}: expected a JSON list (or an object with an 'items' list)")
            return data
        items = args.get("items")
        if isinstance(items, Mapping):
            return [{"key": k, "value": v} for k, v in items.items()]
        if isinstance(items, (list, tuple)):
            return list(items)
        if isinstance(items, str):
            text = items.strip()
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    pass
            raise WorkflowError(f"for_each.items must be a list (or a ${{variable}} holding one), got the string {items!r}")
        if isinstance(items, int) and not isinstance(items, bool):
            return list(range(items))
        raise WorkflowError(f"for_each.items must be a list, got {type(items).__name__}")

    def _loop(self, items: List[Any], name: Optional[str], steps: List[Any], path: str, depth: int, scope: _Scope,
              label: str, *, on_error: str) -> None:
        if name == "loop":
            raise WorkflowError(f"{label}.as: 'loop' is reserved for the loop state; choose another name")
        sentinel = object()
        saved_item = self.vars.get(name, sentinel) if name else sentinel
        saved_loop = self.vars.get("loop", sentinel)
        n = len(items)
        try:
            for i, item in enumerate(items):
                if name:
                    self.vars[name] = item
                self.vars["loop"] = {"index": i, "number": i + 1, "count": n, "first": i == 0, "last": i == n - 1}
                self._contexts.append(f"{label} iteration {i + 1}/{n}")
                mark = len(self.report.steps)
                try:
                    self.run_steps(steps, path, depth + 1, scope)
                except FlowStepError as exc:
                    if on_error != "continue":
                        raise
                    for s in self.report.steps[mark:]:
                        if s.status == FAILED:
                            s.handled = True
                    self.log(f"{label} iteration {i + 1}/{n} failed, continuing: {exc}", level="warning", path=path)
                finally:
                    self._contexts.pop()
        finally:
            if name:
                if saved_item is sentinel:
                    self.vars.pop(name, None)
                else:
                    self.vars[name] = saved_item
            if saved_loop is sentinel:
                self.vars.pop("loop", None)
            else:
                self.vars["loop"] = saved_loop

    def _try(self, raw: Mapping[str, Any], path: str, depth: int, scope: _Scope) -> Any:
        mark = len(self.report.steps)
        outcome: Dict[str, Any] = {"caught": False}
        pending: Optional[BaseException] = None
        try:
            self.run_steps(raw.get("steps") or [], f"{path}.try.steps", depth + 1, scope)
        except FlowStepError as exc:  # fail-safe aborts / interrupts propagate without running 'finally'
            if "catch" in raw:
                for s in self.report.steps[mark:]:
                    if s.status == FAILED:
                        s.handled = True
                outcome["caught"] = True
                original = exc.original or exc
                saved = self.vars.get("error", None)
                had = "error" in self.vars
                self.vars["error"] = {
                    "message": str(original) or type(original).__name__,
                    "type": type(original).__name__,
                    "step": exc.path,
                    "path": exc.path,
                    "text": str(exc),
                }
                try:
                    self.run_steps(raw.get("catch") or [], f"{path}.try.catch", depth + 1, scope)
                except (FailSafeTriggered, KeyboardInterrupt, SystemExit):
                    raise  # an abort/interrupt stops at once: 'finally' must not act on the desktop
                except Exception as catch_exc:
                    pending = catch_exc
                finally:
                    if had:
                        self.vars["error"] = saved
                    else:
                        self.vars.pop("error", None)
            else:
                pending = exc
        if raw.get("finally"):
            self.run_steps(raw["finally"], f"{path}.try.finally", depth + 1, scope)
        if pending is not None:
            raise pending
        return outcome

    def _call(self, args: Any, path: str, depth: int, scope: _Scope) -> Any:
        if isinstance(args, str):
            args = {"flow": args}
        ref = args.get("flow")
        if not isinstance(ref, (str, Mapping, list)):
            raise WorkflowError(f"call.flow must be a flow name or path, got {ref!r}")
        if scope.call_depth >= _MAX_CALL_DEPTH:
            raise WorkflowError(f"call depth limit ({_MAX_CALL_DEPTH}) exceeded - is a flow calling itself?")
        if isinstance(ref, str) and ref in scope.flows:
            sub = scope.flows[ref]
            sub_doc = {"steps": sub} if isinstance(sub, list) else dict(sub)
            sub_dir = scope.base_dir
            label = ref
        else:
            if isinstance(ref, str):
                file_path = self._file(ref, scope)
                if not file_path.is_file():
                    known = ", ".join(map(repr, scope.flows)) or "none"
                    raise WorkflowError(f"call: no sub-flow named {ref!r} (inline flows: {known}) and no file {file_path}")
                sub_doc, fp = load_flow(file_path)
            else:
                sub_doc, fp = load_flow(ref)
            problems = validate_flow(sub_doc, prefix=f"{path}.call")
            if problems:
                raise WorkflowError("invalid sub-flow:\n  " + "\n  ".join(problems))
            sub_dir = fp.parent if fp is not None else scope.base_dir
            label = str(ref) if isinstance(ref, str) else "<inline>"
        explicit = dict(args.get("vars") or {})
        # Same rules as top-level ``vars`` in run_flow: the caller's overrides win, and each
        # sub-flow default may reference the parent's variables, the overrides and the
        # defaults declared before it (``url: https://${host}/login``).
        child_vars = {**self.vars, **explicit}
        for key, value in dict(sub_doc.get("vars") or {}).items():
            if key not in explicit:
                child_vars[str(key)] = interpolate(value, child_vars, path=f"{path}.call.vars.{key}")
        merged_flows = dict(scope.flows)
        merged_flows.update(sub_doc.get("flows") or {})
        settings = dict(scope.settings)
        settings.update(sub_doc.get("settings") or {})
        child_scope = _Scope(base_dir=sub_dir, flows=merged_flows, settings=settings, call_depth=scope.call_depth + 1)
        saved_vars = self.vars
        self.vars = child_vars
        try:
            self.run_steps(sub_doc.get("steps") or [], f"{path}.call.steps", depth + 1, child_scope)
        finally:
            self.vars = saved_vars
        outputs = args.get("outputs")
        copied: Dict[str, Any] = {}
        if outputs:
            mapping = {outputs: outputs} if isinstance(outputs, str) else (
                dict(outputs) if isinstance(outputs, Mapping) else {o: o for o in outputs})
            for child_name, parent_name in mapping.items():
                if child_name not in child_vars:
                    raise WorkflowError(f"call {label!r}: output variable {child_name!r} was not set by the sub-flow")
                self.vars[str(parent_name)] = child_vars[child_name]
                copied[str(parent_name)] = child_vars[child_name]
        return {"flow": label, "outputs": sorted(copied)}

    # ---------------------------------------------------------------- execution
    def run_steps(self, steps: Sequence[Any], path: str, depth: int, scope: _Scope) -> None:
        """Run ``steps`` in order; raises :class:`FlowStepError` on the first unhandled failure."""
        for i, step in enumerate(steps or []):
            self.run_step(step, f"{path}[{i}]", depth, scope)

    def _describe(self, kind: str, step: Mapping[str, Any]) -> str:
        if step.get("name"):
            return str(step["name"])
        value = step[kind]
        shorthand = _SPEC[kind][0]
        if isinstance(value, Mapping):
            value = value.get(shorthand) if shorthand else None
            if value is None and isinstance(step[kind], Mapping):
                value = step[kind].get("selector") or step[kind].get("image") or step[kind].get("target")
        if value is None or (kind in CONTROL_STEPS and isinstance(value, (Mapping, list))):
            return kind
        text = _to_text(value)
        return f"{kind} {text[:60] + '...' if len(text) > 63 else text}"

    def run_step(self, step: Mapping[str, Any], path: str, depth: int, scope: _Scope) -> StepResult:
        kinds = _step_type(step)
        kind = kinds[0]
        raw = step[kind]
        shorthand = _SPEC[kind][0]
        result = StepResult(name=self._describe(kind, step), kind="flow", path=path, depth=depth, started_at=time.time(),
                            data={"type": kind})
        if self._contexts:
            result.data["context"] = self._contexts[-1]
        first = len(self.report.steps)
        self.report.add(result)
        t0 = time.monotonic()
        # settings.retries / retry_delay are per *action* step: a control step (for_each,
        # try, call...) inheriting them would re-run its whole block when one nested step
        # fails, repeating iterations that already succeeded (payments included). The
        # failing nested step is retried on its own instead.
        defaults = scope.settings if kind not in CONTROL_STEPS else {
            k: v for k, v in scope.settings.items() if k not in ("retries", "retry_delay")}
        try:
            retries = int(interpolate(step.get("retries", defaults.get("retries", 0)), self.vars, path=path) or 0)
            delay = float(interpolate(step.get("retry_delay", defaults.get("retry_delay", 0.5)), self.vars, path=path) or 0)
            continue_on_error = _truthy(interpolate(step.get("continue_on_error", defaults.get("continue_on_error", False)), self.vars, path=path))
        except WorkflowError as exc:  # an unresolvable ${...}: fail the step like any other error
            raise self._fail(result, kind, path, exc, t0) from None
        except (TypeError, ValueError) as exc:
            raise self._fail(result, kind, path, WorkflowError(f"invalid retries/retry_delay: {exc}"), t0) from None
        attempts = max(0, retries) + 1
        last: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            result.attempts = attempt
            mark = len(self.report.steps)
            try:
                if kind in CONTROL_STEPS:
                    out = self._control(kind, raw if isinstance(raw, Mapping) else {shorthand: raw}, step, scope, path, depth)
                else:
                    raw_args = dict(raw) if (kind == "set_var" or isinstance(raw, Mapping)) else {shorthand: raw}
                    # The report keeps the arguments as written in the flow (``${env.PASSWORD}``),
                    # never their interpolated values: variables and the environment often hold
                    # credentials, and reports are written to disk (report.json / report.html).
                    try:
                        result.data["args"] = copy.deepcopy(raw_args)
                    except Exception:
                        result.data["args"] = dict(raw_args)
                    args = interpolate(raw_args, self.vars, path=path)
                    out = self._act(kind, args, step, scope, path)
                if out is not None:
                    result.data["result"] = out
                result.status = PASSED
                result.finished_at = time.time()
                result.duration = time.monotonic() - t0
                return result
            except (FailSafeTriggered, KeyboardInterrupt, SystemExit) as exc:
                self._finalize_failure(result, exc, t0, screenshot=False)
                raise
            except Exception as exc:
                last = exc
                if attempt < attempts and not _is_refusal(exc):  # a refusal is never retried, even nested
                    for s in self.report.steps[mark:]:
                        if s.status == FAILED:
                            s.handled = True
                    result.data.setdefault("retry_errors", []).append(f"{type(exc).__name__}: {exc}")
                    self.log(f"attempt {attempt}/{attempts} failed ({type(exc).__name__}: {exc}); retrying in {delay:g}s",
                             level="warning", path=path)
                    if delay > 0:
                        self.desktop.sleep(delay)
                    continue
                break
        assert last is not None
        err = self._fail(result, kind, path, last, t0)
        if continue_on_error and not isinstance(last, FailSafeTriggered):
            # Also the failed steps nested in a container: the flow continued as configured.
            for s in self.report.steps[first:]:
                if s.status == FAILED:
                    s.handled = True
            self.log(f"step failed, continuing: {err}", level="warning", path=path)
            return result
        raise err

    def _finalize_failure(self, result: StepResult, exc: BaseException, t0: float, *, screenshot: bool) -> None:
        original = exc.original if isinstance(exc, FlowStepError) and exc.original is not None else exc
        result.status = FAILED
        result.finished_at = time.time()
        result.duration = time.monotonic() - t0
        result.error = str(exc) if isinstance(exc, FlowStepError) else (str(original) or type(original).__name__)
        result.error_type = type(original).__name__
        result.traceback = "".join(_tb.format_exception(type(original), original, original.__traceback__))
        if screenshot and self.screenshot_on_error and self.run_dir is not None:
            shot = getattr(original, "_locum_screenshot", None)
            if not shot:
                from .robot import save_error_screenshot

                self._shots += 1
                try:
                    shot = save_error_screenshot(self.desktop, self.run_dir, f"{self._shots:03d}-error-{result.path}")
                    try:
                        setattr(original, "_locum_screenshot", shot)
                    except Exception:
                        pass
                except Exception as shot_exc:
                    self.log(f"screenshot failed: {type(shot_exc).__name__}: {shot_exc}", level="warning", path=result.path)
                    shot = None
            result.screenshot = shot

    def _fail(self, result: StepResult, kind: str, path: str, exc: BaseException, t0: float) -> FlowStepError:
        if isinstance(exc, FlowStepError):
            # A nested step already failed: propagate its error unchanged, mark this container failed.
            self._finalize_failure(result, exc, t0, screenshot=False)
            return exc
        context = f", {self._contexts[-1]}" if self._contexts else ""
        message = f"Flow step {path} ({kind}{context}) failed: {type(exc).__name__}: {str(exc) or type(exc).__name__}"
        err = FlowStepError(message, path=path, step_type=kind, original=exc)
        err.__cause__ = exc
        self._finalize_failure(result, exc, t0, screenshot=True)
        result.error = str(exc) or type(exc).__name__
        self.log(message, level="error", path=path)
        return err


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


_TRUE_WORDS = ("1", "true", "yes", "on", "y")
_FALSE_WORDS = ("0", "false", "no", "off", "n", "")


def _bool_arg(v: Any, what: str) -> bool:
    """A boolean step argument; interpolated strings (``"false"`` from ``--var``/CSV) count by meaning."""
    if isinstance(v, str):
        word = v.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise WorkflowError(f"{what} must be true or false, got {v!r}")
    return bool(v)


def run_flow(
    path_or_dict: Union[PathLike, Mapping[str, Any], Sequence[Any]],
    desktop: Optional["Desktop"] = None,
    variables: Optional[Mapping[str, Any]] = None,
    *,
    base_dir: Optional[PathLike] = None,
    run_dir: Optional[PathLike] = None,
    screenshot_on_error: Optional[bool] = None,
    raise_on_error: bool = False,
    logger: Optional[logging.Logger] = None,
    policy: Any = None,
    confirm: Any = None,
    audit: Any = None,
) -> RunReport:
    """Validate and run a flow; returns the :class:`RunReport` (``report.ok`` tells success).

    Args:
        path_or_dict: A YAML/JSON file path, a flow mapping, a bare list of steps or YAML/JSON text.
        desktop: The desktop to drive (default: a new :class:`~locum.desktop.Desktop`).
        variables: Variables overriding the document's ``vars``.
        base_dir: Directory for relative file paths (images, CSV, sub-flows,
            screenshots); default: the flow file's directory, else the current directory.
        run_dir: When given, the run's evidence goes to ``run_dir/<run-id>/``:
            error screenshots plus ``report.json`` and ``report.html``.
        screenshot_on_error: Save a screenshot when a step fails (default: the document's
            ``settings.screenshot_on_error``, else ``True`` when ``run_dir`` is set).
        raise_on_error: Raise the :class:`FlowStepError` instead of returning a failed report.
        policy: A :class:`~locum.llm.safety.Policy` every acting step must pass
            (``None``, the default: steps are not gated).
        confirm: Confirmation callback for actions the policy sends to a human
            (default: the policy's ``confirm``; none = such actions are blocked).
        audit: :class:`~locum.llm.safety.AuditLog` for the policy decisions.

    Raises:
        WorkflowError: The document is invalid (all problems are listed), before anything runs.
    """
    from .robot import _new_run_id

    doc, file_path = load_flow(path_or_dict)
    source = str(file_path) if file_path is not None else "<dict>"
    problems = validate_flow(doc)
    if problems:
        raise WorkflowError(f"Invalid flow {source}:\n  " + "\n  ".join(problems))
    name = str(doc.get("name") or (file_path.stem if file_path is not None else "flow"))
    report = RunReport(name=name, run_id=_new_run_id(name), started_at=time.time(),
                       metadata={"flow": source})
    evidence_dir = pathlib.Path(run_dir).expanduser() / report.run_id if run_dir is not None else None
    if evidence_dir is not None:
        report.run_dir = str(evidence_dir)
    if screenshot_on_error is None:
        setting = (doc.get("settings") or {}).get("screenshot_on_error")
        shots = (run_dir is not None) if setting is None else _bool_arg(setting, "settings.screenshot_on_error")
    else:
        shots = bool(screenshot_on_error)
    if desktop is None:
        from ..desktop import Desktop

        desktop = Desktop()
    base = pathlib.Path(base_dir).expanduser() if base_dir is not None else (
        file_path.parent if file_path is not None else pathlib.Path.cwd())
    vars_: Dict[str, Any] = {}
    runner = FlowRunner(desktop, report, vars_, run_dir=evidence_dir, screenshot_on_error=shots, logger=logger,
                        policy=policy, confirm=confirm, audit=audit)
    if policy is not None:
        report.metadata["policy"] = "allow-list" if getattr(policy, "allow", None) is not None else "heuristic"
    settings = dict(doc.get("settings") or {})
    scope = _Scope(base_dir=base, flows=dict(doc.get("flows") or {}), settings=settings)
    error: Optional[BaseException] = None
    overrides = dict(variables or {})
    try:
        for key, value in dict(doc.get("vars") or {}).items():
            if key not in overrides:  # later vars may reference earlier ones
                vars_[str(key)] = interpolate(value, {**vars_, **overrides}, path=f"vars.{key}")
        vars_.update(overrides)
        report.metadata["variables"] = sorted(vars_)
        runner.run_steps(doc["steps"], "steps", 0, scope)
    except FlowStepError as exc:
        error = exc
        report.error = str(exc)
    except WorkflowError as exc:  # e.g. undefined variable in the document's vars
        error = exc
        report.error = str(exc)
    except FailSafeTriggered as exc:
        error = exc
        report.aborted = True
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        report.finished_at = time.time()
        report.variables = runner.vars
        runner.log(report.summary(), level="info" if report.ok else "error")
        if evidence_dir is not None:
            report.save_json(evidence_dir / "report.json")
            report.save_html(evidence_dir / "report.html")
    if error is not None and raise_on_error:
        raise error
    return report
