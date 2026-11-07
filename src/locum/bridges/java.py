"""Java Swing/AWT applications through the Java Access Bridge (Windows, ctypes only).

UI Automation sees a Swing window as one opaque pane. The Java Access Bridge
(JAB) that ships with every JRE/JDK exposes the application's
``javax.accessibility`` tree to Windows processes through
``WindowsAccessBridge-64.dll`` (``-32.dll`` for 32-bit Python)::

    from locum.bridges.java import JavaBridge

    with JavaBridge() as jab:                       # loads the DLL, starts its message pump
        win = jab.window("Order Entry*")            # a top-level Java window (title glob)
        win.find('edit[name="Customer"]').set_text("ACME")
        win.find('button "Save"').click()           # AccessibleAction "click", else a bound mouse click
        print(win.tree())

Elements are :class:`JavaNode` s -- real
:class:`~locum.backends.base.AccessibleNode` s -- so locum's selector engine
(``find``/``find_all``), ``tree_text`` and the policy's allow-rule selectors
work on Java trees unchanged.

Enabling the bridge (see ``docs/BRIDGES.md``): ``jabswitch -enable`` (in the
JRE's ``bin``) or *Control Panel > Ease of Access > Use the computer without a
display > Enable Java Access Bridge*, then restart the Java application. The
DLL is looked up in ``LOCUM_JAB_DLL``, on ``PATH``/System32, in
``%JAVA_HOME%\\bin`` and next to ``java.exe`` on ``PATH``. The legacy
``WindowsAccessBridge.dll`` (32-bit object handles, JRE <= 6) is not supported.

Object references: every context the bridge hands out pins a Java object in
the target JVM until it is released. :class:`JavaBridge` records each one and
releases them all on :meth:`JavaBridge.close` (``with JavaBridge() as jab``);
:meth:`JavaElement.release` frees one subtree early. Long-running robots should
scope their lookups (one ``with`` per transaction) rather than keep one
bridge open forever.
"""

from __future__ import annotations

import ctypes
import fnmatch
import os
import shutil
import struct
import sys
import threading
import time
import weakref
from ctypes import POINTER, Structure, byref, c_int, c_int32, c_int64, c_long, c_short, c_uint16, c_void_p
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .. import _target
from ..backends.base import AccessibleNode
from ..errors import ActionTimeout, BackendError, ElementNotFound, NotSupported, WindowNotFound
from ..types import Point, Rect
from ..wait import wait_until
from ._guard import ActionGate

__all__ = [
    "MAX_STRING_SIZE", "SHORT_STRING_SIZE", "MAX_ACTION_INFO", "MAX_ACTIONS_TO_DO", "JOBJECT64",
    "AccessibleContextInfo", "AccessibleTextInfo", "AccessibleActionInfo", "AccessibleActions",
    "AccessibleActionsToDo", "JavaBridge", "JavaNode", "JavaElement", "ENABLE_HINT", "dll_name",
]

# --------------------------------------------------------------------- AccessBridgePackages.h
#: ``MAX_STRING_SIZE`` / ``SHORT_STRING_SIZE`` from AccessBridgePackages.h.
MAX_STRING_SIZE = 1024
SHORT_STRING_SIZE = 256
MAX_ACTION_INFO = 256
MAX_ACTIONS_TO_DO = 32

#: ``wchar_t`` on Windows: UTF-16 code units (``c_wchar`` is 4 bytes on Linux/macOS, so the
#: layouts use ``c_uint16`` to stay exact -- and testable -- on every OS).
WCHAR = c_uint16
jint = c_int32
BOOL = c_int  # Win32 BOOL
#: ``JOBJECT64`` is ``jlong`` in both WindowsAccessBridge-32.dll and -64.dll (only the legacy
#: WindowsAccessBridge.dll, built with ACCESSBRIDGE_ARCH_LEGACY, used a 32-bit ``jobject``).
JOBJECT64 = c_int64
#: ``long vmID``: 32 bits on Windows (LLP64).
VMID = c_int32 if sys.platform != "win32" else c_long
HWND = c_void_p


class AccessibleContextInfo(Structure):
    """``AccessibleContextInfo`` (AccessBridgePackages.h): 6188 bytes."""

    _fields_ = [
        ("name", WCHAR * MAX_STRING_SIZE),
        ("description", WCHAR * MAX_STRING_SIZE),
        ("role", WCHAR * SHORT_STRING_SIZE),
        ("role_en_US", WCHAR * SHORT_STRING_SIZE),
        ("states", WCHAR * SHORT_STRING_SIZE),
        ("states_en_US", WCHAR * SHORT_STRING_SIZE),
        ("indexInParent", jint),
        ("childrenCount", jint),
        ("x", jint),
        ("y", jint),
        ("width", jint),
        ("height", jint),
        ("accessibleComponent", BOOL),
        ("accessibleAction", BOOL),
        ("accessibleSelection", BOOL),
        ("accessibleText", BOOL),
        ("accessibleInterfaces", BOOL),
    ]


class AccessibleTextInfo(Structure):
    """``AccessibleTextInfo``: ``charCount``, ``caretIndex``, ``indexAtPoint``."""

    _fields_ = [("charCount", jint), ("caretIndex", jint), ("indexAtPoint", jint)]


class AccessibleActionInfo(Structure):
    """``AccessibleActionInfo``: one action name (``wchar_t name[SHORT_STRING_SIZE]``)."""

    _fields_ = [("name", WCHAR * SHORT_STRING_SIZE)]


class AccessibleActions(Structure):
    """``AccessibleActions``: ``jint actionsCount; AccessibleActionInfo actionInfo[MAX_ACTION_INFO]``."""

    _fields_ = [("actionsCount", jint), ("actionInfo", AccessibleActionInfo * MAX_ACTION_INFO)]


class AccessibleActionsToDo(Structure):
    """``AccessibleActionsToDo``: ``jint actionsCount; AccessibleActionInfo actions[MAX_ACTIONS_TO_DO]``."""

    _fields_ = [("actionsCount", jint), ("actions", AccessibleActionInfo * MAX_ACTIONS_TO_DO)]


def _wstr(arr: Any) -> str:
    """A NUL-terminated UTF-16 ``wchar_t`` array as ``str``."""
    raw = bytes(memoryview(arr).cast("B"))
    text = raw.decode("utf-16-le", errors="replace")
    end = text.find("\0")
    return text if end < 0 else text[:end]


def _set_wstr(arr: Any, text: str) -> None:
    units = text.encode("utf-16-le")
    cap = (len(arr) - 1) * 2
    data = units[:cap] + b"\0\0"
    ctypes.memmove(arr, data, len(data))


# --------------------------------------------------------------------- loading
#: How to enable the bridge (shown in errors).
ENABLE_HINT = (
    "Enable the Java Access Bridge: run 'jabswitch -enable' (in the JRE/JDK bin folder) or tick Control Panel > "
    "Ease of Access Center > Use the computer without a display > 'Enable Java Access Bridge', then restart "
    "the Java application. Set LOCUM_JAB_DLL to the full path of {dll} if it is not on PATH, in System32 or in "
    "%JAVA_HOME%\\bin. 64-bit Python needs WindowsAccessBridge-64.dll, 32-bit Python WindowsAccessBridge-32.dll."
)


def dll_name() -> str:
    """``WindowsAccessBridge-64.dll`` for 64-bit Python, ``-32.dll`` for 32-bit."""
    return "WindowsAccessBridge-64.dll" if struct.calcsize("P") == 8 else "WindowsAccessBridge-32.dll"


def _candidates(path: Optional[str]) -> List[str]:
    name = dll_name()
    out = [p for p in (path, os.environ.get("LOCUM_JAB_DLL")) if p]
    out.append(name)  # the default DLL search: System32 and the application directory
    # Python 3.8+ no longer searches PATH for DLLs (ctypes.CDLL with a bare name), so look there ourselves.
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if d and os.path.isfile(os.path.join(d, name)):
            out.append(os.path.join(d, name))
    for home in (os.environ.get("JAVA_HOME"), os.environ.get("JRE_HOME")):
        if home:
            out.append(os.path.join(home, "bin", name))
    java = shutil.which("java")
    if java:
        out.append(os.path.join(os.path.dirname(os.path.realpath(java)), name))
    seen: List[str] = []
    for c in out:
        if c not in seen:
            seen.append(c)
    return seen


def _prototype(dll: Any) -> Any:
    def proto(name: str, restype: Any, *argtypes: Any) -> None:
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)

    proto("Windows_run", None)
    proto("isJavaWindow", BOOL, HWND)
    proto("getAccessibleContextFromHWND", BOOL, HWND, POINTER(VMID), POINTER(JOBJECT64))
    proto("getHWNDFromAccessibleContext", HWND, VMID, JOBJECT64)
    proto("getAccessibleContextInfo", BOOL, VMID, JOBJECT64, POINTER(AccessibleContextInfo))
    proto("getAccessibleChildFromContext", JOBJECT64, VMID, JOBJECT64, jint)
    proto("getAccessibleParentFromContext", JOBJECT64, VMID, JOBJECT64)
    proto("getAccessibleActions", BOOL, VMID, JOBJECT64, POINTER(AccessibleActions))
    proto("doAccessibleActions", BOOL, VMID, JOBJECT64, POINTER(AccessibleActionsToDo), POINTER(jint))
    proto("getAccessibleTextInfo", BOOL, VMID, JOBJECT64, POINTER(AccessibleTextInfo), jint, jint)
    proto("getAccessibleTextRange", BOOL, VMID, JOBJECT64, jint, jint, POINTER(WCHAR), c_short)
    proto("setTextContents", BOOL, VMID, JOBJECT64, POINTER(WCHAR))
    proto("requestFocus", BOOL, VMID, JOBJECT64)
    proto("isSameObject", BOOL, VMID, JOBJECT64, JOBJECT64)
    proto("releaseJavaObject", None, VMID, JOBJECT64)
    return dll


def _load_dll(path: Optional[str] = None) -> Any:
    """Load and prototype the Access Bridge DLL (raises :class:`NotSupported` with :data:`ENABLE_HINT`)."""
    if sys.platform != "win32":
        raise NotSupported("The Java Access Bridge is only available on Windows")
    errors: List[str] = []
    for cand in _candidates(path):
        try:
            dll = ctypes.CDLL(cand)  # the bridge API is __cdecl (matters for 32-bit only)
        except OSError as exc:
            errors.append(f"{cand}: {exc}")
            continue
        try:
            return _prototype(dll)
        except AttributeError as exc:
            errors.append(f"{cand}: not the Access Bridge API ({exc})")
    raise NotSupported(f"{dll_name()} could not be loaded ({'; '.join(errors[-3:])}). "
                       + ENABLE_HINT.format(dll=dll_name()))


#: The DLL loader (tests replace it with a fake DLL object).
_dll_loader: Callable[[Optional[str]], Any] = _load_dll


# --------------------------------------------------------------------- message pump
class _Pump(threading.Thread):
    """Runs ``Windows_run`` and then pumps this thread's messages until stopped.

    The bridge talks to the JVMs through a hidden window created by
    ``Windows_run`` on the calling thread; nothing works until that thread
    processes messages (the JVMs announce themselves to it), and JVMs started
    later are only seen while it keeps pumping.
    """

    def __init__(self, dll: Any) -> None:
        super().__init__(name="locum-java-access-bridge", daemon=True)
        self.dll = dll
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            self.dll.Windows_run()
        except BaseException as exc:  # noqa: BLE001 - reported to the creating thread
            self.error = exc
            self.ready.set()
            return
        self.ready.set()
        if sys.platform != "win32":
            self.stop_event.wait()
            return
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.WinDLL("user32")  # type: ignore[attr-defined]
        msg = wintypes.MSG()
        pm_remove, qs_allinput = 0x0001, 0x04FF
        while not self.stop_event.is_set():
            while user32.PeekMessageW(byref(msg), None, 0, 0, pm_remove):
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
            user32.MsgWaitForMultipleObjects(0, None, False, 50, qs_allinput)


# --------------------------------------------------------------------- the bridge
_ROLES: Dict[str, str] = {
    "push button": "button", "toggle button": "togglebutton", "check box": "checkbox",
    "radio button": "radiobutton", "combo box": "combobox", "text": "edit", "password text": "edit",
    "editbar": "edit", "label": "text", "page tab": "tab", "page tab list": "tablist", "menu item": "menuitem",
    "check box menu item": "menuitem", "radio button menu item": "menuitem", "menu": "menu",
    "popup menu": "menu", "menu bar": "menubar", "tool bar": "toolbar", "table": "table", "tree": "tree",
    "list": "list", "list item": "listitem", "frame": "window", "window": "window", "dialog": "dialog",
    "panel": "pane", "scroll pane": "pane", "root pane": "pane", "layered pane": "pane", "viewport": "pane",
    "split pane": "pane", "glass pane": "pane", "internal frame": "pane", "desktop pane": "pane",
    "option pane": "pane", "filler": "pane", "canvas": "pane", "group box": "group", "slider": "slider",
    "spin box": "spinbutton", "progress bar": "progressbar", "scroll bar": "scrollbar", "separator": "separator",
    "tool tip": "tooltip", "hyperlink": "link", "status bar": "statusbar", "column header": "columnheader",
    "row header": "rowheader", "icon": "image", "paragraph": "text", "header": "header", "unknown": "unknown",
}
_STATE_MAP = {"indeterminate": "mixed", "collapsed": "collapsed", "expanded": "expanded", "checked": "checked",
              "selected": "selected", "focused": "focused", "focusable": "focusable", "editable": "editable",
              "enabled": "enabled", "busy": "busy", "modal": "modal", "active": "active", "pressed": "pressed"}
#: AccessibleAction names that mean "press this control" (Swing's AbstractButton action is "click").
CLICK_ACTIONS = ("click", "press", "activate", "doclick")
#: Java roles whose first AccessibleAction presses them whatever its name: Swing localises the
#: AbstractButton action (UIManager ``AbstractButton.clickText``: "Klicken", "cliquer", "hacer clic"...).
_PRESSABLE_ROLES = frozenset({"push button", "toggle button", "check box", "radio button", "menu item",
                              "check box menu item", "radio button menu item"})
#: Java roles whose descendants are index-addressed proxies: ``AccessibleJTable.getAccessibleChild``
#: (``AccessibleJTableCell``), ``AccessibleJList`` (``AccessibleJListChild``) and ``AccessibleJTree``
#: (``AccessibleJTreeNode``) return a NEW object on every call, so ``isSameObject`` never matches them.
#: Their children are kept by index: the node stays, its reference is swapped for the new one.
_INDEXED_ROLES = frozenset({"table", "list", "tree", "tree table"})
#: How far from its old position a cached child is looked for when the tree is re-read.
_MATCH_WINDOW = 4


class JavaBridge:
    """A loaded Java Access Bridge.

    Args:
        desktop: :class:`~locum.desktop.Desktop` used for window lookup and the
            mouse fallback of :meth:`JavaElement.click` (default: locum's default
            desktop, created only when needed), or a
            :class:`~locum.llm.safety.GuardedDesktop` (its policy applies).
        policy / confirm / audit: The safety hook (see :class:`~locum.bridges._guard.ActionGate`).
        dll_path: Explicit path of ``WindowsAccessBridge-64.dll``/``-32.dll``.
        pump: Run ``Windows_run`` on a dedicated message-pump thread (default).
            ``False`` calls it on this thread, which must then pump messages
            itself (a GUI main loop).
        settle: Seconds to wait after initialization before the first lookup
            (the JVMs announce themselves asynchronously).
        max_children: Children read per node at most (a ``JTable`` can report
            millions of cells).
    """

    def __init__(self, desktop: Any = None, *, policy: Any = None, confirm: Any = None, audit: Any = None,
                 dll_path: Optional[str] = None, pump: bool = True, settle: float = 0.3, max_children: int = 2000,
                 sleep: Callable[[float], Any] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.desktop, self.gate = ActionGate.resolve(desktop, policy, confirm, audit, bridge="java")
        self.max_children = int(max_children)
        self.sleep = sleep
        self.clock = clock
        self._lock = threading.Lock()
        self._live: Dict[Tuple[int, int], int] = {}
        self._closed = False
        self.dll = _dll_loader(dll_path)
        self._pump: Optional[_Pump] = None
        if pump:
            self._pump = _Pump(self.dll)
            self._pump.start()
            if not self._pump.ready.wait(10.0):
                raise NotSupported("The Java Access Bridge did not initialize (Windows_run hung). "
                                   + ENABLE_HINT.format(dll=dll_name()))
            if self._pump.error is not None:
                raise NotSupported(f"Windows_run failed: {self._pump.error}. "
                                   + ENABLE_HINT.format(dll=dll_name())) from self._pump.error
        else:
            self.dll.Windows_run()
        if settle > 0:
            sleep(settle)

    # ------------------------------------------------------------------ lifetime
    def __enter__(self) -> "JavaBridge":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _track(self, vm: int, ac: int) -> None:
        with self._lock:
            self._live[(vm, ac)] = self._live.get((vm, ac), 0) + 1

    def release(self, vm: int, ac: int) -> None:
        """Release one reference (``releaseJavaObject``) handed out by this bridge."""
        with self._lock:
            n = self._live.get((vm, ac), 0)
            if n <= 0:
                return
            if n == 1:
                del self._live[(vm, ac)]
            else:
                self._live[(vm, ac)] = n - 1
        try:
            self.dll.releaseJavaObject(vm, ac)
        except Exception:  # noqa: BLE001 - the JVM may be gone
            pass

    @property
    def live_references(self) -> int:
        with self._lock:
            return sum(self._live.values())

    def release_all(self) -> None:
        """Release every Java object reference this bridge handed out."""
        with self._lock:
            refs = [(k, n) for k, n in self._live.items()]
            self._live.clear()
        for (vm, ac), n in refs:
            for _ in range(n):
                try:
                    self.dll.releaseJavaObject(vm, ac)
                except Exception:  # noqa: BLE001
                    break

    def close(self) -> None:
        """Release all references and stop the message pump."""
        if self._closed:
            return
        self._closed = True
        self.release_all()
        if self._pump is not None:
            self._pump.stop_event.set()

    # ------------------------------------------------------------------ windows
    def _desktop(self) -> Any:
        if self.desktop is None:
            from .. import get_desktop  # noqa: PLC0415

            self.desktop = get_desktop()
        return self.desktop

    def is_java_window(self, hwnd: Any) -> bool:
        try:
            return bool(self.dll.isJavaWindow(hwnd))
        except Exception:  # noqa: BLE001
            return False

    def windows(self) -> List[Any]:
        """Top-level windows (:class:`~locum.types.WindowInfo`) that belong to a Java VM."""
        return [w for w in self._desktop().backend.list_windows() if self.is_java_window(w.handle)]

    def from_hwnd(self, hwnd: Any, *, timeout: float = 3.0, title: str = "") -> "JavaElement":
        """The accessible root of Java window ``hwnd`` (polls ``isJavaWindow`` up to ``timeout``:
        JVMs register with the bridge asynchronously)."""
        if not self.is_java_window(hwnd):
            try:
                wait_until(lambda: self.is_java_window(hwnd), timeout, 0.1,
                           message="not a Java window", sleep=self.sleep, clock=self.clock)
            except ActionTimeout:
                raise NotSupported(
                    f"Window {title or hwnd!r} is not a Java window the Access Bridge can see. "
                    "If it is a Swing/AWT application, the bridge is probably not enabled for it. "
                    + ENABLE_HINT.format(dll=dll_name())) from None
        vm = VMID(0)
        ac = JOBJECT64(0)
        if not self.dll.getAccessibleContextFromHWND(hwnd, byref(vm), byref(ac)) or not ac.value:
            raise BackendError(f"getAccessibleContextFromHWND failed for window {title or hwnd!r}")
        self._track(vm.value, ac.value)
        node = JavaNode(self, vm.value, ac.value, None, hwnd=hwnd)
        return JavaElement(node)

    def window(self, title: Optional[str] = None, *, timeout: float = 10.0) -> "JavaElement":
        """The first Java window whose title equals / glob-matches / contains ``title``
        (case-insensitive; ``None``: the front-most Java window)."""
        def pick() -> Any:
            for w in self.windows():
                t = w.title or ""
                if title is None or t == title or fnmatch.fnmatchcase(t.lower(), title.lower()) \
                        or title.lower() in t.lower():
                    return w
            return None

        if timeout:
            try:
                info = wait_until(pick, timeout, 0.2, message=f"No Java window titled {title!r}",
                                  sleep=self.sleep, clock=self.clock)
            except ActionTimeout as exc:
                raise WindowNotFound(f"No Java window titled {title!r} within {timeout} s") from exc
        else:
            info = pick()
        if info is None:
            raise WindowNotFound(f"No Java window titled {title!r}")
        return self.from_hwnd(info.handle, title=info.title or "")


# --------------------------------------------------------------------- nodes
class JavaNode(AccessibleNode):
    """One ``AccessibleContext`` of a Java application (a locum :class:`AccessibleNode`).

    Roles are mapped from ``role_en_US`` (``push button`` -> ``button``, ``text`` ->
    ``edit``, ``label`` -> ``text``, ``frame`` -> ``window``...); ``native_role``
    keeps the Java role. States come from ``states_en_US`` (``showing`` ->
    ``visible``, not ``enabled`` -> ``disabled``, ``indeterminate`` -> ``mixed``,
    a ``text`` without ``editable`` -> ``readonly``, ``password text`` ->
    ``password``).
    """

    def __init__(self, bridge: JavaBridge, vm: int, ac: int, parent: Optional["JavaNode"], *,
                 hwnd: Any = None) -> None:
        self.bridge = bridge
        self.vm = vm
        self.ac = ac
        self._parent = parent
        self.hwnd = hwnd
        self._info: Optional[AccessibleContextInfo] = None
        self._kids: Optional[List["JavaNode"]] = None
        #: ``True`` when the cached children must be checked against the JVM on the next read.
        self._kids_stale = False
        self._released = False
        #: The index this node was read at in its parent (``getAccessibleChildFromContext``).
        self._index = -1
        #: Live :class:`JavaElement` objects wrapping this node (a held node is never freed early).
        self._holders = 0

    # ------------------------------------------------------------------ raw data
    @property
    def info(self) -> Optional[AccessibleContextInfo]:
        if self._info is None and not self._released:
            if self._index >= 0 and self._parent is not None and self._parent._index_addressed():
                # A JTable cell over JAB is Swing's shared renderer component: it holds this
                # cell's data only right after the cell was fetched. Fetch it again by index.
                self._refetch()
            self._info = self._read_info()
        return self._info

    def _read_info(self) -> Optional[AccessibleContextInfo]:
        info = AccessibleContextInfo()
        try:
            ok = self.bridge.dll.getAccessibleContextInfo(self.vm, self.ac, byref(info))
        except Exception:  # noqa: BLE001
            ok = False
        return info if ok else None

    def _refetch(self) -> None:
        parent = self._parent
        if parent is None or parent._released:
            return
        child = parent._child(self._index)
        if child:
            self.bridge._track(self.vm, child)
            self._rebind(child)

    def refresh(self) -> "JavaNode":
        """Re-read this node and everything below it from the JVM on next access.

        Names, states, values and bounds are read again. The child lists are
        checked against the JVM: unchanged children keep their node (so
        elements already handed out stay valid), new ones are added, and the
        ones that went away are dropped from the tree and their references
        released, except where the caller still holds an element in that
        subtree (those are released on :meth:`JavaBridge.close`). Cells, list items and tree nodes
        below a table, list or tree are kept by index (Swing hands out a new
        proxy object on every read): a held cell element stays valid and reads
        whatever is at its position now; the swapped-out references are
        released at once.
        """
        self._info = None
        if self._kids is not None:
            self._kids_stale = True
            for kid in self._kids:
                kid.refresh()
        return self

    def _s(self, field: str) -> str:
        info = self.info
        return _wstr(getattr(info, field)) if info is not None else ""

    @property
    def java_role(self) -> str:
        return self._s("role_en_US").strip().lower()

    @property
    def java_states(self) -> frozenset:
        return frozenset(s.strip().lower() for s in self._s("states_en_US").split(",") if s.strip())

    @property
    def window_handle(self) -> Any:
        """The top-level window handle this node lives in."""
        n: Optional[JavaNode] = self
        while n is not None:
            if n.hwnd is not None:
                return n.hwnd
            n = n._parent
        try:
            return self.bridge.dll.getHWNDFromAccessibleContext(self.vm, self.ac)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ AccessibleNode
    @property
    def role(self) -> str:
        r = self.java_role
        return _ROLES.get(r, r.replace(" ", "") or "unknown")

    @property
    def native_role(self) -> str:
        return self.java_role

    @property
    def name(self) -> str:
        return self._s("name")

    @property
    def description(self) -> str:
        return self._s("description")

    @property
    def value(self) -> Optional[str]:
        if self.role in ("edit", "combobox", "spinbutton") and self.java_role != "password text":
            return self.text()
        return None

    @property
    def bounds(self) -> Optional[Rect]:
        info = self.info
        if info is None or info.width <= 0 or info.height <= 0:
            return None
        return Rect(info.x, info.y, info.width, info.height)

    @property
    def states(self) -> frozenset:
        js = self.java_states
        if not js and self.info is None:
            return frozenset()
        st = {_STATE_MAP[s] for s in js if s in _STATE_MAP}
        if "enabled" not in js:
            st.add("disabled")
        st.add("visible" if "showing" in js else "offscreen")
        if self.java_role in ("text", "password text") and "editable" not in js:
            st.add("readonly")
        if self.java_role == "password text":
            st.add("password")
        if self.java_role in ("check box", "radio button", "toggle button", "check box menu item",
                              "radio button menu item") and "checked" not in js and "indeterminate" not in js:
            st.add("unchecked")
        return frozenset(st)

    @property
    def handle(self) -> Any:
        return self.hwnd

    def attributes(self) -> Dict[str, Any]:
        info = self.info
        return {
            "java_role": self.java_role, "java_states": ",".join(sorted(self.java_states)),
            "index_in_parent": info.indexInParent if info is not None else -1, "vm_id": self.vm,
        }

    def children(self) -> Sequence["JavaNode"]:
        if self._kids is None or self._kids_stale:
            info = self.info
            count = min(int(info.childrenCount), self.bridge.max_children) if info is not None else 0
            prev = list(self._kids or ())
            if self._index_addressed():
                kids = self._read_indexed(count, prev)
                kept = {id(k) for k in kids}
                for old in prev:  # replaced by a new proxy or past the last index: free it now
                    if id(old) not in kept:
                        old.release()
            else:
                # An unmatched old child is gone, or is a fresh proxy of a component that hands
                # out new objects on every read. Free it now, unless the caller still holds an
                # element in its subtree: that one may still exist (moved beyond the match
                # window), so its reference stays tracked and JavaBridge.close() releases it.
                kids = self._read_matched(count, prev)
                kept = {id(k) for k in kids}
                for old in prev:
                    if id(old) not in kept and not old._subtree_held():
                        old.release()
            self._kids = kids
            self._kids_stale = False
        return self._kids

    def _child(self, i: int) -> int:
        try:
            return int(self.bridge.dll.getAccessibleChildFromContext(self.vm, self.ac, i) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _new_child(self, child: int, i: int) -> "JavaNode":
        self.bridge._track(self.vm, child)
        node = JavaNode(self.bridge, self.vm, child, self)
        node._index = i
        return node

    def _index_addressed(self) -> bool:
        """``True`` below a table, list or tree, whose children are fresh proxies on every read."""
        n: Optional[JavaNode] = self
        while n is not None:
            if n.java_role in _INDEXED_ROLES:
                return True
            n = n._parent
        return False

    def _read_indexed(self, count: int, prev: List["JavaNode"]) -> List["JavaNode"]:
        """Children of a table/list/tree: no ``isSameObject`` matching (it never matches a new
        proxy); the node at each index is kept and re-pointed at the new reference."""
        by_index = {k._index: k for k in prev if not k._released}
        kids: List[JavaNode] = []
        for i in range(max(0, count)):
            child = self._child(i)
            if not child:
                continue
            old = by_index.get(i)
            if old is None:
                node = self._new_child(child, i)
            else:
                self.bridge._track(self.vm, child)
                old._rebind(child)
                node = old
            # Read it now: a JTable cell is the shared renderer, which the next fetch restamps.
            node._info = node._read_info()
            kids.append(node)
        return kids

    def _rebind(self, ac: int) -> None:
        """Point this node at a new reference to (the proxy at) its position; free the old one."""
        old, self.ac = self.ac, ac
        if self._kids is not None:
            # Keep the children (held elements stay valid); they are re-read by index next time.
            self._kids_stale = True
        self._info = None
        self.bridge.release(self.vm, old)

    def _read_matched(self, count: int, prev: List["JavaNode"]) -> List["JavaNode"]:
        used: set = set()
        shift = 0  # rows inserted/removed above: the old index of child i is i - shift
        kids: List[JavaNode] = []
        for i in range(max(0, count)):
            child = self._child(i)
            if not child:
                continue
            # Same position first, then a few neighbours: a bounded number of isSameObject
            # calls per child (at most 2 * _MATCH_WINDOW + 1), not a scan of every cached child.
            # No cut-off after a run of misses: a rebuilt top of a panel must not stop the
            # unchanged children below it from matching (and held elements from staying valid).
            same = None
            for j in sorted(range(i - shift - _MATCH_WINDOW, i - shift + _MATCH_WINDOW + 1),
                            key=lambda j: abs(j - (i - shift))):
                if 0 <= j < len(prev) and j not in used and not prev[j]._released \
                        and self._same_ref(prev[j].ac, child):
                    same, shift = prev[j], i - j
                    used.add(j)
                    break
            if same is not None:
                # A second reference to a child we already hold: keep the node, drop the new reference.
                if child != same.ac:
                    try:
                        self.bridge.dll.releaseJavaObject(self.vm, child)
                    except Exception:  # noqa: BLE001
                        pass
                same._index = i
                kids.append(same)
                continue
            kids.append(self._new_child(child, i))
        return kids

    def _same_ref(self, a: int, b: int) -> bool:
        if a == b:
            return True
        try:
            return bool(self.bridge.dll.isSameObject(self.vm, a, b))
        except Exception:  # noqa: BLE001
            return False

    @property
    def parent(self) -> Optional["JavaNode"]:
        return self._parent

    def same_as(self, other: "AccessibleNode") -> bool:
        if not isinstance(other, JavaNode) or other.vm != self.vm:
            return False
        if other.ac == self.ac:
            return True
        try:
            return bool(self.bridge.dll.isSameObject(self.vm, self.ac, other.ac))
        except Exception:  # noqa: BLE001
            return False

    def actions(self) -> List[str]:
        info = self.info
        if info is None or not info.accessibleAction:
            return []
        acts = AccessibleActions()
        try:
            if not self.bridge.dll.getAccessibleActions(self.vm, self.ac, byref(acts)):
                return []
        except Exception:  # noqa: BLE001
            return []
        n = max(0, min(int(acts.actionsCount), MAX_ACTION_INFO))
        return [_wstr(acts.actionInfo[i].name) for i in range(n)]

    def do_action(self, name: str) -> bool:
        """``doAccessibleActions`` with one action name; ``True`` when the JVM performed it."""
        todo = AccessibleActionsToDo()
        todo.actionsCount = 1
        _set_wstr(todo.actions[0].name, name)
        failure = jint(-1)
        # ``failure`` receives the index of the first action that failed; the BOOL result says it all.
        return bool(self.bridge.dll.doAccessibleActions(self.vm, self.ac, byref(todo), byref(failure)))

    def click_action(self) -> Optional[str]:
        """The action name that presses this control, if it has one.

        An English name from :data:`CLICK_ACTIONS` first; else, for a button, check box,
        radio button, toggle button or menu item, its first action (Swing localises the
        name: "Klicken", "cliquer", "hacer clic"...).
        """
        acts = self.actions()
        low = {a.lower(): a for a in acts}
        for want in CLICK_ACTIONS:
            if want in low:
                return low[want]
        if acts and acts[0] and self.java_role in _PRESSABLE_ROLES:
            return acts[0]
        return None

    def invoke(self) -> bool:
        name = self.click_action()
        return self.do_action(name) if name else False

    def focus(self) -> bool:
        try:
            return bool(self.bridge.dll.requestFocus(self.vm, self.ac))
        except Exception:  # noqa: BLE001
            return False

    def set_value(self, text: str) -> bool:
        _check_settable(text)
        buf = (WCHAR * (len(text.encode("utf-16-le")) // 2 + 1))()
        _set_wstr(buf, text)
        try:
            return bool(self.bridge.dll.setTextContents(self.vm, self.ac, buf))
        except Exception:  # noqa: BLE001
            return False

    def text(self, chunk: int = 1000) -> str:
        """The accessible text (``getAccessibleTextInfo`` + ``getAccessibleTextRange``), else the name."""
        info = self.info
        if info is None or not info.accessibleText:
            return self.name
        ti = AccessibleTextInfo()
        try:
            if not self.bridge.dll.getAccessibleTextInfo(self.vm, self.ac, byref(ti), 0, 0):
                return ""
        except Exception:  # noqa: BLE001
            return ""
        total = max(0, int(ti.charCount))
        parts: List[str] = []
        start = 0
        while start < total:
            end = min(total, start + chunk) - 1  # inclusive end index
            n = end - start + 1
            buf = (WCHAR * (n + 1))()
            if not self.bridge.dll.getAccessibleTextRange(self.vm, self.ac, start, end, buf, n):
                break
            parts.append(_wstr(buf)[:n])
            start = end + 1
        return "".join(parts)

    def _subtree_held(self) -> bool:
        if self._holders > 0:
            return True
        return any(kid._subtree_held() for kid in self._kids or ())

    def _unhold(self) -> None:
        self._holders -= 1

    def release(self) -> None:
        """Release this node's reference and those of its (already read) descendants."""
        for kid in self._kids or ():
            kid.release()
        self._kids = None
        if not self._released:
            self._released = True
            self._info = None
            self.bridge.release(self.vm, self.ac)

    def __repr__(self) -> str:
        return f"<JavaNode role={self.role!r} name={self.name!r} bounds={self.bounds}>"


def _check_settable(text: str) -> None:
    """``setTextContents`` copies into ``wchar_t text[MAX_STRING_SIZE]``: longer text is cut silently."""
    units = len(text.encode("utf-16-le")) // 2
    if units > MAX_STRING_SIZE - 1:
        raise ValueError(f"set_text: {units} UTF-16 code units is longer than the Java Access Bridge's limit of "
                         f"{MAX_STRING_SIZE - 1} (setTextContents would truncate it); click the field and type "
                         "instead")


# --------------------------------------------------------------------- elements
def _norm(text: Optional[str]) -> str:
    return " ".join(str(text or "").split())


class JavaElement:
    """A Java accessible element with actions (wraps a :class:`JavaNode`)."""

    def __init__(self, node: JavaNode) -> None:
        self.node = node
        node._holders += 1
        weakref.finalize(self, node._unhold)

    @property
    def bridge(self) -> JavaBridge:
        return self.node.bridge

    def _live(self) -> JavaNode:
        """The node, with its own info re-read from the JVM on next access.

        Every property read and action goes through this: a Java UI changes
        under an element (a checkbox is checked, a label's text changes, a
        field is disabled), and a cached read would report the old state.
        """
        self.node._info = None
        return self.node

    # ------------------------------------------------------------------ properties
    @property
    def name(self) -> str:
        return self._live().name

    @property
    def role(self) -> str:
        return self._live().role

    @property
    def java_role(self) -> str:
        return self._live().java_role

    @property
    def description(self) -> str:
        return self._live().description

    @property
    def states(self) -> frozenset:
        return self._live().states

    @property
    def rect(self) -> Optional[Rect]:
        return self._live().bounds

    bounds = rect

    @property
    def text(self) -> str:
        return self._live().text()

    @property
    def is_enabled(self) -> bool:
        return "disabled" not in self._live().states

    def actions(self) -> List[str]:
        return self._live().actions()

    @property
    def parent(self) -> Optional["JavaElement"]:
        p = self.node.parent
        return JavaElement(p) if p is not None else None

    def children(self) -> List["JavaElement"]:
        return [JavaElement(n) for n in self.node.refresh().children()]

    def walk(self, max_depth: int = 64) -> Iterator["JavaElement"]:
        for n in self.node.refresh().walk(max_depth):
            yield JavaElement(n)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ lookup
    def find_all(self, selector: Any, *, max_depth: int = 64) -> List["JavaElement"]:
        """Descendants matching a locum selector (``'button "OK"'``, ``'edit[name^="Cust"]'``, ...)."""
        from ..selectors import parse  # noqa: PLC0415

        self.node.refresh()  # match against the UI as it is now, not as it was first read
        return [JavaElement(n) for n in parse(selector).find_all([self.node], max_depth=max_depth)]  # type: ignore[misc]

    def find(self, selector: Any, timeout: float = 0.0) -> "JavaElement":
        """The first descendant matching ``selector`` (polls ``timeout`` seconds, re-reading the tree)."""
        from ..selectors import parse  # noqa: PLC0415

        sel = parse(selector)

        def probe() -> Optional[JavaNode]:
            return sel.find_first([self.node])  # type: ignore[return-value]

        self.node.refresh()  # match against the UI as it is now, not as it was first read
        node = probe()
        if node is None and timeout and timeout > 0:
            def again() -> Optional[JavaNode]:
                self.node.refresh()
                return probe()

            try:
                node = wait_until(again, timeout, 0.2, sleep=self.bridge.sleep, clock=self.bridge.clock,
                                  ignore_exceptions="transient")
            except ActionTimeout:
                node = None
        if node is None:
            raise ElementNotFound(f"No Java element matches {str(selector)!r} under {self!r}")
        return JavaElement(node)

    def exists(self, selector: Any) -> bool:
        try:
            self.find(selector)
        except ElementNotFound:
            return False
        return True

    def tree(self, max_depth: int = 8) -> str:
        from ..selectors import tree_text  # noqa: PLC0415

        return tree_text([self.node.refresh()], max_depth=max_depth)

    # ------------------------------------------------------------------ policy context
    def _ctx(self, *, activates: bool) -> Dict[str, Any]:
        from .._nodeinfo import target_label  # noqa: PLC0415

        ctx: Dict[str, Any] = {"target_node": self.node}
        label = target_label(self.node)
        if label:
            ctx["target"] = label
        elif activates:
            ctx["target_unknown"] = True
        hwnd = self.node.window_handle
        root: Optional[JavaNode] = self.node
        while root is not None and root.parent is not None:
            root = root.parent
        title = root.name if root is not None else ""
        if title:
            ctx["target_window"] = title
            ctx["window_title"] = title
        app = self._app(hwnd)
        if app:
            ctx["target_app"] = app
            ctx["app"] = app
        return ctx

    def _app(self, hwnd: Any) -> str:
        desktop = self.bridge.desktop
        if desktop is None or hwnd is None:
            return ""
        try:
            for w in desktop.backend.list_windows():
                if w.handle == hwnd:
                    return str(w.app or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ------------------------------------------------------------------ actions
    def click(self, *, native: Any = "auto") -> "JavaElement":
        """Press the element.

        ``native="auto"``: the element's AccessibleAction (``click``) when it has
        one, else a left mouse click at its center through locum's mouse with
        input bound to its window (:mod:`locum._target`: refused with
        :class:`~locum.errors.TargetLost` if another window covers the point).
        ``True``: the action only (:class:`NotSupported` without one);
        ``False``: the mouse only.
        """
        if "disabled" in self._live().states:
            raise ElementNotFound(f"{self!r} is disabled")
        action = self.node.click_action() if native is not False else None
        if native is True and action is None:
            raise NotSupported(f"{self!r} has no click action (actions: {self.node.actions()})")
        ctx = self._ctx(activates=True)
        b = self.node.bounds
        if action is None and b is not None:
            ctx["point"] = b.center  # the mouse fallback: region / app-under-pointer rules apply
        what = ctx.get("target") or self.role
        if not self.bridge.gate.allow("click", {"target": str(what)}, ctx, f"click {what!r}"):
            return self
        if action is not None:
            if not self.node.do_action(action):
                raise BackendError(f"doAccessibleActions({action!r}) failed on {self!r}")
            return self
        self._mouse_click()
        return self

    def _mouse_click(self) -> None:
        from .. import _inputlock  # noqa: PLC0415

        b = self.node.bounds
        if b is None or "offscreen" in self.node.states:
            raise ElementNotFound(f"{self!r} is not visible on screen; cannot click it with the mouse")
        point: Point = b.center
        hwnd = self.node.window_handle
        desktop = self.bridge._desktop()
        backend = desktop.backend
        if hwnd is None:
            raise ElementNotFound(f"The window of {self!r} is unknown; refusing an unbound mouse click")
        with _inputlock.hold(desktop):
            try:
                if backend.window_handle_at(point.x, point.y) != hwnd:
                    backend.activate_window(hwnd)
            except Exception:  # noqa: BLE001 - the binding below decides
                pass
            target = _target.InputTarget(backend, hwnd, title=_norm(self.node.name), strict=True)
            with _target.bind(target):
                desktop.mouse.click(point)

    def set_text(self, text: str, *, verify: bool = True) -> "JavaElement":
        """Replace the element's text (``setTextContents``) and read it back.

        Raises :class:`NotSupported` when the bridge cannot set it (not an
        editable text component), :class:`ElementNotFound` for a disabled or
        read-only field, :class:`ValueError` for text longer than the bridge's
        1023-character limit (before anything is sent) and :class:`ActionTimeout`
        when the read-back differs.
        """
        _check_settable(str(text))
        st = self._live().states
        if "disabled" in st or "readonly" in st:
            raise ElementNotFound(f"{self!r} is disabled or read-only")
        secret = "password" in st
        ctx = self._ctx(activates=False)
        shown = "***" if secret else (text if len(text) <= 60 else text[:57] + "...")
        if not self.bridge.gate.allow("set_value", {"target": ctx.get("target"), "text": text}, ctx,
                                      f"set text of {ctx.get('target') or self.role!r} to {shown!r}", secret=secret):
            return self
        if not self.node.set_value(str(text)):
            raise NotSupported(f"setTextContents failed on {self!r}: not an editable text component "
                               "(click it and type instead)")
        if verify and not secret:
            self.node.refresh()
            actual = self.node.text()
            if actual != str(text):
                raise ActionTimeout(f"{self!r} reads {actual!r} after setting {str(text)!r}")
        return self

    def focus(self) -> "JavaElement":
        ctx = self._ctx(activates=False)
        if self.bridge.gate.allow("focus", {"target": ctx.get("target")}, ctx, f"focus {ctx.get('target') or self.role!r}"):
            if not self.node.focus():
                raise BackendError(f"requestFocus failed on {self!r}")
        return self

    def do_action(self, name: str) -> "JavaElement":
        """Run a named AccessibleAction (``"toggle expand"``, ``"togglePopup"``...)."""
        ctx = self._ctx(activates=True)
        what = ctx.get("target") or self.role
        ctx["arg_texts"] = [name]
        if self.bridge.gate.allow("invoke", {"target": str(what), "action": name}, ctx, f"{name} {what!r}"):
            if not self.node.do_action(name):
                raise BackendError(f"doAccessibleActions({name!r}) failed on {self!r}")
        return self

    def release(self) -> None:
        """Release this element's Java object reference (and its read descendants')."""
        self.node.release()

    def to_dict(self) -> Dict[str, Any]:
        b = self.rect
        return {"role": self.role, "java_role": self.java_role, "name": self.name,
                "states": sorted(self.states), "rect": b.to_dict() if b is not None else None}

    def __repr__(self) -> str:
        return f"<JavaElement {self.role} {self.name!r}>"
