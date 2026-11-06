"""Accessibility part of the X11 backend: AT-SPI roots, hit testing and focus,
as a mixin of :class:`X11Backend`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ...errors import NotSupported
from ...types import WindowInfo
from ..base import AccessibleNode
from .atspi import AtspiNode, load_atspi


class _AccessibilityMixin:
    """AT-SPI accessibility tree access of :class:`X11Backend`."""

    # ============================================================ accessibility
    def _atspi(self) -> Any:
        if not self._atspi_checked:
            self._atspi_checked = True
            if self._atspi_source == "auto":
                self._atspi_mod = load_atspi()
            else:
                self._atspi_mod = self._atspi_source
        return self._atspi_mod

    def _require_atspi(self) -> Any:
        self._check()
        mod = self._atspi()
        if mod is None:
            raise NotSupported(
                "AT-SPI accessibility is unavailable: install PyGObject with the Atspi typelib "
                "(e.g. `apt install python3-gi gir1.2-atspi-2.0 at-spi2-core`)"
            )
        return mod

    def _make_resolver(self) -> Callable[[AtspiNode], Any]:
        cache: Dict[str, Any] = {}

        def windows() -> List[WindowInfo]:
            if "w" not in cache:
                try:
                    cache["w"] = self.list_windows()
                except Exception:
                    cache["w"] = []
            return cache["w"]

        def resolve(node: AtspiNode) -> Any:
            if node.role not in ("window", "dialog"):
                return None
            pid, title, bounds = node.pid, node.name, node.bounds
            wins = windows()
            same_pid = [w for w in wins if pid and w.pid == pid]
            # a known pid never matches another process's window (as in owning_window)
            candidates = same_pid or ([w for w in wins if not w.pid] if pid else wins)
            for w in same_pid:
                if title and w.title == title:
                    return w.handle
            if bounds is not None:
                best = max(candidates, key=lambda w: w.rect.iou(bounds), default=None)
                if best is not None and best.rect.iou(bounds) > 0.5:
                    return best.handle
            if len(same_pid) == 1:
                return same_pid[0].handle
            for w in candidates:
                if title and w.title == title:
                    return w.handle
            return None

        return resolve

    def accessibility_roots(self) -> List[AccessibleNode]:
        """Top-level accessible windows of every AT-SPI application."""
        atspi = self._require_atspi()
        try:
            desktop = atspi.get_desktop(0)
        except Exception as exc:
            raise NotSupported(f"AT-SPI registry unavailable: {exc}") from exc
        if desktop is None:
            raise NotSupported("AT-SPI registry unavailable (is at-spi2-registryd running?)")
        resolver = self._make_resolver()
        desktop_node = AtspiNode(desktop, atspi)
        roots: List[AccessibleNode] = []
        for app in desktop_node.children():
            for win in app.children():
                roots.append(AtspiNode(win.accessible, atspi, app, resolver))
        return roots

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        """Deepest accessible at ``(x, y)``, descending with AT-SPI hit-testing."""
        roots = [r for r in self.accessibility_roots() if isinstance(r, AtspiNode)]
        try:
            order = {w.handle: i for i, w in enumerate(self.list_windows()) if w.is_visible}
        except Exception:
            order = {}
        hits = []
        for r in roots:
            b = r.bounds
            if b is None or not b.contains((x, y)) or "offscreen" in r.states:
                continue
            h = r.handle
            hits.append((order.get(h, len(order) + 1), b.area, r))
        if not hits:
            return None
        hits.sort(key=lambda t: (t[0], t[1]))
        node: AtspiNode = hits[0][2]
        for _ in range(256):
            child = node.child_at_point(x, y)
            if child is None:
                break
            node = child
        return node

    def focused_element(self) -> Optional[AccessibleNode]:
        roots = self.accessibility_roots()
        active = [r for r in roots if "active" in r.states] or roots
        for root in active:
            for node in root.walk(max_depth=48):
                if "focused" in node.states:
                    return node
        return None
