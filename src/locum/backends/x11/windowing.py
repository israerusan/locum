"""Window part of the X11 backend: EWMH / ICCCM window enumeration, activation,
geometry, state and closing, as a mixin of :class:`X11Backend`.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Sequence, Tuple

from ...errors import BackendError, WindowNotFound
from ...types import Rect, WindowInfo
from ._xcore import _x_errors
from .atspi import _proc_comm


class _WindowsMixin:
    """EWMH / ICCCM window enumeration and control of :class:`X11Backend`."""

    # ================================================================== windows
    def _atom(self, name: str) -> int:
        return self._d.get_atom(name)

    def _win_obj(self, xid: int) -> Any:
        return self._d.create_resource_object("window", int(xid))

    def _prop(self, win: Any, name: str, ptype: Any = None) -> Any:
        X = self._xl.X
        try:
            p = win.get_full_property(self._atom(name), X.AnyPropertyType if ptype is None else ptype)
        except self._xl.error.XError:
            return None
        return None if p is None else p.value

    def _prop_list(self, win: Any, name: str) -> List[int]:
        v = self._prop(win, name)
        if v is None:
            return []
        try:
            return [int(x) for x in v]
        except TypeError:
            return []

    def _supported(self) -> frozenset:
        """EWMH atoms supported by the running window manager (empty when none is running)."""
        check = self._prop_list(self._root, "_NET_SUPPORTING_WM_CHECK")
        if not check:
            return frozenset()
        child = self._win_obj(check[0])
        if self._prop_list(child, "_NET_SUPPORTING_WM_CHECK")[:1] != check[:1]:
            return frozenset()  # stale property left by a dead WM
        return frozenset(self._prop_list(self._root, "_NET_SUPPORTED"))

    def _has_wm_feature(self, name: str, supported: Optional[frozenset] = None) -> bool:
        sup = self._supported() if supported is None else supported
        return self._atom(name) in sup

    @staticmethod
    def _decode(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            try:
                return bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                return bytes(value).decode("latin-1", "replace")
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:
            return str(value)

    def _title(self, win: Any) -> str:
        title = self._decode(self._prop(win, "_NET_WM_NAME"))
        if not title:
            title = self._decode(self._prop(win, "WM_NAME"))
        return title.rstrip("\x00")

    def _wm_class(self, win: Any) -> Tuple[str, str]:
        raw = self._prop(win, "WM_CLASS")
        if raw is None:
            return "", ""
        parts = self._decode(raw).split("\x00")
        inst = parts[0] if parts else ""
        cls = parts[1] if len(parts) > 1 else ""
        return inst, cls

    @staticmethod
    def _proc_name(pid: Optional[int]) -> str:
        return _proc_comm(pid)

    def _client_window(self, win: Any, depth: int = 2) -> Optional[Any]:
        """ICCCM: find the window carrying WM_STATE in ``win``'s subtree (XmuClientWindow)."""
        if self._prop(win, "WM_STATE") is not None:
            return win
        if depth <= 0:
            return None
        try:
            kids = win.query_tree().children
        except self._xl.error.XError:
            return None
        for k in reversed(kids):
            found = self._client_window(k, depth - 1)
            if found is not None:
                return found
        return None

    def _top_level_xids(self, supported: frozenset) -> List[int]:
        """Client windows, front-most first."""
        if supported:
            stacking = self._prop_list(self._root, "_NET_CLIENT_LIST_STACKING")
            if stacking:
                return list(reversed(stacking))
            clients = self._prop_list(self._root, "_NET_CLIENT_LIST")
            if clients:
                return list(reversed(clients))
        X = self._xl.X
        out: List[int] = []
        try:
            children = self._root.query_tree().children  # bottom-to-top stacking order
        except self._xl.error.XError:
            return out
        for child in reversed(children):
            try:
                attrs = child.get_attributes()
            except self._xl.error.XError:
                continue
            if attrs.override_redirect:
                continue
            if child.id in self._iconified and attrs.map_state != X.IsViewable:
                out.append(child.id)
                continue
            client = self._client_window(child)
            if client is None:
                # No window manager at all: a named, viewable root child is a top-level.
                if attrs.map_state != X.IsViewable:
                    continue
                if not self._title(child) and not any(self._wm_class(child)):
                    continue
                client = child
            if client.id not in out:
                out.append(client.id)
        return out

    def _abs_geometry(self, win: Any) -> Rect:
        """Root-relative rect of ``win`` *including* its X border: the box whose
        top-left corner ``ConfigureWindow(x, y)`` positions (see :meth:`_configure_outer`)."""
        g = win.get_geometry()
        t = self._root.translate_coords(win, 0, 0)
        bw = int(g.border_width)
        return Rect(int(t.x) - bw, int(t.y) - bw, int(g.width) + 2 * bw, int(g.height) + 2 * bw)

    @staticmethod
    def _check_x_rect(r: Rect) -> None:
        """X geometry is INT16 positions and CARD16 sizes; refuse anything else up front."""
        if not (-0x8000 <= r.left <= 0x7FFF and -0x8000 <= r.top <= 0x7FFF and 0 < r.width <= 0xFFFF and 0 < r.height <= 0xFFFF):
            raise ValueError(f"Window rect {r} is outside the X11 coordinate range (x/y -32768..32767, size 1..65535)")

    def _configure_outer(self, win: Any, r: Rect, **kwargs: Any) -> None:
        """Place ``win`` (no window manager) so that its rect including the X border is ``r``."""
        bw = int(win.get_geometry().border_width)
        win.configure(x=r.left, y=r.top, width=max(1, r.width - 2 * bw), height=max(1, r.height - 2 * bw), **kwargs)

    def _frame_extents(self, win: Any) -> Tuple[int, int, int, int]:
        ext = self._prop_list(win, "_NET_FRAME_EXTENTS")
        if len(ext) == 4:
            return ext[0], ext[1], ext[2], ext[3]  # left, right, top, bottom
        return 0, 0, 0, 0

    def _window_info(self, xid: int, active: int, supported: frozenset, current_desktop: Optional[int]) -> Optional[WindowInfo]:
        X = self._xl.X
        win = self._win_obj(xid)
        try:
            attrs = win.get_attributes()
            client = self._abs_geometry(win)
        except self._xl.error.XError:
            return None  # window vanished meanwhile
        types = set(self._prop_list(win, "_NET_WM_WINDOW_TYPE"))
        if types & {self._atom("_NET_WM_WINDOW_TYPE_DESKTOP"), self._atom("_NET_WM_WINDOW_TYPE_DOCK")}:
            return None
        l, r, t, b = self._frame_extents(win)
        rect = Rect(client.left - l, client.top - t, client.width + l + r, client.height + t + b)
        states = set(self._prop_list(win, "_NET_WM_STATE"))
        wm_state = self._prop_list(win, "WM_STATE")
        minimized = self._atom("_NET_WM_STATE_HIDDEN") in states or (bool(wm_state) and wm_state[0] == 3)
        maximized = {self._atom("_NET_WM_STATE_MAXIMIZED_VERT"), self._atom("_NET_WM_STATE_MAXIMIZED_HORZ")} <= states
        if not supported:
            if xid in self._restore_geometry:
                maximized = True
            if xid in self._iconified:
                if attrs.map_state == X.IsViewable:
                    self._iconified.discard(xid)  # someone else mapped it again
                else:
                    minimized = True
        visible = attrs.map_state == X.IsViewable and not minimized
        if current_desktop is not None and visible:
            desk = self._prop_list(win, "_NET_WM_DESKTOP")
            if desk and desk[0] != 0xFFFFFFFF and desk[0] != current_desktop:
                visible = False
        pid_l = self._prop_list(win, "_NET_WM_PID")
        pid = pid_l[0] if pid_l else None
        inst, cls = self._wm_class(win)
        app = self._proc_name(pid) or inst or cls
        return WindowInfo(
            handle=int(xid),
            title=self._title(win),
            rect=rect,
            app=app,
            pid=pid,
            is_active=int(xid) == active,
            is_minimized=minimized,
            is_maximized=maximized,
            is_visible=visible,
            class_name=cls,
        )

    def _active_xid(self, supported: frozenset) -> int:
        if supported:
            act = self._prop_list(self._root, "_NET_ACTIVE_WINDOW")
            if act and act[0]:
                return act[0]
        try:
            focus = self._d.get_input_focus().focus
        except self._xl.error.XError:
            return 0
        if isinstance(focus, int) or focus is None:
            return 0
        # Climb to the root's child, then down to the client window.
        try:
            win = focus
            for _ in range(64):
                tree = win.query_tree()
                parent_id = getattr(tree.parent, "id", tree.parent)
                if not parent_id or parent_id == self._root.id:
                    break
                win = tree.parent
            if win.id == self._root.id:
                return 0
            client = self._client_window(win)
            return (client or win).id
        except self._xl.error.XError:
            return 0

    @_x_errors
    def list_windows(self) -> List[WindowInfo]:
        """Top-level client windows, front-most first (EWMH, or a bare-X fallback)."""
        self._check()
        with self._lock:
            supported = self._supported()
            active = self._active_xid(supported)
            cur = self._prop_list(self._root, "_NET_CURRENT_DESKTOP")
            current_desktop = cur[0] if cur else None
            out: List[WindowInfo] = []
            for xid in self._top_level_xids(supported):
                info = self._window_info(xid, active, supported, current_desktop)
                if info is not None:
                    out.append(info)
            return out

    @_x_errors
    def active_window(self) -> Optional[WindowInfo]:
        self._check()
        with self._lock:
            supported = self._supported()
            active = self._active_xid(supported)
            if not active:
                return None
            cur = self._prop_list(self._root, "_NET_CURRENT_DESKTOP")
            return self._window_info(active, active, supported, cur[0] if cur else None)

    def _handle(self, handle: Any) -> Any:
        """Validate ``handle`` (XID or WindowInfo) and return the Xlib window object."""
        if isinstance(handle, WindowInfo):
            handle = handle.handle
        try:
            xid = int(handle, 0) if isinstance(handle, str) else int(handle)
        except (TypeError, ValueError):
            raise WindowNotFound(f"Invalid X11 window handle {handle!r}") from None
        win = self._win_obj(xid)
        try:
            win.get_attributes()  # unlike GetGeometry, this fails for pixmaps and other non-windows
        except self._xl.error.XError:
            raise WindowNotFound(f"X11 window {xid:#x} does not exist") from None
        return win

    def _client_message(self, win: Any, msg_type: str, data: Sequence[int], to_root: bool = True) -> None:
        X = self._xl.X
        payload = (list(int(v) & 0xFFFFFFFF for v in data) + [0] * 5)[:5]
        ev = self._xl.event.ClientMessage(window=win, client_type=self._atom(msg_type), data=(32, payload))
        if to_root:
            self._root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        else:
            win.send_event(ev, event_mask=0)

    def _checked_sync(self, xid: int, what: str, since: Optional[int] = None) -> None:
        """XSync and raise for X errors reported since ``since`` (an error-sink sequence number)."""
        if since is None:
            since = self._async_errors.seq
        self._sync()
        errs = self._async_errors.since(since)
        if errs:
            err = errs[-1]
            if isinstance(err, (self._xl.error.BadWindow, self._xl.error.BadDrawable)):
                raise WindowNotFound(f"X11 window {xid:#x} disappeared during {what}")
            raise BackendError(f"X error during {what} of window {xid:#x}: {err}")

    @_x_errors
    def activate_window(self, handle: Any) -> None:
        """Raise and focus a window (``_NET_ACTIVE_WINDOW``; map + raise + focus without a WM)."""
        self._check()
        X = self._xl.X
        with self._lock:
            since = self._async_errors.seq
            win = self._handle(handle)
            supported = self._supported()
            if self._has_wm_feature("_NET_ACTIVE_WINDOW", supported):
                desk = self._prop_list(win, "_NET_WM_DESKTOP")
                cur = self._prop_list(self._root, "_NET_CURRENT_DESKTOP")
                if desk and cur and desk[0] not in (cur[0], 0xFFFFFFFF) and self._has_wm_feature("_NET_CURRENT_DESKTOP", supported):
                    self._client_message(self._root, "_NET_CURRENT_DESKTOP", [desk[0], X.CurrentTime])
                self._client_message(win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
            else:
                win.map()
                self._iconified.discard(win.id)
                win.configure(stack_mode=X.Above)
                self._sync()
                try:
                    if win.get_attributes().map_state == X.IsViewable:
                        win.set_input_focus(X.RevertToParent, X.CurrentTime)
                except self._xl.error.XError:
                    pass
            self._checked_sync(win.id, "activate", since)

    @_x_errors
    def move_window(self, handle: Any, rect: Rect) -> None:
        """Move/resize so the window's *outer* rect (including WM frame) becomes ``rect``."""
        self._check()
        r = Rect.coerce(rect)
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Window size must be positive, got {r}")
        self._check_x_rect(r)
        with self._lock:
            since = self._async_errors.seq
            win = self._handle(handle)
            supported = self._supported()
            l, rr, t, b = self._frame_extents(win)
            bw = int(win.get_geometry().border_width)
            cw, ch = max(1, r.width - l - rr - 2 * bw), max(1, r.height - t - b - 2 * bw)
            if supported:
                states = set(self._prop_list(win, "_NET_WM_STATE"))
                maxed = {self._atom("_NET_WM_STATE_MAXIMIZED_VERT"), self._atom("_NET_WM_STATE_MAXIMIZED_HORZ")} & states
                if maxed and self._has_wm_feature("_NET_WM_STATE", supported):
                    # WMs ignore geometry requests for maximized windows.
                    atoms = sorted(maxed) + [0]
                    self._client_message(win, "_NET_WM_STATE", [0, atoms[0], atoms[1], 2])
            if supported and self._has_wm_feature("_NET_MOVERESIZE_WINDOW", supported):
                flags = 1 | (0xF << 8) | (2 << 12)  # NorthWest gravity, x/y/w/h present, pager source
                self._client_message(win, "_NET_MOVERESIZE_WINDOW", [flags, r.left, r.top, cw, ch])
            else:
                x, y = r.left + (l if not supported else 0), r.top + (t if not supported else 0)
                self._check_x_rect(Rect(x, y, cw, ch))
                win.configure(x=x, y=y, width=cw, height=ch)
            self._restore_geometry.pop(win.id, None)
            self._checked_sync(win.id, "move", since)

    @_x_errors
    def set_window_state(self, handle: Any, state: str) -> None:
        """``state`` in {"minimized", "maximized", "normal"}."""
        self._check()
        st = str(state).strip().lower()
        if st not in ("minimized", "maximized", "normal"):
            raise ValueError(f"Unknown window state {state!r}; expected minimized, maximized or normal")
        X = self._xl.X
        with self._lock:
            since = self._async_errors.seq
            win = self._handle(handle)
            supported = self._supported()
            max_atoms = [self._atom("_NET_WM_STATE_MAXIMIZED_VERT"), self._atom("_NET_WM_STATE_MAXIMIZED_HORZ")]
            ewmh_state = self._has_wm_feature("_NET_WM_STATE", supported)
            if st == "minimized":
                if supported:
                    self._client_message(win, "WM_CHANGE_STATE", [3])  # ICCCM IconicState
                else:
                    win.unmap()
                    self._iconified.add(win.id)
            elif st == "maximized":
                if supported and ewmh_state:
                    if self._prop_list(win, "WM_STATE")[:1] == [3] or win.get_attributes().map_state != X.IsViewable:
                        self._client_message(win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
                    self._client_message(win, "_NET_WM_STATE", [1, max_atoms[0], max_atoms[1], 2])
                else:
                    win.map()
                    self._iconified.discard(win.id)
                    cur = self._abs_geometry(win)
                    mons = self.monitors()
                    target = max(mons, key=lambda m: (m.rect.intersect(cur).area if m.rect.intersect(cur) else 0, m.is_primary)).rect
                    if win.id not in self._restore_geometry:
                        self._restore_geometry[win.id] = cur
                    self._configure_outer(win, target, stack_mode=X.Above)
            else:  # normal
                if supported:
                    if ewmh_state:
                        self._client_message(win, "_NET_WM_STATE", [0, max_atoms[0], max_atoms[1], 2])
                    minimized = self._prop_list(win, "WM_STATE")[:1] == [3] or self._atom("_NET_WM_STATE_HIDDEN") in set(
                        self._prop_list(win, "_NET_WM_STATE")
                    )
                    if minimized:
                        win.map()
                        if self._has_wm_feature("_NET_ACTIVE_WINDOW", supported):
                            self._client_message(win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
                else:
                    win.map()
                    self._iconified.discard(win.id)
                    prev = self._restore_geometry.pop(win.id, None)
                    if prev is not None:
                        self._configure_outer(win, prev)
            self._checked_sync(win.id, f"set_window_state({st})", since)

    @_x_errors
    def close_window(self, handle: Any) -> None:
        """Politely close a window: ``_NET_CLOSE_WINDOW``, else ``WM_DELETE_WINDOW``, else destroy it."""
        self._check()
        X = self._xl.X
        with self._lock:
            since = self._async_errors.seq
            win = self._handle(handle)
            supported = self._supported()
            if self._has_wm_feature("_NET_CLOSE_WINDOW", supported):
                self._client_message(win, "_NET_CLOSE_WINDOW", [X.CurrentTime, 2])
            elif self._atom("WM_DELETE_WINDOW") in self._prop_list(win, "WM_PROTOCOLS"):
                self._client_message(win, "WM_PROTOCOLS", [self._atom("WM_DELETE_WINDOW"), X.CurrentTime], to_root=False)
            else:
                win.destroy()
            self._restore_geometry.pop(win.id, None)
            self._iconified.discard(win.id)
            self._checked_sync(win.id, "close", since)

    def launch(self, command: Any, **kwargs: Any) -> Any:
        """Start an application on *this* display (``DISPLAY`` is set in its environment)."""
        if "env" not in kwargs:
            env = dict(os.environ)
            env["DISPLAY"] = self.display_name
            kwargs["env"] = env
        return super().launch(command, **kwargs)
