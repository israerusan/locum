"""Action recorder: turn live mouse/keyboard input into replayable flows and scripts.

The recorder is an event-driven state machine fed with low-level input
events. It coalesces them into meaningful actions, the way UiPath's recorder
does:

* consecutive characters become one ``type`` action (backspace edits the
  pending text), recorded with the **focused field's selector**;
* modifier chords become ``hotkey`` actions (``ctrl+s``); repeated special
  keys collapse into one ``press`` with a count (``tab`` x3);
* clicks are enriched with a robust accessibility selector
  (:func:`pypilot.selectors.selector_for` of ``backend.element_at(x, y)``),
  falling back to screen coordinates when nothing usable is under the cursor;
  multi-clicks become ``double_click``/``triple_click``; press-move-release
  becomes ``drag``; wheel events at one spot are summed into one ``scroll``.

Event sources (:meth:`Recorder.start`):

* ``"pynput"`` -- global OS hooks (``pip install pypilot[record]``); the
  default ``"auto"`` source.
* ``"backend"`` -- hooks the desktop's backend instance, recording everything
  pypilot itself does (a script, an LLM agent...) so it can be replayed later.

With the ``backend`` and ``pynput`` sources, *native* element actions that
produce no input events (``Element.set_text`` through the accessibility API,
``invoke``, ``toggle``, ``select``, ``expand``, ``collapse`` -- including a
``click(native="auto")`` that falls back to ``invoke``) are recorded too, as
``set_text``/``invoke``/... actions with the element's selector.
* ``"manual"`` -- nothing is hooked; call :meth:`Recorder.feed` yourself.

Output: :meth:`Recorder.to_flow` (a YAML/JSON-ready dict for
:func:`pypilot.rpa.flow.run_flow`), :meth:`Recorder.to_python` (a script using
the :class:`pypilot.Desktop` API) and :meth:`Recorder.save`.

::

    rec = Recorder(desktop).start(source="backend")
    desktop.click('edit "Name"'); desktop.type("Alice"); desktop.press("enter")
    rec.stop()
    run_flow(rec.to_flow(), other_desktop)      # replays the same interaction
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import pathlib
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ..errors import NotSupported
from ..keys import MODIFIERS, SHIFTED_CHARS, normalize_key
from ..types import MouseButton

if TYPE_CHECKING:  # pragma: no cover
    from ..desktop import Desktop

__all__ = ["Recorder", "RecordedAction", "ACTION_KINDS"]

_log = logging.getLogger("pypilot.recorder")

#: Kinds of :class:`RecordedAction`.
ACTION_KINDS = ("click", "double_click", "triple_click", "right_click", "middle_click", "drag", "scroll", "type", "press", "hotkey",
                "set_text", "invoke", "toggle", "select", "expand", "collapse")
# Native element actions (performed through the accessibility API, no input events).
_NATIVE_KINDS = ("set_text", "invoke", "toggle", "select", "expand", "collapse")

# Roles whose center is not equivalent to "anywhere inside": clicks on them keep an offset.
_CONTAINER_ROLES = frozenset({
    "desktop", "application", "window", "dialog", "pane", "group", "toolbar", "statusbar", "titlebar", "menubar",
    "menu", "list", "tree", "table", "document", "tablist", "tabpanel", "scrollbar", "slider", "image", "unknown", "",
})
_TEXT_INPUT_ROLES = frozenset({"edit", "combobox", "document", "spinbutton"})
_SHIFT_UP = {base: shifted for shifted, base in SHIFTED_CHARS.items()}
# Modifiers that turn a key press into a shortcut (shift alone just changes the character).
_CHORD_MODS = ("ctrl", "alt", "cmd")
_MOD_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "cmd": 3}
_PYNPUT_NAMES = {
    "ctrl_l": "ctrl", "shift_l": "shift", "alt_l": "alt", "cmd_l": "cmd", "alt_gr": "altgr",
    "page_up": "pageup", "page_down": "pagedown", "caps_lock": "capslock", "num_lock": "numlock",
    "scroll_lock": "scrolllock", "print_screen": "printscreen", "esc": "escape",
    "media_play_pause": "playpause", "media_next": "nexttrack", "media_previous": "prevtrack",
    "media_volume_up": "volumeup", "media_volume_down": "volumedown", "media_volume_mute": "volumemute",
}


def _base_mod(key: str) -> str:
    return key[:-2] if key.endswith("_r") else key


# X11 keysym / pynput spellings that keys.normalize_key does not know.
_EXTRA_ALIASES = {
    "control_l": "ctrl", "control_r": "ctrl_r", "ctrl_l": "ctrl", "shift_l": "shift", "alt_l": "alt",
    "alt_gr": "altgr", "iso_level3_shift": "altgr", "mode_switch": "altgr",
    "super_l": "cmd", "super_r": "cmd_r", "meta_l": "cmd", "meta_r": "cmd_r", "win_l": "cmd", "win_r": "cmd_r",
    "cmd_l": "cmd", "hyper_l": "cmd", "hyper_r": "cmd_r",
}


def _canon(key: Any) -> Optional[str]:
    if not isinstance(key, str) or key == "":
        return None
    extra = _EXTRA_ALIASES.get(key.lower()) if len(key) > 1 else None
    if extra is not None:
        return extra
    try:
        return normalize_key(key)
    except ValueError:
        return key.lower() if len(key) > 1 else key


def _escape_flow_strings(value: Any) -> Any:
    """Copy of ``value`` with ``${`` in every string written as the literal ``$${``."""
    if isinstance(value, str):
        return value.replace("${", "$${")
    if isinstance(value, dict):
        return {k: _escape_flow_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_escape_flow_strings(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_escape_flow_strings(v) for v in value)
    return value


@dataclass
class RecordedAction:
    """One coalesced user action.

    Attributes:
        kind: One of :data:`ACTION_KINDS`.
        time: Clock time of the action's first event.
        x, y: Screen point (click/drag start/scroll position).
        selector: Accessibility selector of the target (clicks, scroll, ``type`` target).
        offset: Click offset from the target element's center (containers only).
        element: ``{"role", "name", "automation_id"}`` of the target, for humans.
        button, clicks: Mouse button and click count.
        end_x, end_y: Drag end point.
        dx, dy: Scroll notches (``dy > 0`` = up).
        text: Typed text (``type``) or the field's new content (``set_text``).
        keys: Key names (``press``: one key; ``hotkey``: the chord).
        presses: Repeat count for ``press``.
    """

    kind: str
    time: float = 0.0
    x: Optional[int] = None
    y: Optional[int] = None
    selector: Optional[str] = None
    offset: Optional[Tuple[int, int]] = None
    element: Optional[Dict[str, Any]] = None
    button: str = "left"
    clicks: int = 1
    end_x: Optional[int] = None
    end_y: Optional[int] = None
    dx: int = 0
    dy: int = 0
    text: str = ""
    keys: List[str] = field(default_factory=list)
    presses: int = 1
    _last_time: float = field(default=0.0, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"kind must be one of {', '.join(ACTION_KINDS)}, got {self.kind!r}")

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"kind": self.kind, "time": round(self.time, 4)}
        if self.kind in ("click", "double_click", "triple_click", "right_click", "middle_click"):
            d.update(x=self.x, y=self.y, button=self.button, clicks=self.clicks)
        elif self.kind == "drag":
            d.update(x=self.x, y=self.y, end_x=self.end_x, end_y=self.end_y, button=self.button)
        elif self.kind == "scroll":
            d.update(x=self.x, y=self.y, dx=self.dx, dy=self.dy)
        elif self.kind in ("type", "set_text"):
            d["text"] = self.text
        elif self.kind == "press":
            d.update(keys=list(self.keys), presses=self.presses)
        elif self.kind == "hotkey":
            d["keys"] = list(self.keys)
        if self.selector:
            d["selector"] = self.selector
        if self.offset:
            d["offset"] = list(self.offset)
        if self.element:
            d["element"] = dict(self.element)
        return d

    # -- targets ----------------------------------------------------------------
    def _flow_target(self) -> Any:
        if self.selector:
            return self.selector
        return f"{self.x},{self.y}"

    def _py_target(self) -> str:
        if self.selector:
            return repr(self.selector)
        return f"({self.x}, {self.y})"

    # -- emitters ------------------------------------------------------------------
    def to_flow_step(self) -> Dict[str, Any]:
        """This action as one :mod:`pypilot.rpa.flow` step.

        Every recorded string (typed text, selectors, key names) is escaped for
        flow interpolation (``${`` -> ``$${``), so replaying someone who typed
        ``${total}`` types exactly that instead of substituting a variable or an
        environment value.
        """
        return _escape_flow_strings(self._raw_flow_step())

    def _raw_flow_step(self) -> Dict[str, Any]:
        k = self.kind
        if k in ("click", "double_click", "triple_click", "right_click", "middle_click"):
            target = self._flow_target()
            standard = {
                ("left", 1): "click", ("left", 2): "double_click", ("right", 1): "right_click", ("middle", 1): "middle_click",
            }.get((self.button, self.clicks))
            args: Dict[str, Any] = {"target": target}
            if standard is None:
                step_kind = "click"
                args["button"] = self.button
                args["clicks"] = self.clicks
            else:
                step_kind = standard
            if self.offset and self.selector:
                args["offset"] = [int(self.offset[0]), int(self.offset[1])]
            return {step_kind: target if len(args) == 1 else args}
        if k == "drag":
            args = {"start": [self.x, self.y], "end": [self.end_x, self.end_y]}
            if self.button != "left":
                args["button"] = self.button
            return {"drag": args}
        if k == "scroll":
            args = {"dy": self.dy}
            if self.dx:
                args["dx"] = self.dx
            args["at"] = self.selector if self.selector else [self.x, self.y]
            return {"scroll": args}
        if k == "type":
            if self.selector:
                return {"type": {"text": self.text, "into": self.selector}}
            return {"type": self.text}
        if k == "set_text":
            return {"set_text": {"target": self._flow_target(), "text": self.text}}
        if k in ("invoke", "toggle", "select", "expand", "collapse"):
            if not self.selector:  # recorded without a selector: replay as a click on the spot
                return {"click": self._flow_target()}
            return {k: self.selector}
        if k == "press":
            key = self.keys[0] if len(self.keys) == 1 else list(self.keys)
            if self.presses != 1:
                return {"press": {"keys": key, "presses": self.presses}}
            return {"press": key}
        # hotkey
        return {"hotkey": "+".join(self.keys)}

    def to_python(self, var: str = "d") -> str:
        """This action as one line of Python using the :class:`~pypilot.Desktop` API."""
        k = self.kind
        where = f"  # at ({self.x}, {self.y})" if self.selector and self.x is not None else ""
        if k in ("click", "double_click", "triple_click", "right_click", "middle_click"):
            target = self._py_target()
            off = f", offset=({self.offset[0]}, {self.offset[1]})" if self.offset and self.selector else ""
            method = {
                ("left", 1): "click", ("left", 2): "double_click", ("left", 3): "triple_click",
                ("right", 1): "right_click", ("middle", 1): "middle_click",
            }.get((self.button, self.clicks))
            if method is None:
                return f"{var}.click({target}, button={self.button!r}, clicks={self.clicks}{off}){where}"
            return f"{var}.{method}({target}{off}){where}"
        if k == "drag":
            src = f"  # from {self.selector}" if self.selector else ""
            btn = f", button={self.button!r}" if self.button != "left" else ""
            return f"{var}.drag(({self.x}, {self.y}), ({self.end_x}, {self.end_y}){btn}){src}"
        if k == "scroll":
            at = repr(self.selector) if self.selector else f"({self.x}, {self.y})"
            dx = f", dx={self.dx}" if self.dx else ""
            return f"{var}.scroll({self.dy}{dx}, at={at})"
        if k == "type":
            if self.selector:
                return f"{var}.find({self.selector!r}).type({self.text!r})"
            return f"{var}.type({self.text!r})"
        if k == "set_text":
            if self.selector:
                return f"{var}.find({self.selector!r}).set_text({self.text!r})"
            return f"{var}.click(({self.x}, {self.y})); {var}.hotkey('mod+a'); {var}.type({self.text!r})"
        if k in ("invoke", "toggle", "select", "expand", "collapse"):
            if self.selector:
                return f"{var}.find({self.selector!r}).{k}()"
            return f"{var}.click(({self.x}, {self.y}))  # {k}"
        if k == "press":
            keys = ", ".join(repr(x) for x in self.keys)
            presses = f", presses={self.presses}" if self.presses != 1 else ""
            return f"{var}.press({keys}{presses})"
        return f"{var}.hotkey({'+'.join(self.keys)!r})"


class Recorder:
    """Record user (or programmatic) input as high-level actions (see module docstring).

    Args:
        desktop: The desktop whose backend provides ``element_at`` / the focus
            (default: a new :class:`~pypilot.Desktop` on first use).
        use_selectors: Enrich clicks/typing with accessibility selectors.
        double_click_interval: Max seconds between clicks of a multi-click.
        double_click_distance: Max pixels between clicks of a multi-click.
        drag_threshold: Pixels the pointer must travel while pressed to count as a drag.
        clock: Time source used for events without an explicit time.
    """

    def __init__(
        self,
        desktop: Optional["Desktop"] = None,
        *,
        use_selectors: bool = True,
        double_click_interval: float = 0.5,
        double_click_distance: float = 4.0,
        drag_threshold: float = 6.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._desktop = desktop
        self.use_selectors = bool(use_selectors)
        self.double_click_interval = float(double_click_interval)
        self.double_click_distance = float(double_click_distance)
        self.drag_threshold = float(drag_threshold)
        self._clock = clock
        self._lock = threading.RLock()
        self._actions: List[RecordedAction] = []
        self._source: Optional[str] = None
        self._hooks: Dict[str, Any] = {}
        self._listeners: List[Any] = []
        self.started_at: Optional[float] = None
        self._reset_state()

    def _reset_state(self) -> None:
        self._mods: Dict[str, bool] = {}  # held modifier -> used in a chord / with a key
        self._pressed: Dict[str, Tuple[int, int, float, Dict[str, Any]]] = {}
        self._buffer = ""
        self._buffer_time = 0.0
        self._buffer_target: Dict[str, Any] = {}
        self._pos: Optional[Tuple[int, int]] = None

    # ----------------------------------------------------------------- plumbing
    @property
    def desktop(self) -> "Desktop":
        if self._desktop is None:
            from ..desktop import Desktop

            self._desktop = Desktop()
        return self._desktop

    @property
    def backend(self) -> Any:
        return self.desktop.backend

    @property
    def is_recording(self) -> bool:
        return self._source is not None

    @property
    def source(self) -> Optional[str]:
        return self._source

    @property
    def actions(self) -> List[RecordedAction]:
        """Finished actions so far (pending typed text is included only after :meth:`flush`/:meth:`stop`)."""
        with self._lock:
            return list(self._actions)

    def clear(self) -> None:
        """Forget every recorded action and any pending input state."""
        with self._lock:
            self._actions.clear()
            self._reset_state()

    # -------------------------------------------------------------- lifecycle
    def start(self, source: str = "auto") -> "Recorder":
        """Begin recording from ``source`` (``"auto"``/``"pynput"``, ``"backend"`` or ``"manual"``). Returns ``self``."""
        src = str(source).lower()
        if src not in ("auto", "pynput", "backend", "manual"):
            raise ValueError(f"source must be 'auto', 'pynput', 'backend' or 'manual', got {source!r}")
        with self._lock:
            if self._source is not None:
                raise RuntimeError(f"Recorder is already recording (source={self._source!r})")
            self._reset_state()
            self.started_at = time.time()
            if src in ("auto", "pynput"):
                self._start_pynput()
                self._source = "pynput"
            elif src == "backend":
                self._install_backend_hooks()
                self._source = "backend"
            else:
                self._source = "manual"
            if self._source != "manual":
                self._install_action_listener()
        return self

    def stop(self) -> List[RecordedAction]:
        """Stop recording, flush pending input and return all recorded actions."""
        with self._lock:
            source, self._source = self._source, None
        if source == "pynput":
            for listener in self._listeners:
                try:
                    listener.stop()
                except Exception:  # pragma: no cover - listener already dead
                    pass
            self._listeners = []
        elif source == "backend":
            self._remove_backend_hooks()
        self._remove_action_listener()
        with self._lock:
            self.flush()
            self._pressed.clear()
            self._mods.clear()
            return list(self._actions)

    def __enter__(self) -> "Recorder":
        if not self.is_recording:
            self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.is_recording:
            self.stop()

    def flush(self) -> None:
        """Turn pending typed text into a ``type`` action now."""
        with self._lock:
            self._flush_typing()

    # -------------------------------------------------------------- the core
    def feed(self, event: Union[Sequence[Any], Mapping[str, Any]]) -> None:
        """Feed one low-level input event.

        Tuple form (the :class:`~pypilot.backends.mock.MockBackend` event-log format):
        ``("move", x, y)``, ``("down", button, x, y)``, ``("up", button, x, y)``,
        ``("scroll", dx, dy, x, y)``, ``("key_down", key)``, ``("key_up", key)``,
        ``("type", text)``. Dict form: ``{"event": "down", "button": "left",
        "x": 10, "y": 20, "time": 1.5}`` (``"type"`` is accepted for ``"event"``;
        ``"key"``, ``"text"``, ``"dx"``, ``"dy"`` as named). ``time`` is optional
        (default: the recorder's clock). Unknown event kinds are ignored, so a whole
        backend event log can be fed as-is.
        """
        ev = self._normalize(event)
        if ev is None:
            return
        with self._lock:
            kind = ev["event"]
            t = ev["time"]
            if kind == "move":
                self._pos = (ev["x"], ev["y"])
            elif kind == "down":
                self._on_down(ev["button"], ev["x"], ev["y"], t)
            elif kind == "up":
                self._on_up(ev["button"], ev["x"], ev["y"], t)
            elif kind == "scroll":
                self._on_scroll(ev["dx"], ev["dy"], ev["x"], ev["y"], t)
            elif kind == "key_down":
                self._on_key_down(ev["key"], t)
            elif kind == "key_up":
                self._on_key_up(ev["key"], t)
            elif kind == "type":
                self._on_text(ev["text"], t)

    def _normalize(self, event: Union[Sequence[Any], Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        if isinstance(event, Mapping):
            kind = event.get("event", event.get("type"))
            d = dict(event)
            d["event"] = kind
        elif isinstance(event, (list, tuple)) and event:
            kind = event[0]
            args = list(event[1:])
            d = {"event": kind}
            try:
                if kind == "move":
                    d["x"], d["y"] = args[0], args[1]
                elif kind in ("down", "up"):
                    if len(args) >= 3:
                        d["button"], d["x"], d["y"] = args[0], args[1], args[2]
                    else:
                        d["button"] = args[0] if args else "left"
                elif kind == "scroll":
                    d["dx"], d["dy"] = args[0], args[1]
                    if len(args) >= 4:
                        d["x"], d["y"] = args[2], args[3]
                elif kind in ("key_down", "key_up"):
                    d["key"] = args[0]
                elif kind == "type":
                    d["text"] = args[0]
                else:
                    return None
            except IndexError:
                raise ValueError(f"Malformed {kind!r} event: {event!r}") from None
        else:
            raise TypeError(f"An event must be a tuple or a mapping, got {event!r}")
        kind = d.get("event")
        if kind not in ("move", "down", "up", "scroll", "key_down", "key_up", "type"):
            return None
        t = d.get("time", d.get("t"))
        d["time"] = float(t) if t is not None else self._clock()
        if kind in ("down", "up", "scroll", "move"):
            if d.get("x") is None or d.get("y") is None:
                if self._pos is None:
                    pos = self._current_position()
                    if pos is None:
                        raise ValueError(f"{kind!r} event without coordinates and no known pointer position")
                    self._pos = pos
                d["x"], d["y"] = self._pos
            d["x"], d["y"] = int(round(float(d["x"]))), int(round(float(d["y"])))
        if kind in ("down", "up"):
            try:
                d["button"] = MouseButton.coerce(d.get("button", "left")).value
            except ValueError:
                return None
        if kind == "scroll":
            d["dx"], d["dy"] = int(d.get("dx", 0) or 0), int(d.get("dy", 0) or 0)
        if kind in ("key_down", "key_up"):
            key = _canon(d.get("key"))
            if key is None:
                return None
            d["key"] = key
        if kind == "type":
            d["text"] = str(d.get("text", ""))
        return d

    def _current_position(self) -> Optional[Tuple[int, int]]:
        try:
            p = self.backend.mouse_position()
            return (int(p[0]), int(p[1]))
        except Exception:
            return None

    # -- target capture ---------------------------------------------------------
    def _describe(self, node: Any) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        for attr in ("role", "name", "automation_id"):
            try:
                v = getattr(node, attr)
            except Exception:
                v = None
            if v:
                info[attr] = v
        return info

    def _selector(self, node: Any) -> Optional[str]:
        from ..selectors import parse, selector_for

        try:
            roots = list(self.backend.accessibility_roots())
            sel = selector_for(node, roots=roots)
            found = parse(sel).find_first(roots)
        except Exception as exc:
            _log.debug("selector generation failed: %s", exc)
            return None
        if found is None or not found.same_as(node) and not node.same_as(found):
            return None
        return sel

    def _capture_point(self, x: int, y: int) -> Dict[str, Any]:
        """Selector (+offset for containers) of the element at ``(x, y)``; empty when unavailable."""
        if not self.use_selectors:
            return {}
        try:
            node = self.backend.element_at(x, y)
        except Exception as exc:  # no accessibility support
            _log.debug("element_at(%s, %s) failed: %s", x, y, exc)
            return {}
        if node is None:
            return {}
        try:
            bounds = node.bounds
            role = (node.role or "").lower()
        except Exception:
            return {}
        if bounds is None or bounds.is_empty:
            return {}
        sel = self._selector(node)
        if sel is None:
            return {}
        out: Dict[str, Any] = {"selector": sel, "element": self._describe(node)}
        if role in _CONTAINER_ROLES:
            c = bounds.center
            off = (x - c.x, y - c.y)
            if off != (0, 0):
                out["offset"] = off
        return out

    def _with_ancestry(self, node: Any) -> Any:
        """Some backends return the focused node without parent links; re-find it by
        position so the generated selector can include its window."""
        try:
            if node.parent is not None:
                return node
            b = node.bounds
            if b is None or b.is_empty:
                return node
            c = b.center
            hit = self.backend.element_at(c.x, c.y)
            if hit is not None and hit.same_as(node):
                return hit
        except Exception:
            pass
        return node

    def _capture_focus(self) -> Dict[str, Any]:
        if not self.use_selectors:
            return {}
        try:
            node = self.backend.focused_element()
        except Exception:
            return {}
        if node is None:
            return {}
        try:
            role = (node.role or "").lower()
            editable = "editable" in node.states
        except Exception:
            return {}
        if role not in _TEXT_INPUT_ROLES and not editable:
            return {}
        node = self._with_ancestry(node)
        sel = self._selector(node)
        return {"selector": sel, "element": self._describe(node)} if sel else {}

    # -- mouse ----------------------------------------------------------------------
    def _emit(self, action: RecordedAction) -> RecordedAction:
        action._last_time = action._last_time or action.time
        self._actions.append(action)
        return action

    def _flush_typing(self) -> None:
        if not self._buffer:
            self._buffer_target = {}
            return
        target = self._buffer_target
        self._emit(RecordedAction("type", time=self._buffer_time, text=self._buffer,
                                  selector=target.get("selector"), element=target.get("element")))
        self._buffer = ""
        self._buffer_target = {}

    def _on_down(self, button: str, x: int, y: int, t: float) -> None:
        self._flush_typing()
        self._pos = (x, y)
        self._pressed[button] = (x, y, t, self._capture_point(x, y))

    def _on_up(self, button: str, x: int, y: int, t: float) -> None:
        self._pos = (x, y)
        press = self._pressed.pop(button, None)
        if press is None:  # pressed before recording started
            return
        x0, y0, t0, target = press
        if math.hypot(x - x0, y - y0) > self.drag_threshold:
            self._emit(RecordedAction("drag", time=t0, x=x0, y=y0, end_x=x, end_y=y, button=button,
                                      selector=target.get("selector"), element=target.get("element")))
            return
        last = self._actions[-1] if self._actions else None
        if (
            last is not None
            and last.kind in ("click", "double_click", "triple_click", "right_click", "middle_click")
            and last.button == button
            and last.clicks < 3
            and 0 <= t0 - last._last_time <= self.double_click_interval
            and last.x is not None and last.y is not None
            and math.hypot(x0 - last.x, y0 - last.y) <= self.double_click_distance
        ):
            last.clicks += 1
            last._last_time = t
            last.kind = _click_kind(button, last.clicks)
            return
        action = RecordedAction(_click_kind(button, 1), time=t0, x=x0, y=y0, button=button,
                                selector=target.get("selector"), offset=target.get("offset"),
                                element=target.get("element"))
        action._last_time = t
        self._emit(action)

    def _on_scroll(self, dx: int, dy: int, x: int, y: int, t: float) -> None:
        self._flush_typing()
        self._pos = (x, y)
        last = self._actions[-1] if self._actions else None
        if (
            last is not None and last.kind == "scroll" and last.x is not None and last.y is not None
            and math.hypot(x - last.x, y - last.y) <= self.double_click_distance
            and (dy == 0 or last.dy == 0 or (dy > 0) == (last.dy > 0))
            and (dx == 0 or last.dx == 0 or (dx > 0) == (last.dx > 0))
        ):
            last.dx += dx
            last.dy += dy
            last._last_time = t
            return
        target = self._capture_point(x, y)
        sel = target.get("selector")
        if sel and target.get("offset"):  # a container: scroll at the exact point instead of its center
            sel = None
        self._emit(RecordedAction("scroll", time=t, x=x, y=y, dx=dx, dy=dy, selector=sel, element=target.get("element")))

    # -- keyboard -------------------------------------------------------------------
    def _on_key_down(self, key: str, t: float) -> None:
        if key in MODIFIERS:
            self._mods.setdefault(_base_mod(key), False)
            return
        held = set(self._mods)
        chord = [m for m in _CHORD_MODS if m in held]
        shift = "shift" in held
        if chord:
            for m in held:
                self._mods[m] = True
            self._flush_typing()
            mods = sorted(held - {"altgr", "fn"}, key=lambda m: _MOD_ORDER.get(m, 9))
            k = key.lower() if len(key) == 1 else key
            self._emit(RecordedAction("hotkey", time=t, keys=mods + [k]))
            return
        ch = _char_for(key, shift)
        if ch is not None:
            if shift:
                self._mods["shift"] = True
            self._append_text(ch, t)
            return
        if key == "backspace" and self._buffer and not shift:
            self._buffer = self._buffer[:-1]
            return
        self._flush_typing()
        if shift:
            self._mods["shift"] = True
            self._emit(RecordedAction("hotkey", time=t, keys=["shift", key]))
            return
        last = self._actions[-1] if self._actions else None
        if last is not None and last.kind == "press" and last.keys == [key]:
            last.presses += 1
            last._last_time = t
            return
        self._emit(RecordedAction("press", time=t, keys=[key]))

    def _on_key_up(self, key: str, t: float) -> None:
        if key not in MODIFIERS:
            return
        base = _base_mod(key)
        used = self._mods.pop(base, True)
        if not used and base in ("ctrl", "alt", "cmd"):
            # A modifier tapped alone (e.g. alt to focus the menu bar).
            self._flush_typing()
            self._emit(RecordedAction("press", time=t, keys=[base]))

    def _append_text(self, text: str, t: float) -> None:
        if not text:
            return
        if not self._buffer:
            self._buffer_time = t
            self._buffer_target = self._capture_focus()
        self._buffer += text

    def _on_text(self, text: str, t: float) -> None:
        self._append_text(text, t)

    # ---------------------------------------------------------- native actions
    def record_native(self, kind: str, element: Any, text: Optional[str] = None) -> Optional[RecordedAction]:
        """Record a native element action (``set_text``, ``invoke``, ``toggle``,
        ``select``, ``expand``, ``collapse``) performed on ``element`` (an
        :class:`~pypilot.element.Element` or accessibility node).

        Called automatically while recording from the ``backend``/``pynput``
        sources: pypilot's native actions produce no input events, so input
        hooks alone would miss them and the replay would differ.
        """
        if kind not in _NATIVE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(_NATIVE_KINDS)}, got {kind!r}")
        node = getattr(element, "node", element)
        with self._lock:
            self._flush_typing()
            sel = self._selector(node) if self.use_selectors else None
            if sel is None:
                given = getattr(element, "selector", None)
                scope = getattr(element, "_scope", None)
                if isinstance(given, str) and given and scope is None:
                    sel = given  # desktop-wide selector the script itself used
            x = y = None
            try:
                b = node.bounds
                if b is not None and not b.is_empty:
                    x, y = b.center.x, b.center.y
            except Exception:
                pass
            if sel is None and x is None:
                _log.warning("cannot record %s on %r: no selector and no position", kind, element)
                return None
            action = RecordedAction(kind, time=self._clock(), x=x, y=y, selector=sel,
                                    element=self._describe(node), text=text or "")
            return self._emit(action)

    def _install_action_listener(self) -> None:
        from ..element import add_action_listener

        def on_action(kind: str, element: Any, data: Dict[str, Any]) -> None:
            if kind in _NATIVE_KINDS and self._source is not None:
                self.record_native(kind, element, data.get("text"))

        self._action_listener = (self.backend, on_action)
        add_action_listener(self.backend, on_action)

    def _remove_action_listener(self) -> None:
        entry = getattr(self, "_action_listener", None)
        if not entry:
            return
        from ..element import remove_action_listener

        remove_action_listener(entry[0], entry[1])
        self._action_listener = None

    # ----------------------------------------------------------------- sources
    def _install_backend_hooks(self) -> None:
        be = self.backend
        rec = self

        def safe_feed(event: Any) -> None:
            try:
                rec.feed(event)
            except Exception as exc:  # recording must never break the automation
                _log.warning("recorder failed to process %r: %s", event, exc)

        def pos() -> Tuple[int, int]:
            p = originals["mouse_position"]()
            return int(p[0]), int(p[1])

        originals: Dict[str, Any] = {}
        for name in ("mouse_position", "mouse_move", "mouse_down", "mouse_up", "scroll", "key_down", "key_up", "type_text"):
            originals[name] = getattr(be, name)

        def mouse_move(x: int, y: int) -> None:
            originals["mouse_move"](x, y)
            safe_feed(("move",) + pos())

        def mouse_down(button: Any = MouseButton.LEFT) -> None:
            safe_feed(("down", button) + pos())
            originals["mouse_down"](button)

        def mouse_up(button: Any = MouseButton.LEFT) -> None:
            safe_feed(("up", button) + pos())
            originals["mouse_up"](button)

        def scroll(dx: int = 0, dy: int = 0) -> None:
            safe_feed(("scroll", dx, dy) + pos())
            originals["scroll"](dx, dy)

        def key_down(key: str) -> None:
            safe_feed(("key_down", key))
            originals["key_down"](key)

        def key_up(key: str) -> None:
            safe_feed(("key_up", key))
            originals["key_up"](key)

        def type_text(text: str) -> None:
            originals["type_text"](text)  # raises NotSupported -> keyboard falls back to keys, which we see
            safe_feed(("type", text))

        hooks = {"mouse_move": mouse_move, "mouse_down": mouse_down, "mouse_up": mouse_up, "scroll": scroll,
                 "key_down": key_down, "key_up": key_up, "type_text": type_text}
        self._hooks = {"backend": be, "had": {n: n in vars(be) for n in hooks}, "originals": originals}
        for name, fn in hooks.items():
            setattr(be, name, fn)

    def _remove_backend_hooks(self) -> None:
        if not self._hooks:
            return
        be = self._hooks["backend"]
        for name, had in self._hooks["had"].items():
            if had:
                setattr(be, name, self._hooks["originals"][name])
            else:
                try:
                    delattr(be, name)
                except AttributeError:  # pragma: no cover
                    pass
        self._hooks = {}

    def _start_pynput(self) -> None:
        try:
            from pynput import keyboard as pkb  # type: ignore
            from pynput import mouse as pmouse  # type: ignore
        except Exception as exc:  # ImportError, or pynput's own "platform not supported"
            import importlib.util

            missing = isinstance(exc, ModuleNotFoundError) or "No module named" in str(exc)
            if not missing:
                try:
                    missing = importlib.util.find_spec("pynput") is None
                except (ImportError, ValueError):
                    missing = True
            hint = (
                "Install it with: pip install pypilot[record]"
                if missing
                else "pynput is installed but cannot hook input here (is a graphical session/display available?)"
            )
            raise NotSupported(
                f"Live recording needs pynput and a desktop session ({type(exc).__name__}: {exc}). "
                f"{hint}  -- or use start(source='backend') / start(source='manual') with feed()."
            ) from exc
        rec = self

        def safe(event: Any) -> None:
            try:
                rec.feed(event)
            except Exception as err:
                _log.warning("recorder failed to process %r: %s", event, err)

        def on_move(x: Any, y: Any) -> None:
            safe(("move", x, y))

        def on_click(x: Any, y: Any, button: Any, pressed: bool) -> None:
            safe(("down" if pressed else "up", getattr(button, "name", str(button)), x, y))

        def on_scroll(x: Any, y: Any, dx: Any, dy: Any) -> None:
            safe(("scroll", dx, dy, x, y))

        def on_press(key: Any) -> None:
            name = rec._pynput_key(key)
            if name is not None:
                safe(("key_down", name))

        def on_release(key: Any) -> None:
            name = rec._pynput_key(key)
            if name is not None:
                safe(("key_up", name))

        ml = pmouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)
        kl = pkb.Listener(on_press=on_press, on_release=on_release)
        ml.start()
        kl.start()
        self._listeners = [ml, kl]

    def _pynput_key(self, key: Any) -> Optional[str]:
        """Canonical key name for a pynput ``Key``/``KeyCode``."""
        ch = getattr(key, "char", None)
        if ch:
            if len(ch) == 1 and ord(ch) < 32 and self._mods.get("ctrl") is not None:
                return chr(ord(ch) + 96)  # ctrl+a arrives as '\x01' on some platforms
            return ch
        name = getattr(key, "name", None)
        if not name:
            return None
        name = _PYNPUT_NAMES.get(name, name)
        return _canon(name)

    # ------------------------------------------------------------------ output
    def _final_actions(self) -> List[RecordedAction]:
        with self._lock:
            actions = list(self._actions)
            if self._buffer:
                t = self._buffer_target
                actions.append(RecordedAction("type", time=self._buffer_time, text=self._buffer,
                                              selector=t.get("selector"), element=t.get("element")))
            return actions

    def to_flow(self, name: str = "recorded") -> Dict[str, Any]:
        """The recording as a flow document (plain dicts/lists/strings: YAML- and JSON-ready)."""
        return {
            "name": name,
            "description": f"Recorded with pypilot on {_dt.datetime.now().isoformat(timespec='seconds')}",
            "steps": [a.to_flow_step() for a in self._final_actions()],
        }

    def to_python(self, *, desktop_expr: str = "Desktop()", var: str = "d") -> str:
        """The recording as a runnable Python script using :class:`pypilot.Desktop`."""
        if not var.isidentifier():
            raise ValueError(f"var must be a Python identifier, got {var!r}")
        lines = [
            f'"""Recorded with pypilot on {_dt.datetime.now().isoformat(timespec="seconds")}."""',
            "from pypilot import Desktop",
            "",
            f"{var} = {desktop_expr}",
            "",
        ]
        actions = self._final_actions()
        if not actions:
            lines.append("# (no actions were recorded)")
        lines.extend(a.to_python(var) for a in actions)
        return "\n".join(lines) + "\n"

    def save(self, path: Union[str, "pathlib.Path"], *, name: Optional[str] = None) -> str:
        """Write the recording: ``.py`` (script), ``.yaml``/``.yml`` (flow; needs PyYAML) or ``.json`` (flow)."""
        p = pathlib.Path(path).expanduser()
        ext = p.suffix.lower()
        flow_name = name or p.stem or "recorded"
        if ext == ".py":
            text = self.to_python()
        elif ext == ".json":
            text = json.dumps(self.to_flow(flow_name), indent=2, ensure_ascii=False) + "\n"
        elif ext in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise ImportError("Saving YAML flows requires PyYAML: pip install pypilot[yaml]") from exc
            text = yaml.safe_dump(self.to_flow(flow_name), sort_keys=False, allow_unicode=True)
        else:
            raise ValueError(f"Unsupported recording format {ext!r}: use .py, .yaml, .yml or .json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return str(p)

    def __repr__(self) -> str:
        return f"<Recorder source={self._source!r} actions={len(self._actions)}>"


def _click_kind(button: str, clicks: int) -> str:
    if button == "left":
        return {1: "click", 2: "double_click"}.get(clicks, "triple_click")
    if clicks == 1 and button in ("right", "middle"):
        return f"{button}_click"
    return "click"


def _char_for(key: str, shift: bool) -> Optional[str]:
    """The character a key produces, or ``None`` for non-character keys."""
    if key == "space":
        return " "
    if len(key) != 1 or not key.isprintable():
        return None
    if shift:
        return _SHIFT_UP.get(key, key.upper())
    return key
