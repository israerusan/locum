"""AT-SPI on Wayland: the X11 backend's :class:`AtspiNode`, with honest coordinates.

AT-SPI itself is a D-Bus protocol and works the same under Wayland: the
application list, the element tree, names, roles, states, values and the
native actions (``invoke``, ``set_value``, ``focus``...) are all available.

What Wayland takes away is *global geometry*: a Wayland client does not know
where its window is on screen, so most toolkits (GTK 3/4, Qt) report
``CoordType.SCREEN`` extents relative to the window, not the desktop. Clicking
the centre of such a rectangle would hit the wrong place. :class:`WaylandAtspiNode`
therefore hides the extents by default (``bounds`` is ``None``; the raw values
stay readable as the ``atspi_extents`` attribute) and derives visibility from
the AT-SPI ``showing``/``visible`` states. Element actions then use the native
AT-SPI action instead of the mouse. ``screen_coords=True`` trusts the extents
(for toolkits/compositors known to report desktop coordinates, or a single
fullscreen window at the origin).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ...types import Rect
from ..x11.atspi import AtspiNode

#: Native AT-SPI roles of top-level windows (children of an ``application`` node).
WINDOW_ROLES = frozenset({"frame", "window", "dialog", "alert", "file chooser", "color chooser", "font chooser"})
#: Top-level roles that are secondary windows of their application (dialogs, alerts).
DIALOG_ROLES = frozenset({"dialog", "alert", "file chooser", "color chooser", "font chooser"})


def _str_attr(obj: Any, *names: str) -> str:
    for n in names:
        try:
            v = getattr(obj, n)
        except Exception:
            continue
        if callable(v):
            try:
                v = v()
            except Exception:
                continue
        if isinstance(v, str) and v:
            return v
    return ""


class WaylandAtspiNode(AtspiNode):
    """:class:`AtspiNode` for the Wayland backend (see module docstring).

    Args:
        screen_coords: trust AT-SPI screen extents (default False: ``bounds`` is ``None``).
    """

    def __init__(
        self,
        acc: Any,
        atspi: Any,
        parent: Optional[AtspiNode] = None,
        handle_resolver: Optional[Callable[[AtspiNode], Any]] = None,
        max_children: int = 5000,
        *,
        screen_coords: bool = False,
    ) -> None:
        super().__init__(acc, atspi, parent, handle_resolver, max_children)
        self._screen_coords = bool(screen_coords)

    def _wrap(self, acc: Any, parent: Optional[AtspiNode]) -> AtspiNode:
        return WaylandAtspiNode(acc, self._atspi, parent, None, self._max_children, screen_coords=self._screen_coords)

    # ---------------------------------------------------------------- geometry
    @property
    def raw_extents(self) -> Optional[Rect]:
        """The extents AT-SPI reports for ``CoordType.SCREEN`` (usually window-relative on Wayland)."""
        return AtspiNode.bounds.fget(self)  # type: ignore[attr-defined]

    @property
    def bounds(self) -> Optional[Rect]:
        return self.raw_extents if self._screen_coords else None

    @property
    def is_visible(self) -> bool:
        if self._screen_coords:
            return super().is_visible
        st = self.states
        return "visible" in st and "offscreen" not in st

    def attributes(self) -> Dict[str, Any]:
        out = super().attributes()
        if not self._screen_coords:
            ext = self.raw_extents
            if ext is not None:
                out.setdefault("atspi_extents", f"{ext.left},{ext.top},{ext.width},{ext.height}")
        return out

    # ------------------------------------------------------------------ windows
    def is_top_level(self) -> bool:
        if self.native_role.lower() not in WINDOW_ROLES:
            return False
        par = self.parent
        return par is not None and par.native_role.lower() == "application"

    def window_key(self) -> Optional[str]:
        """A stable handle string for a top-level window node (``None`` for other nodes).

        Uses the accessible's D-Bus identity (application bus name + object path)
        when the AT-SPI binding exposes it, else ``pid`` + index in the application.
        """
        if not self.is_top_level():
            return None
        acc = self._acc
        path = _str_attr(acc, "path", "get_path")
        app = getattr(acc, "app", None)
        bus = _str_attr(app, "bus_name", "get_bus_name") if app is not None else ""
        if bus and path:
            return f"atspi:{bus}{path}"
        idx = self._call("get_index_in_parent")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = -1
        return f"atspi:pid{self.pid or 0}:{self.app_name()}:{idx}"

    @property
    def handle(self) -> Any:
        key = self.window_key()
        if key is not None:
            return key
        return super().handle


__all__ = ["DIALOG_ROLES", "WINDOW_ROLES", "WaylandAtspiNode"]
