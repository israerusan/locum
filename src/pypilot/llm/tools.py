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
apps and the clipboard. Every call is checked by a :class:`~pypilot.llm.safety.Policy`
and written to an :class:`~pypilot.llm.safety.AuditLog`.
"""

from __future__ import annotations

import base64
import collections.abc
import dataclasses
import enum
import inspect
import json
import re
import time
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

from ..errors import ElementNotFound, FailSafeTriggered, NotSupported, PolicyViolation, PypilotError
from ..types import Match, Point, Rect, WindowInfo
from .observe import Observation, ObservedElement, observe
from .._nodeinfo import node_app, target_label, window_at
from .safety import AuditLog, Policy, keys_activate

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
    if origin is Union:
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


def schema_from_function(fn: Callable[..., Any]) -> Tuple[str, Dict[str, Any]]:
    """``(description, JSON schema)`` for ``fn``'s parameters (type hints + docstring)."""
    description, arg_docs = parse_docstring(inspect.getdoc(fn))
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
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
    if value is None or not t:
        return True
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
    """A tool: ``name``, ``description``, JSON-schema ``parameters`` and the callable ``fn``."""

    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[..., Any]
    read_only: bool = False

    @classmethod
    def from_function(
        cls,
        fn: Callable[..., Any],
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        read_only: bool = False,
    ) -> "ToolSpec":
        desc, schema = schema_from_function(fn)
        tool_name = name or getattr(fn, "__name__", None)
        if not tool_name or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_name):
            raise ValueError(f"Invalid tool name {tool_name!r} (use 1-64 chars of [A-Za-z0-9_-])")
        return cls(tool_name, (description or desc or tool_name).strip(), parameters or schema, fn, read_only)

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
        is :class:`~pypilot.errors.FailSafeTriggered` -- the human's kill switch
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
        try:
            return _to_result(spec.fn(**call_args))
        except FailSafeTriggered:
            raise
        except PolicyViolation as exc:
            return ToolResult.error(f"Blocked by policy: {exc}")
        except (ElementNotFound, NotSupported, PypilotError) as exc:
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


def _signature(fn: Callable[..., Any]) -> inspect.Signature:
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return inspect.Signature()


# =============================================================================== toolkit


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


_DIRECTIONS = ("up", "down", "left", "right")


class DesktopToolkit:
    """Desktop-control tools for LLM agents (see module docstring).

    Args:
        desktop: The :class:`~pypilot.desktop.Desktop` to drive.
        policy: Safety policy (default: :class:`Policy` with default confirmations).
        audit: Audit log (default: an in-memory :class:`AuditLog`).
        observe_kwargs: Extra arguments for :func:`~pypilot.llm.observe.observe`.
        find_timeout: Seconds selector/text targets wait for their element.
        feedback: Append the focused element/active window to action results.
        launch_kwargs: Extra ``subprocess.Popen`` options for ``launch_app``
            (the MCP server uses this to keep children off its stdio).

    Element ids refer to the most recent observation (:attr:`last_observation`);
    ``x``/``y`` arguments are pixels of the most recent screenshot and are mapped
    to the screen with :meth:`Observation.to_screen`. Before any observation,
    coordinates are raw screen pixels.
    """

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
        self.policy = policy if policy is not None else Policy()
        self.audit = audit if audit is not None else AuditLog()
        self.observe_kwargs = dict(observe_kwargs or {})
        self.find_timeout = float(find_timeout)
        self.feedback = bool(feedback)
        #: Extra keyword arguments for ``Desktop.launch`` in the ``launch_app`` tool
        #: (``subprocess.Popen`` options, e.g. ``{"stdout": subprocess.DEVNULL}``).
        self.launch_kwargs: Dict[str, Any] = dict(launch_kwargs or {})
        self.last_observation: Optional[Observation] = None
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
    )

    def _build(self) -> None:
        for name, attr in self._TOOLS:
            method = getattr(self, attr)
            spec = ToolSpec.from_function(method, name=name, read_only=self.policy.is_read_only(name))
            spec.fn = self._wrap(name, method)
            self.registry.add(spec)

    def _wrap(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(**kwargs: Any) -> ToolResult:
            started = time.monotonic()
            status = "ok"
            result: Optional[ToolResult] = None
            error: Optional[str] = None
            try:
                if self.policy.is_read_only(name):
                    self.policy.check(name, kwargs, context={})
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
                event: Dict[str, Any] = {
                    "event": "tool_call",
                    "tool": name,
                    "args": kwargs,
                    "status": status,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
                if result is not None:
                    event["result"] = result.text[:500]
                    event["image"] = bool(result.image_base64)
                if error is not None:
                    event["error"] = error
                self.audit.record(event)

        wrapped.__name__ = name
        return wrapped

    # ----------------------------------------------------------------- helpers
    def _mapping(self) -> Observation:
        if self.last_observation is not None:
            return self.last_observation
        vr = self.desktop.virtual_rect()
        return Observation(None, [], [], None, Point(0, 0), (vr.width, vr.height), 1.0, None, region=vr)

    def _ensure_observation(self) -> Observation:
        if self.last_observation is None:
            self.last_observation = self._mapping()
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

        ``name`` is the policy label (:func:`~pypilot._nodeinfo.target_label`):
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

    def _focus_target(self, name_it: bool) -> Optional[_Target]:
        """The focused element as a policy target for keyboard actions.

        Keyboard input goes to the focused control, so its application is always
        reported (``target_app``) and, when ``name_it`` is true (keys that can
        activate it, or typed text), its name too -- so confirmation patterns
        cannot be sidestepped by tabbing to a control and pressing Enter.
        """
        try:
            node = self.desktop.backend.focused_element()
        except Exception:
            node = None
        if node is None:
            return None
        name, app = self._node_info(node)
        try:
            role = str(node.role or "")
        except Exception:
            role = ""
        desc = f"the focused {role or 'element'}" + (f" {name!r}" if name else "")
        return _Target(description=desc, name=name if name_it else "", app=app)

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
            return _Target(element=el, description=label, name=name, app=oe.app or self._node_info(oe.node)[1], observed=oe)
        if selector is not None:
            el = self.desktop.find(selector, timeout=self.find_timeout)
            name, app = self._node_info(el.node)
            return _Target(element=el, description=f"{el.role} {name!r} (selector {selector!r})", name=name or selector, app=app)
        if text is not None:
            el, point = self._find_by_text(text)
            if el is not None:
                name, app = self._node_info(el.node)
                return _Target(element=el, description=f"{el.role} {name!r} (text {text!r})", name=name or text, app=app)
            assert point is not None
            node = self._safe_element_at(point)
            apps = self._point_apps(point, node)
            label = self._node_info(node)[0] if node is not None else ""
            name = text if not label or label == text else f"{text} < {label}"
            return _Target(point=point, description=f"on-screen text {text!r} at ({point.x}, {point.y})", name=name,
                           app=apps[0] if apps else "", apps=apps)
        point = self._screen_point(x, y)
        node = self._safe_element_at(point)
        name = self._node_info(node)[0] if node is not None else ""
        apps = self._point_apps(point, node)
        desc = f"({x}, {y})" + (f" on {node.role} {name!r}" if node is not None and name else "")
        return _Target(point=point, description=desc, name=name, app=apps[0] if apps else "", apps=apps)

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
        ctx.update({k: v for k, v in extra.items() if v is not None})
        return ctx

    def _act(self, tool: str, args: Dict[str, Any], ctx: Dict[str, Any], description: str, action: Callable[[], Any]) -> ToolResult:
        """Policy check, then dry-run report or the real action (+ feedback)."""
        ctx.setdefault("description", description)
        self.policy.check(tool, {k: v for k, v in args.items() if v is not None}, context=ctx)
        if self.policy.dry_run:
            note = f" (this would require human confirmation: {ctx['needs_confirmation']})" if ctx.get("needs_confirmation") else ""
            return ToolResult(f"[dry-run] Would {description}{note}. Nothing was done.", data={"dry_run": True, "description": description})
        out = action()
        text = out if isinstance(out, str) and out else description[0].upper() + description[1:] + "."
        return ToolResult(text + self._feedback_text())

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

                parts.append("focus: " + describe_node(node, include_bounds=False))
        except Exception:
            pass
        return (" [" + "; ".join(parts) + "]") if parts else ""

    # =================================================================== tools
    def tool_observe(self, window: Optional[str] = None, include_screenshot: bool = True) -> ToolResult:
        """Look at the screen: returns the visible interactive elements with numeric ids, the windows,
        and a screenshot with each element's id drawn in a box (set-of-marks).

        Call this first and again whenever the screen may have changed. Element ids are only valid
        until the next observe/screenshot call. Coordinates refer to the returned screenshot's pixels.

        Args:
            window: Only observe the window whose title matches (substring/glob); default: whole screen.
            include_screenshot: Include the annotated screenshot image.
        """
        kw = dict(self.observe_kwargs)
        kw["screenshot"] = bool(include_screenshot)
        obs = observe(self.desktop, window=window, **kw)
        self.last_observation = obs
        img = obs.image_base64(annotated=True) if obs.screenshot is not None else None
        return ToolResult(obs.text(kw.get("max_elements", 300)), image_base64=img, data=obs)

    def tool_screenshot(self, annotated: bool = False) -> ToolResult:
        """Take a screenshot of the whole screen (also refreshes element ids and the coordinate space).

        Args:
            annotated: Draw numbered element boxes on the image (like observe).
        """
        kw = dict(self.observe_kwargs)
        kw["screenshot"] = True
        kw["annotate"] = bool(annotated)
        obs = observe(self.desktop, **kw)
        self.last_observation = obs
        w, h = obs.image_size
        return ToolResult(
            f"Screenshot {w}x{h} (screen {obs.screen_size[0]}x{obs.screen_size[1]}, scale {obs.scale:.4g}); "
            f"{len(obs.elements)} element ids refreshed.",
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
        t = self._target(element_id, x, y, selector, text)
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

        return self._act(tool, args, self._context(t), description, run)

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
        t = self._target(element_id, None, None, selector, None, required=False)
        where = f" into {t.description}" if t.element is not None else ""
        shown = text if len(text) <= 60 else text[:57] + "..."
        description = f"type {shown!r}{where}" + (" (replacing its content)" if clear else "") + (" and press Enter" if press_enter else "")
        args = {"text": text, "element_id": element_id, "selector": selector, "clear": clear, "press_enter": press_enter}

        def run() -> str:
            if t.element is not None:
                if clear:
                    t.element.set_text(text)
                else:
                    t.element.type(text)
            else:
                if clear:
                    self.desktop.hotkey("mod+a")
                    self.desktop.press("backspace")
                self.desktop.type(text)
            if press_enter:
                self.desktop.press("enter")
            value = ""
            if t.element is not None:
                v = t.element.value
                if v is not None:
                    value = f" Field value is now {v!r}."
            return f"Typed {shown!r}{where}." + value

        if t.element is not None:
            ctx = self._context(t)
        else:
            ctx = self._context(self._focus_target(True))
        return self._act("type_text", args, ctx, description, run)

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
        from ..keys import parse_hotkey

        for k in keys:
            parse_hotkey(k)  # validate before acting
        description = f"press {' '.join(keys)}" + (f" x{presses}" if presses > 1 else "")
        activates = keys_activate(keys)
        focus = self._focus_target(activates)
        if activates and focus is not None and focus.name:
            description += f" on {focus.description}"
        return self._act(
            "press_keys", {"keys": keys, "presses": presses},
            self._context(focus, keys=" ".join(keys)), description,
            lambda: self.desktop.press(*keys, presses=presses),
        )

    def tool_hotkey(self, combo: str) -> ToolResult:
        """Press a keyboard shortcut such as "ctrl+s", "ctrl+shift+t", "alt+tab", "mod+c"
        (mod = Ctrl, or Cmd on macOS).

        Args:
            combo: Keys joined with "+".
        """
        from ..keys import parse_hotkey

        parse_hotkey(combo)
        activates = keys_activate([combo])
        focus = self._focus_target(activates)
        description = f"press {combo}"
        if activates and focus is not None and focus.name:
            description += f" on {focus.description}"
        ctx = self._context(focus, keys=combo)
        return self._act("hotkey", {"combo": combo}, ctx, description, lambda: self.desktop.hotkey(combo))

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
        point = self._target_point(t)
        dy = amount if direction == "up" else -amount if direction == "down" else 0
        dx = amount if direction == "right" else -amount if direction == "left" else 0
        description = f"scroll {direction} {amount} notch{'es' if amount != 1 else ''}" + (f" over {t.description}" if point is not None else "")
        args = {"direction": direction, "amount": amount, "element_id": element_id, "x": x, "y": y, "selector": selector}
        return self._act("scroll", args, self._context(t if point is not None else None), description,
                         lambda: self.desktop.scroll(dy, dx, at=point))

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
        return self._act("move_mouse", args, self._context(t), f"move the mouse to {t.description}", lambda: self.desktop.move(point))

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
        return self._act("drag", args, ctx, f"drag from {s.description} to {e.description}",
                         lambda: self.desktop.drag(sp, ep, duration=duration))

    def tool_set_value(self, value: str, element_id: Optional[int] = None, selector: Optional[str] = None) -> ToolResult:
        """Replace the content of a text field (uses the accessibility API when possible, else types).

        Args:
            value: The new text.
            element_id: Element id of the field from the last observe call.
            selector: Selector of the field.
        """
        t = self._target(element_id, None, None, selector, None)
        assert t.element is not None
        el = t.element
        shown = value if len(value) <= 60 else value[:57] + "..."

        def run() -> str:
            el.set_text(value)
            v = el.value
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
        for el in found[:limit]:
            entry = obs.add_element(el.node)
            lines.append(obs.describe_element(entry) + (f"  selector: {entry.selector}" if entry.selector else ""))
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
            el = self.desktop.wait(sel, state=state, timeout=timeout)
        except (ElementNotFound, PypilotError) as exc:
            if text is not None and state == "visible":
                try:
                    m = self.desktop.find_text(text, timeout=timeout)
                except NotSupported:
                    m = None
                if m is not None:
                    p = self._mapping().to_image(*m.center)
                    return ToolResult(f"Text {text!r} is visible at ({p.x}, {p.y}).")
            return ToolResult.error(f"Timed out after {timeout:g}s waiting for {sel!r} to be {state}: {exc}")
        if el is None:
            return ToolResult(f"{sel!r} is gone.")
        obs = self._ensure_observation()
        entry = obs.add_element(el.node)
        return ToolResult(f"{sel!r} is {state}: " + obs.describe_element(entry))

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
        ctx = self._context(target=win.title, target_app=win.app)
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
            res = self.desktop.launch(command, wait_window=wait_window, timeout=timeout, **self.launch_kwargs)
            if wait_window is not None:
                return f"Launched {command!r}; window {getattr(res, 'title', wait_window)!r} is open."
            return f"Launched {command!r}."

        args = {"command": command, "wait_window": wait_window, "timeout": timeout}
        return self._act("launch_app", args, self._context(command=command, target=command), f"launch {command!r}", run)

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
            value = oe.node.value if oe.node is not None else oe.value
            if name:
                parts.append(f"name: {name}")
            if value not in (None, "") and value != name:
                parts.append(f"value: {value}")
            if parts:
                return ToolResult("\n".join(parts), data={"name": name, "value": value})
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
