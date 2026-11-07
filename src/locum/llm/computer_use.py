"""Execute Anthropic *computer use* actions on a :class:`~locum.desktop.Desktop`.

Claude's computer-use toolset (``{"type": "computer_toolset_20260801"}``) sends
``tool_use`` blocks whose **name** is the action (``screenshot``,
``left_click``, ``type``, ``key``, ``zoom`` ...) and which carry
``"toolset_name": "computer"``. Several can arrive in one turn (a *batch*):
they must run in order, stop at the first failure, and every one must be
answered with a ``tool_result`` that echoes ``"toolset_name": "computer"``.
:class:`ComputerUseAdapter` implements exactly that, plus the legacy shape
(a single tool named ``computer`` with ``input.action``) for older models.

Coordinates live in the pixel space of the screenshots this adapter returns.
Screenshots are downscaled so the long edge is at most 2576 px **and** they
cost at most 4784 visual tokens (28x28 tiles) -- the API does not downscale
computer-use images for you -- and every coordinate is mapped back to physical
screen pixels before acting.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .. import _target as _input_target
from ..errors import FailSafeTriggered, PolicyViolation
from ..keys import normalize_key
from ..types import Point, Rect
from .observe import MAX_LONG_EDGE, MAX_VISUAL_TOKENS, fit_image_size
from .._nodeinfo import is_password, node_app, owning_window, target_label, window_at
from .safety import (
    AuditLog, Policy, check_activation_combo, is_text_input, keys_activate, keys_may_move_focus, split_typed_text,
    target_identity, typed_text_activates,
)
from .tools import _REDACTED, ToolResult, _default_button, _has_enter, _has_newline, _shortcut_targets

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from ..desktop import Desktop

_log = logging.getLogger(__name__)

TOOLSET_TYPE = "computer_toolset_20260801"
TOOLSET_NAME = "computer"
LEGACY_TOOL_NAME = "computer"
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."

#: Every member action of the toolset.
ACTIONS: Tuple[str, ...] = (
    "screenshot", "zoom", "left_click", "right_click", "middle_click", "double_click", "triple_click",
    "left_click_drag", "mouse_move", "left_mouse_down", "left_mouse_up", "cursor_position", "scroll",
    "type", "key", "hold_key", "wait",
)
READ_ONLY_ACTIONS = frozenset({"screenshot", "zoom", "cursor_position", "wait"})
_KEYBOARD_ACTIONS = frozenset({"type", "key", "hold_key"})
_POINTER_ACTIONS = frozenset({"left_click", "right_click", "middle_click", "double_click", "triple_click",
                              "left_click_drag", "left_mouse_down", "scroll"})
_MAX_DURATION = 300.0

#: Models that predate the toolset: ``model base id -> (tool type, beta header)``
#: (Anthropic's computer-use docs, "Earlier tool versions"). Every newer model
#: (Opus 4.8 and the Claude 5 family) takes :data:`TOOLSET_TYPE`.
_LEGACY_TOOL_VERSIONS: Dict[str, Tuple[str, str]] = {
    **{m: ("computer_20251124", "computer-use-2025-11-24")
       for m in ("claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-opus-4-5")},
    **{m: ("computer_20250124", "computer-use-2025-01-24")
       for m in ("claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1", "claude-opus-4", "claude-opus-4-0",
                 "claude-sonnet-4", "claude-sonnet-4-0")},
}


def _model_base(model: str) -> str:
    """``model`` without platform prefix and snapshot suffix.

    ``"us.anthropic.claude-sonnet-4-5-20250929-v1:0"`` / ``"claude-sonnet-4-5@20250929"``
    -> ``"claude-sonnet-4-5"``.
    """
    import re

    m = str(model or "").strip().lower()
    m = m.rsplit("/", 1)[-1]
    if "claude" in m:
        m = m[m.index("claude"):]
    m = re.sub(r"-v\d+(:\d+)?$", "", m)
    m = re.sub(r"[-@]\d{8}$", "", m)
    return m

# xdotool keysym names (lowercased) -> locum canonical key names.
_XDOTOOL_KEYS: Dict[str, str] = {
    "return": "enter", "enter": "enter", "kp_enter": "num_enter", "linefeed": "enter",
    "tab": "tab", "iso_left_tab": "tab", "backspace": "backspace", "escape": "escape", "esc": "escape",
    "delete": "delete", "kp_delete": "delete", "insert": "insert", "kp_insert": "insert",
    "home": "home", "kp_home": "home", "end": "end", "kp_end": "end",
    "page_up": "pageup", "prior": "pageup", "kp_page_up": "pageup", "kp_prior": "pageup",
    "page_down": "pagedown", "next": "pagedown", "kp_page_down": "pagedown", "kp_next": "pagedown",
    "left": "left", "right": "right", "up": "up", "down": "down",
    "kp_left": "left", "kp_right": "right", "kp_up": "up", "kp_down": "down",
    "space": "space", "kp_space": "space",
    "super": "cmd", "super_l": "cmd", "super_r": "cmd_r", "meta": "cmd", "meta_l": "cmd", "meta_r": "cmd_r",
    "hyper_l": "cmd", "win": "cmd", "windows": "cmd", "cmd": "cmd", "command": "cmd",
    "control": "ctrl", "control_l": "ctrl", "control_r": "ctrl_r", "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl_r",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift_r",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt_r", "option": "alt", "iso_level3_shift": "altgr", "mode_switch": "altgr",
    "caps_lock": "capslock", "num_lock": "numlock", "scroll_lock": "scrolllock", "print": "printscreen",
    "sys_req": "printscreen", "pause": "pause", "break": "pause", "menu": "menu",
    "kp_add": "num_add", "kp_subtract": "num_subtract", "kp_multiply": "num_multiply", "kp_divide": "num_divide",
    "kp_decimal": "num_decimal", "kp_separator": "num_decimal", "kp_equal": "=",
    "xf86audioraisevolume": "volumeup", "xf86audiolowervolume": "volumedown", "xf86audiomute": "volumemute",
    "xf86audioplay": "playpause", "xf86audiopause": "playpause", "xf86audionext": "nexttrack",
    "xf86audioprev": "prevtrack", "xf86audiostop": "stop", "xf86back": "browser_back",
    "xf86forward": "browser_forward", "xf86reload": "browser_refresh", "xf86refresh": "browser_refresh",
    "plus": "+", "minus": "-", "equal": "=", "comma": ",", "period": ".", "slash": "/", "backslash": "\\",
    "semicolon": ";", "apostrophe": "'", "quoteright": "'", "grave": "`", "quoteleft": "`",
    "bracketleft": "[", "bracketright": "]", "exclam": "!", "at": "@", "numbersign": "#", "dollar": "$",
    "percent": "%", "asciicircum": "^", "ampersand": "&", "asterisk": "*", "parenleft": "(", "parenright": ")",
    "underscore": "_", "quotedbl": '"', "colon": ":", "less": "<", "greater": ">", "question": "?",
    "bar": "|", "braceleft": "{", "braceright": "}", "asciitilde": "~",
    **{f"kp_{i}": f"num{i}" for i in range(10)},
}
_MODIFIER_KEYS = frozenset({"ctrl", "shift", "alt", "cmd", "ctrl_r", "shift_r", "alt_r", "cmd_r", "altgr", "fn"})


def map_key(name: str) -> str:
    """Map one xdotool-style key name (``Return``, ``Page_Up``, ``KP_Enter``, ``super``, ``F5``, ``a``)
    to a canonical locum key. Raises ``ValueError`` for unknown names."""
    if name is None or name == "":
        raise ValueError("Empty key name")
    if len(name) == 1:
        return name
    low = name.strip().lower()
    if low in _XDOTOOL_KEYS:
        return _XDOTOOL_KEYS[low]
    return normalize_key(name.strip())


def parse_key_combo(combo: str) -> List[str]:
    """``"ctrl+shift+t"`` / ``"alt+Tab"`` / ``"Return"`` / ``"ctrl+plus"`` -> canonical keys.

    Letters combined with a modifier are lowercased (``ctrl+S`` is ``ctrl+s``,
    so no implicit shift is added); a lone uppercase letter keeps its case.
    """
    if not isinstance(combo, str) or combo == "":
        raise ValueError("Key combination must be a non-empty string")
    s = combo.strip()
    if s == "":
        return ["space"]
    if s == "+":
        return ["+"]
    tokens: List[str] = []
    for part in s.split("+"):
        if part.strip() == "":
            if not tokens or tokens[-1] != "+":
                tokens.append("+")
        else:
            tokens.append(part.strip())
    keys = [map_key(t) for t in tokens]
    if len(keys) > 1 and any(k in _MODIFIER_KEYS for k in keys):
        keys = [k.lower() if len(k) == 1 and k.isalpha() else k for k in keys]
    return keys


def parse_key_sequence(text: str) -> List[List[str]]:
    """xdotool ``key`` syntax: whitespace-separated combos pressed one after another."""
    if not isinstance(text, str) or text == "":
        raise ValueError("'text' must name the key(s) to press, e.g. \"Return\" or \"ctrl+s\"")
    if text.strip() == "":
        return [["space"]]
    return [parse_key_combo(part) for part in text.split()]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class ComputerUseAdapter:
    """Runs Claude computer-use actions against a desktop (see module docstring).

    Args:
        desktop: The :class:`~locum.desktop.Desktop` to drive.
        max_long_edge: Screenshot long-edge limit; ``"auto"`` = 2576 (the model maximum).
        policy: Safety policy (default: :class:`Policy` with default confirmations).
        audit: Audit log (default: in-memory).
        monitor: Monitor index shown to the model (``None`` = the whole virtual screen).
        zoom: Offer the ``zoom`` action (adds ``configs.zoom.enabled = false`` when False).
        max_visual_tokens: Visual-token budget per image (default 4784).
        screenshot_after_action: Attach a fresh screenshot to every successful action result.
    """

    def __init__(
        self,
        desktop: "Desktop",
        *,
        max_long_edge: Union[int, str] = "auto",
        policy: Optional[Policy] = None,
        audit: Optional[AuditLog] = None,
        monitor: Optional[int] = 0,
        zoom: bool = True,
        max_visual_tokens: int = MAX_VISUAL_TOKENS,
        screenshot_after_action: bool = False,
    ):
        self.desktop = desktop
        if max_long_edge == "auto" or max_long_edge is None:
            self.max_long_edge = MAX_LONG_EDGE
        else:
            self.max_long_edge = int(max_long_edge)
            if self.max_long_edge <= 0:
                raise ValueError("max_long_edge must be positive or 'auto'")
        self.policy = policy if policy is not None else Policy.default()
        self.audit = audit if audit is not None else AuditLog()
        self.monitor = monitor
        self.zoom_enabled = bool(zoom)
        self.max_visual_tokens = int(max_visual_tokens)
        self.screenshot_after_action = bool(screenshot_after_action)
        self._local = threading.local()  # per-call state: typed into a password field (audit redaction)
        self._held_buttons: List[str] = []  # pressed by left_mouse_down, not yet released

    # ---------------------------------------------------------------- geometry
    def screen_region(self) -> Rect:
        """The screen rectangle exposed to the model."""
        if self.monitor is None:
            return self.desktop.virtual_rect()
        mons = self.desktop.monitors()
        if not mons:
            return self.desktop.virtual_rect()
        idx = self.monitor if 0 <= self.monitor < len(mons) else 0
        return mons[idx].rect

    def image_size(self) -> Tuple[int, int]:
        """``(width, height)`` of the screenshots (= the model's coordinate space)."""
        r = self.screen_region()
        return fit_image_size(r.width, r.height, self.max_long_edge, self.max_visual_tokens)

    def to_screen(self, x: float, y: float) -> Point:
        """Screenshot coordinates -> physical screen coordinates."""
        r = self.screen_region()
        iw, ih = fit_image_size(r.width, r.height, self.max_long_edge, self.max_visual_tokens)
        sx = r.left + (float(x) + 0.5) * r.width / iw - 0.5
        sy = r.top + (float(y) + 0.5) * r.height / ih - 0.5
        sx = min(max(int(round(sx)), r.left), r.right - 1)
        sy = min(max(int(round(sy)), r.top), r.bottom - 1)
        return Point(sx, sy)

    def to_image(self, x: float, y: float) -> Point:
        """Physical screen coordinates -> screenshot coordinates."""
        r = self.screen_region()
        iw, ih = fit_image_size(r.width, r.height, self.max_long_edge, self.max_visual_tokens)
        ix = (float(x) - r.left + 0.5) * iw / r.width - 0.5
        iy = (float(y) - r.top + 0.5) * ih / r.height - 0.5
        return Point(int(round(ix)), int(round(iy)))

    def _coord(self, value: Any, name: str = "coordinate") -> Point:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"'{name}' must be [x, y], got {value!r}")
        try:
            x, y = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            raise ValueError(f"'{name}' must contain two numbers, got {value!r}") from None
        iw, ih = self.image_size()
        if not (0 <= x < iw and 0 <= y < ih):
            raise ValueError(f"'{name}' {list(value)} is outside the screenshot bounds ({iw}x{ih})")
        return self.to_screen(x, y)

    # ------------------------------------------------------------------ images
    def capture(self) -> "Image":
        """A screenshot of :meth:`screen_region`, resized to :meth:`image_size`."""
        r = self.screen_region()
        img = self.desktop.screenshot(r)
        size = self.image_size()
        if img.size != size:
            img = img.resize(size, _lanczos())
        return img

    @staticmethod
    def tool_version(model: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """``(tool type, beta header or None)`` of the computer tool ``model`` accepts.

        The toolset (no beta header) for Opus 4.8, the Claude 5 family and unknown
        (newer) models; ``computer_20251124`` / ``computer_20250124`` with their beta
        headers for the older Claude 4 models. Raises ``ValueError`` for models
        without computer use (Claude 3 and older).
        """
        if not model:
            return TOOLSET_TYPE, None
        base = _model_base(model)
        if base in _LEGACY_TOOL_VERSIONS:
            return _LEGACY_TOOL_VERSIONS[base]
        if base.startswith(("claude-3", "claude-2", "claude-instant")):
            raise ValueError(f"model {model!r} does not support a computer use tool locum can drive; "
                             "use a Claude 4 or newer model, or mode='tools'")
        return TOOLSET_TYPE, None

    def tool_definition(self, model: Optional[str] = None) -> Dict[str, Any]:
        """The ``tools`` entry for ``model`` (see :meth:`tool_version`).

        ``{"type": "computer_toolset_20260801"}`` (no name, no display size) by default;
        for an older model the legacy ``computer`` tool with the screenshot size as
        its display size (send its beta header too).
        """
        tool_type, _beta = self.tool_version(model)
        if tool_type != TOOLSET_TYPE:
            iw, ih = self.image_size()
            d: Dict[str, Any] = {"type": tool_type, "name": LEGACY_TOOL_NAME,
                                 "display_width_px": iw, "display_height_px": ih}
            if tool_type == "computer_20251124" and self.zoom_enabled:
                d["enable_zoom"] = True
            return d
        d = {"type": TOOLSET_TYPE}
        if not self.zoom_enabled:
            d["configs"] = {"zoom": {"enabled": False}}
        return d

    @staticmethod
    def is_computer_block(block: Any) -> bool:
        """Whether a ``tool_use`` block is a computer action (toolset member or legacy ``computer`` tool)."""
        name = _get(block, "name")
        toolset = _get(block, "toolset_name")
        if toolset is not None:
            return toolset == TOOLSET_NAME
        return name == LEGACY_TOOL_NAME

    # ----------------------------------------------------------------- execute
    def execute(self, name: str, input: Optional[Dict[str, Any]] = None) -> ToolResult:
        """Run one action; never raises (except :class:`FailSafeTriggered`, the human kill switch).

        ``name`` is the member action (``left_click``...) or ``"computer"`` with
        ``input["action"]`` (legacy shape).
        """
        params = dict(input or {})
        action = name
        if name == LEGACY_TOOL_NAME:
            action = params.pop("action", None)
            if not action:
                return ToolResult.error("Legacy computer tool call is missing 'action'")
        started = time.monotonic()
        status = "ok"
        result: Optional[ToolResult] = None
        self._local.secret = False
        import contextlib

        self._local.binds = contextlib.ExitStack()
        try:
            if action not in ACTIONS:
                result = ToolResult.error(f"Unknown computer action {action!r}. Supported: {', '.join(ACTIONS)}")
            elif action == "zoom" and not self.zoom_enabled:
                result = ToolResult.error("The zoom action is disabled")
            else:
                result = self._dispatch(action, params)
        except FailSafeTriggered as exc:
            status = "aborted"
            self._audit(action, params, status, None, str(exc), started)
            raise
        except PolicyViolation as exc:
            status = "blocked"
            result = ToolResult.error(f"Blocked by policy: {exc}", data={"blocked": True})
        except Exception as exc:
            result = ToolResult.error(f"{type(exc).__name__}: {exc}")
        finally:
            binds, self._local.binds = self._local.binds, None
            binds.close()
        if result.is_error and status == "ok":
            status = "error"
        elif isinstance(result.data, dict) and result.data.get("dry_run"):
            status = "dry_run"
        self._audit(action, params, status, result, None, started)
        return result

    def _audit(self, action: Any, params: Dict[str, Any], status: str, result: Optional[ToolResult], error: Optional[str], started: float) -> None:
        if getattr(self._local, "secret", False) and "text" in params:
            params = dict(params, text=_REDACTED)  # typed into a password field
        self._local.secret = False
        event: Dict[str, Any] = {
            "event": "computer_action",
            "tool": action,
            "args": params,
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if result is not None:
            event["result"] = result.text[:500]
            event["image"] = bool(result.image_base64)
        if error:
            event["error"] = error
        try:
            self.audit.record(event)
        except Exception as exc:  # noqa: BLE001 - never turn a performed action into an error
            # The action already happened (or was refused): reporting the audit failure
            # as an error would invite a retry -- a second payment.
            _log.error("Audit log write failed for computer action %r: %s", action, exc)
            if result is not None:
                result.text = (result.text or "OK") + (
                    f"\n[AUDIT LOG WRITE FAILED: {type(exc).__name__}: {exc}. The action above was "
                    "performed (or refused) as reported; do not repeat it.]"
                )

    # -------------------------------------------------------------- dispatch
    def _context(self, points: Sequence[Point] = (), *, activates: bool = False, **extra: Any) -> Dict[str, Any]:
        """Policy context for an action at ``points`` (the last one is where it lands).

        ``activates``: the action can press what is under the last point (a left
        click); if nothing can be hit-tested there, the target is marked unknown
        (``Policy(unknown_target=...)``).
        """
        ctx: Dict[str, Any] = {}
        try:
            aw = self.desktop.backend.active_window()
        except Exception:
            aw = None
        if aw is not None:
            ctx["app"] = aw.app
            ctx["window_title"] = aw.title
        if points:
            ctx["point"] = points[-1]
            ctx["points"] = list(points)
            names = []
            apps: List[str] = []
            for i, p in enumerate(points):
                name, point_apps, node = self._describe_point(p)
                if i == len(points) - 1:
                    # Nothing hit-testable, or a control with no usable identity (an
                    # icon-only button: no name, automation id, value or named child here).
                    if activates and (node is None or not target_identity(node, p, self.desktop.backend)):
                        ctx["target_unknown"] = True
                    if node is not None:
                        ctx["target_node"] = node
                    title = self._window_title(node, p)
                    if title:
                        ctx["target_window"] = title
                if name:
                    names.append(name)
                for app in point_apps:
                    if app and app not in apps:
                        apps.append(app)
            if names:
                ctx["target"] = " -> ".join(names)
            if apps:
                ctx["target_app"] = apps[0]
                ctx["target_apps"] = apps  # every app touched (both ends of a drag)
        ctx.update({k: v for k, v in extra.items() if v is not None})
        return ctx

    def _keyboard_context(self, activates: bool, *, enter: bool = False, activating: Optional[bool] = None,
                          **extra: Any) -> Dict[str, Any]:
        """Context for keyboard actions: the focused element is the target.

        Its application is always reported; its name is added when the input can
        activate it (Enter/Space, typed text), so confirmation patterns apply to
        "Tab to the button, press Return" exactly as to clicking it. ``enter``:
        the input includes Enter, which in a single-line field presses the
        form's default button -- its label is added too ("Amount -> Pay now").
        ``activating`` (default ``activates``): the input can press the focused
        control; if the focus cannot be read, the target is marked unknown
        (``Policy(unknown_target=...)``) rather than letting it through unchecked.
        """
        ctx = self._context(**extra)
        try:
            node = self.desktop.backend.focused_element()
        except Exception:
            node = None
        acts = activates if activating is None else activating
        if node is None:
            if acts:
                ctx["target_unknown"] = True
            return ctx
        if is_password(node):
            self._local.secret = True
        try:
            name = target_label(node)
        except Exception:
            name = ""
        app = node_app(self.desktop.backend, node)
        if app:
            ctx["target_app"] = app
        ctx["target_node"] = node
        title = self._window_title(node)
        if title:
            ctx["target_window"] = title
        identified = bool(target_identity(node))
        if activates and enter:
            button = _default_button(node)
            label = ""
            if button is not None:
                try:
                    label = target_label(button)
                except Exception:
                    label = ""
            if label:
                name = f"{name} -> {label}" if name else label
                ctx["default_button"] = label
                ctx["target_node"] = button  # allow rules judge the button Enter activates
                identified = True
        # Input that can press a focused control with no usable identity: unknown target.
        # Typing (no Enter) into a text field only edits that field.
        if acts and not identified and not (is_text_input(node) and not enter):
            ctx["target_unknown"] = True
        if activates and name:
            ctx["target"] = name
        return ctx

    def _window_title(self, node: Any = None, point: Optional[Point] = None) -> str:
        """Title of the window an action on ``node`` / at ``point`` lands in (``""`` if unknown)."""
        backend = self.desktop.backend
        w = window_at(backend, point) if point is not None else None
        if w is None and node is not None:
            try:
                w = owning_window(backend, node)
            except Exception:
                w = None
        return str(w.title or "") if w is not None else ""

    def _recheck_focus(self, action: str, params: Dict[str, Any], activates: bool, description: str, *,
                       enter: bool = False, activating: Optional[bool] = None, **extra: Any) -> None:
        """Re-run the policy for the next part of a keyboard action after focus may have moved.

        ``"Tab Return"`` or typed ``"1000\t\n"`` checked only against the control
        focused *before* the action would press Enter on whatever the Tab reached
        (a "Pay" button) unchecked. Not counted against the action budget.
        """
        ctx = self._keyboard_context(activates, enter=enter, activating=activating, **extra)
        ctx["description"] = description
        try:
            self.policy.check(action, params, context=ctx, count=False)
        except PolicyViolation as exc:
            raise PolicyViolation(f"{exc} (the input before it in this action was already sent)") from None

    def _describe_point(self, p: Point) -> Tuple[str, List[str], Any]:
        """``(label, apps, node)`` of what is at screen point ``p`` (``node``: the hit-tested node or ``None``).

        ``label`` is the hit-tested node's name plus its enclosing actionable
        control's (an unnamed icon inside a "Pay" button reads as "Pay").
        ``apps`` holds the node's application and the front-most window's
        application at ``p`` (both, when they differ), so the policy checks the
        app actually under the pointer and never falls back to the active one.
        """
        backend = self.desktop.backend
        try:
            node = backend.element_at(p.x, p.y)
        except Exception:
            node = None
        name = ""
        apps: List[str] = []
        if node is not None:
            try:
                name = target_label(node)
            except Exception:
                name = ""
            app = node_app(backend, node)
            if app:
                apps.append(app)
        w = window_at(backend, p)
        if w is not None and w.app and w.app not in apps:
            apps.append(w.app)
        return name, apps, node

    def _guard(self, action: str, params: Dict[str, Any], ctx: Dict[str, Any], description: str) -> Optional[ToolResult]:
        """Policy check; returns the dry-run result when acting is disabled."""
        if ctx.get("default_button"):
            description += f" (Enter activates the default button {ctx['default_button']!r})"
        ctx.setdefault("description", description)
        # What the policy judged: the focused window (keyboard actions) or the window under
        # the first point (presses, scrolls). The input of this action is only sent there.
        bind = None
        be = self.desktop.backend
        try:
            if action in _KEYBOARD_ACTIONS:
                h = be.foreground_handle()
                if h is not None:
                    bind = _input_target.InputTarget(be, h, title="the window that had the focus when this action "
                                                     "was checked", strict=True)
            elif action in _POINTER_ACTIONS and ctx.get("points"):
                p0 = ctx["points"][0]
                h = be.window_handle_at(int(p0.x), int(p0.y))
                if h is not None:
                    bind = _input_target.InputTarget(be, h, title="the window under the point when this action "
                                                     "was checked", strict=True)
        except Exception:
            bind = None
        self.policy.check(action, params, context=ctx)
        if self.policy.dry_run and action not in READ_ONLY_ACTIONS:
            note = f" (this would require human confirmation: {ctx['needs_confirmation']})" if ctx.get("needs_confirmation") else ""
            return ToolResult(f"[dry-run] Would {description}{note}. Nothing was done.", data={"dry_run": True, "description": description})
        if bind is not None:
            stack = getattr(self._local, "binds", None)
            if stack is not None:
                stack.enter_context(_input_target.bind(bind))
        return None

    def _done(self, text: str = "OK") -> ToolResult:
        if self.screenshot_after_action:
            from ..vision import to_base64_png

            return ToolResult(text, image_base64=to_base64_png(self.capture()))
        return ToolResult(text)

    def _modifiers(self, params: Dict[str, Any]) -> List[str]:
        text = params.get("text")
        if text in (None, ""):
            return []
        if not isinstance(text, str):
            raise ValueError("'text' (modifier keys) must be a string such as \"shift\" or \"ctrl+shift\"")
        keys = parse_key_combo(text)
        bad = [k for k in keys if k not in _MODIFIER_KEYS]
        if bad:
            raise ValueError(f"'text' for clicks/scroll may only contain modifier keys, got {text!r}")
        return keys

    @staticmethod
    def _duration(params: Dict[str, Any]) -> float:
        if "duration" not in params or params["duration"] is None:
            raise ValueError("'duration' (seconds) is required")
        try:
            d = float(params["duration"])
        except (TypeError, ValueError):
            raise ValueError(f"'duration' must be a number, got {params['duration']!r}") from None
        if not 0 <= d <= _MAX_DURATION:
            raise ValueError(f"'duration' must be between 0 and {_MAX_DURATION:g} seconds")
        return d

    def _dispatch(self, action: str, p: Dict[str, Any]) -> ToolResult:
        d = self.desktop
        if action == "screenshot":
            self._guard(action, p, {}, "take a screenshot")
            from ..vision import to_base64_png

            img = self.capture()
            return ToolResult(image_base64=to_base64_png(img), data={"size": img.size})

        if action == "zoom":
            region = p.get("region")
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                raise ValueError("'region' must be [x0, y0, x1, y1] in screenshot coordinates")
            try:
                x0, y0, x1, y1 = (float(v) for v in region)
            except (TypeError, ValueError):
                raise ValueError(f"'region' must contain four numbers, got {region!r}") from None
            iw, ih = self.image_size()
            if not (0 <= x0 < x1 <= iw and 0 <= y0 < y1 <= ih):
                raise ValueError(f"'region' {list(region)} must satisfy 0 <= x0 < x1 <= {iw} and 0 <= y0 < y1 <= {ih}")
            self._guard(action, p, {}, f"zoom into {list(region)}")
            r = self.screen_region()
            sx, sy = r.width / float(iw), r.height / float(ih)
            rect = Rect.from_ltrb(
                int(round(r.left + x0 * sx)), int(round(r.top + y0 * sy)),
                max(int(round(r.left + x1 * sx)), int(round(r.left + x0 * sx)) + 1),
                max(int(round(r.top + y1 * sy)), int(round(r.top + y0 * sy)) + 1),
            )
            img = d.screenshot(rect)
            s = min(iw / float(rect.width), ih / float(rect.height))
            out = (max(1, min(iw, int(round(rect.width * s)))), max(1, min(ih, int(round(rect.height * s)))))
            if img.size != out:
                img = img.resize(out, _lanczos())
            from ..vision import to_base64_png

            return ToolResult(image_base64=to_base64_png(img), data={"size": img.size, "screen_rect": rect.to_dict()})

        if action == "cursor_position":
            self._guard(action, p, {}, "read the cursor position")
            ip = self.to_image(*d.position())
            return ToolResult(f"[{ip.x}, {ip.y}]", data={"x": ip.x, "y": ip.y})

        if action == "wait":
            dur = self._duration(p)
            self._guard(action, p, {}, f"wait {dur:g}s")
            d.sleep(dur)
            return self._done()

        if action in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            mods = self._modifiers(p)
            point = self._coord(p["coordinate"]) if p.get("coordinate") is not None else None
            button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
            clicks = {"double_click": 2, "triple_click": 3}.get(action, 1)
            target = point if point is not None else d.position()
            where = f"({p['coordinate'][0]}, {p['coordinate'][1]})" if point is not None else "the current position"
            desc = action.replace("_", " ") + f" at {where}" + (f" holding {'+'.join(mods)}" if mods else "")
            dry = self._guard(action, p, self._context([target], activates=button == "left"), desc)
            if dry is not None:
                return dry
            with d.hold(*mods) if mods else _null():
                if point is not None:
                    d.mouse.click(point, button=button, clicks=clicks)
                else:
                    d.mouse.click(None, button=button, clicks=clicks)
            if button in self._held_buttons:
                self._held_buttons.remove(button)  # the click released it
            return self._done()

        if action == "left_click_drag":
            if p.get("start_coordinate") is None or p.get("coordinate") is None:
                raise ValueError("left_click_drag needs 'start_coordinate' and 'coordinate'")
            mods = self._modifiers(p)
            start = self._coord(p["start_coordinate"], "start_coordinate")
            end = self._coord(p["coordinate"])
            desc = f"drag from {list(p['start_coordinate'])} to {list(p['coordinate'])}"
            dry = self._guard(action, p, self._context([start, end], start_point=start, end_point=end), desc)
            if dry is not None:
                return dry
            with d.hold(*mods) if mods else _null():
                d.mouse.drag(start, end, duration=0.3)
            if "left" in self._held_buttons:
                self._held_buttons.remove("left")  # the drag ends with a release
            return self._done()

        if action == "mouse_move":
            if p.get("coordinate") is None:
                raise ValueError("mouse_move needs 'coordinate'")
            point = self._coord(p["coordinate"])
            dry = self._guard(action, p, self._context([point]), f"move the mouse to {list(p['coordinate'])}")
            if dry is not None:
                return dry
            d.mouse.move(point)
            return self._done()

        if action in ("left_mouse_down", "left_mouse_up"):
            point = self._coord(p["coordinate"]) if p.get("coordinate") is not None else None
            target = point if point is not None else d.position()
            verb = "press" if action == "left_mouse_down" else "release"
            dry = self._guard(action, p, self._context([target], activates=action == "left_mouse_down"),
                              f"{verb} the left mouse button")
            if dry is not None:
                return dry
            if point is not None:
                d.mouse.move(point)
            if action == "left_mouse_down":
                d.mouse_down("left")
                if "left" not in self._held_buttons:
                    self._held_buttons.append("left")
            else:
                d.mouse_up("left")
                if "left" in self._held_buttons:
                    self._held_buttons.remove("left")
            return self._done()

        if action == "scroll":
            direction = str(p.get("scroll_direction") or "").lower()
            if direction not in ("up", "down", "left", "right"):
                raise ValueError("'scroll_direction' must be up, down, left or right")
            try:
                amount = int(p.get("scroll_amount", 3) if p.get("scroll_amount") is not None else 3)
            except (TypeError, ValueError):
                raise ValueError(f"'scroll_amount' must be an integer, got {p.get('scroll_amount')!r}") from None
            if amount < 0 or amount > 100:
                raise ValueError("'scroll_amount' must be between 0 and 100")
            mods = self._modifiers(p)
            point = self._coord(p["coordinate"]) if p.get("coordinate") is not None else None
            target = point if point is not None else d.position()
            dy = amount if direction == "up" else -amount if direction == "down" else 0
            dx = amount if direction == "right" else -amount if direction == "left" else 0
            dry = self._guard(action, p, self._context([target]), f"scroll {direction} by {amount}")
            if dry is not None:
                return dry
            with d.hold(*mods) if mods else _null():
                d.mouse.scroll(dy, dx, at=point)
            return self._done()

        if action == "type":
            text = p.get("text")
            if not isinstance(text, str):
                raise ValueError("'text' (the text to type) is required")
            pieces = split_typed_text(text)
            first = pieces[0] if pieces else ""
            ctx = self._keyboard_context(True, enter=_has_newline(first), activating=typed_text_activates(first))
            secret = bool(getattr(self._local, "secret", False))
            shown = _REDACTED if secret else text if len(text) <= 60 else text[:57] + "..."
            dry = self._guard(action, p, ctx, f"type {shown!r}")
            if dry is not None:
                return dry
            for i, piece in enumerate(pieces):
                if i:  # a Tab/newline was typed: the rest may go to another control
                    self._recheck_focus(action, p, True, f"type {_REDACTED if secret else piece!r} (part of {shown!r})",
                                        enter=_has_newline(piece), activating=typed_text_activates(piece))
                d.type(piece)
            return self._done()

        if action == "key":
            seq = parse_key_sequence(p.get("text"))  # type: ignore[arg-type]
            repeat = p.get("repeat", 1)
            try:
                repeat = 1 if repeat is None else int(repeat)
            except (TypeError, ValueError):
                raise ValueError(f"'repeat' must be an integer, got {repeat!r}") from None
            if not 1 <= repeat <= 100:
                raise ValueError("'repeat' must be between 1 and 100")
            for keys in seq:
                check_activation_combo(keys)
            combo = p.get("text")
            activates = keys_activate(seq)
            pressed = _shortcut_targets(d, seq)
            ctx = self._keyboard_context(activates, enter=any(_has_enter(keys) for keys in seq),
                                         keys=" ".join("+".join(keys) for keys in seq), shortcut_targets=pressed or None)
            dry = self._guard(action, p, ctx, f"press {combo}" + (f" x{repeat}" if repeat > 1 else "")
                              + (f" (presses {', '.join(map(repr, pressed))})" if pressed else ""))
            if dry is not None:
                return dry
            moved = False
            for _ in range(repeat):
                for keys in seq:
                    if moved:  # an earlier key may have moved focus: check what this one acts on
                        self._recheck_focus(action, p, keys_activate([keys]), f"press {'+'.join(keys)} (part of {combo!r})",
                                            enter=_has_enter(keys), step_keys="+".join(keys))
                    if len(keys) == 1:
                        d.keyboard.press(keys[0])
                    else:
                        d.keyboard.hotkey(*keys)
                    moved = moved or keys_may_move_focus(keys)
            return self._done()

        if action == "hold_key":
            text = p.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError("'text' (the key(s) to hold) is required")
            keys = parse_key_combo(text)
            check_activation_combo(keys)
            dur = self._duration(p)
            dry = self._guard(action, p, self._keyboard_context(keys_activate([keys]), enter=_has_enter(keys), keys=text),
                              f"hold {text} for {dur:g}s")
            if dry is not None:
                return dry
            with d.hold(*keys):
                d.sleep(dur)
            return self._done()

        raise ValueError(f"Unsupported action {action!r}")  # pragma: no cover - ACTIONS is exhaustive

    def release_all(self) -> List[str]:
        """Release the mouse buttons pressed with ``left_mouse_down`` and not released since.

        Call it when a session ends (the :class:`~locum.llm.agent.Agent` does,
        however its run ends) so a dangling ``left_mouse_down`` cannot turn the
        user's next mouse move into a drag. Releasing is never blocked by the
        fail-safe; every button is attempted and the released ones are returned.
        """
        released: List[str] = []
        for button in list(self._held_buttons):
            try:
                self.desktop.mouse_up(button)
            except Exception:
                continue
            self._held_buttons.remove(button)
            released.append(button)
        return released

    # ------------------------------------------------------------------ batch
    @staticmethod
    def make_result(block: Any, result: ToolResult) -> Dict[str, Any]:
        """The ``tool_result`` dict answering ``block`` (echoes ``toolset_name`` for toolset members)."""
        out: Dict[str, Any] = {"type": "tool_result", "tool_use_id": _get(block, "id")}
        toolset = _get(block, "toolset_name")
        if toolset is not None or _get(block, "name") != LEGACY_TOOL_NAME:
            out["toolset_name"] = toolset or TOOLSET_NAME
        if result.is_error:
            out["is_error"] = True
            out["content"] = result.text or "Error"
        else:
            out["content"] = result.to_anthropic_content()
        return out

    @staticmethod
    def not_executed(block: Any) -> Dict[str, Any]:
        """The result for an action skipped because an earlier one in the batch failed."""
        out: Dict[str, Any] = {"type": "tool_result", "tool_use_id": _get(block, "id")}
        toolset = _get(block, "toolset_name")
        if toolset is not None or _get(block, "name") != LEGACY_TOOL_NAME:
            out["toolset_name"] = toolset or TOOLSET_NAME
        out["is_error"] = True
        out["content"] = NOT_EXECUTED
        return out

    def run_batch(self, tool_use_blocks: Iterable[Any]) -> List[Dict[str, Any]]:
        """Execute the computer actions of one turn **in order**, stopping at the first failure.

        Returns one ``tool_result`` dict per block (same order): executed actions
        get their result, the failing one ``is_error`` with the message, and every
        later one ``is_error`` with :data:`NOT_EXECUTED`. All results carry
        ``"toolset_name": "computer"``.
        """
        results: List[Dict[str, Any]] = []
        failed = False
        for block in tool_use_blocks:
            if failed:
                results.append(self.not_executed(block))
                continue
            res = self.execute(_get(block, "name"), _get(block, "input") or {})
            results.append(self.make_result(block, res))
            if res.is_error:
                failed = True
        return results

    def __repr__(self) -> str:
        return f"<ComputerUseAdapter image={self.image_size()} zoom={self.zoom_enabled} dry_run={self.policy.dry_run}>"


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


def _lanczos() -> Any:
    from PIL import Image

    return getattr(Image, "Resampling", Image).LANCZOS


__all__ = [
    "ACTIONS",
    "ComputerUseAdapter",
    "NOT_EXECUTED",
    "TOOLSET_NAME",
    "TOOLSET_TYPE",
    "map_key",
    "parse_key_combo",
    "parse_key_sequence",
]
