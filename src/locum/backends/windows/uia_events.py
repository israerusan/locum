"""UI Automation change events for event-driven waits and observe reuse.

A background thread joins the multithreaded COM apartment (MTA), where UI
Automation delivers events on its own threads without a message pump, and
subscribes desktop-wide to the events a screen reader uses:

* focus changes;
* structure changes (elements added, removed, reordered) in the whole tree;
* property changes of Name, Value, IsEnabled, IsOffscreen, ToggleState,
  selection, expand/collapse and scroll position.

Every event bumps a generation counter and records the sending process id.
Waits sleep on the counter (they wake at once when something changes, and keep
polling slowly as a fallback for apps that raise no events), and ``observe``
asks which processes changed since its previous snapshot to reuse the elements
of windows that did not.

The recorded pid is the *sending element's* process, which is not always the
owner of the top-level window: a Store/UWP app's window (Calculator, Settings)
belongs to ApplicationFrameHost.exe while its content and events come from the
app. ``observe`` therefore maps each window to every process it hosts
(``WindowsBackend.window_content_pids``) before deciding a window is unchanged.

Set ``LOCUM_UIA_EVENTS=0`` to turn it off.
"""

from __future__ import annotations

import collections
import os
import threading
from typing import Any, Deque, Optional, Set, Tuple

#: UIA property ids watched (Name, Value, IsEnabled, IsOffscreen, ToggleState,
#: SelectionItem.IsSelected, ExpandCollapseState, Scroll H/V percent).
WATCHED_PROPERTIES = (30005, 30045, 30010, 30022, 30086, 30079, 30070, 30053, 30057)
NAME_PROPERTY = 30005
#: Events remembered for :meth:`ChangeTracker.dirty_since`; older history is "unknown".
LOG_SIZE = 8192


def events_enabled() -> bool:
    return os.environ.get("LOCUM_UIA_EVENTS", "1").strip().lower() not in ("0", "false", "no", "off")


class ChangeTracker:
    """Desktop-wide UIA event subscription (see the module docstring).

    Args:
        module: The comtypes ``UIAutomationClient`` module.
        clsids: CUIAutomation class ids to try, newest first.
    """

    START_TIMEOUT = 10.0

    def __init__(self, module: Any, clsids: Tuple[str, ...]) -> None:
        self._module = module
        self._clsids = clsids
        self._cond = threading.Condition()
        self.generation = 0
        self._log: Deque[Tuple[int, Optional[int]]] = collections.deque(maxlen=LOG_SIZE)
        self._forgotten = 0  # generations <= this are no longer in the log
        #: Like ``generation``, for the events that can change which element a selector
        #: matches: elements added/removed/reordered and Name changes (not focus, values,
        #: scrolling...). See :meth:`identity_dirty_since`.
        self.identity_generation = 0
        self._identity_log: Deque[Tuple[int, Optional[int]]] = collections.deque(maxlen=LOG_SIZE)
        self._identity_forgotten = 0
        self._focus_pid: Optional[int] = None
        self.alive = False
        self.error: Optional[BaseException] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="locum-uia-events", daemon=True)
        self._thread.start()
        self._ready.wait(self.START_TIMEOUT)

    # ------------------------------------------------------------- recording
    def _bump(self, *pids: Optional[int]) -> None:
        with self._cond:
            for pid in pids or (None,):
                self.generation += 1
                if len(self._log) == self._log.maxlen:
                    self._forgotten = self._log[0][0]
                self._log.append((self.generation, pid))
            self._cond.notify_all()

    def _bump_identity(self, pid: Optional[int]) -> None:
        with self._cond:
            self.identity_generation += 1
            if len(self._identity_log) == self._identity_log.maxlen:
                self._identity_forgotten = self._identity_log[0][0]
            self._identity_log.append((self.identity_generation, pid))

    # ------------------------------------------------------------- queries
    def wait(self, since: int, timeout: float) -> int:
        """Block until an event newer than generation ``since`` (or ``timeout``); the generation."""
        with self._cond:
            self._cond.wait_for(lambda: self.generation > since or not self.alive, max(0.0, float(timeout)))
            return self.generation

    def dirty_since(self, since: int) -> Optional[Set[int]]:
        """Process ids that raised events after generation ``since``; ``None`` if unknown
        (history overflowed, an event without a pid, or the subscription is down)."""
        with self._cond:
            if not self.alive or since < self._forgotten:
                return None
            out: Set[int] = set()
            for gen, pid in reversed(self._log):
                if gen <= since:
                    break
                if pid is None:
                    return None
                out.add(pid)
            return out

    def identity_dirty_since(self, since: int) -> Optional[Set[int]]:
        """:meth:`dirty_since` for the identity events (structure and Name changes) after
        ``identity_generation`` ``since``."""
        with self._cond:
            if not self.alive or since < self._identity_forgotten:
                return None
            out: Set[int] = set()
            for gen, pid in reversed(self._identity_log):
                if gen <= since:
                    break
                if pid is None:
                    return None
                out.add(pid)
            return out

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5.0)

    # ------------------------------------------------------------- thread
    def _run(self) -> None:
        try:
            import comtypes

            try:
                comtypes.CoInitializeEx(getattr(comtypes, "COINIT_MULTITHREADED", 0))
            except Exception:
                pass
            UIA = self._module
            uia = None
            for clsid in self._clsids:
                try:
                    uia = comtypes.CoCreateInstance(comtypes.GUID(clsid), interface=UIA.IUIAutomation,
                                                    clsctx=comtypes.CLSCTX_INPROC_SERVER)
                    break
                except Exception:
                    continue
            if uia is None:
                raise OSError("could not create IUIAutomation for events")
            handler = _make_handler(UIA, self)
            root = uia.GetRootElement()
            uia.AddFocusChangedEventHandler(None, handler)
            uia.AddStructureChangedEventHandler(root, UIA.TreeScope_Subtree, None, handler)
            uia.AddPropertyChangedEventHandler(root, UIA.TreeScope_Subtree, None, handler, list(WATCHED_PROPERTIES))
        except BaseException as exc:  # no events: waits simply keep polling
            self.error = exc
            self._ready.set()
            return
        with self._cond:
            self.alive = True
        self._ready.set()
        try:
            self._stop.wait()
        finally:
            with self._cond:
                self.alive = False
                self._cond.notify_all()
            try:
                uia.RemoveAllEventHandlers()
            except Exception:
                pass


def _pid(sender: Any) -> Optional[int]:
    try:
        pid = int(sender.CurrentProcessId)
        return pid or None
    except Exception:
        return None


_UIA_APPEND_RUNTIME_ID = 3  # first runtime-id part of HWND-based providers: [3, hwnd, ...]
_user32: Any = None


def _pid_from_runtime_id(runtime_id: Any) -> Optional[int]:
    """Process of an element that is already gone (``CurrentProcessId`` fails), from the
    window handle that HWND-based providers put in the runtime id."""
    global _user32
    try:
        parts = [int(x) for x in runtime_id]
    except Exception:
        return None
    if len(parts) < 2 or parts[0] not in (_UIA_APPEND_RUNTIME_ID, 42):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        if _user32 is None:
            _user32 = ctypes.WinDLL("user32", use_last_error=True)
            _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        pid = wintypes.DWORD(0)
        if not _user32.GetWindowThreadProcessId(parts[1], ctypes.byref(pid)):
            return None
        return int(pid.value) or None
    except Exception:
        return None


def _make_handler(UIA: Any, tracker: ChangeTracker) -> Any:
    import comtypes

    class _Handler(comtypes.COMObject):
        _com_interfaces_ = [UIA.IUIAutomationFocusChangedEventHandler,
                            UIA.IUIAutomationStructureChangedEventHandler,
                            UIA.IUIAutomationPropertyChangedEventHandler]

        def HandleFocusChangedEvent(self, sender: Any) -> int:
            pid = _pid(sender)
            old, tracker._focus_pid = tracker._focus_pid, pid
            tracker._bump(pid, old) if old is not None and old != pid else tracker._bump(pid)
            return 0

        def HandleStructureChangedEvent(self, sender: Any, change_type: Any, runtime_id: Any) -> int:
            pid = _pid(sender)
            if pid is None:
                pid = _pid_from_runtime_id(runtime_id)
            tracker._bump_identity(pid)
            tracker._bump(pid)
            return 0

        def HandlePropertyChangedEvent(self, sender: Any, property_id: Any, new_value: Any) -> int:
            pid = _pid(sender)
            if property_id == NAME_PROPERTY:
                tracker._bump_identity(pid)
            tracker._bump(pid)
            return 0

    return _Handler()


__all__ = ["ChangeTracker", "LOG_SIZE", "WATCHED_PROPERTIES", "events_enabled"]
