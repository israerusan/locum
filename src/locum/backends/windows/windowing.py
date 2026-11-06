"""Window management for the Windows backend: the :class:`_WindowsMixin`
(enumeration, window info, activation, move/resize, state, close).
"""

from __future__ import annotations

import ctypes
from typing import Any, Dict, List, Optional, Sequence

from ...errors import BackendError, NotSupported, WindowNotFound
from ...types import Rect, WindowInfo
from ._win32api import (
    DWMWA_CLOAKED,
    DWMWA_EXTENDED_FRAME_BOUNDS,
    DWORD,
    GWL_EXSTYLE,
    GWL_STYLE,
    POINT,
    PROCESS_QUERY_LIMITED_INFORMATION,
    RECT,
    SW_HIDE,
    SW_MAXIMIZE,
    SW_MINIMIZE,
    SW_RESTORE,
    SW_SHOW,
    WM_CLOSE,
    WS_EX_APPWINDOW,
    WS_EX_TOOLWINDOW,
    WS_POPUP,
)
from .keys import VK_MENU


class _WindowsMixin:
    """Top-level window listing and management of :class:`WindowsBackend`."""

    # ================================================================== windows
    def _hwnd(self, handle: Any) -> int:
        h = getattr(handle, "handle", handle)
        try:
            hwnd = int(h)
        except (TypeError, ValueError):
            raise WindowNotFound(f"Invalid window handle {handle!r}") from None
        if hwnd == 0 or not self._api.IsWindow(hwnd):
            raise WindowNotFound(f"No window with handle {hwnd:#x}")
        return hwnd

    def _text(self, hwnd: int) -> str:
        a = self._api
        n = int(a.GetWindowTextLengthW(hwnd))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 2)
        got = int(a.GetWindowTextW(hwnd, buf, n + 2))
        return buf.value[:got] if got >= 0 else ""

    def _class(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(257)
        got = int(self._api.GetClassNameW(hwnd, buf, 257))
        return buf.value[:got] if got > 0 else ""

    def _pid(self, hwnd: int) -> int:
        pid = DWORD(0)
        self._api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _process_name(self, pid: int) -> str:
        if not pid:
            return ""
        a = self._api
        hproc = a.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not hproc:
            return ""
        try:
            size = DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not a.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return ""
            path = buf.value[: size.value]
        finally:
            a.CloseHandle(hproc)
        return path.replace("/", "\\").rsplit("\\", 1)[-1]

    def _is_cloaked(self, hwnd: int) -> bool:
        fn = getattr(self._api, "DwmGetWindowAttribute", None)
        if fn is None:
            return False
        val = DWORD(0)
        try:
            hr = int(fn(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)))
        except Exception:
            return False
        return hr == 0 and val.value != 0

    def _outer_rect(self, hwnd: int) -> Rect:
        rc = RECT()
        if not self._api.GetWindowRect(hwnd, ctypes.byref(rc)):
            raise self._error("GetWindowRect")
        return rc.to_rect()

    def _frame_rect(self, hwnd: int) -> Rect:
        """Visible window bounds (DWM extended frame bounds, without invisible resize borders)."""
        fn = getattr(self._api, "DwmGetWindowAttribute", None)
        if fn is not None:
            rc = RECT()
            try:
                hr = int(fn(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rc), ctypes.sizeof(rc)))
            except Exception:
                hr = -1
            if hr == 0 and rc.right > rc.left and rc.bottom > rc.top:
                return rc.to_rect()
        return self._outer_rect(hwnd)

    def _is_candidate(self, hwnd: int) -> bool:
        a = self._api
        if not a.IsWindowVisible(hwnd):
            return False
        exstyle = int(a.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        if exstyle & WS_EX_TOOLWINDOW and not exstyle & WS_EX_APPWINDOW:
            return False
        if int(a.GetWindowTextLengthW(hwnd)) <= 0:
            return False
        return not self._is_cloaked(hwnd)

    def _info(self, hwnd: int, foreground: Any, names: Optional[Dict[int, str]] = None) -> WindowInfo:
        a = self._api
        pid = self._pid(hwnd)
        if names is not None and pid in names:
            app = names[pid]
        else:
            app = self._process_name(pid)
            if names is not None:
                names[pid] = app
        return WindowInfo(
            handle=hwnd,
            title=self._text(hwnd),
            rect=self._frame_rect(hwnd),
            app=app,
            pid=pid or None,
            is_active=bool(foreground) and int(foreground) == hwnd,
            is_minimized=bool(a.IsIconic(hwnd)),
            is_maximized=bool(a.IsZoomed(hwnd)),
            is_visible=bool(a.IsWindowVisible(hwnd)),
            class_name=self._class(hwnd),
        )

    def _toplevel_hwnds(self) -> List[int]:
        """Every top-level window, front-most first (``EnumWindows`` order)."""
        a = self._api
        hwnds: List[int] = []

        def cb(hwnd: Any, lparam: Any) -> int:
            if hwnd:
                hwnds.append(int(hwnd))
            return 1

        proc = a.WNDENUMPROC(cb)
        if not a.EnumWindows(proc, 0):
            raise self._error("EnumWindows")
        return hwnds

    def _windows_from(self, hwnds: Sequence[int], names: Dict[int, str]) -> List[WindowInfo]:
        fg = self._api.GetForegroundWindow()
        out: List[WindowInfo] = []
        for hwnd in hwnds:
            try:
                if self._is_candidate(hwnd):
                    out.append(self._info(hwnd, fg, names))
            except BackendError:
                continue  # window vanished while enumerating
        return out

    def list_windows(self) -> List[WindowInfo]:
        """Visible, titled, non-tool, non-cloaked top-level windows, front-most first."""
        self._check()
        return self._windows_from(self._toplevel_hwnds(), {})

    # ---------------------------------------------------------- input targets
    def foreground_handle(self) -> Any:
        fg = self._api.GetForegroundWindow()
        return int(fg) if fg else None

    def window_handle_at(self, x: int, y: int) -> Any:
        a = self._api
        wfp, anc = getattr(a, "WindowFromPoint", None), getattr(a, "GetAncestor", None)
        if wfp is None or anc is None:
            raise NotSupported("WindowFromPoint is unavailable")
        h = wfp(POINT(int(x), int(y)))
        if not h:
            return None
        return int(anc(h, 2) or h)  # GA_ROOT: the top-level window

    def _owners(self, hwnd: int) -> List[int]:
        get = getattr(self._api, "GetWindow", None)
        out: List[int] = []
        if get is None:
            return out
        h = hwnd
        for _ in range(16):
            h = get(h, 4)  # GW_OWNER
            if not h:
                break
            out.append(int(h))
        return out

    def window_belongs_to(self, handle: Any, target: Any) -> bool:
        """The same window, a window it owns (a dialog, a pop-up), or an untitled pop-up of
        its process (menus, drop-down lists and tooltips have no owner)."""
        h, t = int(handle), int(target)
        if h == t or t in self._owners(h):
            return True
        a = self._api
        try:
            untitled_popup = int(a.GetWindowTextLengthW(h)) <= 0 and bool(int(a.GetWindowLongPtrW(h, GWL_STYLE)) & WS_POPUP)
            return untitled_popup and self._pid(h) == self._pid(t)
        except Exception:
            return False

    def windows_above(self, handle: Any) -> List[Rect]:
        """Every visible, uncloaked top-level window in front of ``handle`` (titled or not:
        notifications and tool windows too) that is not part of it."""
        target = int(handle)
        out: List[Rect] = []
        a = self._api
        for h in self._toplevel_hwnds():
            if h == target:
                return out
            try:
                if not a.IsWindowVisible(h) or a.IsIconic(h) or self._is_cloaked(h) or self.window_belongs_to(h, target):
                    continue
                r = self._frame_rect(h)
            except Exception:
                continue
            if r.width > 0 and r.height > 0:
                out.append(r)
        return out

    def active_window(self) -> Optional[WindowInfo]:
        self._check()
        fg = self._api.GetForegroundWindow()
        if not fg or not self._api.IsWindow(fg):
            return None
        try:
            return self._info(int(fg), fg)
        except BackendError:
            return None

    def window_info(self, handle: Any) -> WindowInfo:
        """Snapshot of any top-level window by handle (even ones :meth:`list_windows` filters out)."""
        self._check()
        hwnd = self._hwnd(handle)
        return self._info(hwnd, self._api.GetForegroundWindow())

    def _is_foreground(self, hwnd: int) -> bool:
        fg = self._api.GetForegroundWindow()
        return bool(fg) and int(fg) == hwnd

    def activate_window(self, handle: Any) -> None:
        """Restore (if minimized) and bring a window to the foreground.

        ``SetForegroundWindow`` is refused unless the caller "owns" the last
        input event, so this escalates: plain call → ``AttachThreadInput`` to
        the foreground and target threads → ALT tap (makes this process the
        source of the last input event).

        Raises:
            WindowNotFound: the handle is invalid.
            BackendError: Windows refused every attempt (foreground lock).
        """
        self._check()
        a = self._api
        hwnd = self._hwnd(handle)
        with self._lock:
            if a.IsIconic(hwnd):
                a.ShowWindow(hwnd, SW_RESTORE)
            if self._is_foreground(hwnd):
                return
            allow = getattr(a, "AllowSetForegroundWindow", None)
            if allow is not None:
                allow(0xFFFFFFFF)  # ASFW_ANY
            if a.SetForegroundWindow(hwnd) and self._is_foreground(hwnd):
                return
            # 1) share the input state with the foreground and target threads
            cur = int(a.GetCurrentThreadId())
            fg = a.GetForegroundWindow()
            tids = []
            for w in (fg, hwnd):
                if w:
                    tid = int(a.GetWindowThreadProcessId(w, None))
                    if tid and tid != cur and tid not in tids:
                        tids.append(tid)
            attached = [t for t in tids if a.AttachThreadInput(cur, t, True)]
            try:
                a.BringWindowToTop(hwnd)
                a.SetForegroundWindow(hwnd)
            finally:
                for t in attached:
                    a.AttachThreadInput(cur, t, False)
            if self._is_foreground(hwnd):
                return
            # 2) ALT tap: our injected input becomes the last input event.
            if self._modifier_held("alt"):
                a.SetForegroundWindow(hwnd)
            else:
                alt_down, alt_up = self._vk_events(VK_MENU, False)
                self._send([alt_down])
                try:
                    a.SetForegroundWindow(hwnd)
                finally:
                    self._send([alt_up])
            if self._is_foreground(hwnd):
                return
        raise BackendError(f"Windows refused to bring window {hwnd:#x} to the foreground (foreground lock)")

    def move_window(self, handle: Any, rect: Rect) -> None:
        """Place the window's *visible* frame at ``rect`` (DWM invisible borders are compensated)."""
        self._check()
        a = self._api
        hwnd = self._hwnd(handle)
        r = Rect.coerce(rect)
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Window size must be positive, got {r}")
        self._restore_normal(hwnd)
        outer = self._outer_rect(hwnd)
        frame = self._frame_rect(hwnd)
        dl = max(0, frame.left - outer.left)
        dt = max(0, frame.top - outer.top)
        dr = max(0, outer.right - frame.right)
        db = max(0, outer.bottom - frame.bottom)
        if not a.MoveWindow(hwnd, r.left - dl, r.top - dt, r.width + dl + dr, r.height + dt + db, True):
            raise self._error("MoveWindow")

    _SHOW_STATES = {
        "minimized": SW_MINIMIZE,
        "minimize": SW_MINIMIZE,
        "maximized": SW_MAXIMIZE,
        "maximize": SW_MAXIMIZE,
        "normal": SW_RESTORE,
        "restored": SW_RESTORE,
        "restore": SW_RESTORE,
        "hidden": SW_HIDE,
        "hide": SW_HIDE,
        "shown": SW_SHOW,
        "show": SW_SHOW,
    }

    def set_window_state(self, handle: Any, state: str) -> None:
        """``state``: ``minimized``, ``maximized``, ``normal`` (also ``hidden``/``shown``)."""
        self._check()
        key = str(state).strip().lower()
        if key not in self._SHOW_STATES:
            raise ValueError(f"Unknown window state {state!r}; use minimized, maximized or normal")
        hwnd = self._hwnd(handle)
        cmd = self._SHOW_STATES[key]
        if cmd == SW_RESTORE:
            self._restore_normal(hwnd, always=True)  # SW_RESTORE also shows a hidden window
        else:
            self._api.ShowWindow(hwnd, cmd)  # returns previous visibility, not success

    def _restore_normal(self, hwnd: int, always: bool = False) -> None:
        """Bring ``hwnd`` to the normal (neither minimized nor maximized) state.

        A window minimized while maximized (WPF_RESTORETOMAXIMIZED) comes back
        *maximized* from the first SW_RESTORE; a second one un-maximizes it.
        """
        a = self._api
        for i in range(2):
            if not (a.IsIconic(hwnd) or a.IsZoomed(hwnd)) and not (always and i == 0):
                return
            a.ShowWindow(hwnd, SW_RESTORE)

    def close_window(self, handle: Any) -> None:
        """Ask the window to close (``WM_CLOSE``; the application may prompt to save)."""
        self._check()
        hwnd = self._hwnd(handle)
        if not self._api.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            raise self._error("PostMessageW(WM_CLOSE)")
