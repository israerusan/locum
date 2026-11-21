"""Accessibility entry points for the Windows backend: the
:class:`_AccessibilityMixin` (UIA availability, roots, element at point, focus).
"""

from __future__ import annotations

import ctypes
import sys
import weakref
from ctypes import wintypes
from typing import Any, Dict, FrozenSet, List, Optional, Set

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
            # Failed, or still subscribing after START_TIMEOUT: stop it, or a late
            # subscription would stay registered with nobody left to close it.
            try:
                tracker.close(0)
            except Exception:
                pass
            self._change_tracker_failed = True
            return None
        self._change_tracker = tracker
        # A backend dropped without close() must not leave the UIA event thread
        # running: the tracker holds no reference back to the backend, so this fires
        # when the backend is collected (as the X11/Wayland backends' finalizers do).
        self._change_tracker_finalizer = weakref.finalize(self, tracker._stop.set)
        # Shared selector trees (locum.selectors) drop the windows these events touch.
        from ...selectors import register_change_source

        self._selector_change_source = _SelectorChangeSource(self, tracker)
        register_change_source(self._selector_change_source)
        return tracker

    def window_content_pids(self, handle: Any) -> Optional[FrozenSet[int]]:
        """Processes drawing content inside top-level window ``handle``: its owner plus the
        owner of every descendant child window; ``None`` if the window cannot be read.

        UI Automation change events carry the *sending* element's process. A Store/UWP
        app's top-level window (Calculator, Settings) belongs to ApplicationFrameHost.exe
        while its content -- and its events -- come from the app's own process, whose
        ``Windows.UI.Core.CoreWindow`` is a child window of the frame; WebView2 and other
        cross-process children work the same way. ``observe(reuse=...)`` treats the window
        as changed when any of these processes raised an event. Win32 only (no UIA call):
        ``EnumChildWindows`` + ``GetWindowThreadProcessId``, microseconds per child. A
        minimized Store app's ``CoreWindow`` is a cloaked top-level window with the frame's
        title instead of a child; it is found with ``EnumWindows``.
        """
        u32 = _user32()
        try:
            hwnd = int(handle)
        except (TypeError, ValueError):
            return None
        if u32 is None or not hwnd or not u32.IsWindow(hwnd):
            return None
        pids = set()

        def pid_of(h: int) -> int:
            out = wintypes.DWORD(0)
            u32.GetWindowThreadProcessId(h, ctypes.byref(out))
            return int(out.value)

        own = pid_of(hwnd)
        if own:
            pids.add(own)

        def cb(child: int, _lp: int) -> bool:
            p = pid_of(child)
            if p:
                pids.add(p)
            return len(pids) < 64  # a pathological window cannot make this unbounded

        try:
            u32.EnumChildWindows(hwnd, _ENUM_PROC(cb), 0)
        except Exception:
            return None
        if len(pids) <= 1 and _class_of(u32, hwnd) == _UWP_FRAME_CLASS:
            # A minimized (or suspended) Store app's CoreWindow is no longer a child of
            # its frame: Windows re-parents it to the desktop, cloaked, under the same
            # title. It still raises the app's events, so look for it there.
            title = _text_of(u32, hwnd)
            hosted: List[int] = []

            def top(h: int, _lp: int) -> bool:
                if _class_of(u32, h) == _UWP_CORE_CLASS and _text_of(u32, h) == title:
                    hosted.append(int(h))
                return len(hosted) < 8

            try:
                if title:
                    u32.EnumWindows(_ENUM_PROC(top), 0)
            except Exception:
                hosted = []
            for h in hosted:
                p = pid_of(h)
                if p:
                    pids.add(p)
        return frozenset(pids) if pids else None

    def _close_change_tracker(self) -> None:
        tracker = getattr(self, "_change_tracker", None)
        self._change_tracker = None
        self._change_tracker_failed = True
        finalizer = getattr(self, "_change_tracker_finalizer", None)
        if finalizer is not None:
            finalizer.detach()  # close() below stops the thread itself
            self._change_tracker_finalizer = None
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
        if getattr(ctx, "dpi_call", None) is None:
            try:
                ctx.dpi_call = self._dpi_call  # UIANode.bounds is read after the backend call returned
            except Exception:
                pass
        if getattr(ctx, "window_bounds", None) is None:
            try:
                ctx.window_bounds = self._uia_window_bounds  # visible frame of top-level roots
            except Exception:
                pass
        return ctx

    def _uia_window_bounds(self, hwnd: Any) -> Optional[Any]:
        """Visible bounds of top-level window ``hwnd`` for :attr:`UIANode.bounds`, the same
        rectangle as :attr:`WindowInfo.rect` (``None``: let UIA answer -- minimized, gone
        or unreadable). Called inside the backend's DPI scope."""
        try:
            h = int(hwnd)
            a = self._api
            if not h or not a.IsWindow(h) or a.IsIconic(h):
                return None
            return self._visible_rect(h)
        except Exception:
            return None

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
        roots: List[AccessibleNode] = []
        for hwnd in self._toplevel_hwnds():
            try:
                listed = self._is_candidate(hwnd)  # what list_windows keeps, without its WindowInfo
            except Exception:
                continue  # window vanished while enumerating
            if self._is_hung(hwnd):
                # ElementFromHandle would wait the full UIA timeout on a hung window's
                # thread and return nothing: skip it up front.
                continue
            if listed:
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


_ENUM_PROC: Any = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if sys.platform == "win32" else None
_U32: Any = None


def _user32() -> Any:
    """``user32`` with the signatures :meth:`_AccessibilityMixin.window_content_pids` uses."""
    global _U32
    if _U32 is None and sys.platform == "win32":
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.EnumChildWindows.argtypes = [wintypes.HWND, _ENUM_PROC, wintypes.LPARAM]
        u.EnumChildWindows.restype = wintypes.BOOL
        u.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        _U32 = u
    return _U32


_UWP_FRAME_CLASS = "ApplicationFrameWindow"
_UWP_CORE_CLASS = "Windows.UI.Core.CoreWindow"


def _class_of(u32: Any, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(257)
    try:
        n = int(u32.GetClassNameW(hwnd, buf, 257))
    except Exception:
        return ""
    return buf.value[:n] if n > 0 else ""


def _text_of(u32: Any, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    try:
        n = int(u32.GetWindowTextW(hwnd, buf, 512))
    except Exception:
        return ""
    return buf.value[:n] if n > 0 else ""


class _SelectorChangeSource:
    """Adapter for :func:`locum.selectors.register_change_source`: which windows changed."""

    def __init__(self, backend: Any, tracker: Any) -> None:
        import weakref

        self._backend = weakref.ref(backend)
        self.tracker = tracker

    @property
    def alive(self) -> bool:
        return bool(self.tracker.alive) and self._backend() is not None

    def stamp(self) -> int:
        return int(self.tracker.identity_generation)

    def dirty_since(self, stamp: int) -> Optional[Set[int]]:
        return self.tracker.identity_dirty_since(stamp)

    def pids_of(self, node: Any) -> Optional[FrozenSet[int]]:
        """Processes whose events can change ``node``'s subtree (``None``: unknown)."""
        try:
            own = node.pid
        except Exception:
            own = None
        be = self._backend()
        content = None
        if be is not None:
            try:
                content = be.window_content_pids(node.handle)
            except Exception:
                content = None
        if content is None:
            return frozenset({own}) if own else None
        return frozenset(content) | (frozenset({own}) if own else frozenset())
