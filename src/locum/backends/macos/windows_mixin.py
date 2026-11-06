"""``MacOSBackend`` windows (CGWindowList + AX: list, activate, move, state, close) and launch.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ...errors import BackendError, WindowNotFound
from ...types import Rect, WindowInfo
from .constants import (
    _NS_ACTIVATE_ALL_WINDOWS,
    _NS_ACTIVATE_IGNORING_OTHER_APPS,
    kCGNullWindowID,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionAll,
    kCGWindowListOptionIncludingWindow,
    kCGWindowListOptionOnScreenOnly,
)
from .windowing import _plausible_record, _record_bounds, pair_ax_windows


#: Folders searched for ``<name>.app`` when deciding whether a string names an app.
APP_FOLDERS = (
    "/Applications", "/Applications/Utilities", "/System/Applications",
    "/System/Applications/Utilities", "/System/Library/CoreServices", "~/Applications",
)


def _find_app_bundle(name: str) -> Optional[str]:
    """Path of the installed ``<name>.app`` bundle, or None."""
    for folder in APP_FOLDERS:
        path = os.path.join(os.path.expanduser(folder), name + ".app")
        if os.path.isdir(path):
            return path
    return None


def _split_app_prefix(parts: List[str]) -> Tuple[Optional[str], List[str]]:
    """``(app name, remaining words)`` for the longest leading words naming an installed app."""
    for k in range(len(parts), 0, -1):
        name = " ".join(parts[:k])
        if _find_app_bundle(name) is not None:
            return name, list(parts[k:])
    return None, list(parts)


def _open_app_argv(app: str, rest: List[str]) -> List[str]:
    """``open`` command line for ``app``: documents as-is, options after ``--args``."""
    if not rest:
        return ["open", "-a", app]
    if rest[0] == "--args":
        return ["open", "-a", app, "--args", *rest[1:]]
    if any(r.startswith("-") for r in rest):
        return ["open", "-a", app, "--args", *rest]
    return ["open", "-a", app, *rest]


class _WindowsMixin:
    # --------------------------------------------------------------------- windows
    def _window_records(self, options: int, relative: int = kCGNullWindowID) -> List[Dict[str, Any]]:
        recs = self._q.CGWindowListCopyWindowInfo(options, relative)
        return [dict(r) for r in (recs or [])]

    def _pump_appkit(self) -> bool:
        """Let AppKit process pending notifications so ``NSWorkspace`` and
        ``NSRunningApplication`` values are current.

        AppKit only refreshes them while the main run loop runs, which a plain
        Python script never does, so without this they stay a snapshot of the
        moment they were first read. Only possible on the main thread; returns
        whether the run loop was serviced."""
        appkit = self._appkit
        if appkit is None or threading.current_thread() is not threading.main_thread():
            return False
        try:
            run, mode = appkit.CFRunLoopRunInMode, appkit.kCFRunLoopDefaultMode
        except AttributeError:
            return False
        try:
            for _ in range(64):  # kCFRunLoopRunHandledSource == 4: more may be pending
                if run(mode, 0.0, True) != 4:
                    break
            return True
        except Exception:
            return False

    def _front_window_pid(self) -> Optional[int]:
        """Owner of the front-most layer-0 window (live window-server z-order)."""
        try:
            records = self._window_records(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements)
        except Exception:
            return None
        for rec in records:
            if _plausible_record(rec):
                return int(rec.get("kCGWindowOwnerPID", -1))
        return None

    def _frontmost_pid(self) -> Optional[int]:
        """Pid of the application that receives keyboard input.

        Read live: the system-wide AX ``AXFocusedApplication`` first. AppKit's
        ``frontmostApplication`` is only trusted after the run loop could be
        serviced (see :meth:`_pump_appkit`); otherwise the owner of the front-most
        window is used, and the possibly stale AppKit value only as a last resort."""
        ax = self._ax
        if ax is not None:
            try:
                el = ax.attr(ax.system_wide, "AXFocusedApplication")
                pid = ax.pid(el) if el is not None else None
                if pid is not None and pid > 0:
                    return int(pid)
            except Exception:
                pass
        if self._pump_appkit():
            pid = self._workspace_front_pid()
            if pid is not None:
                return pid
        pid = self._front_window_pid()
        if pid is not None:
            return pid
        return self._workspace_front_pid()

    def _workspace_front_pid(self) -> Optional[int]:
        if self._appkit is None:
            return None
        try:
            app = self._appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app is not None else None
        except Exception:
            return None

    def _app_name(self, pid: Optional[int]) -> str:
        if pid is None:
            return ""
        if pid in self._app_names:
            return self._app_names[pid]
        name = ""
        if self._appkit is not None:
            try:
                app = self._appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
                if app is not None:
                    name = str(app.localizedName() or "")
            except Exception:
                name = ""
        self._app_names[pid] = name
        return name

    def _activate_pid(self, pid: int) -> bool:
        if self._appkit is None:
            return False
        app = self._appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        if app is None:
            return False
        try:
            return bool(app.activateWithOptions_(_NS_ACTIVATE_ALL_WINDOWS | _NS_ACTIVATE_IGNORING_OTHER_APPS))
        except Exception:
            return False

    def _ax_windows(self, pid: int) -> List[Tuple[Any, Optional[Tuple[float, float, float, float]], str, bool,
                                                  Optional[int]]]:
        """``(element, bounds_points, title, minimized, window_id)`` for every AX window
        of ``pid`` (``window_id`` is the exact ``CGWindowID`` when it can be read)."""
        ax = self._ax
        if ax is None:
            return []
        get_id = getattr(ax, "window_id", None)
        out = []
        for el in ax.attr(ax.application(pid), "AXWindows") or []:
            a = ax.attrs(el, ("AXPosition", "AXSize", "AXTitle", "AXMinimized"))
            pos, size = a.get("AXPosition"), a.get("AXSize")
            bounds = (pos[0], pos[1], size[0], size[1]) if pos and size else None
            wid = get_id(el) if get_id is not None else None
            out.append((el, bounds, str(a.get("AXTitle") or ""), bool(a.get("AXMinimized")), wid))
        return out

    def _ax_pairing(self, pid: int, records: Optional[List[Dict[str, Any]]] = None) -> Tuple[list, Dict[int, int]]:
        """``(ax_windows, {ax_index: CGWindowID})`` for ``pid`` (see :func:`pair_ax_windows`)."""
        wins = self._ax_windows(pid)
        if records is None:
            records = self._window_records(kCGWindowListOptionAll)
        own = [r for r in records if int(r.get("kCGWindowOwnerPID", -1)) == pid]
        return wins, pair_ax_windows(own, [(b, t, wid) for _, b, t, _, wid in wins])

    def _handle_for_ax_window(self, win: Any, pid: Optional[int],
                              records: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
        ax = self._ax
        if ax is None:
            return None
        if pid is None:
            pid = ax.pid(win)
        if pid is None:
            return None
        if records is None:
            records = self._window_records(kCGWindowListOptionAll)
        wins, pairs = self._ax_pairing(pid, records)
        for i, w in enumerate(wins):
            if ax.same(w[0], win):
                return pairs.get(i)
        # not in AXWindows (e.g. a sheet): match it against the still unpaired windows
        a = ax.attrs(win, ("AXPosition", "AXSize", "AXTitle"))
        pos, size = a.get("AXPosition"), a.get("AXSize")
        bounds = (pos[0], pos[1], size[0], size[1]) if pos and size else None
        get_id = getattr(ax, "window_id", None)
        taken = set(pairs.values())
        rest = [r for r in records if int(r.get("kCGWindowOwnerPID", -1)) == pid
                and int(r.get("kCGWindowNumber", -1)) not in taken]
        return pair_ax_windows(rest, [(bounds, str(a.get("AXTitle") or ""),
                                       get_id(win) if get_id is not None else None)]).get(0)

    def _window_record(self, handle: Any) -> Dict[str, Any]:
        try:
            num = int(handle)
        except (TypeError, ValueError):
            raise WindowNotFound(f"Invalid macOS window handle: {handle!r}") from None
        # kCGWindowListOptionIncludingWindow leaves out minimized windows, so a window
        # could not be restored, moved or closed once minimized; describing the id does.
        describe = getattr(self._q, "CGWindowListCreateDescriptionFromArray", None)
        if describe is not None:
            try:
                for rec in describe([num]) or []:
                    if int(rec.get("kCGWindowNumber", -1)) == num:
                        return dict(rec)
            except Exception:
                pass
        for rec in self._window_records(kCGWindowListOptionIncludingWindow, num):
            if int(rec.get("kCGWindowNumber", -1)) == num:
                return rec
        raise WindowNotFound(f"No window with id {num}")

    def _ax_window(self, handle: Any) -> Any:
        """The AXUIElement of the CG window ``handle``.

        Raises :class:`WindowNotFound` when no AX window pairs with it one-to-one
        (e.g. two untitled windows with the same frame): acting on a guess could
        close or move the wrong document."""
        self._require_ax()
        rec = self._window_record(handle)
        num = int(rec.get("kCGWindowNumber", -1))
        pid = int(rec.get("kCGWindowOwnerPID", -1))
        records = [r for r in self._window_records(kCGWindowListOptionAll)
                   if int(r.get("kCGWindowNumber", -1)) != num]
        wins, pairs = self._ax_pairing(pid, [rec, *records])
        for i, n in pairs.items():
            if n == num:
                return wins[i][0]
        raise WindowNotFound(
            f"Cannot tell which accessibility window is window {handle!r} (pid {pid}); "
            "it has no unambiguous AX counterpart")

    def _ax_frame(self, win: Any) -> Optional[Tuple[float, float, float, float]]:
        a = self._ax.attrs(win, ("AXPosition", "AXSize")) if self._ax is not None else {}
        pos, size = a.get("AXPosition"), a.get("AXSize")
        return (pos[0], pos[1], size[0], size[1]) if pos and size else None

    def _work_area_for(self, bounds: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
        x, y, w, h = bounds
        d = self._get_layout().display_at_points(x + w / 2, y + h / 2)
        return d.work_area or (d.x, d.y, d.width, d.height)

    def _is_maximized(self, bounds: Tuple[float, float, float, float]) -> bool:
        work = self._work_area_for(bounds)
        return all(abs(a - b) <= 2.0 for a, b in zip(bounds, work))

    def list_windows(self) -> List[WindowInfo]:
        """Layer-0 windows: on-screen ones front-most first, then minimized ones
        (``is_minimized=True``, ``is_visible=False``; needs Accessibility permission).
        Handles are ``CGWindowID`` ints.

        Titles need Screen Recording permission; when missing they are filled in
        from the accessibility API if Accessibility permission was granted."""
        records = self._window_records(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements)
        layout = self._get_layout()
        front_pid = self._frontmost_pid()
        ax_ok = self._ax is not None and self._ax.is_trusted()
        all_records: List[Dict[str, Any]] = []
        if ax_ok:
            try:
                all_records = self._window_records(kCGWindowListOptionAll)
            except Exception:
                all_records = []
        ax_cache: Dict[int, Tuple[list, Dict[int, int]]] = {}

        def ax_info(pid: int, num: int) -> Optional[tuple]:
            if pid not in ax_cache:
                try:
                    ax_cache[pid] = self._ax_pairing(pid, all_records or records)
                except Exception:
                    ax_cache[pid] = ([], {})
            wins, pairs = ax_cache[pid]
            for i, n in pairs.items():
                if n == num:
                    return wins[i]
            return None

        out: List[WindowInfo] = []
        seen: Set[int] = set()
        active_seen = False
        for rec in records:
            if not _plausible_record(rec):
                continue
            bounds = _record_bounds(rec)
            num = int(rec.get("kCGWindowNumber", 0))
            pid = int(rec.get("kCGWindowOwnerPID", -1))
            owner = str(rec.get("kCGWindowOwnerName") or "")
            title = str(rec.get("kCGWindowName") or "")
            if not title and ax_ok:
                info = ax_info(pid, num)
                if info is not None:
                    title = info[2]
            is_active = not active_seen and front_pid is not None and pid == front_pid
            active_seen = active_seen or is_active
            seen.add(num)
            out.append(WindowInfo(
                handle=num,
                title=title,
                rect=layout.points_rect_to_pixels(*bounds),
                app=owner or self._app_name(pid),
                pid=pid,
                is_active=is_active,
                is_minimized=False,
                is_maximized=self._is_maximized(bounds),
                is_visible=True,
                class_name="",
            ))
        # minimized windows are not on screen; the AX tree says which off-screen
        # windows are minimized (rather than on another Space or merely hidden)
        regular: Optional[Set[int]] = None
        for rec in all_records:
            num = int(rec.get("kCGWindowNumber", 0))
            if num in seen or not _plausible_record(rec):
                continue
            pid = int(rec.get("kCGWindowOwnerPID", -1))
            if regular is None:  # only ask apps with a Dock icon (agents never minimize)
                regular = {p for p, _ in self._regular_apps()} if self._appkit is not None else set()
            if self._appkit is not None and pid not in regular:
                continue
            info = ax_info(pid, num)
            if info is None or not info[3]:
                continue
            seen.add(num)
            owner = str(rec.get("kCGWindowOwnerName") or "")
            out.append(WindowInfo(
                handle=num,
                title=str(rec.get("kCGWindowName") or "") or info[2],
                rect=layout.points_rect_to_pixels(*_record_bounds(rec)),
                app=owner or self._app_name(pid),
                pid=pid,
                is_active=False,
                is_minimized=True,
                is_maximized=False,
                is_visible=False,
                class_name="",
            ))
        return out

    #: Seconds :meth:`activate_window` waits for the app to become front-most.
    ACTIVATE_TIMEOUT = 1.0

    def _wait_frontmost(self, pid: int, timeout: float) -> Optional[bool]:
        """Whether ``pid`` became front-most within ``timeout`` (``None``: unknown)."""
        deadline = time.monotonic() + timeout
        while True:
            front = self._frontmost_pid()
            if front is None or front == pid:
                return None if front is None else True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def activate_window(self, handle: Any) -> None:
        """Bring the window to the front and give its app keyboard focus.

        Raises :class:`BackendError` when the app could not be made front-most
        (``AXRaise`` alone only reorders windows inside an app; typing would still
        go to the previous app)."""
        rec = self._window_record(handle)
        pid = int(rec.get("kCGWindowOwnerPID", -1))
        activated = self._activate_pid(pid)
        ax = self._ax
        if ax is not None and ax.is_trusted():
            try:
                win = self._ax_window(handle)
            except WindowNotFound:
                win = None
            if win is not None:
                if ax.attr(win, "AXMinimized"):
                    ax.set_attr(win, "AXMinimized", False)
                ax.perform(win, "AXRaise")
                ax.set_attr(win, "AXMain", True)
            # works where activateWithOptions_ is declined (cooperative activation)
            try:
                activated = ax.set_attr(ax.application(pid), "AXFrontmost", True) or activated
            except Exception:
                pass
        if not activated:
            raise BackendError(f"Could not activate window {handle!r} (pid {pid})")
        if self._wait_frontmost(pid, self.ACTIVATE_TIMEOUT) is False:
            raise BackendError(
                f"Window {handle!r} (pid {pid}) did not become the front-most app; "
                f"{self._app_name(self._frontmost_pid()) or 'another app'} still has keyboard focus")

    def move_window(self, handle: Any, rect: Rect) -> None:
        """Move/resize to the pixel ``rect`` (converted to points of the target display)."""
        ax = self._require_ax()
        win = self._ax_window(handle)
        x, y, w, h = self._get_layout().pixels_rect_to_points(Rect.coerce(rect))
        ok = ax.set_point(win, "AXPosition", x, y)
        ok = ax.set_size(win, "AXSize", w, h) and ok
        ax.set_point(win, "AXPosition", x, y)  # size changes can be constrained by the screen edge
        if not ok:
            raise BackendError(f"The application refused to move/resize window {handle!r}")
        # an explicit placement leaves the maximized state: 'normal' must not undo it
        self._restore_rects.pop(handle, None)

    def set_window_state(self, handle: Any, state: str) -> None:
        ax = self._require_ax()
        state = str(state).lower()
        win = self._ax_window(handle)
        if state in ("minimized", "minimize", "min"):
            if not ax.set_attr(win, "AXMinimized", True):
                raise BackendError(f"Could not minimize window {handle!r}")
            return
        if state in ("normal", "restore", "restored"):
            if ax.attr(win, "AXMinimized"):
                ax.set_attr(win, "AXMinimized", False)
            if ax.attr(win, "AXFullScreen"):
                ax.set_attr(win, "AXFullScreen", False)
            saved = self._restore_rects.pop(handle, None)
            if saved is not None:
                ax.set_point(win, "AXPosition", saved[0], saved[1])
                ax.set_size(win, "AXSize", saved[2], saved[3])
                return
            if self._is_maximized(self._ax_frame(win) or (0.0, 0.0, 0.0, 0.0)):
                # zoomed by the user (green button / double-click on the title bar):
                # zoom back to the user size, never toggle full screen instead
                button = ax.attr(win, "AXZoomButton")
                if button is not None:
                    if "AXZoomWindow" in ax.action_names(button):
                        ax.perform(button, "AXZoomWindow")
                    elif ax.attr(button, "AXSubrole") != "AXFullScreenButton":
                        ax.perform(button, "AXPress")
                deadline = time.monotonic() + 0.5
                while self._is_maximized(self._ax_frame(win) or (0.0, 0.0, 0.0, 0.0)):
                    if time.monotonic() >= deadline:
                        raise BackendError(f"Could not restore window {handle!r} from its maximized size")
                    time.sleep(0.02)
            return
        if state in ("maximized", "maximize", "max"):
            if ax.attr(win, "AXMinimized"):
                ax.set_attr(win, "AXMinimized", False)
            a = ax.attrs(win, ("AXPosition", "AXSize"))
            pos, size = a.get("AXPosition"), a.get("AXSize")
            if pos and size:
                current = (pos[0], pos[1], size[0], size[1])
                if not self._is_maximized(current):
                    self._restore_rects[handle] = current
                work = self._work_area_for(current)
            else:
                d = self._get_layout().primary
                work = d.work_area or (d.x, d.y, d.width, d.height)
            ok = ax.set_point(win, "AXPosition", work[0], work[1])
            ok = ax.set_size(win, "AXSize", work[2], work[3]) and ok
            if not ok:
                raise BackendError(f"Could not maximize window {handle!r}")
            return
        raise ValueError(f"Unknown window state {state!r}; expected minimized, maximized or normal")

    def close_window(self, handle: Any) -> None:
        ax = self._require_ax()
        win = self._ax_window(handle)
        button = ax.attr(win, "AXCloseButton")
        if button is None or not ax.perform(button, "AXPress"):
            raise BackendError(f"Window {handle!r} has no usable close button")

    def launch(self, command: Any, **kwargs: Any) -> Any:
        """Start an application. App names (``"Safari"``, ``"Microsoft Excel"``),
        ``"/Applications/Safari.app"`` and bundle paths are opened with ``open -a``;
        anything else runs via :class:`subprocess.Popen`. A string is treated as an
        app name when its first word is not a command on ``PATH`` and it contains
        no ``/`` or quotes."""
        if isinstance(command, str):
            stripped = command.strip()
            if stripped.endswith(".app") or stripped.endswith(".app/"):
                return subprocess.Popen(["open", "-a", stripped], **kwargs)
            try:
                parts = shlex.split(stripped)
            except ValueError:
                parts = []
            unquoted = bool(parts) and not any(c in stripped for c in "\"'\\") and "/" not in parts[0]
            if unquoted:
                # "Preview /tmp/report.pdf", "TextEdit --args -X": the longest leading run of
                # words that names an installed app is the app; the rest are its documents
                # (or, after --args / when they look like options, its arguments). The
                # default APFS volume is case-insensitive, so which("Mail") finds
                # /usr/bin/mail (likewise Calendar, Script Editor, Shortcuts): a capitalised
                # name that is an installed app opens the app; "mail" stays the command.
                app, rest = _split_app_prefix(parts)
                if app is not None and (shutil.which(parts[0]) is None or parts[0][:1].isupper()):
                    return subprocess.Popen(_open_app_argv(app, rest), **kwargs)
                if "/" not in stripped and shutil.which(parts[0]) is None:
                    # Not in the usual folders and not a command: let Launch Services find it.
                    return subprocess.Popen(["open", "-a", " ".join(parts)], **kwargs)
            if parts and parts[0].endswith(".app") and os.path.isdir(parts[0]):
                return subprocess.Popen(["open", "-a", parts[0], "--args", *parts[1:]], **kwargs)
            if parts:
                # Pass the POSIX-split argv on: the base class splits strings by the
                # *host* platform's rules, which differ when this runs off macOS (tests).
                return super().launch(parts, **kwargs)
        return super().launch(command, **kwargs)
