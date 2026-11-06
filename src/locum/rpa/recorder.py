"""Action recorder: turn live mouse/keyboard input into replayable flows and scripts.

The recorder is an event-driven state machine fed with low-level input
events. It coalesces them into meaningful actions, the way UiPath's recorder
does:

* consecutive characters become one ``type`` action (backspace edits the
  pending text), recorded with the **focused field's selector**;
* modifier chords become ``hotkey`` actions (``ctrl+s``); repeated special
  keys collapse into one ``press`` with a count (``tab`` x3);
* clicks are enriched with a robust accessibility selector
  (:func:`locum.selectors.selector_for` of ``backend.element_at(x, y)``),
  falling back to screen coordinates when nothing usable is under the cursor;
  multi-clicks become ``double_click``/``triple_click``; press-move-release
  becomes ``drag``; wheel events at one spot are summed into one ``scroll``.

Event sources (:meth:`Recorder.start`):

* ``"pynput"`` -- global OS hooks (``pip install locum[record]``); the
  default ``"auto"`` source.
* ``"backend"`` -- hooks the desktop's backend instance, recording everything
  locum itself does (a script, an LLM agent...) so it can be replayed later.

With the ``backend`` and ``pynput`` sources, *native* element actions that
produce no input events (``Element.set_text`` through the accessibility API,
``invoke``, ``toggle``, ``select``, ``expand``, ``collapse`` -- including a
``click(native="auto")`` that falls back to ``invoke``) are recorded too, as
``set_text``/``invoke``/... actions with the element's selector.
* ``"manual"`` -- nothing is hooked; call :meth:`Recorder.feed` yourself.

Output: :meth:`Recorder.to_flow` (a YAML/JSON-ready dict for
:func:`locum.rpa.flow.run_flow`), :meth:`Recorder.to_python` (a script using
the :class:`locum.Desktop` API) and :meth:`Recorder.save`.

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
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .._nodeinfo import is_password
from ..errors import NotSupported
from ..keys import MODIFIERS, SHIFTED_CHARS, normalize_key
from ..types import MouseButton

if TYPE_CHECKING:  # pragma: no cover
    from ..desktop import Desktop

__all__ = ["Recorder", "RecordedAction", "ACTION_KINDS", "PASSWORD_ENV"]

_log = logging.getLogger("locum.recorder")

#: Attribute of a backend holding the :class:`_BackendHooks` of its ``source="backend"`` recorders.
_HOOKS_ATTR = "_locum_recorder_hooks"
_HOOKS_LOCK = threading.Lock()
_HOOKED = ("mouse_move", "mouse_down", "mouse_up", "scroll", "key_down", "key_up", "type_text", "set_clipboard")


class _BackendHooks:
    """One set of hooks per backend, shared by every recorder using ``source="backend"`` on it.

    Each hooked primitive calls the original and feeds the event to every recorder
    active at that moment, so recorders can start and stop in any order: each one
    records only between its own start and stop, and the backend is unhooked when
    the last one stops.
    """

    def __init__(self, be: Any) -> None:
        self.backend = be
        self.recorders: List["Recorder"] = []
        self.had = {n: n in vars(be) for n in _HOOKED}
        self.originals: Dict[str, Any] = {n: getattr(be, n) for n in _HOOKED + ("mouse_position",)}
        self.hooks = self._make_hooks()
        for name, fn in self.hooks.items():
            setattr(be, name, fn)

    def _feed(self, event: Any) -> None:
        for rec in list(self.recorders):
            rec._feed_hooked(event)

    def _make_hooks(self) -> Dict[str, Callable[..., Any]]:
        originals, feed = self.originals, self._feed

        def pos() -> Tuple[int, int]:
            p = originals["mouse_position"]()
            return int(p[0]), int(p[1])

        def mouse_move(x: int, y: int) -> None:
            originals["mouse_move"](x, y)
            if self.recorders:
                feed(("move",) + pos())

        def mouse_down(button: Any = MouseButton.LEFT) -> None:
            if self.recorders:
                feed(("down", button) + pos())
            originals["mouse_down"](button)

        def mouse_up(button: Any = MouseButton.LEFT) -> None:
            if self.recorders:
                feed(("up", button) + pos())
            originals["mouse_up"](button)

        def scroll(dx: int = 0, dy: int = 0) -> None:
            if self.recorders:
                feed(("scroll", dx, dy) + pos())
            originals["scroll"](dx, dy)

        def key_down(key: str) -> None:
            feed(("key_down", key))
            originals["key_down"](key)

        def key_up(key: str) -> None:
            feed(("key_up", key))
            originals["key_up"](key)

        def type_text(text: str) -> None:
            originals["type_text"](text)  # raises NotSupported -> keyboard falls back to keys, which we see
            feed(("type", text))

        def set_clipboard(text: str) -> None:
            originals["set_clipboard"](text)
            # Keyboard.type(mode="clipboard") (and the non-ASCII fallback of "auto") pastes
            # with mod+v: remember the text so that paste is recorded as typed text.
            for rec in list(self.recorders):
                with rec._lock:
                    rec._paste = text if isinstance(text, str) and text else None

        return {"mouse_move": mouse_move, "mouse_down": mouse_down, "mouse_up": mouse_up, "scroll": scroll,
                "key_down": key_down, "key_up": key_up, "type_text": type_text, "set_clipboard": set_clipboard}

    def uninstall(self) -> None:
        """Restore the backend's primitives (only those still ours: a later wrapper is left alone)."""
        be = self.backend
        for name, hook in self.hooks.items():
            if vars(be).get(name) is not hook:
                continue  # someone wrapped it after us; our hook stays a pass-through
            if self.had[name]:
                setattr(be, name, self.originals[name])
            else:
                try:
                    delattr(be, name)
                except AttributeError:  # pragma: no cover
                    pass

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


# Keys pynput reports as a bare virtual-key code (no char, no name): numpad keys,
# Ctrl+digit on Windows... Without a translation they would vanish from the recording.
_WIN_VK = {
    **{96 + i: str(i) for i in range(10)},  # VK_NUMPAD0..9
    106: "*", 107: "+", 109: "-", 110: ".", 111: "/",
    **{48 + i: str(i) for i in range(10)},  # '0'..'9' (e.g. with Ctrl held)
    **{65 + i: chr(97 + i) for i in range(26)},  # 'A'..'Z'
}
_X11_KEYSYMS = {
    **{0xFFB0 + i: str(i) for i in range(10)},  # KP_0..KP_9
    0xFFAA: "*", 0xFFAB: "+", 0xFFAC: ",", 0xFFAD: "-", 0xFFAE: ".", 0xFFAF: "/", 0xFFBD: "=",
    0xFF80: "space", 0xFF89: "tab", 0xFF8D: "enter",
    0xFF9D: "5",  # KP_Begin: pynput reports numpad 5 like this even with NumLock on
    0xFF95: "home", 0xFF96: "left", 0xFF97: "up", 0xFF98: "right", 0xFF99: "down",
    0xFF9A: "pageup", 0xFF9B: "pagedown", 0xFF9C: "end", 0xFF9E: "insert", 0xFF9F: "delete",
}


def _vk_key(vk: Any, platform: str = sys.platform) -> Optional[str]:
    """Key name/character of a pynput virtual-key code without ``char``/``name``, or ``None``."""
    if not isinstance(vk, int) or isinstance(vk, bool):
        return None
    if platform == "win32":
        if 96 <= vk <= 111 or 48 <= vk <= 57 or 65 <= vk <= 90:
            try:  # the active layout's character (VK_DECIMAL is ',' on a German keyboard)
                import ctypes

                code = int(ctypes.windll.user32.MapVirtualKeyW(vk, 2))  # MAPVK_VK_TO_CHAR
                if code and not code & 0x80000000 and chr(code & 0xFFFF).isprintable():
                    return chr(code & 0xFFFF).lower()
            except Exception:  # pragma: no cover - not on Windows / no user32
                pass
        return _WIN_VK.get(vk)
    if platform == "darwin":  # macOS key codes overlap the tables above (96 is F5)
        return None
    return _X11_KEYSYMS.get(vk)


def _is_keypad_vk(vk: Any, platform: str = sys.platform) -> bool:
    if not isinstance(vk, int) or isinstance(vk, bool):
        return False
    if platform == "win32":
        return 96 <= vk <= 111
    return platform != "darwin" and (0xFFAA <= vk <= 0xFFB9 or vk == 0xFF9D)


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


#: Environment variable that recorded flows/scripts read passwords from (typed text in
#: password fields is never recorded).
PASSWORD_ENV = "LOCUM_PASSWORD"
_PASSWORD_REF = "${env." + PASSWORD_ENV + "}"
_PASSWORD_PY = f"os.environ[{PASSWORD_ENV!r}]"


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
        secret: ``type``/``set_text`` into a password field: the text is not kept;
            the flow reads it from ``${env.LOCUM_PASSWORD}`` and the script from
            ``os.environ["LOCUM_PASSWORD"]`` (:data:`PASSWORD_ENV`).
        mode: ``type`` only: ``"clipboard"`` for text locum pasted that holds a
            Tab/newline, so the replay pastes it too instead of pressing those keys.
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
    secret: bool = False
    mode: str = ""
    _last_time: float = field(default=0.0, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"kind must be one of {', '.join(ACTION_KINDS)}, got {self.kind!r}")
        if self.secret:
            self.text = ""  # a password is never stored, saved or logged

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
            if self.secret:
                d["secret"] = True
            if self.mode and self.kind == "type":
                d["mode"] = self.mode
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
        """This action as one :mod:`locum.rpa.flow` step.

        Every recorded string (typed text, selectors, key names) is escaped for
        flow interpolation (``${`` -> ``$${``), so replaying someone who typed
        ``${total}`` types exactly that instead of substituting a variable or an
        environment value.
        """
        step = _escape_flow_strings(self._raw_flow_step())
        if self.secret:  # the one deliberate interpolation: the password comes from the environment
            args = step.get("type") if self.kind == "type" else step.get("set_text")
            if isinstance(args, dict):
                args["text"] = _PASSWORD_REF
            else:
                step["type"] = _PASSWORD_REF
        return step

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
                args = {"text": self.text, "into": self.selector}
            elif self.mode:
                args = {"text": self.text}
            else:
                return {"type": self.text}
            if self.mode:
                args["mode"] = self.mode
            return {"type": args}
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
        """This action as one line of Python using the :class:`~locum.Desktop` API."""
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
        text = _PASSWORD_PY if self.secret else repr(self.text)
        if k == "type":
            mode = f", mode={self.mode!r}" if self.mode else ""
            if self.selector:
                return f"{var}.find({self.selector!r}).type({text}{mode})"
            return f"{var}.type({text}{mode})"
        if k == "set_text":
            if self.selector:
                return f"{var}.find({self.selector!r}).set_text({text})"
            return f"{var}.click(({self.x}, {self.y})); {var}.hotkey('mod+a'); {var}.type({text})"
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
            (default: a new :class:`~locum.Desktop` on first use).
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
        self._worker: Optional[Tuple[threading.Thread, "queue.Queue[Any]"]] = None
        self.started_at: Optional[float] = None
        self._reset_state()

    def _reset_state(self) -> None:
        self._mods: Dict[str, bool] = {}  # held modifier -> used in a chord / with a key
        self._pressed: Dict[str, Tuple[int, int, float, Dict[str, Any]]] = {}
        self._buffer = ""
        self._buffer_time = 0.0
        self._buffer_target: Dict[str, Any] = {}
        self._pos: Optional[Tuple[int, int]] = None
        self._paste: Optional[str] = None  # text locum just put on the clipboard to paste

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
            self._stop_worker()
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

        Tuple form (the :class:`~locum.backends.mock.MockBackend` event-log format):
        ``("move", x, y)``, ``("down", button, x, y)``, ``("up", button, x, y)``,
        ``("scroll", dx, dy, x, y)``, ``("key_down", key)``, ``("key_up", key)``,
        ``("type", text)``. Dict form: ``{"event": "down", "button": "left",
        "x": 10, "y": 20, "time": 1.5}`` (``"type"`` is accepted for ``"event"``;
        ``"key"``, ``"text"``, ``"dx"``, ``"dy"`` as named). ``time`` is optional
        (default: the recorder's clock). A ``key_down`` with ``"char": True`` carries
        the character the OS keyboard layout already produced (Shift/AltGr applied,
        as pynput reports it), so it is recorded as-is; otherwise a key name is
        unshifted and Shift is applied with the US layout (backend key events).
        Unknown event kinds are ignored, so a whole backend event log can be fed as-is.
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
                self._on_key_down(ev["key"], t, layout_char=bool(ev.get("char")))
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
        d["char"] = bool(d.get("char")) if kind == "key_down" else False
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
        """Selector of the focused text field (with ``use_selectors``) and ``secret``: it is a password field."""
        try:
            node = self.backend.focused_element()
        except Exception:
            return {}
        if node is None:
            return {}
        out: Dict[str, Any] = {"secret": True} if is_password(node) else {}
        if not self.use_selectors:
            return out
        try:
            role = (node.role or "").lower()
            editable = "editable" in node.states
        except Exception:
            return out
        if role not in _TEXT_INPUT_ROLES and not editable:
            return out
        node = self._with_ancestry(node)
        sel = self._selector(node)
        if sel:
            out.update(selector=sel, element=self._describe(node))
        return out

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
        self._emit(RecordedAction("type", time=self._buffer_time, text=self._buffer, selector=target.get("selector"),
                                  element=target.get("element"), secret=bool(target.get("secret"))))
        self._buffer = ""
        self._buffer_target = {}

    def _on_down(self, button: str, x: int, y: int, t: float) -> None:
        self._paste = None
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
        self._paste = None
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
    def _on_key_down(self, key: str, t: float, *, layout_char: bool = False) -> None:
        if key in MODIFIERS:
            base = _base_mod(key)
            self._mods.setdefault(base, False)
            if base == "altgr" and "ctrl" in self._mods:
                # Windows reports AltGr as LCtrl + RAltGr: that Ctrl is no shortcut and must
                # not be recorded as a lone "press ctrl" when it is released.
                self._mods["ctrl"] = True
            return
        if self._paste is not None:
            text, self._paste = self._paste, None
            if key.lower() == "v" and set(self._mods) - {"shift"} in ({"ctrl"}, {"cmd"}):
                # The mod+v of Keyboard.type(mode="clipboard") right after locum put the
                # text on the clipboard: record the text, not a paste of whatever the
                # clipboard holds at replay time.
                for m in self._mods:
                    self._mods[m] = True
                if any(c in text for c in ("\t", "\n", "\r")):
                    # Typed as keys on replay, a pasted Tab/newline would move the focus
                    # or press Enter: record it as its own pasted ``type`` step.
                    self._flush_typing()
                    target = self._capture_focus()
                    self._emit(RecordedAction("type", time=t, text=text, selector=target.get("selector"),
                                              element=target.get("element"), secret=bool(target.get("secret")),
                                              mode="clipboard"))
                    return
                self._append_text(text, t)
                return
        held = set(self._mods)
        altgr = "altgr" in held
        # With AltGr held, Ctrl/Alt are part of AltGr (Windows: LCtrl+RAlt), not a chord:
        # AltGr+Q is '@' on a German keyboard, not ctrl+@.
        chord = [m for m in _CHORD_MODS if m in held and not (altgr and m in ("ctrl", "alt"))]
        shift = "shift" in held
        if chord:
            for m in held:
                self._mods[m] = True
            self._flush_typing()
            mods = sorted(held - {"altgr", "fn"}, key=lambda m: _MOD_ORDER.get(m, 9))
            k = key.lower() if len(key) == 1 else key
            self._emit(RecordedAction("hotkey", time=t, keys=mods + [k]))
            return
        # A layout-produced character (pynput) already has Shift/AltGr applied: re-applying
        # the US shift table would turn a German Shift+7 '/' into '?' and AZERTY digits into
        # '!@#'.
        ch = _char_for(key, shift and not layout_char)
        if ch is not None:
            for m in held:
                self._mods[m] = True
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
        :class:`~locum.element.Element` or accessibility node).

        Called automatically while recording from the ``backend``/``pynput``
        sources: locum's native actions produce no input events, so input
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
            action = RecordedAction(kind, time=self._clock(), x=x, y=y, selector=sel, element=self._describe(node),
                                    text=text or "", secret=kind == "set_text" and is_password(node))
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
        with _HOOKS_LOCK:
            chain = vars(be).get(_HOOKS_ATTR)
            if chain is None:
                chain = _BackendHooks(be)
                setattr(be, _HOOKS_ATTR, chain)
            chain.recorders.append(self)
        self._hooks = {"backend": be, "chain": chain}

    def _remove_backend_hooks(self) -> None:
        if not self._hooks:
            return
        be, chain = self._hooks["backend"], self._hooks["chain"]
        with _HOOKS_LOCK:
            if self in chain.recorders:
                chain.recorders.remove(self)
            if not chain.recorders:
                chain.uninstall()
                if vars(be).get(_HOOKS_ATTR) is chain:
                    delattr(be, _HOOKS_ATTR)
        self._hooks = {}

    def _feed_hooked(self, event: Any) -> None:
        if self._source != "backend":  # stopped meanwhile
            return
        try:
            self.feed(event)
        except Exception as exc:  # recording must never break the automation
            _log.warning("recorder failed to process %r: %s", event, exc)

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
                "Install it with: pip install locum[record]"
                if missing
                else "pynput is installed but cannot hook input here (is a graphical session/display available?)"
            )
            raise NotSupported(
                f"Live recording needs pynput and a desktop session ({type(exc).__name__}: {exc}). "
                f"{hint}  -- or use start(source='backend') / start(source='manual') with feed()."
            ) from exc
        rec = self
        # pynput callbacks run on the thread that services the OS input hooks (on Windows
        # the WH_MOUSE_LL/WH_KEYBOARD_LL hooks, which Windows silently removes when they
        # take too long): they only timestamp and queue the raw event. Selector capture
        # (element_at + accessibility tree searches) runs on this worker thread.
        events: "queue.Queue[Any]" = queue.Queue()

        def work() -> None:
            while True:
                item = events.get()
                if item is None:
                    return
                try:
                    if item[0] == "key":
                        ev = rec._pynput_event(item[1], item[2], item[3])
                    else:
                        ev = item[1]
                    if ev is not None:
                        rec.feed(ev)
                except Exception as err:
                    _log.warning("recorder failed to process %r: %s", item, err)

        def on_move(x: Any, y: Any) -> None:
            events.put(("ev", {"event": "move", "x": x, "y": y, "time": rec._clock()}))

        def on_click(x: Any, y: Any, button: Any, pressed: bool) -> None:
            events.put(("ev", {"event": "down" if pressed else "up", "button": getattr(button, "name", str(button)),
                               "x": x, "y": y, "time": rec._clock()}))

        def on_scroll(x: Any, y: Any, dx: Any, dy: Any) -> None:
            events.put(("ev", {"event": "scroll", "dx": dx, "dy": dy, "x": x, "y": y, "time": rec._clock()}))

        def on_press(key: Any) -> None:
            events.put(("key", "key_down", key, rec._clock()))

        def on_release(key: Any) -> None:
            events.put(("key", "key_up", key, rec._clock()))

        worker = threading.Thread(target=work, name="locum-recorder", daemon=True)
        worker.start()
        self._worker = (worker, events)
        ml = pmouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)
        kl = pkb.Listener(on_press=on_press, on_release=on_release)
        try:
            ml.start()
            kl.start()
        except BaseException:
            ml.stop()
            self._stop_worker()
            raise
        self._listeners = [ml, kl]

    def _stop_worker(self, timeout: float = 30.0) -> None:
        """Process the queued pynput events, then end the worker thread."""
        entry = getattr(self, "_worker", None)
        self._worker = None
        if not entry:
            return
        worker, events = entry
        events.put(None)
        if worker is not threading.current_thread():
            worker.join(timeout)
            if worker.is_alive():  # pragma: no cover - a hung accessibility call
                _log.warning("recorder: input events still being processed after %gs; stopping anyway", timeout)

    def _pynput_event(self, kind: str, key: Any, t: float) -> Optional[Dict[str, Any]]:
        """A ``key_down``/``key_up`` event for a pynput key (``None`` when it has no meaning)."""
        name = self._pynput_key(key)
        if name is None:
            return None
        ch = getattr(key, "char", None)
        # pynput's char is what the active layout produced (Shift/AltGr applied); so is
        # a numpad digit. Control characters were turned back into the letter above.
        produced = (bool(ch) and len(name) == 1 and name == ch) or (not ch and _is_keypad_vk(getattr(key, "vk", None)))
        return {"event": kind, "key": name, "time": t, "char": produced}

    def _pynput_key(self, key: Any) -> Optional[str]:
        """Canonical key name for a pynput ``Key``/``KeyCode``."""
        ch = getattr(key, "char", None)
        if ch:
            if len(ch) == 1 and ord(ch) < 32 and self._mods.get("ctrl") is not None:
                return chr(ord(ch) + 96)  # ctrl+a arrives as '\x01' on some platforms
            return ch
        name = getattr(key, "name", None)
        if not name:
            vk = getattr(key, "vk", None)
            mapped = _vk_key(vk)
            if mapped is None:
                if vk is not None:
                    _log.warning("recorder: cannot translate key %r (virtual-key code %r); it is not recorded", key, vk)
                return None
            return _canon(mapped)
        name = _PYNPUT_NAMES.get(name, name)
        return _canon(name)

    # ------------------------------------------------------------------ output
    def _final_actions(self) -> List[RecordedAction]:
        with self._lock:
            actions = list(self._actions)
            if self._buffer:
                t = self._buffer_target
                actions.append(RecordedAction("type", time=self._buffer_time, text=self._buffer, selector=t.get("selector"),
                                              element=t.get("element"), secret=bool(t.get("secret"))))
            return actions

    def to_flow(self, name: str = "recorded") -> Dict[str, Any]:
        """The recording as a flow document (plain dicts/lists/strings: YAML- and JSON-ready)."""
        return {
            "name": name,
            "description": f"Recorded with locum on {_dt.datetime.now().isoformat(timespec='seconds')}",
            "steps": [a.to_flow_step() for a in self._final_actions()],
        }

    def to_python(self, *, desktop_expr: str = "Desktop()", var: str = "d") -> str:
        """The recording as a runnable Python script using :class:`locum.Desktop`."""
        if not var.isidentifier():
            raise ValueError(f"var must be a Python identifier, got {var!r}")
        actions = self._final_actions()
        lines = [
            f'"""Recorded with locum on {_dt.datetime.now().isoformat(timespec="seconds")}."""',
            *(["import os", ""] if any(a.secret for a in actions) else []),
            "from locum import Desktop",
            "",
            f"{var} = {desktop_expr}",
            "",
        ]
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
                raise ImportError("Saving YAML flows requires PyYAML: pip install locum[yaml]") from exc
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
