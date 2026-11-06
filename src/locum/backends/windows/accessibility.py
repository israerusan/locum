"""Accessibility entry points for the Windows backend: the
:class:`_AccessibilityMixin` (UIA availability, roots, element at point, focus).
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from ...errors import NotSupported
from ..base import AccessibleNode
from ._win32api import GWL_STYLE, WS_POPUP
from .uia import UIANode, UIA_MenuControlTypeId, _UIAContext, _safe


class _AccessibilityMixin:
    """UI Automation entry points of :class:`WindowsBackend`."""

    # ============================================================ accessibility
    def _uia_available(self) -> bool:
        if self._uia_ctx is not None:
            return True
        if self._uia_error is not None:
            return False
        if sys.platform != "win32":
            return False
        import importlib.util

        return importlib.util.find_spec("comtypes") is not None

    def change_tracker(self) -> Any:
        """Desktop-wide UI Automation change events (see :mod:`.uia_events`); ``None`` if off/unavailable."""
        tracker = getattr(self, "_change_tracker", None)
        if tracker is not None:
            return tracker if tracker.alive else None
        if getattr(self, "_change_tracker_failed", False) or getattr(self, "_closed", False):
            return None
        from .uia_events import ChangeTracker, events_enabled

        if not events_enabled():
            self._change_tracker_failed = True
            return None
        try:
            from .uia import CLSID_CUIAutomation, CLSID_CUIAutomation8

            tracker = ChangeTracker(self.uia.module, (CLSID_CUIAutomation8, CLSID_CUIAutomation))
        except Exception:
            self._change_tracker_failed = True
            return None
        if not tracker.alive:
            self._change_tracker_failed = True
            return None
        self._change_tracker = tracker
        return tracker

    def _close_change_tracker(self) -> None:
        tracker = getattr(self, "_change_tracker", None)
        self._change_tracker = None
        self._change_tracker_failed = True
        if tracker is not None:
            try:
                tracker.close()
            except Exception:
                pass

    @property
    def uia(self) -> Any:
        """The :class:`_UIAContext` (raises NotSupported when UI Automation is unavailable)."""
        if not self._accessibility:
            raise NotSupported("Accessibility was disabled for this WindowsBackend")
        if self._uia_ctx is None:
            if self._uia_error is not None:
                raise NotSupported(self._uia_error)
            try:
                self._uia_ctx = _UIAContext(self._uia_view, self._uia_timeout)
            except NotSupported as exc:
                self._uia_error = str(exc)
                raise
        ctx = self._uia_ctx
        if getattr(ctx, "app_name", None) is None:
            try:
                ctx.app_name = self._app_for_pid  # lets UIANode report its process name
            except Exception:
                pass
        return ctx

    def _app_for_pid(self, pid: int) -> str:
        """:meth:`_process_name`, cached per tree snapshot.

        Nodes ask once per element, so names are cached -- but only until the
        next :meth:`accessibility_roots` / :meth:`element_at` /
        :meth:`focused_element` call: Windows recycles PIDs quickly, and a
        lifetime cache would report an exited process's name for a new one.
        """
        cache = self._pid_names
        name = cache.get(pid)
        if name is None:
            name = self._process_name(int(pid))
            if len(cache) > 512:
                cache.clear()
            cache[pid] = name
        return name

    def _popup_candidate(self, hwnd: int) -> Optional[bool]:
        """For a top-level window :meth:`list_windows` skips: True for a visible Win32
        pop-up menu (``#32768``), None for another visible ``WS_POPUP`` window (a menu
        only if its UIA element says so, as WinForms/WPF/Office context menus do),
        False otherwise."""
        a = self._api
        if not a.IsWindowVisible(hwnd):
            return False
        if self._class(hwnd) == "#32768":
            menu: Optional[bool] = True
        elif int(a.GetWindowLongPtrW(hwnd, GWL_STYLE)) & WS_POPUP:
            menu = None
        else:
            return False
        return False if self._is_cloaked(hwnd) else menu

    def accessibility_roots(self) -> List[AccessibleNode]:
        """One UIA root per window from :meth:`list_windows`, plus open pop-up menus
        (context menus, menu-bar drop-downs), in Z-order; ``handle`` = HWND.

        UIA parents pop-up menus to the desktop rather than to the window that
        opened them, so without these roots an open menu's items could not be found.
        """
        self._check()
        ctx = self.uia
        names: Dict[int, str] = {}
        self._pid_names = names  # fresh per snapshot: PIDs are recycled
        hwnds = self._toplevel_hwnds()
        listed = {int(w.handle) for w in self._windows_from(hwnds, names)}
        roots: List[AccessibleNode] = []
        for hwnd in hwnds:
            if hwnd in listed:
                el = ctx.element_from_handle(hwnd)
            else:
                try:
                    menu = self._popup_candidate(hwnd)
                except Exception:
                    continue  # window vanished while enumerating
                if menu is False:
                    continue
                el = ctx.element_from_handle(hwnd)
                if menu is None and _safe(lambda: int(el.CurrentControlType), 0) != UIA_MenuControlTypeId:
                    continue
            if el is not None:
                roots.append(UIANode(el, ctx, None, handle=hwnd, top_level=True))
        return roots

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        """Deepest UIA element at screen point ``(x, y)`` (``IUIAutomation::ElementFromPoint``)."""
        self._check()
        ctx = self.uia
        self._pid_names = {}
        el = ctx.element_from_point(x, y)
        return UIANode(el, ctx) if el is not None else None

    def focused_element(self) -> Optional[AccessibleNode]:
        """The element with keyboard focus (``IUIAutomation::GetFocusedElement``)."""
        self._check()
        ctx = self.uia
        self._pid_names = {}
        el = ctx.focused()
        return UIANode(el, ctx) if el is not None else None
