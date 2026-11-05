"""``MacOSBackend`` accessibility-tree roots and element lookup.
"""

from __future__ import annotations

from typing import List, Optional, Set, Tuple

from ..base import AccessibleNode
from .constants import (
    _NS_APPLICATION_ACTIVATION_POLICY_REGULAR,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionAll,
    kCGWindowListOptionOnScreenOnly,
)
from .ax import AXNode
from .windowing import _plausible_record


class _AccessibilityMixin:
    # --------------------------------------------------------------- accessibility
    def _regular_apps(self) -> List[Tuple[int, str]]:
        """``(pid, name)`` of running apps with a Dock icon.

        ``NSWorkspace.runningApplications`` is a snapshot unless the run loop runs
        (see :meth:`_pump_appkit`), so owners of on-screen windows that are not in
        it (apps launched since) are added from the live window list."""
        appkit = self._appkit
        if appkit is None:
            return []
        self._pump_appkit()
        out = []
        seen: Set[int] = set()
        for app in appkit.NSWorkspace.sharedWorkspace().runningApplications() or []:
            try:
                pid = int(app.processIdentifier())
                seen.add(pid)
                if int(app.activationPolicy()) != _NS_APPLICATION_ACTIVATION_POLICY_REGULAR:
                    continue
                if app.isTerminated():
                    continue
                name = str(app.localizedName() or "")
            except Exception:
                continue
            self._app_names[pid] = name
            out.append((pid, name))
        try:
            records = self._window_records(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements)
        except Exception:
            records = []
        for rec in records:
            pid = int(rec.get("kCGWindowOwnerPID", -1))
            if pid in seen or not _plausible_record(rec):
                continue
            seen.add(pid)
            try:
                app = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app is None or int(app.activationPolicy()) != _NS_APPLICATION_ACTIVATION_POLICY_REGULAR:
                    continue
                name = str(app.localizedName() or rec.get("kCGWindowOwnerName") or "")
            except Exception:
                continue
            self._app_names[pid] = name
            out.append((pid, name))
        return out

    def accessibility_roots(self) -> List[AccessibleNode]:
        """Top-level AX windows of every regular application, front-most first.

        Each root's ``handle`` is the matching ``CGWindowID`` (so it pairs with
        :meth:`list_windows`), or ``None`` when no CG window pairs with it
        unambiguously; two roots never share a handle."""
        ax = self._require_ax()
        records = self._window_records(kCGWindowListOptionAll)
        z_order = {int(r.get("kCGWindowNumber", 0)): i for i, r in enumerate(
            self._window_records(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements))}
        roots: List[Tuple[int, int, AXNode]] = []
        for app_index, (pid, name) in enumerate(self._regular_apps()):
            app_el = ax.application(pid)
            wins, pairs = self._ax_pairing(pid, records)
            for i, w in enumerate(wins):
                handle = pairs.get(i)
                parent = AXNode(app_el, self, app=name, pid=pid, handle=None, parent=None)
                node = AXNode(w[0], self, app=name, pid=pid, handle=handle, parent=parent)
                roots.append((z_order.get(handle, len(z_order) + app_index) if handle is not None
                              else len(z_order) + 10000 + app_index, len(roots), node))
        roots.sort(key=lambda t: (t[0], t[1]))
        return [n for _, _, n in roots]

    def application_node(self, pid: int) -> AXNode:
        """The AX application element of ``pid`` (includes its menu bar)."""
        ax = self._require_ax()
        return AXNode(ax.application(pid), self, app=self._app_name(pid), pid=pid, handle=None, parent=None)

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        """Deepest element at pixel ``(x, y)`` via ``AXUIElementCopyElementAtPosition``."""
        ax = self._require_ax()
        px, py = self._get_layout().pixel_to_point(x, y)
        el = ax.element_at(px, py)
        if el is None:
            return None
        pid = ax.pid(el)
        return AXNode(el, self, app=self._app_name(pid), pid=pid)

    def focused_element(self) -> Optional[AccessibleNode]:
        ax = self._require_ax()
        el = ax.attr(ax.system_wide, "AXFocusedUIElement")
        if el is None:
            pid = self._frontmost_pid()
            if pid is None:
                return None
            el = ax.attr(ax.application(pid), "AXFocusedUIElement")
            if el is None:
                return None
        pid = ax.pid(el)
        return AXNode(el, self, app=self._app_name(pid), pid=pid)
