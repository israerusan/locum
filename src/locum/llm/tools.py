"""LLM tools: a schema-generating tool registry and the :class:`DesktopToolkit`.

:class:`ToolRegistry` turns plain Python functions into tools that any LLM
API understands. Parameter schemas come from type hints, descriptions from the
docstring (summary + ``Args:`` section)::

    reg = ToolRegistry()

    @reg.register
    def add(a: int, b: int = 0) -> int:
        '''Add two numbers.

        Args:
            a: First operand.
            b: Second operand.
        '''
        return a + b

    reg.to_anthropic()   # [{"name": "add", "description": ..., "input_schema": {...}}]
    reg.call("add", {"a": 2, "b": "3"})   # ToolResult(text="5") -- never raises

:class:`DesktopToolkit` is the ready-made registry for desktop control:
set-of-marks observation, element ids, selectors, OCR, keyboard/mouse, windows,
apps and the clipboard. Every call is checked by a :class:`~locum.llm.safety.Policy`
and written to an :class:`~locum.llm.safety.AuditLog`. The toolkit reports to
the policy what each action really reaches -- the hit-tested element, its window
and application, the focused control for keyboard input (and the default button
Enter would press) -- so heuristic confirmations and allow rules
(``Policy(allow=[...])``) judge the actual target; an activating action on a
control with no usable identity is marked ``target_unknown``.
"""

from __future__ import annotations

import base64
import collections.abc
import dataclasses
import enum
import inspect
import json
import logging
import re
import sys
import threading
import time
import types
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional, Tuple, Union

from .. import _target as _input_target
from ..errors import ActionTimeout, ElementNotFound, FailSafeTriggered, NotSupported, PolicyViolation, LocumError
from ..types import Match, Point, Rect, WindowInfo
from .observe import Observation, ObservedElement, observe
from .._nodeinfo import ancestors, is_password, node_app, owning_window, target_label, window_at
from .safety import (
    AuditLog, Policy, check_activation_combo, check_launch_options, is_text_input, keys_activate, keys_may_move_focus, split_typed_text,
    pressed_control, target_identity, typed_text_activates,
)

_log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from ..desktop import Desktop
    from ..element import Element


# =============================================================================== results


@dataclass
class ToolResult:
    """What a tool returns to the model.

    Attributes:
        text: Human/model readable outcome.
        image_base64: Optional PNG screenshot (base64, no ``data:`` prefix).
        is_error: The call failed (sent to the model as ``is_error``/``isError``).
        data: Structured payload for programmatic callers (not sent to the model).
    """

    text: str = ""
    image_base64: Optional[str] = None
    is_error: bool = False
    data: Any = None

    def __post_init__(self) -> None:
        # Text reaches the model (and MCP clients): registered secrets never do.
        from .._secret import scrub

        self.text = scrub(self.text) or self.text

    @classmethod
    def ok(cls, text: str = "OK", **kw: Any) -> "ToolResult":
        return cls(text=text, **kw)

    @classmethod
    def error(cls, text: str, **kw: Any) -> "ToolResult":
        return cls(text=text, is_error=True, **kw)

    def to_anthropic_content(self) -> List[Dict[str, Any]]:
        """Content blocks for an Anthropic ``tool_result`` (text and/or base64 PNG image)."""
        blocks: List[Dict[str, Any]] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        if self.image_base64:
            blocks.append(
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": self.image_base64}}
            )
        if not blocks:
            blocks.append({"type": "text", "text": "Error" if self.is_error else "OK"})
        return blocks

    def to_anthropic(self, tool_use_id: str) -> Dict[str, Any]:
        """A complete ``tool_result`` block answering ``tool_use_id``."""
        block: Dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": self.to_anthropic_content()}
        if self.is_error:
            block["is_error"] = True
        return block

    def to_mcp_content(self) -> List[Dict[str, Any]]:
        """MCP ``tools/call`` content items."""
        items: List[Dict[str, Any]] = []
        if self.text or not self.image_base64:
            items.append({"type": "text", "text": self.text or ("Error" if self.is_error else "OK")})
        if self.image_base64:
            items.append({"type": "image", "data": self.image_base64, "mimeType": "image/png"})
        return items

    def to_openai_content(self) -> str:
        """OpenAI-style tool message content (text only; images must be sent separately)."""
        return self.text or ("Error" if self.is_error else "OK")


# =============================================================================== schemas

_NONE_TYPE = type(None)
#: ``X | Y`` hints (PEP 604, Python 3.10+) have this origin instead of ``typing.Union``.
_UNION_TYPES = tuple(t for t in (Union, getattr(types, "UnionType", None)) if t is not None)
_SECTION_RE = re.compile(r"^\s*(Args|Arguments|Parameters|Params|Returns?|Yields?|Raises|Examples?|Notes?|Attributes)\s*:\s*$")
_ARG_RE = re.compile(r"^(\s*)\*{0,2}([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$")


def parse_docstring(doc: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """Split a Google-style docstring into ``(description, {arg: description})``."""
    if not doc:
        return "", {}
    lines = inspect.cleandoc(doc).splitlines()
    desc_lines: List[str] = []
    args: Dict[str, str] = {}
    section: Optional[str] = None
    current: Optional[str] = None
    arg_indent: Optional[int] = None
    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).lower()
            current, arg_indent = None, None
            continue
        if section is None:
            desc_lines.append(line)
        elif section in ("args", "arguments", "parameters", "params"):
            if not line.strip():
                current = None
                continue
            am = _ARG_RE.match(line)
            indent = len(line) - len(line.lstrip())
            if am and (arg_indent is None or indent <= arg_indent):
                arg_indent = indent
                current = am.group(2)
                args[current] = am.group(4).strip()
            elif current is not None:
                args[current] = (args[current] + " " + line.strip()).strip()
    description = "\n".join(desc_lines).strip()
    description = re.sub(r"\n{3,}", "\n\n", description)
    return description, args


def _json_type_schema(tp: Any) -> Tuple[Dict[str, Any], bool]:
    """``(schema, nullable)`` for a Python type hint."""
    if tp is inspect.Parameter.empty or tp is Any:
        return {}, False
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in _UNION_TYPES:
        non_none = [a for a in args if a is not _NONE_TYPE]
        nullable = len(non_none) != len(args)
        if len(non_none) == 1:
            schema, _ = _json_type_schema(non_none[0])
            return schema, nullable
        return {"anyOf": [_json_type_schema(a)[0] for a in non_none]}, nullable
    if origin is typing.Literal:
        values = list(args)
        schema: Dict[str, Any] = {"enum": values}
        kinds = {type(v) for v in values}
        if kinds == {str}:
            schema["type"] = "string"
        elif kinds <= {int}:
            schema["type"] = "integer"
        return schema, False
    if origin in (list, tuple, set, frozenset, collections.abc.Sequence) or tp in (list, tuple, set, frozenset):
        schema = {"type": "array"}
        if args and args[0] is not Ellipsis:
            item, _ = _json_type_schema(args[0])
            if item:
                schema["items"] = item
        return schema, False
    if origin in (dict, Dict) or tp is dict:
        return {"type": "object"}, False
    if tp is bool:
        return {"type": "boolean"}, False
    if tp is int:
        return {"type": "integer"}, False
    if tp is float:
        return {"type": "number"}, False
    if tp is str:
        return {"type": "string"}, False
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return {"enum": [m.value for m in tp]}, False
    return {}, False


def _param_hints(fn: Callable[..., Any], sig: Optional[inspect.Signature] = None) -> Dict[str, Any]:
    """Resolved type hints of ``fn``'s parameters.

    If the hints cannot all be resolved at once (e.g. a return type imported only
    under ``TYPE_CHECKING``), each parameter is resolved on its own so one bad
    annotation does not drop the types of all the others.
    """
    try:
        return typing.get_type_hints(fn)
    except Exception:
        pass
    sig = sig if sig is not None else _signature(fn)
    globalns = getattr(inspect.unwrap(fn), "__globals__", None)
    if not isinstance(globalns, dict):
        globalns = getattr(getattr(inspect.unwrap(fn), "__call__", None), "__globals__", None)
    hints: Dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        ann = param.annotation
        if ann is inspect.Parameter.empty:
            continue
        if not isinstance(ann, str):
            hints[pname] = ann
            continue

        def _stub() -> None:  # pragma: no cover - never called
            pass

        _stub.__annotations__ = {pname: ann}
        try:
            hints[pname] = typing.get_type_hints(_stub, globalns=dict(globalns or {}))[pname]
        except Exception:
            continue
    return hints


def schema_from_function(fn: Callable[..., Any]) -> Tuple[str, Dict[str, Any]]:
    """``(description, JSON schema)`` for ``fn``'s parameters (type hints + docstring)."""
    description, arg_docs = parse_docstring(inspect.getdoc(fn))
    sig = inspect.signature(fn)
    hints = _param_hints(fn, sig)
    props: Dict[str, Any] = {}
    required: List[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls") or param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        tp = hints.get(pname, param.annotation)
        schema, nullable = _json_type_schema(tp)
        schema = dict(schema)
        if pname in arg_docs and arg_docs[pname]:
            schema["description"] = arg_docs[pname]
        if param.default is inspect.Parameter.empty:
            required.append(pname)
        elif param.default is not None and _json_safe(param.default):
            schema["default"] = param.default
        props[pname] = schema
    out: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return description, out


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _coerce(value: Any, schema: Dict[str, Any]) -> Any:
    """Lenient coercion of model-provided JSON values to the schema type."""
    t = schema.get("type")
    if value is None:
        return None
    try:
        if t == "integer":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, str) and re.fullmatch(r"\s*-?\d+(\.0*)?\s*", value):
                return int(float(value))
        elif t == "number":
            if isinstance(value, str) and value.strip():
                return float(value)
        elif t == "boolean":
            if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
                return value.strip().lower() in ("true", "1", "yes")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
                return bool(value)
        elif t == "string":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(value)
        elif t == "array":
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("["):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, list):
                            value = parsed
                    except ValueError:
                        value = [value]
                else:
                    value = [value]
            if isinstance(value, tuple):
                value = list(value)
            item = schema.get("items")
            if item and isinstance(value, list):
                value = [_coerce(v, item) for v in value]
    except (TypeError, ValueError):
        return value
    return value


def _type_ok(value: Any, schema: Dict[str, Any]) -> bool:
    t = schema.get("type")
    if value is None:
        return True
    if not t:
        # No type (an Enum, or a Literal of floats/bools/mixed values): still check membership.
        return "enum" not in schema or value in schema["enum"]
    checks = {
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "string": lambda v: isinstance(v, str),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    fn = checks.get(t)
    ok = fn(value) if fn else True
    if ok and "enum" in schema:
        ok = value in schema["enum"]
    return ok


@dataclass
class ToolSpec:
    """A tool: ``name``, ``description``, JSON-schema ``parameters`` and the callable ``fn``.

    ``read_only``: the tool only reads state (skips confirmations, budgets and
    dry-run). ``confirm``: every call needs human confirmation through the
    policy (e.g. an integration tool that releases payments), whatever its
    name or arguments; honoured for :class:`~locum.llm.agent.Agent` extra tools.
    """

    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[..., Any]
    read_only: bool = False
    confirm: bool = False

    @classmethod
    def from_function(
        cls,
        fn: Callable[..., Any],
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        read_only: bool = False,
        confirm: bool = False,
    ) -> "ToolSpec":
        desc, schema = schema_from_function(fn)
        tool_name = name or getattr(fn, "__name__", None)
        if not tool_name or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_name):
            raise ValueError(f"Invalid tool name {tool_name!r} (use 1-64 chars of [A-Za-z0-9_-])")
        return cls(tool_name, (description or desc or tool_name).strip(), parameters or schema, fn, read_only, confirm)

    def to_anthropic(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}

    def to_openai(self) -> Dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def to_mcp(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": self.name, "description": self.description, "inputSchema": self.parameters}
        if self.read_only:
            d["annotations"] = {"readOnlyHint": True}
        return d


def _to_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if value is None:
        return ToolResult("OK")
    if isinstance(value, str):
        return ToolResult(value)
    if isinstance(value, (bytes, bytearray)):
        return ToolResult(image_base64=base64.b64encode(bytes(value)).decode("ascii"))
    mod = type(value).__module__ or ""
    if mod.startswith("PIL."):
        from ..vision import to_base64_png

        return ToolResult(f"Image {value.size[0]}x{value.size[1]}", image_base64=to_base64_png(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    try:
        return ToolResult(json.dumps(value, ensure_ascii=False, default=str), data=value)
    except (TypeError, ValueError):
        return ToolResult(str(value), data=value)


class ToolRegistry:
    """A named collection of tools (see module docstring)."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(
        self,
        fn: Optional[Callable[..., Any]] = None,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        read_only: bool = False,
    ) -> Any:
        """Register ``fn`` as a tool; usable as ``@reg.register`` or ``@reg.register(name=...)``.

        Returns the function unchanged (so decorated functions stay callable).
        """

        def deco(f: Callable[..., Any]) -> Callable[..., Any]:
            self.add(ToolSpec.from_function(f, name=name, description=description, parameters=parameters, read_only=read_only))
            return f

        if fn is not None:
            return deco(fn)
        return deco

    def add(self, spec: ToolSpec) -> ToolSpec:
        """Register a prepared :class:`ToolSpec` (replaces any tool with the same name)."""
        self._tools[spec.name] = spec
        return spec

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools)

    def specs(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def call(self, name: str, args: Optional[Dict[str, Any]] = None) -> ToolResult:
        """Validate ``args`` against the schema and run the tool.

        Never raises for tool errors: unknown tools, bad arguments, policy
        violations and exceptions all come back as ``ToolResult(is_error=True)``
        with a message the model can act on. The only exception that propagates
        is :class:`~locum.errors.FailSafeTriggered` -- the human's kill switch
        must stop the agent, not be reported to it.
        """
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult.error(f"Unknown tool {name!r}. Available tools: {', '.join(self._tools)}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return ToolResult.error(f"Arguments for {name!r} must be a JSON object, got {type(args).__name__}")
        props = spec.parameters.get("properties", {}) or {}
        accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in _signature(spec.fn).parameters.values())
        unknown = [k for k in args if k not in props]
        if unknown and not accepts_kwargs and props is not None:
            return ToolResult.error(
                f"Unexpected argument(s) for {name}: {', '.join(unknown)}. Valid arguments: {', '.join(props) or 'none'}"
            )
        missing = [k for k in spec.parameters.get("required", []) or [] if args.get(k) is None]
        if missing:
            return ToolResult.error(f"Missing required argument(s) for {name}: {', '.join(missing)}")
        call_args: Dict[str, Any] = {}
        for k, v in args.items():
            schema = props.get(k, {})
            cv = _coerce(v, schema)
            if not _type_ok(cv, schema):
                expected = schema.get("enum") or schema.get("type")
                return ToolResult.error(f"Argument {k!r} of {name} must be {expected}, got {v!r}")
            call_args[k] = cv
        enums = _enum_params(spec.fn) if call_args else {}
        for k, cls in enums.items():
            v = call_args.get(k)
            if v is not None and not isinstance(v, cls):
                try:
                    call_args[k] = cls(v)  # the function gets Color.RED, not the raw "red"
                except (ValueError, TypeError):
                    return ToolResult.error(
                        f"Argument {k!r} of {name} must be one of {[m.value for m in cls]}, got {v!r}"
                    )
        try:
            return _to_result(spec.fn(**call_args))
        except FailSafeTriggered:
            raise
        except PolicyViolation as exc:
            return ToolResult.error(f"Blocked by policy: {exc}", data={"blocked": True})
        except (ElementNotFound, NotSupported, LocumError) as exc:
            return ToolResult.error(f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            return ToolResult.error(f"{type(exc).__name__}: {exc}")

    def to_anthropic(self) -> List[Dict[str, Any]]:
        return [s.to_anthropic() for s in self._tools.values()]

    def to_openai(self) -> List[Dict[str, Any]]:
        return [s.to_openai() for s in self._tools.values()]

    def to_mcp(self) -> List[Dict[str, Any]]:
        return [s.to_mcp() for s in self._tools.values()]

    def __repr__(self) -> str:
        return f"<ToolRegistry {self.names()}>"


def _enum_params(fn: Callable[..., Any]) -> Dict[str, Any]:
    """``{parameter: Enum class}`` for ``fn``'s ``Enum`` (or ``Optional[Enum]``) parameters."""
    try:
        hints = _param_hints(fn)
    except Exception:
        return {}
    out: Dict[str, Any] = {}
    for pname, tp in hints.items():
        if pname == "return":
            continue
        if typing.get_origin(tp) in _UNION_TYPES:
            non_none = [a for a in typing.get_args(tp) if a is not _NONE_TYPE]
            tp = non_none[0] if len(non_none) == 1 else None
        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            out[pname] = tp
    return out


def _signature(fn: Callable[..., Any]) -> inspect.Signature:
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return inspect.Signature()


# =============================================================================== toolkit


#: Shown instead of text typed into a password field (audit log, results, confirmations).
_REDACTED = "***"


class _MaskedValue:
    """A password field node whose ``value`` reads as :data:`_REDACTED` (for descriptions)."""

    value = _REDACTED

    def __init__(self, node: Any) -> None:
        self._node = node

    def __getattr__(self, name: str) -> Any:
        return getattr(self._node, name)


def _has_enter(parts: Any) -> bool:
    """Whether a parsed key combination includes Enter."""
    return any(p in ("enter", "num_enter") for p in parts)


def _has_newline(text: str) -> bool:
    return "\n" in text or "\r" in text


_TEXT_INPUT_ROLES = frozenset({"edit", "combobox", "spinbutton", "passwordtext", "entry", "searchbox", "textbox"})
_FORM_ROLES = frozenset({"form", "dialog", "alert", "window", "frame"})


def _default_button(node: Any, limit: int = 400) -> Any:
    """The default button that Enter in ``node`` (a single-line text field) would activate, or ``None``.

    In dialogs (Win32 ``DM_GETDEFID``, GTK/Qt default buttons) and HTML forms
    (implicit submission), Enter in a single-line field presses the default
    button -- "Pay now" -- although the focused control is "Amount". The button
    is found in the field's nearest form/dialog/window by its ``"default"``
    state (searching at most ``limit`` nodes).
    """
    try:
        role = str(node.role or "").lower()
        states = node.states or frozenset()
    except Exception:
        return None
    if role not in _TEXT_INPUT_ROLES and not (role != "document" and "editable" in states):
        return None
    if states & {"multi_line", "multiline"}:
        return None  # Enter inserts a line break
    from .._nodeinfo import ancestors

    parents = ancestors(node)
    if not parents:
        return None
    scope = next((a for a in parents if str(getattr(a, "role", "") or "").lower() in _FORM_ROLES), parents[-1])
    queue: Deque[Any] = collections.deque([scope])
    seen = 0
    while queue and seen < limit:
        cur = queue.popleft()
        seen += 1
        try:
            # Role first: reading every node's states is expensive on UI Automation.
            if str(cur.role or "").lower() == "button" and "default" in (cur.states or ()):
                return cur
            queue.extend(cur.children() or ())
        except Exception:
            continue
    return None


def _with_default_button(t: "_Target", node: Any) -> "_Target":
    """``t`` with the label of the default button that Enter in ``node`` would press added to its name."""
    button = _default_button(node)
    if button is None:
        return t
    try:
        label = target_label(button)
    except Exception:
        label = ""
    if not label:
        return t
    return dataclasses.replace(
        t,
        name=f"{t.name} -> {label}" if t.name else label,
        description=f"{t.description} (Enter activates the default button {label!r})",
        # Allow rules judge the control Enter really activates: the button, not the field.
        node=button,
    )


_FKEY = re.compile(r"f\d{1,2}")
#: Controls that carry access keys / accelerators (searched by :func:`_shortcut_targets`).
_SHORTCUT_ROLES = frozenset({"button", "togglebutton", "menuitem", "link", "checkbox", "radiobutton", "tab"})
#: Editing shortcuts (select all, copy, cut, paste, undo, redo) never press a labelled control.
_EDIT_SHORTCUTS = frozenset(frozenset({m, k}) for m in ("ctrl", "cmd") for k in "acvxyz")


def _shortcut_targets(desktop: Any, combos: List[List[str]], limit: int = 400) -> List[str]:
    """Labels of the controls in the focused window that the shortcut ``combos`` press.

    ``alt+p`` presses the button whose access key is Alt+P (Win32 caption
    ``"&Pay now"``, UI Automation ``AccessKey``) exactly like a click, and a
    menu item's accelerator (``Ctrl+S`` / ``F8`` next to "Post"/"Execute") runs
    its command: the policy judges such shortcuts by those labels. Only
    combos with Alt/Ctrl/Cmd (but not copy/paste-style editing shortcuts) or a
    function key are looked up, on buttons, menu items, tabs and the like; at most
    ``limit`` nodes of the window are searched.
    """
    from .safety import _combo_sets

    wanted = []
    for keys in combos:
        for ks in _combo_sets(list(keys)):
            if ks in _EDIT_SHORTCUTS:
                continue
            if (len(ks) > 1 and ks & {"alt", "ctrl", "cmd"}) or any(_FKEY.fullmatch(k) for k in ks):
                wanted.append(ks)
    if not wanted:
        return []
    mnemonics = {next(iter(ks - {"alt"})) for ks in wanted if "alt" in ks and len(ks) == 2 and not ks & {"ctrl", "cmd", "shift"}}
    scope = None
    try:
        node = desktop.backend.focused_element()
        parents = ancestors(node) if node is not None else []
        scope = parents[-1] if parents else None
    except Exception:
        scope = None
    if scope is None:  # the focus is unknown or has no readable parents: search the active window
        try:
            win = desktop.active_window()
            scope = win.element.node if win is not None else None
        except Exception:
            scope = None
    if scope is None:
        return []
    labels: List[str] = []
    queue: Deque[Any] = collections.deque([scope])
    seen = 0
    while queue and seen < limit:
        cur = queue.popleft()
        seen += 1
        try:
            queue.extend(cur.children() or ())
            if str(cur.role or "").lower() not in _SHORTCUT_ROLES:
                continue
            name = str(cur.name or "")
            hit = bool(mnemonics) and any(f"&{m}" in name.lower() for m in mnemonics if len(m) == 1)
            if not hit:
                attrs = cur.attributes() or {}
                hit = any(c in wanted for a in ("access_key", "accelerator_key") if attrs.get(a)
                          for c in _combo_sets(str(attrs[a])))
            if hit:
                try:
                    label = target_label(cur) or name
                except Exception:
                    label = name
                label = label.replace("&", "")
                if label and label not in labels:
                    labels.append(label)
        except Exception:
            continue
    return labels


@dataclass
class _Target:
    element: Optional["Element"] = None
    point: Optional[Point] = None
    description: str = ""
    name: str = ""
    app: str = ""
    observed: Optional[ObservedElement] = None
    #: Every application the action may land in (hit-tested node's app and the
    #: front-most window under the point); all must pass the app policy.
    apps: Tuple[str, ...] = ()
    #: The input can activate something, but what it reaches cannot be identified
    #: (unreadable focus, nothing under the pointer): ``Policy(unknown_target=...)`` decides.
    unknown: bool = False
    #: The target is a password field (typed text is redacted from audit logs and results).
    secret: bool = False
    #: The accessibility node acted on (allow rules match selectors against it).
    node: Any = None
    #: Title of the window the action lands in (allow rules: ``window=``).
    window: str = ""


_DIRECTIONS = ("up", "down", "left", "right")


class DesktopToolkit:
    """Desktop-control tools for LLM agents (see module docstring).

    Args:
        desktop: The :class:`~locum.desktop.Desktop` to drive.
        policy: Safety policy (default: :class:`Policy` with default confirmations).
        audit: Audit log (default: an in-memory :class:`AuditLog`).
        observe_kwargs: Extra arguments for :func:`~locum.llm.observe.observe`.
        find_timeout: Seconds selector/text targets wait for their element.
        feedback: Append the focused element/active window to action results.
        launch_kwargs: Extra ``subprocess.Popen`` options for ``launch_app``
            (the MCP server uses this to keep children off its stdio). Only
            options that cannot change *which* program runs are accepted
            (:data:`~locum.llm.safety.SAFE_LAUNCH_OPTIONS`); ``shell``,
            ``executable`` and ``preexec_fn`` raise :class:`PolicyViolation`, and
            ``cwd``/``env`` need ``Policy(launch_options=[...])``.

    Element ids refer to the most recent observation (:attr:`last_observation`);
    ``x``/``y`` arguments are pixels of the most recent screenshot and are mapped
    to the screen with :meth:`Observation.to_screen`. Before any observation,
    coordinates are pixels of an unscaled screenshot of the whole virtual screen:
    ``(0, 0)`` is the virtual screen's top-left corner, so they equal raw screen
    pixels only when no monitor lies left of or above the primary one (with a
    monitor at ``x=-1920``, screen point ``(-500, 135)`` is ``(1420, 135)``).
    """

    #: Seconds ``find_elements`` spends building selectors for its listing.
    SELECTOR_BUDGET = 3.0
    #: Seconds ``observe`` / ``screenshot`` may spend walking the accessibility trees
    #: (``observe_kwargs={"time_budget": ...}`` overrides; ``None`` = no limit). Past it
    #: the observation is returned partial, naming the windows it could not finish.
    OBSERVE_BUDGET: Optional[float] = 8.0

    def __init__(
        self,
        desktop: "Desktop",
        policy: Optional[Policy] = None,
        audit: Optional[AuditLog] = None,
        observe_kwargs: Optional[Dict[str, Any]] = None,
        *,
        find_timeout: float = 5.0,
        feedback: bool = True,
        launch_kwargs: Optional[Dict[str, Any]] = None,
    ):
        self.desktop = desktop
        self.policy = policy if policy is not None else Policy.default()
        self.audit = audit if audit is not None else AuditLog()
        self.observe_kwargs = dict(observe_kwargs or {})
        self.find_timeout = float(find_timeout)
        self.feedback = bool(feedback)
        #: Extra keyword arguments for ``Desktop.launch`` in the ``launch_app`` tool
        #: (``subprocess.Popen`` options, e.g. ``{"stdout": subprocess.DEVNULL}``).
        self.launch_kwargs: Dict[str, Any] = dict(launch_kwargs or {})
        # Fail at construction, not on the first launch: options such as shell=True or
        # executable= would let an allow-listed command run something else.
        check_launch_options(self.launch_kwargs, self.policy)
        self.last_observation: Optional[Observation] = None
        self._local = threading.local()
        self.registry = ToolRegistry()
        self._build()

    # ------------------------------------------------------------- registry api
    def call(self, name: str, args: Optional[Dict[str, Any]] = None) -> ToolResult:
        return self.registry.call(name, args)

    def to_anthropic(self) -> List[Dict[str, Any]]:
        return self.registry.to_anthropic()

    def to_openai(self) -> List[Dict[str, Any]]:
        return self.registry.to_openai()

    def to_mcp(self) -> List[Dict[str, Any]]:
        return self.registry.to_mcp()

    def names(self) -> List[str]:
        return self.registry.names()

    # ------------------------------------------------------------------- build
    _TOOLS: Tuple[Tuple[str, str], ...] = (
        ("observe", "tool_observe"),
        ("screenshot", "tool_screenshot"),
        ("click", "tool_click"),
        ("double_click", "tool_double_click"),
        ("right_click", "tool_right_click"),
        ("type_text", "tool_type_text"),
        ("press_keys", "tool_press_keys"),
        ("hotkey", "tool_hotkey"),
        ("scroll", "tool_scroll"),
        ("move_mouse", "tool_move_mouse"),
        ("drag", "tool_drag"),
        ("set_value", "tool_set_value"),
        ("find_elements", "tool_find_elements"),
        ("wait_for", "tool_wait_for"),
        ("list_windows", "tool_list_windows"),
        ("focus_window", "tool_focus_window"),
        ("launch_app", "tool_launch_app"),
        ("read_clipboard", "tool_read_clipboard"),
        ("write_clipboard", "tool_write_clipboard"),
        ("read_text", "tool_read_text"),
        ("wait", "tool_wait"),
        ("browser_pages", "tool_browser_pages"),
        ("browser_launch", "tool_browser_launch"),
        ("browser_observe", "tool_browser_observe"),
        ("browser_click", "tool_browser_click"),
        ("browser_type", "tool_browser_type"),
        ("browser_navigate", "tool_browser_navigate"),
        ("browser_read", "tool_browser_read"),
    )

    #: Built-in tools that do not go through ``_act`` (so ``_wrap`` runs their policy check).
    _QUERY_TOOLS: frozenset = frozenset(
        {"observe", "screenshot", "find_elements", "wait_for", "list_windows", "read_clipboard", "read_text", "wait",
         "browser_pages", "browser_observe", "browser_read"}
    )

    def _build(self) -> None:
        for name, attr in self._TOOLS:
            method = getattr(self, attr)
            spec = ToolSpec.from_function(method, name=name, read_only=self.policy.is_read_only(name))
            spec.fn = self._wrap(name, method)
            self.registry.add(spec)

    def _redact(self, *arg_names: str) -> None:
        """Keep these arguments of the running tool call (a password) out of the audit log."""
        pending = getattr(self._local, "redact", None)
        if pending is not None:
            pending.update(arg_names)

    def _wrap(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(**kwargs: Any) -> ToolResult:
            self._local.redact = redact = set()
            started = time.monotonic()
            status = "ok"
            result: Optional[ToolResult] = None
            error: Optional[str] = None
            try:
                if self.policy.is_read_only(name):
                    self.policy.check(name, kwargs, context={})
                elif name in self._QUERY_TOOLS:
                    # A query tool the policy does not treat as read-only never reaches
                    # ``_act``: check (and count) it here, or it would run unchecked.
                    self.policy.check(name, kwargs, context=self._context(description=f"run {name}"))
                # Never act on a self-healed (guessed) element from the LLM tools: the model
                # re-observes instead, whatever Desktop(heal_selectors=...) says.
                from ..heal import suppressed as _no_heal

                with _no_heal():
                    result = _to_result(method(**kwargs))
                if result.is_error:
                    status = "error"
                elif isinstance(result.data, dict) and result.data.get("dry_run"):
                    status = "dry_run"
                return result
            except PolicyViolation as exc:
                status, error = "blocked", str(exc)
                raise
            except FailSafeTriggered as exc:
                status, error = "aborted", str(exc)
                raise
            except Exception as exc:
                status, error = "error", f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self._local.redact = None
                # The shared selector trees of this call hold accessibility (COM) nodes; on a
                # long-lived worker thread they would stay referenced until the next selector.
                from ..selectors import release_trees

                release_trees()
                event: Dict[str, Any] = {
                    "event": "tool_call",
                    "tool": name,
                    "args": {k: (_REDACTED if k in redact and v is not None else v) for k, v in kwargs.items()},
                    "status": status,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
                if result is not None:
                    event["result"] = result.text[:500]
                    event["image"] = bool(result.image_base64)
                if error is not None:
                    event["error"] = error
                try:
                    self.audit.record(event)
                except Exception as exc:  # noqa: BLE001 - never turn a performed action into an error
                    # The action already happened (or was refused): reporting the audit
                    # failure as the tool's error would invite a retry -- a second payment.
                    _log.error("Audit log write failed for tool %r: %s", name, exc)
                    if result is not None:
                        result.text = (result.text or "OK") + (
                            f"\n[AUDIT LOG WRITE FAILED: {type(exc).__name__}: {exc}. The action above was "
                            "performed (or refused) as reported; do not repeat it.]"
                        )

        wrapped.__name__ = name
        # The tool's real signature, not ``**kwargs``: ToolRegistry.call rejects unknown
        # arguments with a usable message only for functions that take no ``**kwargs``.
        wrapped.__signature__ = _signature(method)  # type: ignore[attr-defined]
        return wrapped

    # ----------------------------------------------------------------- helpers
    def _mapping(self) -> Observation:
        if self.last_observation is not None:
            return self.last_observation
        vr = self.desktop.virtual_rect()
        return Observation(None, [], [], None, Point(0, 0), (vr.width, vr.height), 1.0, None, region=vr)

    def _ensure_observation(self) -> Observation:
        if self.last_observation is None:
            obs = self._mapping()
            # Elements added later (find_elements / wait_for before any observe) must get
            # selectors proven unique across every window, not just their own one.
            try:
                obs._roots = list(self.desktop.backend.accessibility_roots()) or None
            except Exception:
                obs._roots = None
            obs._prefix_cache = []
            self.last_observation = obs
        return self.last_observation

    def _screen_point(self, x: Any, y: Any) -> Point:
        if x is None or y is None:
            raise ValueError("Both x and y are required for a coordinate target")
        obs = self._mapping()
        iw, ih = obs.image_size
        if not (-2 <= float(x) <= iw + 1 and -2 <= float(y) <= ih + 1):
            raise ValueError(f"Coordinates ({x}, {y}) are outside the screenshot ({iw}x{ih})")
        return obs.to_screen(float(x), float(y))

    def _window_for(self, handle: Any) -> Optional[WindowInfo]:
        if handle is None:
            return None
        try:
            for w in self.desktop.backend.list_windows():
                if w.handle == handle:
                    return w
        except Exception:
            return None
        return None

    def _node_info(self, node: Any) -> Tuple[str, str]:
        """``(name, app)`` of an accessibility node (app via its window).

        ``name`` is the policy label (:func:`~locum._nodeinfo.target_label`):
        the node's name plus its enclosing actionable control's, so an unnamed
        icon inside a "Pay" button is still treated as "Pay".
        """
        name = ""
        app = ""
        try:
            name = target_label(node)
        except Exception:
            pass
        try:
            app = node_app(self.desktop.backend, node)
        except Exception:
            pass
        return name, app

    def _point_apps(self, point: Point, node: Any) -> Tuple[str, ...]:
        """Apps a click at ``point`` may land in: the hit-tested node's and the front-most window's there.

        Both are reported (when known) so a node whose window cannot be
        resolved never lets the policy fall back to the *active* app.
        """
        apps: List[str] = []
        if node is not None:
            a = self._node_info(node)[1]
            if a:
                apps.append(a)
        w = window_at(self.desktop.backend, point)
        if w is not None and w.app and w.app not in apps:
            apps.append(w.app)
        return tuple(apps)

    def _window_title(self, node: Any = None, point: Optional[Point] = None) -> str:
        """Title of the window an action on ``node`` / at ``point`` lands in (``""`` if unknown).

        A point is judged by the front-most window there (that is where the
        click goes); an element by the window that owns it.
        """
        backend = self.desktop.backend
        w = None
        if point is not None:
            w = window_at(backend, point)
        if w is None and node is not None:
            try:
                w = owning_window(backend, node)
            except Exception:
                w = None
        return str(w.title or "") if w is not None else ""

    def _focus_target(self, name_it: bool, enter: bool = False, *, activating: Optional[bool] = None) -> Optional[_Target]:
        """The focused element as a policy target for keyboard actions.

        Keyboard input goes to the focused control, so its application is always
        reported (``target_app``) and, when ``name_it`` is true (keys that can
        activate it, or typed text), its name too -- so confirmation patterns
        cannot be sidestepped by tabbing to a control and pressing Enter.
        ``enter``: the input includes Enter, which in a single-line field
        activates the form's default button (see :func:`_with_default_button`).
        ``activating`` (default: ``name_it``): the input can press the focused
        control; if the focus cannot be read, the target is then marked unknown
        (``Policy(unknown_target=...)``) instead of letting Enter/Space through unchecked.
        """
        try:
            node = self.desktop.backend.focused_element()
        except Exception:
            node = None
        if node is None:
            unknown = name_it if activating is None else activating
            return _Target(description="the focused control (unknown)", unknown=True) if unknown else None
        name, app = self._node_info(node)
        try:
            role = str(node.role or "")
        except Exception:
            role = ""
        desc = f"the focused {role or 'element'}" + (f" {name!r}" if name else "")
        t = _Target(description=desc, name=name if name_it else "", app=app, secret=is_password(node), node=node,
                    window=self._window_title(node))
        if enter:
            t = _with_default_button(t, node)
        acts = name_it if activating is None else activating
        # Input that can press the focused control, which has no usable identity
        # (an icon-only button): no label rule can judge it, so it is an unknown
        # target. Typing (no Enter) into a text field only edits that field.
        if acts and not (is_text_input(node) and not enter) and not (target_identity(node) or t.name):
            t = dataclasses.replace(t, unknown=True)
        return t

    def _element_from_observed(self, oe: ObservedElement) -> "Element":
        from ..element import Element

        return Element(oe.node, self.desktop, oe.selector or None)

    def _find_by_text(self, text: str) -> Tuple[Optional["Element"], Optional[Point]]:
        """Accessibility lookup by visible text (interactive elements first), then OCR."""
        from ..selectors import quote

        for sel in (f"*[text={quote(text)}]", f"*:has-text({quote(text)})"):
            try:
                found = [e for e in self.desktop.find_all(sel) if e.is_visible]
            except Exception:
                found = []
            found = [e for e in found if e.role != "window"]
            if found:
                from .observe import _is_interactive

                found.sort(key=lambda e: (not _is_interactive(e.node), (e.bounds.area if e.bounds else 1 << 60)))
                return found[0], None
        try:
            match: Optional[Match] = self.desktop.find_text(text, timeout=0)
        except NotSupported as exc:
            raise ElementNotFound(f"No element with text {text!r} in the accessibility tree, and OCR is unavailable ({exc})") from exc
        if match is None:
            raise ElementNotFound(f"No element or on-screen text matching {text!r}")
        return None, match.center

    def _target(
        self,
        element_id: Optional[int] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        selector: Optional[str] = None,
        text: Optional[str] = None,
        *,
        required: bool = True,
        activates: bool = False,
    ) -> _Target:
        given = [k for k, v in (("element_id", element_id), ("x/y", x if x is not None else y), ("selector", selector), ("text", text)) if v is not None]
        if len(given) > 1:
            raise ValueError(f"Give only one target, got {', '.join(given)}")
        if not given:
            if required:
                raise ValueError("A target is required: element_id, x+y, selector or text")
            return _Target(point=None, description="the current mouse position")
        if element_id is not None:
            oe = self._mapping().element(element_id)
            el = self._element_from_observed(oe)
            label = f"element [{oe.id}] {oe.role}" + (f" {oe.name!r}" if oe.name else "")
            name = self._node_info(oe.node)[0] or oe.name or (oe.value or "")
            return self._element_target(_Target(element=el, description=label, name=name,
                                                app=oe.app or self._node_info(oe.node)[1], observed=oe,
                                                node=oe.node), activates)
        if selector is not None:
            el = self.desktop.find(selector, timeout=self.find_timeout)
            name, app = self._node_info(el.node)
            return self._element_target(_Target(element=el, description=f"{el.role} {name!r} (selector {selector!r})",
                                                name=name or selector, app=app, node=el.node), activates)
        if text is not None:
            el, point = self._find_by_text(text)
            if el is not None:
                name, app = self._node_info(el.node)
                return self._element_target(_Target(element=el, description=f"{el.role} {name!r} (text {text!r})",
                                                    name=name or text, app=app, node=el.node), activates)
            assert point is not None
            node = self._safe_element_at(point)
            apps = self._point_apps(point, node)
            label = self._node_info(node)[0] if node is not None else ""
            name = text if not label or label == text else f"{text} < {label}"
            return _Target(point=point, description=f"on-screen text {text!r} at ({point.x}, {point.y})", name=name,
                           app=apps[0] if apps else "", apps=apps, unknown=activates and node is None, node=node,
                           window=self._window_title(node, point))
        return self._point_target(self._screen_point(x, y), f"({x}, {y})", activates=activates)

    def _element_target(self, t: _Target, activates: bool) -> _Target:
        """``t`` (an element target) judged by what a mouse action on it really reaches.

        The mouse lands at the element's centre, which may lie on a child control:
        clicking a group/toolbar/pane (or typing into it, which clicks to focus)
        whose centre is a "Pay now" button presses "Pay now". For ``activates``
        actions that child's label is added to the policy target and shown in
        the description.
        """
        el = t.element
        assert el is not None
        t = dataclasses.replace(t, secret=is_password(el.node), node=el.node, window=self._window_title(el.node))
        if not activates:
            return t
        try:
            point = el._click_point((0, 0), require=False)
        except Exception:
            point = None
        # The element must be identifiable by itself (name, automation id, value) or by a named
        # child under the click point; the selector text the caller used is not an identity
        # (``pane > button:nth(3)`` names no control).
        identified = bool(target_identity(el.node, point, self.desktop.backend))
        if not identified:
            t = dataclasses.replace(t, unknown=True)
        if point is None:
            return t
        hit = self._safe_element_at(point)
        if hit is None:
            return t
        try:
            if hit.same_as(el.node) or not any(a.same_as(el.node) for a in ancestors(hit)):
                return t  # the element itself, or something covering it (Element refuses to click that)
        except Exception:
            return t
        label = self._node_info(hit)[0]
        pressed = pressed_control(hit, el.node)
        if pressed is not el.node:
            # An actionable child receives the press ("Pay now" in a toolbar): allow rules and
            # the identity rule judge that control, not the container that was asked for.
            t = dataclasses.replace(t, node=pressed, unknown=not target_identity(pressed, point, self.desktop.backend))
        elif label and not identified:
            t = dataclasses.replace(t, unknown=False)
        if not label or label in t.name:
            return t
        try:
            role = str(hit.role or "element")
        except Exception:
            role = "element"
        return dataclasses.replace(
            t,
            name=f"{t.name} > {label}" if t.name else label,
            description=f"{t.description} (the mouse lands on its child {role} {label!r})",
        )

    def _point_target(self, point: Point, description: str, *, activates: bool = False) -> _Target:
        """A screen point as a policy target: the hit-tested element's label and every app there.

        ``activates``: the action can press what is there (a left click); when
        nothing can be hit-tested the target is marked unknown (``Policy(unknown_target=...)``).
        """
        node = self._safe_element_at(point)
        name = self._node_info(node)[0] if node is not None else ""
        apps = self._point_apps(point, node)
        desc = description + (f" on {node.role} {name!r}" if node is not None and name else "")
        # Nothing hit-testable there, or a control with no usable identity (no name,
        # automation id, text value or named descendant at the point): unknown target.
        unknown = activates and (node is None or not target_identity(node, point, self.desktop.backend))
        return _Target(point=point, description=desc, name=name, app=apps[0] if apps else "", apps=apps,
                       unknown=unknown, node=node, window=self._window_title(node, point))

    def _safe_element_at(self, point: Point) -> Any:
        try:
            return self.desktop.backend.element_at(point.x, point.y)
        except Exception:
            return None

    def _target_point(self, t: _Target) -> Optional[Point]:
        if t.point is not None:
            return t.point
        if t.element is not None:
            b = t.element.bounds
            return b.center if b is not None else None
        return None

    def _context(self, t: Optional[_Target] = None, **extra: Any) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {}
        try:
            aw = self.desktop.backend.active_window()
        except Exception:
            aw = None
        if aw is not None:
            ctx["app"] = aw.app
            ctx["window_title"] = aw.title
        if t is not None:
            if t.name:
                ctx["target"] = t.name
            if t.app:
                ctx["target_app"] = t.app
            if t.apps:
                ctx["target_apps"] = list(t.apps)
            p = self._target_point(t)
            if p is not None:
                ctx["point"] = p
            if t.element is not None and isinstance(t.element.selector, str):
                ctx["selector"] = t.element.selector
            if t.node is not None:
                ctx["target_node"] = t.node
            if t.window:
                ctx["target_window"] = t.window
            if t.unknown:
                ctx["target_unknown"] = True
        ctx.update({k: v for k, v in extra.items() if v is not None})
        return ctx

    def _act(self, tool: str, args: Dict[str, Any], ctx: Dict[str, Any], description: str, action: Callable[[], Any],
             *, feedback: bool = True, bind: Optional[_input_target.InputTarget] = None) -> ToolResult:
        """Policy check, then dry-run report or the real action (+ feedback).

        ``bind``: the window the policy judged (the foreground window for keyboard input
        to the focused control, the window under the point for a coordinate click). The
        input is only sent while it still goes there (see :mod:`locum._target`): a window
        that takes the focus between the check and the input -- or during it -- gets none.
        """
        ctx.setdefault("description", description)
        self.policy.check(tool, {k: v for k, v in args.items() if v is not None}, context=ctx)
        if self.policy.dry_run:
            note = f" (this would require human confirmation: {ctx['needs_confirmation']})" if ctx.get("needs_confirmation") else ""
            return ToolResult(f"[dry-run] Would {description}{note}. Nothing was done.", data={"dry_run": True, "description": description})
        with _input_target.bind(bind):
            out = action()
        text = out if isinstance(out, str) and out else description[0].upper() + description[1:] + "."
        return ToolResult(text + (self._feedback_text() if feedback else ""))

    def _bind_foreground(self) -> Optional[_input_target.InputTarget]:
        """Keyboard input for "the focused control": bound to the window that has the
        focus now, while the policy judges that control."""
        be = self.desktop.backend
        try:
            handle = be.foreground_handle()
        except Exception:
            return None
        if handle is None:
            return None
        return _input_target.InputTarget(be, handle, title="the window that had the focus when this action was "
                                         "checked", strict=True)

    def _bind_point(self, point: Optional[Point]) -> Optional[_input_target.InputTarget]:
        """Pointer input at a coordinate: bound to the window under it now."""
        if point is None:
            return None
        be = self.desktop.backend
        try:
            handle = be.window_handle_at(int(point.x), int(point.y))
        except Exception:
            return None
        if handle is None:
            return None
        return _input_target.InputTarget(be, handle, title="the window under the point when this action was checked",
                                         strict=True)

    def _recheck_focus(self, tool: str, args: Dict[str, Any], activates: bool, description: str, *, enter: bool = False,
                       activating: Optional[bool] = None, **extra: Any) -> None:
        """Re-run the policy for the next part of a keyboard call after focus may have moved.

        ``press_keys(["tab", "enter"])`` or typed ``"1000\t\n"`` checked only against
        the control focused *before* the call would press Enter on whatever the Tab
        reached (a "Pay" button) unchecked. Not counted against the action budget.
        """
        ctx = self._context(self._focus_target(activates, enter, activating=activating), **extra)
        ctx["description"] = description
        try:
            self.policy.check(tool, {k: v for k, v in args.items() if v is not None}, context=ctx, count=False)
        except PolicyViolation as exc:
            raise PolicyViolation(f"{exc} (the input before it in this call was already sent)") from None

    def _feedback_text(self) -> str:
        if not self.feedback:
            return ""
        parts = []
        try:
            aw = self.desktop.backend.active_window()
            if aw is not None:
                parts.append(f"active window {aw.title!r}")
        except Exception:
            pass
        try:
            node = self.desktop.backend.focused_element()
            if node is not None:
                from ..selectors import describe_node

                parts.append("focus: " + describe_node(_MaskedValue(node) if is_password(node) else node, include_bounds=False))
        except Exception:
            pass
        return (" [" + "; ".join(parts) + "]") if parts else ""

    # =================================================================== tools
    def tool_observe(self, window: Optional[str] = None, include_screenshot: bool = True,
                     changes_only: bool = False) -> ToolResult:
        """Look at the screen: returns the visible interactive elements with numeric ids, the windows,
        and a screenshot with each element's id drawn in a box (set-of-marks).

        Call this first and again whenever the screen may have changed. Element ids are only valid
        until the next observe/screenshot call. Coordinates refer to the returned screenshot's pixels.

        Args:
            window: Only observe the window whose title matches (substring/glob); default: whole screen.
            include_screenshot: Include the annotated screenshot image.
            changes_only: After an earlier observe of the same area, return only what changed:
                new, changed and gone elements (elements that are still there keep their ids)
                and a screenshot cropped to the changed area, or none if nothing visible changed.
                Much smaller than a full observation; use it after each action.
        """
        kw = dict(self.observe_kwargs)
        kw["screenshot"] = bool(include_screenshot)
        kw.setdefault("time_budget", self.OBSERVE_BUDGET)
        prev = self.last_observation
        kw.setdefault("reuse", prev)  # unchanged windows are not re-walked (Windows: UIA change events)
        self._prune_browser(kw)
        obs = observe(self.desktop, window=window, **kw)
        if changes_only:
            if prev is not None and prev.region == obs.region and prev.image_size == obs.image_size and prev.elements is not obs.elements:
                return self._observe_changes(prev, obs, include_screenshot)
            obs.notes.append("changes_only: there is no earlier observation of the same area to compare with, "
                             "so this is a full observation; later calls will return only changes.")
        self.last_observation = obs
        img = obs.image_base64(annotated=True) if obs.screenshot is not None else None
        return ToolResult(obs.text(kw.get("max_elements", 300)), image_base64=img, data=obs)

    def _prune_browser(self, kw: Dict[str, Any]) -> None:
        """While a browser is connected over DevTools, observe skips its page content (use browser_observe)."""
        b = getattr(self, "_browser", None)
        if b is None or "prune_documents" in kw:
            return
        try:
            pid = b.pid if b.alive else None
        except Exception:
            pid = None
        if pid:
            kw["prune_documents"] = [pid]

    def _observe_changes(self, prev: Observation, obs: Observation, include_screenshot: bool) -> ToolResult:
        from ..vision import crop as _crop, to_base64_png
        from .obsdiff import compare

        diff = compare(prev, obs)
        self.last_observation = obs
        img = None
        if include_screenshot and obs.screenshot is not None:
            src = obs.annotated if obs.annotated is not None else obs.screenshot
            if diff.full_image:
                img = to_base64_png(src)
            elif diff.crop is not None:
                img = to_base64_png(_crop(src, diff.crop))
        return ToolResult(diff.text(obs), image_base64=img, data=obs)

    def tool_screenshot(self, annotated: bool = False) -> ToolResult:
        """Take a screenshot of the whole screen (also refreshes element ids and the coordinate space).

        Args:
            annotated: Draw numbered element boxes on the image (like observe).
        """
        kw = dict(self.observe_kwargs)
        kw["screenshot"] = True
        kw["annotate"] = bool(annotated)
        kw.setdefault("time_budget", self.OBSERVE_BUDGET)
        kw.setdefault("reuse", self.last_observation)
        self._prune_browser(kw)
        obs = observe(self.desktop, **kw)
        self.last_observation = obs
        w, h = obs.image_size
        return ToolResult(
            f"Screenshot {w}x{h} (screen {obs.screen_size[0]}x{obs.screen_size[1]}, scale {obs.scale:.4g}); "
            f"{len(obs.elements)} element ids refreshed." + "".join(f"\nNote: {n}" for n in obs.notes),
            image_base64=obs.image_base64(annotated=annotated),
            data=obs,
        )

    def _click(self, tool: str, button: str, clicks: int, element_id: Optional[int], x: Optional[float], y: Optional[float],
               selector: Optional[str], text: Optional[str]) -> ToolResult:
        if clicks < 1 or clicks > 3:
            raise ValueError("clicks must be 1, 2 or 3")
        button = (button or "left").lower()
        if button not in ("left", "right", "middle"):
            raise ValueError("button must be left, right or middle")
        t = self._target(element_id, x, y, selector, text, activates=button == "left")
        verb = {1: "click", 2: "double-click", 3: "triple-click"}[clicks]
        if button != "left":
            verb = f"{button}-{verb}"
        description = f"{verb} {t.description}"
        args = {"element_id": element_id, "x": x, "y": y, "selector": selector, "text": text, "button": button, "clicks": clicks}

        def run() -> str:
            if t.element is not None:
                el = t.element
                if button == "left" and clicks == 1:
                    el.click(native="auto")
                elif button == "left" and clicks == 2:
                    el.double_click()
                elif button == "right" and clicks == 1:
                    el.right_click()
                else:
                    el.click(button, clicks, native=False)
            else:
                assert t.point is not None
                self.desktop.click(t.point, button=button, clicks=clicks)
            return ""

        bind = self._bind_point(t.point) if t.element is None else None
        return self._act(tool, args, self._context(t), description, run, bind=bind)

    def tool_click(
        self,
        element_id: Optional[int] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        selector: Optional[str] = None,
        text: Optional[str] = None,
        button: str = "left",
        clicks: int = 1,
    ) -> ToolResult:
        """Click a target. Give exactly one of: element_id (from observe), x+y (screenshot pixels),
        selector (e.g. 'button "OK"', 'edit#username', 'window "Invoice" button:has-text("Save")'),
        or text (visible label of the element).

        Args:
            element_id: Id of an element from the last observe call (preferred).
            x: X coordinate in the last screenshot's pixels.
            y: Y coordinate in the last screenshot's pixels.
            selector: Accessibility selector of the element.
            text: Visible text/label of the element to click.
            button: Mouse button: left, right or middle.
            clicks: Number of clicks (1-3).
        """
        return self._click("click", button, int(clicks), element_id, x, y, selector, text)

    def tool_double_click(
        self,
        element_id: Optional[int] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        selector: Optional[str] = None,
        text: Optional[str] = None,
    ) -> ToolResult:
        """Double-click a target (element_id, x+y, selector or text -- exactly one).

        Args:
            element_id: Id of an element from the last observe call.
            x: X coordinate in the last screenshot's pixels.
            y: Y coordinate in the last screenshot's pixels.
            selector: Accessibility selector of the element.
            text: Visible text/label of the element.
        """
        return self._click("double_click", "left", 2, element_id, x, y, selector, text)

    def tool_right_click(
        self,
        element_id: Optional[int] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        selector: Optional[str] = None,
        text: Optional[str] = None,
    ) -> ToolResult:
        """Right-click a target to open its context menu (element_id, x+y, selector or text -- exactly one).

        Args:
            element_id: Id of an element from the last observe call.
            x: X coordinate in the last screenshot's pixels.
            y: Y coordinate in the last screenshot's pixels.
            selector: Accessibility selector of the element.
            text: Visible text/label of the element.
        """
        return self._click("right_click", "right", 1, element_id, x, y, selector, text)

    def tool_type_text(
        self,
        text: str,
        element_id: Optional[int] = None,
        selector: Optional[str] = None,
        clear: bool = False,
        press_enter: bool = False,
    ) -> ToolResult:
        """Type text. If element_id or selector is given, that field is focused first; otherwise the text
        goes to the currently focused control.

        Args:
            text: The text to type (unicode is fine; \\n presses Enter).
            element_id: Id of a text field from the last observe call to type into.
            selector: Selector of the text field to type into.
            clear: Replace the field's existing content instead of appending.
            press_enter: Press Enter after typing.
        """
        # The field may be clicked to focus it: judged like a click on it.
        t = self._target(element_id, None, None, selector, None, required=False, activates=True)
        where = f" into {t.description}" if t.element is not None else ""
        # Enter typed into the first control (before any Tab) may submit its form's default button.
        first = split_typed_text(text)[:1]
        keys_in_text = any(c in text for c in "\t\n\r")
        enter_here = (bool(first) and _has_newline(first[0])) or (press_enter and not keys_in_text)
        focus = None
        if t.element is None:
            focus = self._focus_target(True, enter_here, activating=(bool(first) and typed_text_activates(first[0]))
                                       or (press_enter and not keys_in_text))
        secret = t.secret or (focus is not None and focus.secret)
        if secret:
            self._redact("text")
        shown = _REDACTED if secret else text if len(text) <= 60 else text[:57] + "..."
        description = f"type {shown!r}{where}" + (" (replacing its content)" if clear else "") + (" and press Enter" if press_enter else "")
        args = {"text": text, "element_id": element_id, "selector": selector, "clear": clear, "press_enter": press_enter}

        def now_secret() -> None:
            # Text after a Tab may land in a password field: from then on the call is secret.
            nonlocal secret, shown
            if secret:
                return
            try:
                node = self.desktop.backend.focused_element()
                pw = node is not None and is_password(node)
            except Exception:
                pw = False
            if pw:
                secret, shown = True, _REDACTED
                self._redact("text")

        def run() -> str:
            # Text after a Tab/newline may land in another control: each such piece
            # (and the final Enter) is checked against the element focused by then.
            pieces = split_typed_text(text)
            if t.element is not None:
                if clear and not keys_in_text:
                    t.element.set_text(text)
                else:
                    if clear:
                        t.element.set_text("")
                    if pieces:
                        t.element.type(pieces[0])
            else:
                if clear:
                    self.desktop.hotkey("mod+a")
                    self.desktop.press("backspace")
                if pieces:
                    self.desktop.type(pieces[0])
            for piece in pieces[1:]:
                now_secret()
                self._recheck_focus("type_text", args, True, f"type {_REDACTED if secret else piece!r} (part of {shown!r})",
                                    enter=_has_newline(piece), activating=typed_text_activates(piece))
                self.desktop.type(piece)
            if press_enter:
                if keys_in_text:
                    now_secret()
                    self._recheck_focus("type_text", args, True, f"press Enter after typing {shown!r}", enter=True)
                self.desktop.press("enter")
            value = ""
            if t.element is not None and not secret:
                v = t.element.value
                if v is not None:
                    value = f" Field value is now {v!r}."
            return f"Typed {shown!r}{where}." + value

        if t.element is not None:
            ctx = self._context(_with_default_button(t, t.element.node) if enter_here else t)
        else:
            ctx = self._context(focus)
        return self._act("type_text", args, ctx, description, run,
                         bind=self._bind_foreground() if t.element is None else None)

    def tool_press_keys(self, keys: List[str], presses: int = 1) -> ToolResult:
        """Press keys one after another, e.g. ["tab", "tab", "enter"]. An item like "ctrl+a" is
        pressed as a combination. Key names: enter, tab, escape, backspace, delete, up, down, left,
        right, home, end, pageup, pagedown, f1-f24, space, or a single character.

        Args:
            keys: Keys to tap in order.
            presses: Repeat the whole sequence this many times (1-50).
        """
        if not keys:
            raise ValueError("keys must not be empty")
        presses = int(presses)
        if not 1 <= presses <= 50:
            raise ValueError("presses must be between 1 and 50")
        keys = [str(k) for k in keys]
        from ..keys import press_actions

        # Validate before acting. An item like "tab tab enter" is a sequence (as in
        # Keyboard.press): split it so every tap gets its own focus re-check.
        parsed = [parts for k in keys for parts in press_actions(k)]
        for parts in parsed:
            check_activation_combo(parts)
        description = f"press {' '.join(keys)}" + (f" x{presses}" if presses > 1 else "")
        activates = keys_activate(keys)
        focus = self._focus_target(activates, any(_has_enter(parts) for parts in parsed))
        if activates and focus is not None and focus.name:
            description += f" on {focus.description}"
        args = {"keys": keys, "presses": presses}
        combos = " ".join("+".join(parts) for parts in parsed)
        pressed = _shortcut_targets(self.desktop, parsed)
        if pressed:
            description += f" (presses {', '.join(map(repr, pressed))})"

        def run() -> None:
            moved = False
            for _ in range(presses):
                for parts in parsed:
                    k = "+".join(parts)
                    if moved:  # an earlier key may have moved focus: check what this one acts on
                        self._recheck_focus("press_keys", args, keys_activate([parts]), f"press {k} (part of {' '.join(keys)!r})",
                                            enter=_has_enter(parts), step_keys=k)
                    self.desktop.press(parts)
                    moved = moved or keys_may_move_focus(parts)

        return self._act("press_keys", args, self._context(focus, keys=combos, shortcut_targets=pressed or None),
                         description, run, bind=self._bind_foreground())

    def tool_hotkey(self, combo: str) -> ToolResult:
        """Press a keyboard shortcut such as "ctrl+s", "ctrl+shift+t", "alt+tab", "mod+c"
        (mod = Ctrl, or Cmd on macOS).

        Args:
            combo: Keys joined with "+".
        """
        from ..keys import parse_hotkey

        parts = parse_hotkey(combo)
        check_activation_combo(parts)
        activates = keys_activate([combo])
        focus = self._focus_target(activates, _has_enter(parts))
        description = f"press {combo}"
        if activates and focus is not None and focus.name:
            description += f" on {focus.description}"
        pressed = _shortcut_targets(self.desktop, [parts])
        if pressed:
            description += f" (presses {', '.join(map(repr, pressed))})"
        ctx = self._context(focus, keys="+".join(parts), shortcut_targets=pressed or None)
        return self._act("hotkey", {"combo": combo}, ctx, description, lambda: self.desktop.hotkey(combo),
                         bind=self._bind_foreground())

    def tool_scroll(
        self,
        direction: str = "down",
        amount: int = 3,
        element_id: Optional[int] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        selector: Optional[str] = None,
    ) -> ToolResult:
        """Scroll the mouse wheel, optionally over a target (element_id, x+y or selector).

        Args:
            direction: up, down, left or right.
            amount: Number of wheel notches (1-50).
            element_id: Scroll over this element from the last observe call.
            x: X coordinate (screenshot pixels) to scroll at.
            y: Y coordinate (screenshot pixels) to scroll at.
            selector: Scroll over the element matching this selector.
        """
        direction = (direction or "down").lower()
        if direction not in _DIRECTIONS:
            raise ValueError("direction must be up, down, left or right")
        amount = int(amount)
        if not 1 <= amount <= 50:
            raise ValueError("amount must be between 1 and 50")
        t = self._target(element_id, x, y, selector, None, required=False)
        dy = amount if direction == "up" else -amount if direction == "down" else 0
        dx = amount if direction == "right" else -amount if direction == "left" else 0
        description = f"scroll {direction} {amount} notch{'es' if amount != 1 else ''}"
        if t.element is None and t.point is None:
            # The wheel event goes to whatever is under the pointer: judge that point and its app.
            t = self._point_target(self.desktop.position(), "the mouse position")
        else:
            description += f" over {t.description}"
        point = self._target_point(t)
        el = t.element

        def run() -> None:
            if el is not None:
                el.scroll(dy, dx)  # raises rather than scrolling a window that covers the element
            else:
                self.desktop.scroll(dy, dx, at=point)

        args = {"direction": direction, "amount": amount, "element_id": element_id, "x": x, "y": y, "selector": selector}
        return self._act("scroll", args, self._context(t), description, run,
                         bind=self._bind_point(point) if el is None else None)

    def tool_move_mouse(
        self,
        element_id: Optional[int] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        selector: Optional[str] = None,
    ) -> ToolResult:
        """Move the mouse pointer (hover) over a target: element_id, x+y or selector.

        Args:
            element_id: Element id from the last observe call.
            x: X coordinate in screenshot pixels.
            y: Y coordinate in screenshot pixels.
            selector: Accessibility selector.
        """
        t = self._target(element_id, x, y, selector, None)
        point = self._target_point(t)
        if point is None:
            raise ElementNotFound(f"{t.description} has no position on screen")
        args = {"element_id": element_id, "x": x, "y": y, "selector": selector}
        el = t.element

        def run() -> None:
            if el is not None:
                el.hover()  # brings a covered element's window to the front, or raises
            else:
                self.desktop.move(point)

        return self._act("move_mouse", args, self._context(t), f"move the mouse to {t.description}", run)

    def tool_drag(
        self,
        start_element_id: Optional[int] = None,
        start_x: Optional[float] = None,
        start_y: Optional[float] = None,
        end_element_id: Optional[int] = None,
        end_x: Optional[float] = None,
        end_y: Optional[float] = None,
        duration: float = 0.5,
    ) -> ToolResult:
        """Drag with the left button from a start target to an end target (element ids or coordinates).

        Args:
            start_element_id: Element to start the drag on.
            start_x: Start X (screenshot pixels).
            start_y: Start Y (screenshot pixels).
            end_element_id: Element to drop on.
            end_x: End X (screenshot pixels).
            end_y: End Y (screenshot pixels).
            duration: Seconds the drag movement takes (0-5).
        """
        s = self._target(start_element_id, start_x, start_y, None, None)
        e = self._target(end_element_id, end_x, end_y, None, None)
        sp, ep = self._target_point(s), self._target_point(e)
        if sp is None or ep is None:
            raise ElementNotFound("Drag targets must be visible on screen")
        duration = min(max(float(duration), 0.0), 5.0)
        ctx = self._context(s, start_point=sp, end_point=ep, points=[sp, ep])
        # Both endpoints are policy targets: dropping into an app counts as acting in it.
        drag_apps: List[str] = []
        for a in (s.app, *s.apps, e.app, *e.apps):
            if a and a not in drag_apps:
                drag_apps.append(a)
        ctx["target_apps"] = drag_apps
        if e.name:
            ctx["target"] = f"{s.name} -> {e.name}".strip()
        args = {"start_element_id": start_element_id, "start_x": start_x, "start_y": start_y,
                "end_element_id": end_element_id, "end_x": end_x, "end_y": end_y}

        def run() -> None:
            # Element ends are hit-tested like clicks: a window covering one would
            # otherwise receive the press or the drop instead of the checked element.
            start = s.element._mouse_point() if s.element is not None else sp
            end = e.element._mouse_point() if e.element is not None else ep
            # Bringing the drop target's window forward may have covered the start point.
            if s.element is not None:
                covered = s.element._occluded(start)
            else:
                covered = e.element is not None and self._point_apps(start, self._safe_element_at(start)) != s.apps
            if covered:
                raise ElementNotFound(
                    f"The drag start {s.description} is covered once {e.description} is in front; "
                    "refusing to drag from the wrong thing"
                )
            self.desktop.drag(start, end, duration=duration)

        return self._act("drag", args, ctx, f"drag from {s.description} to {e.description}", run,
                         bind=self._bind_point(sp) if s.element is None else None)

    def tool_set_value(self, value: str, element_id: Optional[int] = None, selector: Optional[str] = None) -> ToolResult:
        """Replace the content of a text field (uses the accessibility API when possible, else types).

        Args:
            value: The new text.
            element_id: Element id of the field from the last observe call.
            selector: Selector of the field.
        """
        t = self._target(element_id, None, None, selector, None, activates=True)  # may click to focus it
        assert t.element is not None
        el = t.element
        if t.secret:
            self._redact("value")
        shown = _REDACTED if t.secret else value if len(value) <= 60 else value[:57] + "..."

        def run() -> str:
            if _has_newline(value) or "\t" in value:
                # Typed, a Tab would move focus and the newline press whatever control it
                # reached (unchecked by the policy): only the native set-value path is safe.
                try:
                    el.set_text(value, native=True)
                except NotSupported:
                    raise ValueError(
                        f"{t.description} cannot be set natively and the value contains a Tab or newline, which "
                        "typing would turn into focus moves and key presses; use type_text instead"
                    ) from None
            else:
                el.set_text(value)
            v = None if t.secret else el.value
            return f"Set {t.description} to {shown!r}." + (f" Value is now {v!r}." if v is not None else "")

        args = {"value": value, "element_id": element_id, "selector": selector}
        return self._act("set_value", args, self._context(t), f"set {t.description} to {shown!r}", run)

    def tool_find_elements(
        self,
        selector: Optional[str] = None,
        text: Optional[str] = None,
        role: Optional[str] = None,
        limit: int = 20,
    ) -> ToolResult:
        """Search the accessibility tree; matches get element ids usable with the other tools.

        Args:
            selector: Selector such as 'button', 'edit[name*="mail" i]', 'window "Invoice" checkbox'.
            text: Find elements whose name or value contains this text (case-insensitive).
            role: Restrict to a role (button, edit, checkbox, listitem, menuitem, link, ...).
            limit: Maximum number of results (1-100).
        """
        from ..selectors import quote

        if selector is None and text is None and role is None:
            raise ValueError("Give a selector, text and/or role")
        if selector is not None and (text is not None or role is not None):
            raise ValueError("Use either selector, or text/role")
        if selector is None:
            selector = (role or "*") + (f":has-text({quote(text)})" if text else "")
        limit = min(max(int(limit), 1), 100)
        found = self.desktop.find_all(selector)
        obs = self._ensure_observation()
        lines = []
        # Selectors are shown while they are cheap: each one is proven unique by searching
        # the accessibility tree, which can take seconds on a busy desktop. Past the budget
        # the listing goes on without them (the element ids work either way).
        deadline = time.monotonic() + self.SELECTOR_BUDGET
        skipped = 0
        for el in found[:limit]:
            entry = obs.add_element(el.node)
            sel = ""
            if entry.selector_ready or time.monotonic() < deadline:
                sel = entry.selector
            else:
                skipped += 1
            lines.append(obs.describe_element(entry) + (f"  selector: {sel}" if sel else ""))
        if skipped:
            lines.append(f"(selectors omitted for {skipped} element(s) to stay fast; act on them by element_id)")
        if not found:
            return ToolResult(f"No elements match {selector!r}.")
        more = f"\n... {len(found) - limit} more (raise limit or refine the selector)" if len(found) > limit else ""
        return ToolResult(f"{len(found)} element(s) match {selector!r}:\n" + "\n".join(lines) + more)

    def tool_wait_for(
        self,
        selector: Optional[str] = None,
        text: Optional[str] = None,
        state: str = "visible",
        timeout: float = 10.0,
    ) -> ToolResult:
        """Wait until an element (selector) or text appears, or reaches a state.

        Args:
            selector: Selector of the element to wait for.
            text: Wait for an element/on-screen text containing this text.
            state: visible, enabled, focused, checked, or gone.
            timeout: Maximum seconds to wait (0-120).
        """
        from ..selectors import quote

        if (selector is None) == (text is None):
            raise ValueError("Give exactly one of selector or text")
        timeout = min(max(float(timeout), 0.0), 120.0)
        state = (state or "visible").lower()
        sel = selector if selector is not None else f"*:has-text({quote(text or '')})"
        try:
            if text is not None and state == "visible":
                # Accessibility tree and OCR are polled together within one deadline
                # (one after the other would wait up to twice the timeout).
                found = self._wait_for_text(sel, text, timeout)
                if isinstance(found, Match):
                    p = self._mapping().to_image(*found.center)
                    return ToolResult(f"Text {text!r} is visible at ({p.x}, {p.y}).")
                el = found
            else:
                el = self.desktop.wait(sel, state=state, timeout=timeout)
        except (FailSafeTriggered, PolicyViolation):
            raise
        except (ElementNotFound, LocumError) as exc:
            return ToolResult.error(f"Timed out after {timeout:g}s waiting for {sel!r} to be {state}: {exc}")
        if el is None:
            return ToolResult(f"{sel!r} is gone.")
        if isinstance(el, Match):  # the selector named an image file
            p = self._mapping().to_image(*el.center)
            return ToolResult(f"{sel!r} is visible at ({p.x}, {p.y}).")
        obs = self._ensure_observation()
        entry = obs.add_element(el.node)
        return ToolResult(f"{sel!r} is {state}: " + obs.describe_element(entry))

    def _wait_for_text(self, sel: str, text: str, timeout: float) -> Any:
        """Poll the accessibility tree (``sel``) and OCR (``text``) until either shows the text.

        Returns the visible :class:`Element` or the OCR :class:`Match`; raises
        :class:`ActionTimeout` after ``timeout`` seconds.
        """
        ocr = [True]

        def probe() -> Any:
            try:
                return self.desktop.wait(sel, state="visible", timeout=0)
            except (FailSafeTriggered, PolicyViolation):
                raise
            except LocumError:
                pass
            if ocr[0]:
                try:
                    return self.desktop.find_text(text, timeout=0)
                except NotSupported:
                    ocr[0] = False  # no OCR engine: keep polling the accessibility tree only
            return None

        try:
            return self.desktop.wait_until(probe, timeout, getattr(self.desktop, "poll_interval", 0.1), ignore_exceptions=False)
        except ActionTimeout:
            raise ActionTimeout(
                f"No element or on-screen text matching {text!r} within {timeout:g}s" + ("" if ocr[0] else " (no OCR engine)")
            ) from None

    def tool_list_windows(self) -> ToolResult:
        """List the open top-level windows (front-most first) with their app and position."""
        obs = self._mapping()
        wins = self.desktop.backend.list_windows()
        if not wins:
            return ToolResult("No windows are open.")
        lines = []
        for w in wins:
            r = obs.rect_to_image(w.rect)
            flags = [f for f, on in (("active", w.is_active), ("minimized", w.is_minimized), ("maximized", w.is_maximized)) if on]
            lines.append(
                f"- {w.title!r} app={w.app or '?'} at ({r.left}, {r.top}) size {r.width}x{r.height}"
                + (f" [{', '.join(flags)}]" if flags else "")
            )
        return ToolResult("\n".join(lines), data=[w.to_dict() for w in wins])

    def tool_focus_window(self, title: str) -> ToolResult:
        """Bring a window to the front and give it keyboard focus.

        Args:
            title: Window title (exact, substring or glob such as "*Notepad").
        """
        win = self.desktop.window(title, timeout=min(self.find_timeout, 5.0))
        ctx = self._context(target=win.title, target_app=win.app, target_window=win.title)
        return self._act("focus_window", {"title": title}, ctx, f"focus window {win.title!r}", lambda: win.activate())

    def tool_launch_app(self, command: str, wait_window: Optional[str] = None, timeout: float = 15.0) -> ToolResult:
        """Start an application.

        Args:
            command: Command line, e.g. "notepad.exe", "gnome-calculator", "open -a TextEdit".
            wait_window: Optionally wait for a window with this title to appear.
            timeout: Seconds to wait for the window (0-120).
        """
        timeout = min(max(float(timeout), 0.0), 120.0)

        def run() -> str:
            check_launch_options(self.launch_kwargs, self.policy)  # launch_kwargs/policy may change after init
            res = self.desktop.launch(command, wait_window=wait_window, timeout=timeout, **self.launch_kwargs)
            if wait_window is not None:
                return f"Launched {command!r}; window {getattr(res, 'title', wait_window)!r} is open."
            return f"Launched {command!r}."

        args = {"command": command, "wait_window": wait_window, "timeout": timeout}
        return self._act("launch_app", args, self._context(command=command, target=command), f"launch {command!r}", run)

    # ======================================================== browser (CDP)
    def _browser_conn(self) -> Any:
        """The DevTools connection (attaching to a browser with an open port on first use)."""
        from ..browser import Browser, CDPError

        b = getattr(self, "_browser", None)
        if b is not None and b.alive:
            return b
        self._browser = None
        try:
            self._browser = Browser.attach()
        except CDPError as exc:
            raise CDPError(f"{exc} (browser_launch starts a locum-managed browser)") from None
        return self._browser

    def _browser_page(self, tab: Optional[str]) -> Any:
        b = self._browser_conn()
        return b, b.page(tab)

    def _browser_ctx(self, b: Any, page: Any, target: Optional[str] = None) -> Dict[str, Any]:
        return self._context(app=b.app, window_title=page.info.title, target=target, target_app=b.app,
                             target_apps=[b.app], url=page.info.url)

    def tool_browser_pages(self) -> ToolResult:
        """List the tabs of the browser locum is connected to over DevTools (Edge/Chrome).

        Connects on first use to a browser that exposes a DevTools port; see browser_launch.
        """
        b = self._browser_conn()
        pages = b.pages()
        kind = "locum-managed profile" if b.managed else "attached"
        lines = [f"Browser: {b.version.get('Browser', b.name)} ({kind}, DevTools port {b.port})"]
        lines += [f"  {i}. {p.title!r} <{p.url}>" for i, p in enumerate(pages, 1)] or ["  (no tabs)"]
        lines.append("Use browser_observe(tab=...) with part of a title or URL (default: the first tab).")
        return ToolResult("\n".join(lines))

    def tool_browser_launch(self, browser: str = "edge", url: Optional[str] = None, headless: bool = False) -> ToolResult:
        """Start Edge or Chrome with a separate, locum-managed profile and a DevTools port, so its pages
        can be read and driven with the browser_* tools (much faster than the screen's accessibility tree).
        Reuses that browser if it is already running. Your everyday browser profile is not touched.

        Args:
            browser: "edge", "chrome" or "chromium".
            url: Page to open.
            headless: Run without a window.
        """
        from ..browser import Browser, find_executable

        name = str(browser or "edge").lower()
        app = {"edge": "msedge.exe", "chrome": "chrome.exe", "chromium": "chrome.exe"}.get(name, name)
        if sys.platform != "win32":
            app = {"edge": "Microsoft Edge", "chrome": "Google Chrome", "chromium": "Chromium"}.get(name, name)
        exe = find_executable(name) or name

        def run() -> str:
            self._browser = Browser.launch(name, url=url, headless=bool(headless))
            pages = self._browser.pages()
            first = f" Tab: {pages[0].title!r} <{pages[0].url}>." if pages else ""
            return f"Started {name} (locum-managed profile, DevTools port {self._browser.port}).{first}"

        args = {"browser": name, "url": url, "headless": bool(headless)}
        ctx = self._context(command=exe, target=f"{name} (locum profile)" + (f" {url}" if url else ""),
                            target_app=app, target_apps=[app])
        return self._act("browser_launch", args, ctx, f"start {name} with a locum-managed profile", run, feedback=False)

    def tool_browser_observe(self, tab: Optional[str] = None, max_elements: int = 150, include_text: bool = True,
                             include_screenshot: bool = False) -> ToolResult:
        """Read a browser page over DevTools: its interactive elements (with ids for browser_click /
        browser_type), title, URL and visible text. Ids are valid until the next browser_observe.

        Args:
            tab: Part of the tab's title or URL (default: the first tab).
            max_elements: Maximum elements listed (1-500).
            include_text: Include the page's text (first 3000 characters).
            include_screenshot: Include a screenshot of the page.
        """
        import base64

        b, page = self._browser_page(tab)
        snap = page.snapshot(min(max(int(max_elements), 1), 500))
        lines = [f"Page {snap['title']!r} <{snap['url']}> (viewport {snap['vw']}x{snap['vh']}, "
                 f"scrolled to {snap['scrollY']} of {snap['docH']} px)"]
        els = snap.get("elements") or []
        if els:
            lines.append("Elements (ids for browser_click/browser_type; valid until the next browser_observe):")
            for e in els:
                part = f"  [{e['id']}] {e['role']}"
                if e.get("name"):
                    part += f" {e['name']!r}"
                if e.get("value") not in (None, ""):
                    part += f" value={e['value']!r}"
                part += f" at ({e['x']}, {e['y']})"
                if e.get("states"):
                    part += " [" + ", ".join(e["states"]) + "]"
                lines.append(part)
        else:
            lines.append("No interactive elements found on the page.")
        if include_text:
            text = page.text(max_chars=3000).strip()
            if text:
                lines.append("Text:\n" + text)
        img = base64.b64encode(page.screenshot_png()).decode("ascii") if include_screenshot else None
        self._browser_snapshot = (page.info.id, {e["id"]: e for e in els})
        return ToolResult("\n".join(lines), image_base64=img)

    def _browser_target(self, page: Any, element: Optional[int], selector: Optional[str]) -> Tuple[Any, Dict[str, Any]]:
        if (element is None) == (selector is None):
            raise ValueError("Give exactly one of element (an id from browser_observe) or selector (CSS)")
        target: Any = int(element) if element is not None else str(selector)
        return target, page.resolve(target, scroll=False)

    def tool_browser_click(self, element: Optional[int] = None, selector: Optional[str] = None,
                           tab: Optional[str] = None) -> ToolResult:
        """Click an element of a browser page (real mouse events inside the page, no screen input).

        Args:
            element: Element id from the last browser_observe.
            selector: CSS selector instead of an id.
            tab: Part of the tab's title or URL (default: the first tab).
        """
        b, page = self._browser_page(tab)
        target, info = self._browser_target(page, element, selector)
        name = info.get("name") or info.get("tag") or str(target)
        args = {"element": element, "selector": selector, "tab": tab}
        return self._act("browser_click", args, self._browser_ctx(b, page, name),
                         f"click {name!r} on {page.info.title!r}",
                         lambda: (page.click(target), f"Clicked {name!r}; the tab is now {page.title!r} <{page.url}>.")[1],
                         feedback=False)

    def tool_browser_type(self, text: str, element: Optional[int] = None, selector: Optional[str] = None,
                          clear: bool = False, submit: bool = False, tab: Optional[str] = None) -> ToolResult:
        """Type text into a browser page field (clicks it first), optionally clearing it or pressing Enter.

        Args:
            text: Text to insert.
            element: Element id from the last browser_observe (default: the focused field).
            selector: CSS selector instead of an id.
            clear: Empty the field first.
            submit: Press Enter afterwards.
            tab: Part of the tab's title or URL (default: the first tab).
        """
        b, page = self._browser_page(tab)
        info: Dict[str, Any] = {}
        target = None
        if element is not None or selector is not None:
            target, info = self._browser_target(page, element, selector)
        secret = bool(info.get("password")) if target is not None else page.focused_is_password()
        if secret:
            self._redact("text")
        shown = _REDACTED if secret else (text if len(text) <= 60 else text[:57] + "...")
        name = info.get("name") or info.get("tag") or "the focused field"
        desc = f"type {shown!r} into {name!r} on {page.info.title!r}" + (" and press Enter" if submit else "")
        args = {"text": text, "element": element, "selector": selector, "clear": bool(clear), "submit": bool(submit), "tab": tab}
        ctx = self._browser_ctx(b, page, name)
        if submit:
            # Enter submits the field's form (its default button: "Pay now") or triggers the page's
            # own handler: the policy judges that control, as for a click on it (cf. _with_default_button).
            ctx["enter"] = True
            hit = page.enter_target(target)
            kind = hit.get("kind") if isinstance(hit, dict) else None
            label = str(hit.get("label") or "") if isinstance(hit, dict) else ""
            if kind not in ("self", "form", "nearby") or (not label and not (kind == "form" and hit.get("action"))):
                # Nothing focused, nothing found, the focus inside an embedded (cross-origin)
                # frame -- a Stripe-style card field --, a form without a submit button or a
                # control without a usable name: what Enter activates is unknown.
                ctx["target_unknown"] = True
                if kind == "frame":
                    desc += " (the focus is inside an embedded frame: what Enter activates cannot be seen)"
            if isinstance(hit, dict):
                extra = [x for x in (label, str(hit.get("form") or ""), str(hit.get("action") or "")) if x]
                if extra:
                    ctx["enter_target"] = extra
                if label and hit.get("kind") != "self":
                    ctx["target"] = f"{name} -> {label}"
                    how = "the form's default button" if hit.get("kind") == "form" else "the nearby button"
                    desc += f" (Enter activates {how} {label!r})"
                elif hit.get("kind") == "form" and hit.get("action"):
                    desc += f" (Enter submits the form to {str(hit['action'])!r})"
            ctx["description"] = desc
        return self._act("browser_type", args, ctx, desc,
                         lambda: (page.type(text, target, clear=bool(clear), submit=bool(submit)),
                                  f"{desc[0].upper()}{desc[1:]}; the tab is now {page.title!r}.")[1],
                         feedback=False)

    def tool_browser_navigate(self, url: str, tab: Optional[str] = None) -> ToolResult:
        """Open a URL in a browser tab and wait for it to load.

        Args:
            url: The address to open.
            tab: Part of the tab's title or URL (default: the first tab).
        """
        b, page = self._browser_page(tab)

        def run() -> str:
            page.navigate(url)
            return f"Opened {url!r}; the tab is now {page.title!r}."

        return self._act("browser_navigate", {"url": url, "tab": tab}, self._browser_ctx(b, page, url),
                         f"open {url!r} in {page.info.title!r}", run, feedback=False)

    def tool_browser_read(self, selector: Optional[str] = None, tab: Optional[str] = None, max_chars: int = 8000) -> ToolResult:
        """Read the text of a browser page, or of one element (CSS selector).

        Args:
            selector: CSS selector (default: the whole page).
            tab: Part of the tab's title or URL (default: the first tab).
            max_chars: Maximum characters returned (1-50000).
        """
        b, page = self._browser_page(tab)
        if selector is not None:
            info = page.resolve(selector, scroll=False)
            if info.get("password"):
                return ToolResult("(a password field: its value is not shown)")
        text = page.text(selector, max_chars=min(max(int(max_chars), 1), 50000))
        return ToolResult(text if text.strip() else "(no text)", data=text)

    def tool_read_clipboard(self) -> ToolResult:
        """Return the text currently on the clipboard."""
        text = self.desktop.clipboard_get()
        return ToolResult(text if text else "(the clipboard is empty)", data=text)

    def tool_write_clipboard(self, text: str) -> ToolResult:
        """Put text on the clipboard (paste it with hotkey "mod+v").

        Args:
            text: The text to copy.
        """
        shown = text if len(text) <= 60 else text[:57] + "..."
        return self._act("write_clipboard", {"text": text}, self._context(), f"copy {shown!r} to the clipboard",
                         lambda: self.desktop.clipboard_set(text))

    def tool_read_text(
        self,
        element_id: Optional[int] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        width: Optional[float] = None,
        height: Optional[float] = None,
    ) -> ToolResult:
        """Read text: an element's accessible text (element_id), or OCR of a screenshot region
        (x, y, width, height in screenshot pixels), or OCR of the whole screen (no arguments).

        Args:
            element_id: Element id from the last observe call.
            x: Region left (screenshot pixels).
            y: Region top (screenshot pixels).
            width: Region width (screenshot pixels).
            height: Region height (screenshot pixels).
        """
        region_args = (x, y, width, height)
        if element_id is not None:
            if any(v is not None for v in region_args):
                raise ValueError("Give either element_id or a region")
            oe = self._mapping().element(element_id)
            parts = []
            name = oe.node.name if oe.node is not None else oe.name
            if oe.node is not None:
                secret = is_password(oe.node)
                value = oe.node.value
                if secret and value not in (None, ""):
                    value = "***"  # the model and the audit log never see passwords
            else:
                secret = oe.value == "***"
                value = oe.value
            if name:
                parts.append(f"name: {name}")
            if value not in (None, "") and value != name:
                parts.append(f"value: {value}")
            if parts:
                return ToolResult("\n".join(parts), data={"name": name, "value": value})
            if secret:
                # Never OCR a password field: the screen may show the plaintext (a reveal toggle).
                return ToolResult("(no text: password field)", data={"name": name, "value": value})
            region: Optional[Rect] = oe.bounds
        elif any(v is not None for v in region_args):
            if any(v is None for v in region_args):
                raise ValueError("A region needs x, y, width and height")
            obs = self._mapping()
            tl = obs.to_screen(float(x), float(y))  # type: ignore[arg-type]
            br = obs.to_screen(float(x) + float(width), float(y) + float(height))  # type: ignore[arg-type]
            region = Rect.from_ltrb(tl.x, tl.y, max(br.x, tl.x + 1), max(br.y, tl.y + 1))
        else:
            region = None
        text = self.desktop.read_text(region)
        return ToolResult(text if text.strip() else "(no text recognized)", data=text)

    def tool_wait(self, seconds: float = 1.0) -> ToolResult:
        """Pause, e.g. while an application loads.

        Args:
            seconds: How long to wait (0-60).
        """
        seconds = min(max(float(seconds), 0.0), 60.0)
        self.desktop.sleep(seconds)
        return ToolResult(f"Waited {seconds:g}s.")

    def __repr__(self) -> str:
        return f"<DesktopToolkit tools={len(self.registry)} policy={self.policy!r}>"


__all__ = [
    "DesktopToolkit",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "parse_docstring",
    "schema_from_function",
]
