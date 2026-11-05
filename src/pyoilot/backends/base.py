"""The backend contract.

A *backend* is the only layer that talks to the operating system. Everything
above it (humanized motion, vision, selectors, workflows, the LLM agent) is
pure Python written against :class:`Backend` and :class:`AccessibleNode`, which
is why the whole library can be exercised against :class:`~pyoilot.backends.mock.MockBackend`.

Rules for implementers
----------------------
* Coordinates are **physical pixels in the virtual desktop** (multi-monitor,
  may be negative). Backends must make the process DPI-aware where relevant
  so that screenshots and mouse coordinates agree.
* Primitives are **instant and atomic**: no sleeping, no tweening, no retries.
  Timing/humanization lives in :mod:`pyoilot.mouse` / :mod:`pyoilot.keyboard`.
* Key names arrive already canonical (see :mod:`pyoilot.keys`).
* Anything not supported raises :class:`~pyoilot.errors.NotSupported` and is
  omitted from :attr:`Backend.capabilities`.
* ``screenshot`` returns an ``RGB`` :class:`PIL.Image.Image` whose pixel (0, 0)
  is the region's top-left corner.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Sequence

from ..errors import NotSupported
from ..types import Monitor, MouseButton, Point, Rect, WindowInfo

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image


# Capability flags. ``Backend.capabilities`` is a subset of these.
CAP_MOUSE = "mouse"
CAP_KEYBOARD = "keyboard"
CAP_UNICODE_TYPING = "unicode_typing"  # type_text() injects arbitrary unicode without key mapping
CAP_SCREENSHOT = "screenshot"
CAP_WINDOWS = "windows"  # list/activate/move windows
CAP_CLIPBOARD = "clipboard"
CAP_ACCESSIBILITY = "accessibility"  # accessibility_roots() returns a real tree
CAP_HSCROLL = "hscroll"

ALL_CAPABILITIES = frozenset(
    {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_SCREENSHOT, CAP_WINDOWS, CAP_CLIPBOARD, CAP_ACCESSIBILITY, CAP_HSCROLL}
)


class AccessibleNode(abc.ABC):
    """One node of a platform accessibility tree (UIA element, AT-SPI accessible, AXUIElement...).

    Implementations should be *lazy*: compute children only when asked, because
    real desktop trees are huge. Property access must never raise for a stale
    element; return empty values instead (``""``, ``None``, ``[]``).

    Roles are normalized to lowercase ARIA-like names (see :data:`ROLES`) so
    selectors are portable: ``button``, ``edit``, ``window``, ``menuitem``...
    The native role stays available in :attr:`native_role`.
    """

    # --- identity / description -------------------------------------------------
    @property
    @abc.abstractmethod
    def role(self) -> str: ...

    @property
    def native_role(self) -> str:
        return self.role

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @property
    def value(self) -> Optional[str]:
        return None

    @property
    def automation_id(self) -> str:
        """Stable developer id (UIA AutomationId, AX identifier, AT-SPI id attr)."""
        return ""

    @property
    def class_name(self) -> str:
        return ""

    @property
    def description(self) -> str:
        return ""

    @property
    @abc.abstractmethod
    def bounds(self) -> Optional[Rect]: ...

    @property
    def states(self) -> frozenset:
        """Lowercase state flags: ``enabled``, ``focused``, ``focusable``, ``selected``,
        ``checked``, ``expanded``, ``visible``/``offscreen``, ``editable``, ``readonly``."""
        return frozenset()

    @property
    def pid(self) -> Optional[int]:
        return None

    @property
    def handle(self) -> Any:
        """Native window handle if this node is (or belongs to) a top-level window."""
        return None

    def attributes(self) -> Dict[str, Any]:
        """Extra backend-specific attributes usable in selectors as ``[attr=value]``."""
        return {}

    # --- tree --------------------------------------------------------------------
    @abc.abstractmethod
    def children(self) -> Sequence["AccessibleNode"]: ...

    @property
    def parent(self) -> Optional["AccessibleNode"]:
        return None

    # --- actions -----------------------------------------------------------------
    def actions(self) -> List[str]:
        """Names of natively supported actions, e.g. ``["invoke", "set_value", "toggle"]``."""
        return []

    def invoke(self) -> bool:
        """Perform the default action (press button, select item) *without* moving the mouse.
        Return True on success, False if not supported (caller then falls back to clicking)."""
        return False

    def set_value(self, text: str) -> bool:
        """Set the text/value natively. Return False if not supported."""
        return False

    def focus(self) -> bool:
        return False

    def toggle(self) -> bool:
        return False

    def expand(self) -> bool:
        return False

    def collapse(self) -> bool:
        return False

    def select(self) -> bool:
        return False

    def scroll_into_view(self) -> bool:
        return False

    # --- helpers -----------------------------------------------------------------
    @property
    def is_enabled(self) -> bool:
        st = self.states
        return "disabled" not in st and ("enabled" in st or not st)

    @property
    def is_visible(self) -> bool:
        st = self.states
        if "offscreen" in st or "invisible" in st:
            return False
        b = self.bounds
        return b is not None and not b.is_empty

    def walk(self, max_depth: int = 64) -> Iterator["AccessibleNode"]:
        """Depth-first pre-order traversal including ``self``."""
        stack = [(self, 0)]
        while stack:
            node, depth = stack.pop()
            yield node
            if depth < max_depth:
                try:
                    kids = list(node.children())
                except Exception:  # stale / access denied
                    kids = []
                stack.extend((k, depth + 1) for k in reversed(kids))

    def same_as(self, other: "AccessibleNode") -> bool:
        """Identity comparison across re-queries (backends may override with runtime ids)."""
        return self is other

    def __repr__(self) -> str:
        return f"<{type(self).__name__} role={self.role!r} name={self.name!r} bounds={self.bounds}>"


# Portable role vocabulary. Backends map native roles onto these; anything else
# is passed through lowercased.
ROLES = frozenset(
    {
        "desktop", "application", "window", "dialog", "pane", "group", "toolbar", "statusbar", "titlebar",
        "menubar", "menu", "menuitem", "button", "togglebutton", "checkbox", "radiobutton", "combobox",
        "edit", "text", "document", "link", "image", "list", "listitem", "tree", "treeitem", "table",
        "row", "cell", "columnheader", "rowheader", "tab", "tablist", "tabpanel", "slider", "spinbutton",
        "progressbar", "scrollbar", "separator", "tooltip", "header", "unknown",
    }
)


class Backend(abc.ABC):
    """Abstract platform backend. See module docstring for the contract."""

    name: str = "abstract"

    # ---------------------------------------------------------------- capabilities
    @property
    def capabilities(self) -> frozenset:
        return frozenset()

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def close(self) -> None:
        """Release native resources (display connections, COM, ...)."""

    # ----------------------------------------------------------------------- mouse
    @abc.abstractmethod
    def mouse_position(self) -> Point: ...

    @abc.abstractmethod
    def mouse_move(self, x: int, y: int) -> None:
        """Warp the cursor to absolute virtual-desktop coordinates."""

    @abc.abstractmethod
    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> None: ...

    @abc.abstractmethod
    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> None: ...

    @abc.abstractmethod
    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Scroll by wheel *notches*. ``dy > 0`` scrolls **up** (content moves down), ``dx > 0`` scrolls right."""

    # -------------------------------------------------------------------- keyboard
    @abc.abstractmethod
    def key_down(self, key: str) -> None:
        """Press a canonical key name or a single character."""

    @abc.abstractmethod
    def key_up(self, key: str) -> None: ...

    def type_text(self, text: str) -> None:
        """Insert ``text`` as-is (layout independent) if :data:`CAP_UNICODE_TYPING` is supported.

        The default implementation raises NotSupported; :mod:`pyoilot.keyboard`
        then falls back to per-character key presses.
        """
        raise NotSupported(f"{self.name} backend cannot inject unicode text")

    # ---------------------------------------------------------------------- screen
    @abc.abstractmethod
    def monitors(self) -> List[Monitor]:
        """All monitors; index 0 must be the primary one."""

    def virtual_screen(self) -> Rect:
        """Bounding rect of all monitors."""
        mons = self.monitors()
        r = mons[0].rect
        for m in mons[1:]:
            r = r.union(m.rect)
        return r

    @abc.abstractmethod
    def screenshot(self, region: Optional[Rect] = None) -> "Image":
        """RGB image of ``region`` (default: the whole virtual screen)."""

    def pixel(self, x: int, y: int) -> tuple:
        return self.screenshot(Rect(x, y, 1, 1)).getpixel((0, 0))[:3]

    # --------------------------------------------------------------------- windows
    def list_windows(self) -> List[WindowInfo]:
        """Visible top-level windows in Z-order (front-most first)."""
        raise NotSupported(f"{self.name} backend cannot enumerate windows")

    def active_window(self) -> Optional[WindowInfo]:
        for w in self.list_windows():
            if w.is_active:
                return w
        return None

    def activate_window(self, handle: Any) -> None:
        raise NotSupported(f"{self.name} backend cannot activate windows")

    def move_window(self, handle: Any, rect: Rect) -> None:
        raise NotSupported(f"{self.name} backend cannot move windows")

    def set_window_state(self, handle: Any, state: str) -> None:
        """``state`` in {"minimized", "maximized", "normal"}."""
        raise NotSupported(f"{self.name} backend cannot change window state")

    def close_window(self, handle: Any) -> None:
        raise NotSupported(f"{self.name} backend cannot close windows")

    def launch(self, command: Any, **kwargs: Any) -> Any:
        """Start an application. Default: :class:`subprocess.Popen`."""
        import shlex
        import subprocess
        import sys

        if isinstance(command, str):
            args = command if sys.platform == "win32" else shlex.split(command)
        else:
            args = list(command)
        return subprocess.Popen(args, **kwargs)

    # ------------------------------------------------------------------- clipboard
    def get_clipboard(self) -> str:
        raise NotSupported(f"{self.name} backend has no clipboard support")

    def set_clipboard(self, text: str) -> None:
        raise NotSupported(f"{self.name} backend has no clipboard support")

    # --------------------------------------------------------------- accessibility
    def accessibility_roots(self) -> List[AccessibleNode]:
        """Top-level accessible nodes (usually one per top-level window/application)."""
        raise NotSupported(f"{self.name} backend has no accessibility tree")

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        """Deepest accessible element at a screen point (default: tree walk)."""
        best = None
        for root in self.accessibility_roots():
            for node in root.walk():
                b = node.bounds
                if b is not None and b.contains((x, y)):
                    if best is None or (b.area <= (best.bounds.area if best.bounds else 1 << 62)):
                        best = node
        return best

    def focused_element(self) -> Optional[AccessibleNode]:
        for root in self.accessibility_roots():
            for node in root.walk():
                if "focused" in node.states:
                    return node
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"
