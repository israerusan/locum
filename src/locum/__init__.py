"""locum - desktop automation for humans, robots and LLMs.

Quick start::

    import locum

    locum.click('button "OK"')          # accessibility selector
    locum.click("ok_button.png")         # template image on screen
    locum.click(640, 400)                # coordinates
    locum.type("hello world\\n")
    locum.hotkey("mod+s")                # ctrl+s, or cmd+s on macOS
    win = locum.window("Notepad")
    win.find("edit").set_text("typed natively when possible")

The module-level functions act on a lazily created default :class:`Desktop`
(see :func:`get_desktop`); ``import locum`` itself never opens a display
connection, imports numpy, or touches the operating system. For explicit
control create your own :class:`Desktop` (e.g. ``Desktop("mock")`` in tests).
"""

from __future__ import annotations

import importlib
import threading
from typing import TYPE_CHECKING, Any, List, Optional

from .errors import (
    ActionTimeout,
    AmbiguousElement,
    BackendError,
    BackendUnavailable,
    ElementNotFound,
    FailSafeTriggered,
    ImageNotFound,
    NotSupported,
    PolicyViolation,
    LocumError,
    SelectorSyntaxError,
    WindowNotFound,
    WorkflowError,
)
from .types import Match, Monitor, MouseButton, Point, Rect, WindowInfo, coerce_point

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from .desktop import Desktop
    from .element import Element
    from .failsafe import FailSafe
    from .motion import Humanize
    from .window import Window

__version__ = "0.1.0"

# Heavy names resolved on first attribute access (PEP 562) so that
# ``import locum`` stays cheap and side-effect free.
_LAZY = {
    "Desktop": ("locum.desktop", "Desktop"),
    "Element": ("locum.element", "Element"),
    "Window": ("locum.window", "Window"),
    "Humanize": ("locum.motion", "Humanize"),
    "FailSafe": ("locum.failsafe", "FailSafe"),
    "abort": ("locum.failsafe", "abort"),
    "reset_abort": ("locum.failsafe", "reset_abort"),
    "is_aborted": ("locum.failsafe", "is_aborted"),
    "install_kill_hotkey": ("locum.failsafe", "install_kill_hotkey"),
    "Selector": ("locum.selectors", "Selector"),
    "parse_selector": ("locum.selectors", "parse"),
    "selector_for": ("locum.selectors", "selector_for"),
    "retry": ("locum.wait", "retry"),
    "retry_call": ("locum.wait", "retry_call"),
    "Deadline": ("locum.wait", "Deadline"),
    "get_backend": ("locum.backends", "get_backend"),
    "set_backend": ("locum.backends", "set_backend"),
    "create_backend": ("locum.backends", "create_backend"),
    "register_backend": ("locum.backends", "register_backend"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'locum' has no attribute {name!r}")
    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value  # cache: later lookups bypass __getattr__
    return value


def __dir__() -> List[str]:
    return sorted(set(globals()) | set(_LAZY))


# Import the submodules whose names clash with convenience functions *now*, so
# that the import system's ``setattr(locum, "wait", <module>)`` happens before
# the function proxies below are bound (and never again). Both are lightweight.
from . import wait as _wait_module  # noqa: E402,F401
from . import window as _window_module  # noqa: E402,F401


class _CallableModule:
    """A function that doubles as a proxy for the same-named submodule."""

    __slots__ = ("_func", "_module_name")

    def __init__(self, func: Any, module_name: str):
        object.__setattr__(self, "_func", func)
        object.__setattr__(self, "_module_name", module_name)

    def _module(self) -> Any:
        return importlib.import_module(object.__getattribute__(self, "_module_name"))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return object.__getattribute__(self, "_func")(*args, **kwargs)

    @property
    def __wrapped__(self) -> Any:  # lets inspect.signature() see the real function
        return object.__getattribute__(self, "_func")

    @property
    def __doc__(self) -> Optional[str]:  # type: ignore[override]
        return object.__getattribute__(self, "_func").__doc__

    @property
    def __name__(self) -> str:
        return object.__getattribute__(self, "_func").__name__

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module(), name)

    def __setattr__(self, name: str, value: Any) -> None:  # e.g. monkeypatching module globals
        setattr(self._module(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._module(), name)

    def __dir__(self) -> List[str]:
        return dir(self._module())

    def __repr__(self) -> str:
        return f"<function locum.{self.__name__} (also module {object.__getattribute__(self, '_module_name')!r})>"


# ------------------------------------------------------------- default desktop
_default_desktop: Optional["Desktop"] = None
_desktop_lock = threading.Lock()


def get_desktop() -> "Desktop":
    """The process-wide default :class:`Desktop`, created on first use.

    It uses the default backend (``LOCUM_BACKEND`` env var or the platform
    backend). Replace it with :func:`set_desktop`.
    """
    global _default_desktop
    d = _default_desktop
    if d is None:
        with _desktop_lock:
            if _default_desktop is None:
                from .desktop import Desktop

                _default_desktop = Desktop()
            d = _default_desktop
    return d


def set_desktop(desktop: Optional["Desktop"]) -> Optional["Desktop"]:
    """Replace the default desktop used by the module-level functions (``None`` = recreate lazily).

    Returns the previous default desktop (or ``None``).
    """
    global _default_desktop
    with _desktop_lock:
        old, _default_desktop = _default_desktop, desktop
    return old


# ------------------------------------------------- module-level convenience API
# Each function forwards to the same-named method of get_desktop(); see
# :class:`locum.desktop.Desktop` for full documentation of the arguments.


def position() -> Point:
    """Current cursor position."""
    return get_desktop().position()


def size() -> tuple:
    """``(width, height)`` of the primary monitor."""
    return get_desktop().size()


def move(target: Any, y: Optional[float] = None, **kw: Any) -> Point:
    """Move the cursor to a target (coordinates, element, selector, image...)."""
    return get_desktop().move(target, y, **kw)


def click(target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
    """Click a target (``None`` = current position). See :meth:`Desktop.click`."""
    return get_desktop().click(target, y, **kw)


def double_click(target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
    """Double-click a target."""
    return get_desktop().double_click(target, y, **kw)


def triple_click(target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
    """Triple-click a target."""
    return get_desktop().triple_click(target, y, **kw)


def right_click(target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
    """Right-click a target."""
    return get_desktop().right_click(target, y, **kw)


def middle_click(target: Any = None, y: Optional[float] = None, **kw: Any) -> Point:
    """Middle-click a target."""
    return get_desktop().middle_click(target, y, **kw)


def drag(start: Any, end: Any, **kw: Any) -> Point:
    """Drag from ``start`` (``None`` = current position) to ``end``."""
    return get_desktop().drag(start, end, **kw)


def scroll(dy: int = 0, dx: int = 0, **kw: Any) -> None:
    """Scroll wheel notches (``dy > 0`` = up), optionally ``at=`` a target."""
    get_desktop().scroll(dy, dx, **kw)


def type(text: str, **kw: Any) -> None:  # noqa: A001 - deliberate, mirrors Desktop.type
    """Type text into the focused control."""
    get_desktop().type(text, **kw)


def press(*keys: Any, **kw: Any) -> None:
    """Tap keys in sequence (``press("tab", "enter")``)."""
    get_desktop().press(*keys, **kw)


def hotkey(*keys: Any, **kw: Any) -> None:
    """Press a key combination (``hotkey("ctrl+c")``)."""
    get_desktop().hotkey(*keys, **kw)


def screenshot(region: Any = None, **kw: Any) -> "Image":
    """Capture the screen (or a region) as a PIL image; ``path=`` saves it."""
    return get_desktop().screenshot(region, **kw)


def locate(image: Any, **kw: Any) -> Optional[Match]:
    """Find a template image on screen; returns a :class:`Match` or ``None``."""
    return get_desktop().locate(image, **kw)


def find(selector: Any, **kw: Any) -> "Element":
    """Find an element by selector (waits up to the default timeout)."""
    return get_desktop().find(selector, **kw)


def find_all(selector: Any, **kw: Any) -> List["Element"]:
    """All elements matching a selector (no waiting)."""
    return get_desktop().find_all(selector, **kw)


def exists(selector: Any, **kw: Any) -> bool:
    """Whether an element matching ``selector`` exists (``timeout=`` to wait)."""
    return get_desktop().exists(selector, **kw)


def _wait(selector: Any, **kw: Any) -> Optional["Element"]:
    """Wait for an element to reach a state (``state="visible"`` by default; ``"gone"`` returns None)."""
    return get_desktop().wait(selector, **kw)


def _window(title: Any = None, **kw: Any) -> "Window":
    """The best window matching a title / ``app=`` query (waits up to the default timeout)."""
    return get_desktop().window(title, **kw)


_wait.__name__ = _wait.__qualname__ = "wait"
_window.__name__ = _window.__qualname__ = "window"

# ``wait`` and ``window`` are both convenience functions *and* submodules. The
# proxies are callable like the functions and forward attribute access to the
# submodules, so ``locum.wait("button")``, ``locum.wait.retry`` and
# ``import locum.window as w; w.Window`` all work.
wait = _CallableModule(_wait, "locum.wait")
window = _CallableModule(_window, "locum.window")


def windows(title: Any = None, **kw: Any) -> List["Window"]:
    """All windows matching the query, front-most first."""
    return get_desktop().windows(title, **kw)


def active_window() -> Optional["Window"]:
    """The foreground window."""
    return get_desktop().active_window()


def launch(command: Any, **kw: Any) -> Any:
    """Start an application (``wait_window=`` waits for and returns its window)."""
    return get_desktop().launch(command, **kw)


def clipboard_get() -> str:
    """Text on the clipboard."""
    return get_desktop().clipboard_get()


def clipboard_set(text: str) -> None:
    """Put text on the clipboard."""
    get_desktop().clipboard_set(text)


def tree(**kw: Any) -> str:
    """Readable dump of the accessibility tree."""
    return get_desktop().tree(**kw)


__all__ = [
    "__version__",
    # facade / objects
    "Desktop",
    "Element",
    "Window",
    "Humanize",
    "FailSafe",
    "Selector",
    "get_desktop",
    "set_desktop",
    # types
    "Point",
    "Rect",
    "Match",
    "Monitor",
    "MouseButton",
    "WindowInfo",
    "coerce_point",
    # errors
    "LocumError",
    "BackendError",
    "BackendUnavailable",
    "NotSupported",
    "FailSafeTriggered",
    "ActionTimeout",
    "ElementNotFound",
    "AmbiguousElement",
    "ImageNotFound",
    "WindowNotFound",
    "SelectorSyntaxError",
    "PolicyViolation",
    "WorkflowError",
    # safety / helpers
    "abort",
    "reset_abort",
    "is_aborted",
    "install_kill_hotkey",
    "parse_selector",
    "selector_for",
    "retry",
    "retry_call",
    "Deadline",
    "get_backend",
    "set_backend",
    "create_backend",
    "register_backend",
    # convenience functions
    "position",
    "size",
    "move",
    "click",
    "double_click",
    "triple_click",
    "right_click",
    "middle_click",
    "drag",
    "scroll",
    "type",
    "press",
    "hotkey",
    "screenshot",
    "locate",
    "find",
    "find_all",
    "exists",
    "wait",
    "window",
    "windows",
    "active_window",
    "launch",
    "clipboard_get",
    "clipboard_set",
    "tree",
]
