"""An in-memory virtual desktop.

``MockBackend`` simulates a full desktop: monitors, a rendered screen, stacked
windows containing interactive widgets (buttons, text fields, checkboxes,
labels, lists), a matching accessibility tree, a clipboard and a keyboard focus
model. Input events are applied to the widgets exactly like a real GUI would:

* clicking a window raises/activates it; clicking a widget focuses it
* clicking a button fires its ``on_click`` callback (and counts clicks)
* clicking a checkbox toggles it; typing goes to the focused edit field
* ``backspace``/``delete``/``ctrl+a`` behave sensibly in edit fields
* ``enter``/``space`` on a focused button activates it

Every primitive is appended to :attr:`MockBackend.events`, which makes it the
reference backend for unit tests, CI, dry-runs and LLM agent evaluations.

    >>> from locum.backends.mock import MockBackend
    >>> be = MockBackend()
    >>> win = be.add_window("Invoice", (100, 100, 400, 300), app="erp")
    >>> ok = win.add_button("OK", (20, 240, 80, 30))
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..errors import NotSupported, WindowNotFound
from ..types import Monitor, MouseButton, Point, Rect, RectLike, WindowInfo
from .base import (
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOWS,
    AccessibleNode,
    Backend,
)

_handles = itertools.count(0x1000)

#: Caret-navigation keys: in an edit field they cancel a ``ctrl+a`` selection.
_NAV_KEYS = frozenset({"home", "end", "left", "right", "up", "down", "pageup", "pagedown"})

#: Named keys that type a character into an edit field (like any real backend).
_NAMED_CHAR_KEYS = {
    "space": " ", **{f"num{i}": str(i) for i in range(10)},
    "num_add": "+", "num_subtract": "-", "num_multiply": "*", "num_divide": "/", "num_decimal": ".",
}

_SHIFT_MAP = {
    "`": "~", "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&", "8": "*", "9": "(",
    "0": ")", "-": "_", "=": "+", "[": "{", "]": "}", "\\": "|", ";": ":", "'": '"', ",": "<", ".": ">", "/": "?",
}


@dataclass(eq=False)
class MockWidget:
    """A simulated control. ``rect`` is relative to the owning window's top-left."""

    role: str
    name: str
    rect: Rect
    value: Optional[str] = None
    automation_id: str = ""
    class_name: str = ""
    enabled: bool = True
    visible: bool = True
    checked: Optional[bool] = None
    on_click: Optional[Callable[["MockWidget"], Any]] = None
    on_change: Optional[Callable[["MockWidget"], Any]] = None
    fill: Tuple[int, int, int] = (225, 225, 225)
    text_color: Tuple[int, int, int] = (0, 0, 0)
    children: List["MockWidget"] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)
    clicks: int = 0
    parent: Optional["MockWidget"] = field(default=None, repr=False)
    window: Optional["MockWindow"] = field(default=None, repr=False)

    # -- tree building helpers (same API on windows and widgets) ------------------
    def add(self, role: str, name: str = "", rect: RectLike = (0, 0, 0, 0), **kw: Any) -> "MockWidget":
        w = MockWidget(role=role, name=name, rect=Rect.coerce(rect), **kw)
        w.parent = self
        w.window = self.window
        self.children.append(w)
        return w

    def add_button(self, name: str, rect: RectLike, on_click: Optional[Callable] = None, **kw: Any) -> "MockWidget":
        kw.setdefault("fill", (210, 220, 240))
        return self.add("button", name, rect, on_click=on_click, **kw)

    def add_edit(self, name: str, rect: RectLike, value: str = "", **kw: Any) -> "MockWidget":
        kw.setdefault("fill", (255, 255, 255))
        return self.add("edit", name, rect, value=value, **kw)

    def add_text(self, text: str, rect: RectLike, **kw: Any) -> "MockWidget":
        kw.setdefault("fill", None)
        return self.add("text", text, rect, value=text, **kw)

    def add_checkbox(self, name: str, rect: RectLike, checked: bool = False, **kw: Any) -> "MockWidget":
        kw.setdefault("fill", (255, 255, 255))
        return self.add("checkbox", name, rect, checked=checked, **kw)

    def add_list(self, name: str, rect: RectLike, items: Sequence[str] = (), item_height: int = 24, **kw: Any) -> "MockWidget":
        kw.setdefault("fill", (255, 255, 255))
        lst = self.add("list", name, rect, **kw)
        for i, item in enumerate(items):
            lst.add("listitem", item, (lst.rect.left, lst.rect.top + i * item_height, lst.rect.width, item_height), fill=None)
        return lst

    # -- geometry -------------------------------------------------------------------
    @property
    def screen_rect(self) -> Rect:
        if self.window is None:
            return self.rect
        return self.rect.offset(self.window.rect.left, self.window.rect.top)

    def iter_tree(self):
        yield self
        for c in self.children:
            yield from c.iter_tree()

    def iter_shown(self):
        """Like :meth:`iter_tree`, but skips invisible widgets *and their whole subtree*."""
        if not self.visible:
            return
        yield self
        for c in self.children:
            yield from c.iter_shown()

    @property
    def shown(self) -> bool:
        """Visible, and so are all its ancestors (an invisible panel hides its children)."""
        w: Optional[MockWidget] = self
        while w is not None:
            if not w.visible:
                return False
            w = w.parent
        return True

    def find(self, name: Optional[str] = None, role: Optional[str] = None) -> Optional["MockWidget"]:
        for w in self.iter_tree():
            if (name is None or w.name == name) and (role is None or w.role == role):
                return w
        return None

    def activate(self) -> None:
        """Perform the widget's default action (what a click / Enter does)."""
        if not self.enabled:
            return
        self.clicks += 1
        if self.role == "checkbox":
            self.checked = not bool(self.checked)
            if self.on_change:
                self.on_change(self)
        if self.on_click:
            self.on_click(self)


@dataclass(eq=False)
class MockWindow:
    title: str
    rect: Rect
    app: str = "mockapp"
    pid: int = 4242
    handle: int = field(default_factory=lambda: next(_handles))
    minimized: bool = False
    maximized: bool = False
    visible: bool = True
    closed: bool = False
    fill: Tuple[int, int, int] = (240, 240, 240)
    class_name: str = "MockWindow"
    root: MockWidget = field(init=False)
    _restore_rect: Optional[Rect] = field(default=None, repr=False)
    #: The widget that had keyboard focus when the window was last active (restored on activation).
    _last_focus: Optional["MockWidget"] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.root = MockWidget(role="window", name=self.title, rect=Rect(0, 0, self.rect.width, self.rect.height))
        self.root.window = self

    # delegate builders to the root widget
    def add(self, *a: Any, **kw: Any) -> MockWidget:
        return self.root.add(*a, **kw)

    def add_button(self, *a: Any, **kw: Any) -> MockWidget:
        return self.root.add_button(*a, **kw)

    def add_edit(self, *a: Any, **kw: Any) -> MockWidget:
        return self.root.add_edit(*a, **kw)

    def add_text(self, *a: Any, **kw: Any) -> MockWidget:
        return self.root.add_text(*a, **kw)

    def add_checkbox(self, *a: Any, **kw: Any) -> MockWidget:
        return self.root.add_checkbox(*a, **kw)

    def add_list(self, *a: Any, **kw: Any) -> MockWidget:
        return self.root.add_list(*a, **kw)

    def find(self, name: Optional[str] = None, role: Optional[str] = None) -> Optional[MockWidget]:
        return self.root.find(name, role)

    @property
    def widgets(self) -> List[MockWidget]:
        return list(self.root.iter_tree())[1:]


class MockNode(AccessibleNode):
    """Accessibility view of a :class:`MockWidget`."""

    def __init__(self, backend: "MockBackend", widget: MockWidget, parent: Optional["MockNode"] = None):
        self._be = backend
        self.widget = widget
        self._parent = parent

    @property
    def role(self) -> str:
        return self.widget.role

    @property
    def name(self) -> str:
        return self.widget.name

    @property
    def value(self) -> Optional[str]:
        w = self.widget
        if w.role == "checkbox":
            return "on" if w.checked else "off"
        return w.value

    @property
    def automation_id(self) -> str:
        return self.widget.automation_id

    @property
    def class_name(self) -> str:
        return self.widget.class_name or (self.widget.window.class_name if self.widget.role == "window" and self.widget.window else "")

    @property
    def bounds(self) -> Optional[Rect]:
        w = self.widget
        if w.window is not None and (w.window.minimized or not w.window.visible):
            return None
        if not w.shown:
            return None
        return w.screen_rect

    @property
    def states(self) -> frozenset:
        w = self.widget
        s = {"enabled" if w.enabled else "disabled", "visible" if w.shown else "invisible"}
        if w.role in ("button", "edit", "checkbox", "listitem", "window"):
            s.add("focusable")
        if self._be._focus is w:
            s.add("focused")
        if w.role == "edit":
            s.add("editable")
        if w.checked:
            s.add("checked")
        if w.role == "window" and w.window is not None and self._be._active is w.window:
            s.add("active")
        if w.attrs.get("selected"):
            s.add("selected")
        if w.attrs.get("default"):
            s.add("default")  # the dialog's default button (Enter in a single-line field presses it)
        if w.attrs.get("multiline"):
            s.add("multi_line")
        return frozenset(s)

    @property
    def pid(self) -> Optional[int]:
        return self.widget.window.pid if self.widget.window else None

    @property
    def handle(self) -> Any:
        return self.widget.window.handle if self.widget.window else None

    def attributes(self) -> Dict[str, Any]:
        d = dict(self.widget.attrs)
        if self.widget.window is not None:
            d.setdefault("app", self.widget.window.app)
        return d

    def children(self) -> Sequence["MockNode"]:
        return [MockNode(self._be, c, self) for c in self.widget.children if c.visible]

    @property
    def parent(self) -> Optional["MockNode"]:
        return self._parent

    def actions(self) -> List[str]:
        r = self.widget.role
        acts = ["focus"]
        if r in ("button", "listitem", "menuitem", "link", "checkbox"):
            acts.append("invoke")
        if r == "checkbox":
            acts.append("toggle")
        if r == "edit":
            acts.append("set_value")
        return acts

    def invoke(self) -> bool:
        if "invoke" not in self.actions() or not self.widget.enabled:
            return False
        self._be._log("a11y_invoke", self.widget.name)
        self.widget.activate()
        return True

    def toggle(self) -> bool:
        if self.widget.role != "checkbox" or not self.widget.enabled:
            return False  # nothing would change: report it, like invoke()
        self.widget.activate()
        return True

    def set_value(self, text: str) -> bool:
        if self.widget.role != "edit" or not self.widget.enabled:
            return False
        self._be._log("a11y_set_value", self.widget.name, text)
        self.widget.value = text
        if self.widget.on_change:
            self.widget.on_change(self.widget)
        return True

    def focus(self) -> bool:
        if not self.widget.enabled or not self.widget.shown:
            return False
        self._be._set_focus(self.widget)
        return True

    def same_as(self, other: AccessibleNode) -> bool:
        return isinstance(other, MockNode) and other.widget is self.widget


class MockBackend(Backend):
    """Simulated desktop. See module docstring."""

    name = "mock"

    def __init__(
        self,
        width: int = 1920,
        height: int = 1080,
        monitors: Optional[Sequence[RectLike]] = None,
        background: Tuple[int, int, int] = (32, 48, 64),
        scale: float = 1.0,
    ):
        rects = [Rect.coerce(r) for r in monitors] if monitors else [Rect(0, 0, width, height)]
        self._monitors = [Monitor(i, r, scale, i == 0, f"MOCK{i}") for i, r in enumerate(rects)]
        self.background = background
        self.events: List[tuple] = []
        self.windows: List[MockWindow] = []  # z-order: index 0 = front-most
        self.overlays: List[Tuple[Any, Point]] = []  # (PIL image, top-left) painted above everything
        self.clipboard = ""
        self.pressed_keys: set = set()
        self.pressed_buttons: set = set()
        self.launched: List[Any] = []
        self._pos = Point(self.virtual_screen().center.x, self.virtual_screen().center.y)
        self._focus_widget: Optional[MockWidget] = None
        self._active: Optional[MockWindow] = None
        self._down_target: Optional[MockWidget] = None
        self._lock = threading.RLock()
        self._font_cache: Dict[int, Any] = {}

    # ---------------------------------------------------------------- scene setup
    @property
    def capabilities(self) -> frozenset:
        return frozenset(
            {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_SCREENSHOT, CAP_WINDOWS, CAP_CLIPBOARD, CAP_ACCESSIBILITY, CAP_HSCROLL}
        )

    def add_window(self, title: str, rect: RectLike = (100, 100, 640, 480), app: str = "mockapp", pid: int = 4242, activate: bool = True, **kw: Any) -> MockWindow:
        w = MockWindow(title=title, rect=Rect.coerce(rect), app=app, pid=pid, **kw)
        with self._lock:
            self.windows.insert(0, w)
            if activate:
                self._raise(w)  # the new window takes the keyboard focus, like a real OS
        return w

    def remove_window(self, window: MockWindow) -> None:
        with self._lock:
            window.closed = True
            if window in self.windows:
                self.windows.remove(window)
            if self._active is window:
                self._activate_next()
            if self._focus is not None and self._focus.window is window:
                self._focus = None

    def paint(self, image: Any, at: Tuple[int, int]) -> None:
        """Overlay an arbitrary PIL image onto the simulated screen (for vision tests)."""
        self.overlays.append((image.convert("RGB"), Point(*at)))

    def clear_events(self) -> None:
        self.events.clear()

    @property
    def focused_widget(self) -> Optional[MockWidget]:
        return self._focus

    def _log(self, *event: Any) -> None:
        self.events.append(tuple(event))

    @property
    def _focus(self) -> Optional[MockWidget]:
        """The widget with keyboard focus (always inside the active window, or None)."""
        return self._focus_widget

    @_focus.setter
    def _focus(self, widget: Optional[MockWidget]) -> None:
        old = self._focus_widget
        if old is not None and old is not widget:
            old.attrs.pop("select_all", None)  # leaving a field cancels its selection
        self._focus_widget = widget
        if widget is not None and widget.window is not None:
            widget.window._last_focus = widget

    def _set_focus(self, widget: Optional[MockWidget]) -> None:
        self._focus = widget
        if widget is not None and widget.window is not None:
            self._raise(widget.window)

    def _raise(self, window: MockWindow) -> None:
        if window in self.windows:
            self.windows.remove(window)
            self.windows.insert(0, window)
        window.minimized = False
        self._active = window
        # Keyboard input follows the active window: back to the widget it had focused.
        last = window._last_focus
        if last is not None and (last.window is not window or not last.shown or not last.enabled
                                 or not any(w is last for w in window.root.iter_tree())):
            last = None
        self._focus = last

    def _activate_next(self, exclude: Optional[MockWindow] = None) -> None:
        """The front-most remaining window becomes active (after a minimize/close)."""
        nxt = next((x for x in self.windows if not x.minimized and x.visible and x is not exclude), None)
        if nxt is not None:
            self._raise(nxt)
        else:
            self._active = None
            self._focus = None

    def _keyboard_target(self) -> Optional[MockWidget]:
        """The focused widget, if it can receive keystrokes right now."""
        w = self._focus
        if w is None or not w.enabled or not w.shown:
            return None
        win = w.window
        if win is not None and (win is not self._active or win.minimized or not win.visible or win.closed):
            return None
        return w

    def window_at(self, x: int, y: int) -> Optional[MockWindow]:
        for w in self.windows:
            if w.visible and not w.minimized and w.rect.contains((x, y)):
                return w
        return None

    def widget_at(self, x: int, y: int) -> Optional[MockWidget]:
        win = self.window_at(x, y)
        if win is None:
            return None
        best = win.root
        for wd in win.root.iter_shown():  # an invisible panel hides (and disables) its children
            if wd.screen_rect.contains((x, y)) and wd.screen_rect.area <= best.screen_rect.area:
                best = wd
        return best

    # ----------------------------------------------------------------------- mouse
    def mouse_position(self) -> Point:
        return self._pos

    def mouse_move(self, x: int, y: int) -> None:
        vs = self.virtual_screen()
        x = min(max(int(x), vs.left), vs.right - 1)
        y = min(max(int(y), vs.top), vs.bottom - 1)
        self._pos = Point(x, y)
        self._log("move", x, y)

    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> None:
        button = MouseButton.coerce(button)
        self.pressed_buttons.add(button)
        self._log("down", button.value, self._pos.x, self._pos.y)
        with self._lock:
            target = self.widget_at(*self._pos)
            self._down_target = target
            win = self.window_at(*self._pos)
            if win is not None:
                self._raise(win)
            if button is MouseButton.LEFT and target is not None and target.enabled and target.role != "text":
                target.attrs.pop("select_all", None)  # a click places the caret: the selection is gone
                self._focus = target

    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> None:
        button = MouseButton.coerce(button)
        self.pressed_buttons.discard(button)
        self._log("up", button.value, self._pos.x, self._pos.y)
        with self._lock:
            target = self.widget_at(*self._pos)
            if button is MouseButton.LEFT and target is not None and target is self._down_target:
                if target.role in ("button", "checkbox", "listitem", "menuitem", "link"):
                    if target.role == "listitem" and target.parent is not None:
                        for sib in target.parent.children:
                            sib.attrs["selected"] = sib is target
                    target.activate()
            self._down_target = None

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        self._log("scroll", int(dx), int(dy), self._pos.x, self._pos.y)

    # -------------------------------------------------------------------- keyboard
    def key_down(self, key: str) -> None:
        self.pressed_keys.add(key)
        self._log("key_down", key)
        self._apply_key(key)

    def key_up(self, key: str) -> None:
        self.pressed_keys.discard(key)
        self._log("key_up", key)

    def _mods(self) -> set:
        return {k.replace("_r", "") for k in self.pressed_keys if k in ("ctrl", "shift", "alt", "cmd", "ctrl_r", "shift_r", "alt_r", "cmd_r")}

    def _apply_key(self, key: str) -> None:
        w = self._keyboard_target()
        mods = self._mods()
        command = mods & {"ctrl", "cmd"}
        if command:
            k = key.lower()
            if k == "a" and w is not None and w.role == "edit":
                w.attrs["select_all"] = True
            elif k == "c" and w is not None and w.role == "edit":
                self.clipboard = w.value or ""
            elif k == "x" and w is not None and w.role == "edit":
                self.clipboard = w.value or ""
                self._edit(w, "")
            elif k == "v" and w is not None and w.role == "edit":
                self._insert(w, self.clipboard)
            return
        if "alt" in mods:
            return  # Alt+key is a menu accelerator (WM_SYSCHAR): it never types into the field
        if w is None:
            return
        if key in ("enter", "space") and w.role in ("button", "checkbox", "listitem"):
            w.activate()
            return
        if key == "tab":
            self._focus_next(w)
            return
        if w.role != "edit":
            return
        if key in _NAV_KEYS:
            w.attrs.pop("select_all", None)  # the caret moved: typing inserts instead of replacing
        elif key == "backspace":
            if w.attrs.pop("select_all", False):
                self._edit(w, "")
            else:
                self._edit(w, (w.value or "")[:-1])
        elif key == "delete":
            if w.attrs.pop("select_all", False):
                self._edit(w, "")
        elif key == "enter":
            if w.attrs.get("multiline"):
                self._insert(w, "\n")
        elif len(key) == 1 or key in _NAMED_CHAR_KEYS:
            ch = _NAMED_CHAR_KEYS.get(key, key)
            if "shift" in mods and len(key) == 1:
                ch = _SHIFT_MAP.get(ch, ch.upper())
            self._insert(w, ch)

    def _focus_next(self, w: MockWidget) -> None:
        if w.window is None:
            return
        focusables = [x for x in w.window.root.iter_shown() if x.role in ("button", "edit", "checkbox") and x.enabled]
        if not focusables:
            return
        i = focusables.index(w) if w in focusables else -1
        shift = "shift" in self._mods()
        self._focus = focusables[(i + (-1 if shift else 1)) % len(focusables)]

    def _insert(self, w: MockWidget, text: str) -> None:
        if w.attrs.pop("select_all", False):
            self._edit(w, text)
        else:
            self._edit(w, (w.value or "") + text)

    def _edit(self, w: MockWidget, value: str) -> None:
        maxlen = w.attrs.get("maxlength")
        if maxlen is not None:
            value = value[: int(maxlen)]
        w.value = value
        if w.on_change:
            w.on_change(w)

    def type_text(self, text: str) -> None:
        self._log("type", text)
        w = self._keyboard_target()
        if w is not None and w.role == "edit":
            if not w.attrs.get("multiline"):
                text = text.replace("\r", "").replace("\n", "")
            self._insert(w, text)

    # ---------------------------------------------------------------------- screen
    def monitors(self) -> List[Monitor]:
        return list(self._monitors)

    def _font(self, size: int = 14):
        if size not in self._font_cache:
            from PIL import ImageFont

            try:
                self._font_cache[size] = ImageFont.load_default(size=size)
            except TypeError:  # Pillow < 10.1
                self._font_cache[size] = ImageFont.load_default()
        return self._font_cache[size]

    def render(self) -> Any:
        """Render the whole virtual screen to an RGB PIL image."""
        from PIL import Image, ImageDraw

        vs = self.virtual_screen()
        img = Image.new("RGB", (vs.width, vs.height), self.background)
        draw = ImageDraw.Draw(img)
        ox, oy = -vs.left, -vs.top
        font = self._font(14)
        for win in reversed(self.windows):  # back to front
            if win.minimized or not win.visible:
                continue
            r = win.rect.offset(ox, oy)
            draw.rectangle([r.left, r.top, r.right - 1, r.bottom - 1], fill=win.fill, outline=(90, 90, 90))
            title_bg = (40, 90, 160) if win is self._active else (120, 120, 130)
            draw.rectangle([r.left, r.top, r.right - 1, r.top + 27], fill=title_bg)
            draw.text((r.left + 8, r.top + 6), win.title, fill=(255, 255, 255), font=font)
            for wd in win.root.iter_shown():  # an invisible panel hides its whole subtree
                if wd is win.root:
                    continue
                wr = wd.screen_rect.offset(ox, oy)
                if wr.is_empty:
                    continue
                if wd.fill is not None:
                    outline = (0, 120, 215) if wd is self._focus else (130, 130, 130)
                    draw.rectangle([wr.left, wr.top, wr.right - 1, wr.bottom - 1], fill=wd.fill, outline=outline)
                color = wd.text_color if wd.enabled else (150, 150, 150)
                if wd.role == "checkbox":
                    side = min(wr.height, wr.width) - 8  # the box is inset 4px; tiny widgets get no box
                    if side > 0:
                        box = [wr.left + 4, wr.top + 4, wr.left + 4 + side, wr.top + 4 + side]
                        draw.rectangle(box, outline=(0, 0, 0), fill=(0, 0, 0) if wd.checked else (255, 255, 255))
                    draw.text((wr.left + wr.height + 2, wr.top + max(0, (wr.height - 14) // 2)), wd.name, fill=color, font=font)
                elif wd.role == "edit":
                    draw.text((wr.left + 4, wr.top + max(0, (wr.height - 14) // 2)), wd.value or "", fill=color, font=font)
                elif wd.role in ("button", "text", "listitem", "menuitem", "link", "tab"):
                    label = wd.name if wd.role != "text" else (wd.value or wd.name)
                    if wd.attrs.get("selected"):
                        draw.rectangle([wr.left, wr.top, wr.right - 1, wr.bottom - 1], fill=(0, 120, 215))
                        color = (255, 255, 255)
                    tw = draw.textlength(label, font=font) if hasattr(draw, "textlength") else len(label) * 7
                    tx = wr.left + max(2, int((wr.width - tw) // 2)) if wd.role == "button" else wr.left + 4
                    draw.text((tx, wr.top + max(0, (wr.height - 14) // 2)), label, fill=color, font=font)
        for overlay, at in self.overlays:
            img.paste(overlay, (at.x + ox, at.y + oy))
        return img

    def screenshot(self, region: Optional[Rect] = None) -> Any:
        region = Rect.coerce(region) if region is not None else None
        self._log("screenshot", region.as_tuple() if region else None)
        img = self.render()
        if region is None:
            return img
        vs = self.virtual_screen()
        r = Rect.coerce(region).offset(-vs.left, -vs.top)
        return img.crop(r.as_ltrb())

    # --------------------------------------------------------------------- windows
    def _info(self, w: MockWindow) -> WindowInfo:
        return WindowInfo(
            handle=w.handle,
            title=w.title,
            rect=w.rect,
            app=w.app,
            pid=w.pid,
            is_active=w is self._active,
            is_minimized=w.minimized,
            is_maximized=w.maximized,
            is_visible=w.visible and not w.minimized,
            class_name=w.class_name,
        )

    def _win(self, handle: Any) -> MockWindow:
        for w in self.windows:
            if w.handle == handle:
                return w
        raise WindowNotFound(f"No mock window with handle {handle!r}")

    def list_windows(self) -> List[WindowInfo]:
        return [self._info(w) for w in self.windows if w.visible]

    def activate_window(self, handle: Any) -> None:
        self._log("activate", handle)
        self._raise(self._win(handle))

    def move_window(self, handle: Any, rect: Rect) -> None:
        self._log("move_window", handle, Rect.coerce(rect).as_tuple())
        w = self._win(handle)
        w.rect = Rect.coerce(rect)
        w.root.rect = Rect(0, 0, w.rect.width, w.rect.height)
        # an explicit placement leaves the maximized state (like the real backends)
        w.maximized, w._restore_rect = False, None

    def set_window_state(self, handle: Any, state: str) -> None:
        self._log("window_state", handle, state)
        w = self._win(handle)
        if state == "minimized":
            w.minimized = True
            if self._active is w:
                self._activate_next(exclude=w)
        elif state == "maximized":
            # maximizing twice must not forget the normal-state rect
            restore = w._restore_rect if w.maximized else w.rect
            # maximize onto the window's own monitor (largest overlap, primary on a tie)
            mon = max(
                self._monitors,
                key=lambda m: ((m.rect.intersect(w.rect) or Rect(0, 0, 0, 0)).area, m.is_primary),
            )
            self.move_window(handle, mon.rect)
            w.maximized, w.minimized, w._restore_rect = True, False, restore
            self._raise(w)
        elif state == "normal":
            w.minimized = False
            if w.maximized and w._restore_rect is not None:
                self.move_window(handle, w._restore_rect)
            w.maximized, w._restore_rect = False, None
            self._raise(w)
        else:
            raise ValueError(f"Unknown window state {state!r}")

    def close_window(self, handle: Any) -> None:
        self._log("close", handle)
        self.remove_window(self._win(handle))

    def launch(self, command: Any, **kwargs: Any) -> Any:
        self._log("launch", command)
        self.launched.append(command)
        return None

    # ------------------------------------------------------------------- clipboard
    def get_clipboard(self) -> str:
        return self.clipboard

    def set_clipboard(self, text: str) -> None:
        self._log("set_clipboard", text)
        self.clipboard = text

    # --------------------------------------------------------------- accessibility
    def accessibility_roots(self) -> List[AccessibleNode]:
        return [MockNode(self, w.root) for w in self.windows if w.visible]

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        return self._node_with_parents(self.widget_at(x, y))

    def _node_with_parents(self, wd: Optional[MockWidget]) -> Optional[AccessibleNode]:
        """A node for ``wd`` whose ``parent`` chain leads up to its window (like real backends)."""
        if wd is None:
            return None
        chain = []
        cur: Optional[MockWidget] = wd
        while cur is not None:
            chain.append(cur)
            cur = cur.parent
        node: Optional[MockNode] = None
        for w in reversed(chain):
            node = MockNode(self, w, node)
        return node

    def focused_element(self) -> Optional[AccessibleNode]:
        if self._focus is None:
            return None
        return MockNode(self, self._focus)


__all__ = ["MockBackend", "MockWindow", "MockWidget", "MockNode", "NotSupported"]
