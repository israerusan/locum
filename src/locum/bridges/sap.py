"""SAP GUI for Windows through the SAP GUI Scripting API (COM).

UI Automation sees an SAP GUI screen as a handful of opaque panes. The
Scripting API exposes every field, button, table control and ALV grid by its
stable id (``wnd[0]/usr/ctxtBKPF-BUKRS``) -- the ids SAP's own script
recorder writes. This module wraps it::

    from locum.bridges import sap

    s = sap.attach(system="PRD", client="100")        # an already logged-on session
    s.start_transaction("SE16N")
    s.find("wnd[0]/usr/ctxtGD-TAB").text = "T001"
    s.send_vkey(0)                                     # Enter
    s.find("wnd[0]/tbar[1]/btn[8]").press()            # F8 Execute
    grid = s.find("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell")
    rows = grid.rows(["BUKRS", "BUTXT"])
    s.check_status()                                   # raises SapError on an E/A message

Requirements (see ``docs/BRIDGES.md``):

* ``pip install locum[sap]`` (pywin32), SAP GUI for Windows with SAP Logon running;
* client side: *SAP GUI Options > Accessibility & Scripting > Scripting >
  Enable scripting*;
* server side: profile parameter ``sapgui/user_scripting = TRUE`` (transaction
  RZ11; and ``S_SCR`` authorization when ``sapgui/user_scripting_per_user`` is
  set). With ``sapgui/user_scripting_set_readonly = TRUE`` every write fails.

Acting calls (``press``, setting ``text``/``key``/cells, ``send_vkey``,
``start_transaction``, ``select``, ``close``...) go through the configured
:class:`~locum.llm.safety.Policy` first (``attach(policy=...)`` or
``attach(desktop=GuardedDesktop(...))``) with the same context
``GuardedDesktop`` builds: the component's label is matched against
``confirm_patterns``, ``send_vkey(11)`` is the ``ctrl+s`` shortcut for
``confirm_keys``, allow rules see the component as an accessibility node
(``button[name*="Post"]``, ``window=...``). They also wait until the session
is no longer busy afterwards.

COM objects belong to the thread that created them: use a session on the thread
that attached it (call ``pythoncom.CoInitialize()`` first on a worker thread).
A session that is frozen (an endless loop on the server, a modal system
message) blocks *every* COM call into it, even ``Busy``; this module cannot
time out such a call.
"""

from __future__ import annotations

import fnmatch
import re
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from ..backends.base import AccessibleNode
from ..errors import (
    ActionTimeout,
    AmbiguousElement,
    BackendError,
    BackendUnavailable,
    ElementNotFound,
    LocumError,
    NotSupported,
    warn_experimental,
)
from ..types import Rect
from ..wait import wait_until
from ._guard import ActionGate

__all__ = [
    "SapError", "SapSessionInfo", "SapGui", "SapSession", "SapComponent", "SapWindow", "SapTextField",
    "SapButton", "SapComboBox", "SapCheckBox", "SapRadioButton", "SapTab", "SapStatusbar", "SapTableControl",
    "SapGridView", "SapNode", "VKEYS", "vkey_combo", "attach", "sessions", "engine", "SCRIPTING_HINT",
]

#: How to enable SAP GUI Scripting (shown in errors).
SCRIPTING_HINT = (
    "Enable SAP GUI Scripting on both sides: client -- SAP GUI Options > Accessibility & Scripting > "
    "Scripting > 'Enable scripting' (and consider unticking the 'Notify when a script attaches' prompts); "
    "server -- profile parameter sapgui/user_scripting = TRUE (transaction RZ11; it takes effect for new "
    "logons), plus authorization object S_SCR if sapgui/user_scripting_per_user = TRUE. "
    "sapgui/user_scripting_set_readonly = TRUE allows reading only."
)

_INSTALL_HINT = "SAP GUI Scripting needs pywin32 on Windows: pip install locum[sap]"

#: SAP virtual key numbers (``sendVKey``) -> the key combination they stand for.
VKEYS: Dict[int, str] = {0: "enter", 11: "ctrl+s", 12: "f12"}
for _n in range(1, 11):
    VKEYS[_n] = f"f{_n}"
for _i in range(12):
    VKEYS[13 + _i] = f"shift+f{_i + 1}"
    VKEYS[25 + _i] = f"ctrl+f{_i + 1}"
    VKEYS[37 + _i] = f"ctrl+shift+f{_i + 1}"
VKEYS.update({80: "ctrl+pageup", 81: "pageup", 82: "pagedown", 83: "ctrl+pagedown"})
del _n, _i


def vkey_combo(vkey: int) -> str:
    """The key combination SAP virtual key ``vkey`` stands for (``11`` -> ``"ctrl+s"``)."""
    return VKEYS.get(int(vkey), f"vkey{int(vkey)}")


class SapError(LocumError):
    """An SAP status-bar error (message type ``E`` or ``A``) after an action."""

    def __init__(self, text: str, *, message_type: str = "", message_id: str = "", message_number: str = "") -> None:
        detail = f"{message_type} {message_id}/{message_number}".strip(" /")
        super().__init__(f"SAP reported: {text}" + (f" ({detail})" if detail else ""))
        self.text = text
        self.message_type = message_type
        self.message_id = message_id
        self.message_number = message_number


# ---------------------------------------------------------------------- COM helpers
def _com_message(exc: BaseException) -> str:
    """The human part of a ``pywintypes.com_error`` (its excepinfo description), else ``str(exc)``."""
    args = getattr(exc, "args", ())
    try:
        if len(args) >= 3 and args[2] and len(args[2]) >= 3 and args[2][2]:
            return str(args[2][2]).strip()
        if len(args) >= 2 and isinstance(args[1], str):
            return args[1]
    except Exception:  # noqa: BLE001
        pass
    return str(exc)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:  # noqa: BLE001 - a COM property that is missing / stale
        return default


def _items(coll: Any) -> List[Any]:
    """Items of a SAP COM collection (``Count`` + ``Item``/call/``ElementAt``), or iteration."""
    if coll is None:
        return []
    count = _get(coll, "Count")
    if isinstance(count, int):
        out = []
        for i in range(count):
            for getter in (lambda: coll(i), lambda: coll.Item(i), lambda: coll.ElementAt(i)):
                try:
                    out.append(getter())
                    break
                except Exception:  # noqa: BLE001
                    continue
        return out
    try:
        return list(coll)
    except Exception:  # noqa: BLE001
        return []


def _load_engine() -> Any:
    """The SAP GUI Scripting engine of the running SAP Logon (``GetObject("SAPGUI").GetScriptingEngine``)."""
    if sys.platform != "win32":
        raise NotSupported("SAP GUI Scripting is only available on Windows (SAP GUI for Windows)")
    try:
        import pythoncom  # noqa: PLC0415
        import win32com.client  # noqa: PLC0415
    except ImportError as exc:
        raise NotSupported(_INSTALL_HINT) from exc
    try:
        pythoncom.CoInitialize()  # S_FALSE when this thread is already initialized
    except Exception:  # noqa: BLE001
        pass
    try:
        sapgui = win32com.client.GetObject("SAPGUI")
    except Exception as exc:  # noqa: BLE001 - pywintypes.com_error: no running object
        raise BackendUnavailable(
            "SAP GUI is not running (no 'SAPGUI' object in the running object table): start SAP Logon and "
            f"log on first ({_com_message(exc)})"
        ) from exc
    try:
        eng = sapgui.GetScriptingEngine
    except Exception as exc:  # noqa: BLE001
        raise NotSupported(f"SAP GUI refused the scripting engine ({_com_message(exc)}). {SCRIPTING_HINT}") from exc
    if eng is None:
        raise NotSupported(f"SAP GUI Scripting is disabled on this PC. {SCRIPTING_HINT}")
    return eng


#: The engine loader (tests replace it with a fake COM object graph).
_engine_factory: Callable[[], Any] = _load_engine


def engine() -> Any:
    """The raw ``GuiApplication`` scripting object (for calls this module does not wrap)."""
    return _engine_factory()


# ---------------------------------------------------------------------- session info
@dataclass
class SapSessionInfo:
    """One open SAP GUI session (``sap.sessions()``)."""

    connection: int
    session: int
    system: str
    client: str
    user: str
    transaction: str
    description: str
    busy: bool
    disabled_by_server: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _info_of(ci: int, si: int, conn: Any, sess: Any) -> SapSessionInfo:
    info = _get(sess, "Info")
    return SapSessionInfo(
        connection=ci, session=si,
        system=str(_get(info, "SystemName", "") or ""), client=str(_get(info, "Client", "") or ""),
        user=str(_get(info, "User", "") or ""), transaction=str(_get(info, "Transaction", "") or ""),
        description=str(_get(conn, "Description", "") or ""), busy=bool(_get(sess, "Busy", False)),
    )


def _norm_client(c: Any) -> str:
    s = str(c).strip()
    return s.zfill(3) if s.isdigit() else s.upper()


def _conn_matches(index: int, conn: Any, want: Union[int, str]) -> bool:
    if isinstance(want, int):
        return index == want
    desc = str(_get(conn, "Description", "") or "")
    return desc.lower() == str(want).lower() or fnmatch.fnmatchcase(desc.lower(), str(want).lower())


class SapGui:
    """The SAP GUI Scripting engine: connections and sessions.

    Args:
        engine_obj: The ``GuiApplication`` COM object (default: the running SAP Logon's).
        policy / confirm / audit / desktop: Safety hook, see :func:`attach`.
    """

    def __init__(self, engine_obj: Any = None, *, policy: Any = None, confirm: Any = None, audit: Any = None,
                 desktop: Any = None, default_timeout: float = 30.0, poll_interval: float = 0.1,
                 sleep: Callable[[float], Any] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        warn_experimental("locum.bridges.sap", "a real SAP GUI")
        self.com = engine_obj if engine_obj is not None else engine()
        self.desktop, self.gate = ActionGate.resolve(desktop, policy, confirm, audit, bridge="sap")
        self.default_timeout = float(default_timeout)
        self.poll_interval = float(poll_interval)
        self.sleep = sleep
        self.clock = clock

    def connections(self) -> List[Any]:
        return _items(_get(self.com, "Children"))

    def sessions(self) -> List[SapSessionInfo]:
        """Every session of every connection (connections the server refuses are listed with
        ``disabled_by_server=True`` and session ``-1``)."""
        out: List[SapSessionInfo] = []
        for ci, conn in enumerate(self.connections()):
            if _get(conn, "DisabledByServer", False):
                out.append(SapSessionInfo(ci, -1, "", "", "", "", str(_get(conn, "Description", "") or ""),
                                          False, True))
                continue
            for si, sess in enumerate(_items(_get(conn, "Children"))):
                out.append(_info_of(ci, si, conn, sess))
        return out

    def attach(self, *, system: Optional[str] = None, client: Optional[Union[str, int]] = None,
               connection: Optional[Union[int, str]] = None, session: Optional[int] = None,
               user: Optional[str] = None) -> "SapSession":
        """The one session matching every given filter.

        Args:
            system: System id (``Info.SystemName``, e.g. ``"PRD"``) or the SAP
                Logon entry's description (case-insensitive).
            client: Logon client (``"100"``, ``100``).
            connection: Connection index (``con[n]``) or SAP Logon description (glob).
            session: Session index within its connection (``ses[n]``).
            user: Logon user (case-insensitive).

        Raises:
            BackendUnavailable: SAP Logon is not running.
            NotSupported: scripting is disabled on the PC, or by the server
                (``sapgui/user_scripting``) for every matching connection.
            ElementNotFound: no session matches.
            AmbiguousElement: several match -- narrow the filters (another
                session may hold someone's unsaved work).
        """
        conns = self.connections()
        if not conns:
            raise ElementNotFound("SAP Logon has no open connection: log on to the system first")
        matches: List[Tuple[SapSessionInfo, Any]] = []
        refused: List[str] = []
        seen: List[SapSessionInfo] = []
        for ci, conn in enumerate(conns):
            if connection is not None and not _conn_matches(ci, conn, connection):
                continue
            if _get(conn, "DisabledByServer", False):
                refused.append(str(_get(conn, "Description", "") or f"con[{ci}]"))
                continue
            for si, sess in enumerate(_items(_get(conn, "Children"))):
                if session is not None and si != int(session):
                    continue
                info = _info_of(ci, si, conn, sess)
                seen.append(info)
                if system is not None and str(system).lower() not in (info.system.lower(), info.description.lower()):
                    continue
                if client is not None and _norm_client(client) != _norm_client(info.client):
                    continue
                if user is not None and str(user).lower() != info.user.lower():
                    continue
                matches.append((info, sess))
        want = ", ".join(f"{k}={v!r}" for k, v in (("system", system), ("client", client), ("connection", connection),
                                                   ("session", session), ("user", user)) if v is not None) or "any"
        if not matches:
            if refused and not seen:
                raise NotSupported(
                    f"The SAP server refuses scripting for {', '.join(refused)} (connection.DisabledByServer): "
                    f"sapgui/user_scripting is FALSE there. {SCRIPTING_HINT}")
            listed = "; ".join(f"con[{i.connection}]/ses[{i.session}] {i.system}/{i.client} {i.user} "
                               f"({i.transaction})" for i in seen) or "none"
            raise ElementNotFound(f"No SAP GUI session matches {want}. Open sessions: {listed}")
        if len(matches) > 1:
            listed = "; ".join(f"con[{i.connection}]/ses[{i.session}] {i.system}/{i.client} {i.user} ({i.transaction})"
                               for i, _ in matches)
            raise AmbiguousElement(f"{len(matches)} SAP GUI sessions match {want}: {listed}. "
                                   "Pass connection=/session= to pick one")
        info, sess = matches[0]
        return SapSession(sess, self, info)


def attach(*, system: Optional[str] = None, client: Optional[Union[str, int]] = None,
           connection: Optional[Union[int, str]] = None, session: Optional[int] = None, user: Optional[str] = None,
           policy: Any = None, confirm: Any = None, audit: Any = None, desktop: Any = None,
           default_timeout: float = 30.0) -> "SapSession":
    """Attach to a running, logged-on SAP GUI session (see :meth:`SapGui.attach` for the filters).

    Args:
        policy: A :class:`~locum.llm.safety.Policy` every acting call is checked against.
        confirm: Confirmation callback (default: the policy's).
        audit: :class:`~locum.llm.safety.AuditLog` for the decisions.
        desktop: A :class:`~locum.desktop.Desktop` (its fail-safe is honoured, and the
            SAP window's application name is looked up on it) or a
            :class:`~locum.llm.safety.GuardedDesktop` (whose policy, confirm and
            audit are used unless given).
        default_timeout: Seconds to wait for a busy session after an action.
    """
    gui = SapGui(policy=policy, confirm=confirm, audit=audit, desktop=desktop, default_timeout=default_timeout)
    return gui.attach(system=system, client=client, connection=connection, session=session, user=user)


def sessions() -> List[SapSessionInfo]:
    """Every open SAP GUI session (read-only; never attaches)."""
    return SapGui().sessions()


# ---------------------------------------------------------------------- the session
#: ``btn[11]``, ``tbar[0]``, ``txtX[2,3]``: positional names that identify nothing to a human.
_POSITIONAL = re.compile(r"^\w*\[\d+(?:,\d+)?\]$")


def _meaningful_name(name: str) -> str:
    return "" if _POSITIONAL.match(name or "") else (name or "")


def _relative_id(full: str) -> str:
    """``/app/con[0]/ses[0]/wnd[0]/usr/x`` -> ``wnd[0]/usr/x``."""
    marker = full.find("/ses[")
    if marker >= 0:
        rest = full[marker + 1:]
        slash = rest.find("/")
        return rest[slash + 1:] if slash >= 0 else ""
    return full.lstrip("/")


class SapSession:
    """One SAP GUI session (``GuiSession``). Get it from :func:`attach`."""

    def __init__(self, com: Any, gui: SapGui, info: Optional[SapSessionInfo] = None) -> None:
        self.com = com
        self.gui = gui
        self._info = info

    # ------------------------------------------------------------------ state
    @property
    def gate(self) -> ActionGate:
        return self.gui.gate

    @property
    def busy(self) -> bool:
        return bool(_get(self.com, "Busy", False))

    @property
    def info(self) -> Dict[str, Any]:
        """``system, client, user, language, transaction, program, screen_number``."""
        i = _get(self.com, "Info")
        return {
            "system": str(_get(i, "SystemName", "") or ""), "client": str(_get(i, "Client", "") or ""),
            "user": str(_get(i, "User", "") or ""), "language": str(_get(i, "Language", "") or ""),
            "transaction": str(_get(i, "Transaction", "") or ""), "program": str(_get(i, "Program", "") or ""),
            "screen_number": _get(i, "ScreenNumber", 0),
        }

    @property
    def transaction(self) -> str:
        return self.info["transaction"]

    def wait_ready(self, timeout: Optional[float] = None) -> "SapSession":
        """Wait until the session is no longer ``Busy`` (raises :class:`ActionTimeout`)."""
        t = self.gui.default_timeout if timeout is None else float(timeout)
        if not self.busy:
            return self
        wait_until(lambda: not self.busy, t, self.gui.poll_interval, message=f"SAP session still busy after {t:g}s",
                   ignore_exceptions="transient", sleep=self.gui.sleep, clock=self.gui.clock)
        return self

    # ------------------------------------------------------------------ lookup
    def _find_raw(self, component_id: str) -> Any:
        try:
            com = self.com.findById(component_id)
        except Exception as exc:  # noqa: BLE001 - pywintypes.com_error "control could not be found by id"
            raise ElementNotFound(f"SAP component {component_id!r} not found ({_com_message(exc)})") from exc
        if com is None:
            raise ElementNotFound(f"SAP component {component_id!r} not found")
        return com

    def find(self, component_id: str, timeout: float = 0.0) -> "SapComponent":
        """The component with this id (``findById``), as its typed wrapper.

        ``timeout`` polls for a component that is about to appear (a popup's
        ``wnd[1]/...``); ids are relative to the session (``wnd[0]/usr/txtX``)
        or full (``/app/con[0]/ses[0]/...``).
        """
        if timeout and timeout > 0:
            com = wait_until(lambda: self._find_raw(component_id), float(timeout), self.gui.poll_interval,
                             message=f"SAP component {component_id!r} did not appear within {timeout:g}s",
                             ignore_exceptions=(ElementNotFound,), sleep=self.gui.sleep, clock=self.gui.clock)
        else:
            com = self._find_raw(component_id)
        return wrap(com, self)

    def exists(self, component_id: str) -> bool:
        try:
            self._find_raw(component_id)
        except ElementNotFound:
            return False
        return True

    def windows(self) -> List["SapWindow"]:
        """The session's windows (``wnd[0]`` main window, ``wnd[1]``... popups), front-most last."""
        return [w for w in (wrap(c, self) for c in _items(_get(self.com, "Children"))) if isinstance(w, SapWindow)]

    @property
    def active_window(self) -> "SapWindow":
        w = wrap(self._require(_get(self.com, "ActiveWindow"), "active window"), self)
        return w if isinstance(w, SapWindow) else SapWindow(w.com, self)

    def _require(self, com: Any, what: str) -> Any:
        if com is None:
            raise ElementNotFound(f"The SAP session has no {what}")
        return com

    def find_all(self, selector: Any) -> List["SapComponent"]:
        """Components matching a locum selector over the session's windows
        (``'button[name*="Save" i]'``, ``'edit#wnd[0]/usr/txtX'`` -- see :class:`SapNode` for roles)."""
        from ..selectors import parse  # noqa: PLC0415

        roots = [SapNode(c, self) for c in _items(_get(self.com, "Children"))]
        return [wrap(n.com, self) for n in parse(selector).find_all(roots)]  # type: ignore[attr-defined]

    def tree(self, max_depth: int = 8) -> str:
        """Indented dump of the session's components (roles, names, ids)."""
        from ..selectors import tree_text  # noqa: PLC0415

        return tree_text([SapNode(c, self) for c in _items(_get(self.com, "Children"))], max_depth=max_depth)

    # ------------------------------------------------------------------ status
    @property
    def statusbar(self) -> "SapStatusbar":
        sb = wrap(self._find_raw("wnd[0]/sbar"), self)
        return sb if isinstance(sb, SapStatusbar) else SapStatusbar(sb.com, self)

    def check_status(self, raise_on: str = "EA") -> "SapStatusbar":
        """The status bar; raises :class:`SapError` when its message type is in ``raise_on``
        (``E`` error, ``A`` abort; add ``W`` to stop on warnings)."""
        sb = self.statusbar
        if sb.message_type and sb.message_type in raise_on.upper():
            raise SapError(sb.text, message_type=sb.message_type, message_id=sb.message_id,
                           message_number=sb.message_number)
        return sb

    # ------------------------------------------------------------------ actions
    def _ctx(self, component: Optional["SapComponent"] = None, *, activates: bool = True,
             window: Optional[Any] = None) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {}
        win = window if window is not None else _get(self.com, "ActiveWindow")
        title = str(_get(win, "Text", "") or "")
        app = self._app(win)
        if app:
            ctx["app"] = app
            ctx["target_app"] = app
        if title:
            ctx["window_title"] = title
            ctx["target_window"] = title
        if component is not None:
            ctx["target_node"] = SapNode(component.com, self)
            ctx["selector"] = component.id
            label = component.label
            if label:
                ctx["target"] = label
            elif activates:
                ctx["target_unknown"] = True
        return ctx

    def _app(self, win: Any) -> str:
        desktop = self.gui.desktop
        handle = _get(win, "Handle")
        if desktop is not None and handle:
            try:
                for w in desktop.backend.list_windows():
                    if w.handle == handle and w.app:
                        return str(w.app)
            except Exception:  # noqa: BLE001
                pass
        return "saplogon.exe"

    def _run(self, tool: str, args: Dict[str, Any], ctx: Dict[str, Any], description: str, action: Callable[[], Any],
             *, secret: bool = False, wait: bool = True) -> bool:
        """Gate, run ``action`` (COM errors -> :class:`BackendError`), then wait until not busy."""
        if not self.gate.allow(tool, args, ctx, description, secret=secret):
            return False
        try:
            action()
        except LocumError:
            raise
        except Exception as exc:  # noqa: BLE001 - pywintypes.com_error
            raise BackendError(f"SAP GUI refused to {description} ({_com_message(exc)}). If every write fails, "
                               "the server may allow read-only scripting (sapgui/user_scripting_set_readonly)") \
                from exc
        if wait:
            self.wait_ready()
        return True

    def send_vkey(self, vkey: int, window: str = "wnd[0]") -> "SapStatusbar":
        """Send a virtual key to a window (``0`` Enter, ``3`` F3 Back, ``8`` F8 Execute,
        ``11`` Ctrl+S Save, ``12`` F12 Cancel; see :data:`VKEYS`). Returns the status bar."""
        vkey = int(vkey)
        win = self._find_raw(window)
        combo = vkey_combo(vkey)
        ctx = self._ctx(window=win)
        ctx["keys"] = combo
        self._run("press_keys", {"keys": combo, "vkey": vkey, "window": window}, ctx,
                  f"press {combo} (vkey {vkey}) in {ctx.get('target_window') or window!r}",
                  lambda: win.sendVKey(vkey))
        return self.statusbar

    def start_transaction(self, tcode: str) -> "SapStatusbar":
        """Leave the current transaction and start ``tcode`` (like ``/n<tcode>``). Returns the status bar."""
        code = str(tcode).strip()
        if code.lower().startswith(("/n", "/o")):
            code = code[2:]
        if not code:
            raise ValueError("start_transaction needs a transaction code")
        ctx = self._ctx()
        ctx["target"] = f"transaction {code}"

        def go() -> None:
            try:
                self.com.StartTransaction(code)
            except Exception:  # noqa: BLE001 - older releases: type it into the command field
                self._find_raw("wnd[0]/tbar[0]/okcd").Text = f"/n{code}"
                self._find_raw("wnd[0]").sendVKey(0)

        self._run("sap_start_transaction", {"tcode": code}, ctx, f"start transaction {code}", go)
        return self.statusbar

    def press(self, component_id: str) -> "SapComponent":
        """``find(id).press()``."""
        comp = self.find(component_id)
        press = getattr(comp, "press", None)
        if press is None:
            raise NotSupported(f"SAP component {component_id!r} ({comp.type}) cannot be pressed")
        return press()

    def set_text(self, component_id: str, text: str) -> "SapComponent":
        """``find(id).set_text(text)``."""
        comp = self.find(component_id)
        setter = getattr(comp, "set_text", None)
        if setter is None:
            raise NotSupported(f"SAP component {component_id!r} ({comp.type}) has no text to set")
        return setter(text)

    def __repr__(self) -> str:
        i = self._info
        where = f"con[{i.connection}]/ses[{i.session}] {i.system}/{i.client}" if i else ""
        return f"<SapSession {where}>"


# ---------------------------------------------------------------------- components
class SapComponent:
    """Any SAP GUI component (``GuiVComponent``). Typed subclasses add their actions."""

    def __init__(self, com: Any, session: SapSession) -> None:
        self.com = com
        self.session = session

    @property
    def id(self) -> str:
        return _relative_id(str(_get(self.com, "Id", "") or ""))

    @property
    def type(self) -> str:
        return str(_get(self.com, "Type", "") or "")

    @property
    def sub_type(self) -> str:
        return str(_get(self.com, "SubType", "") or "")

    @property
    def name(self) -> str:
        return str(_get(self.com, "Name", "") or "")

    @property
    def text(self) -> str:
        return str(_get(self.com, "Text", "") or "")

    @property
    def tooltip(self) -> str:
        return str(_get(self.com, "Tooltip", "") or _get(self.com, "DefaultTooltip", "") or "")

    @property
    def changeable(self) -> bool:
        return bool(_get(self.com, "Changeable", True))

    @property
    def bounds(self) -> Optional[Rect]:
        x, y = _get(self.com, "ScreenLeft"), _get(self.com, "ScreenTop")
        w, h = _get(self.com, "Width"), _get(self.com, "Height")
        if not all(isinstance(v, int) for v in (x, y, w, h)):
            return None
        return Rect(x, y, w, h)

    @property
    def label(self) -> str:
        """What a human reads as this control's name: its text, else tooltip, else its technical
        name unless that is only a position (``btn[9]``: an icon button with no tooltip has no label)."""
        return (self.text or self.tooltip or _meaningful_name(self.name)).strip()

    def children(self) -> List["SapComponent"]:
        return [wrap(c, self.session) for c in _items(_get(self.com, "Children"))]

    def find(self, relative_id: str) -> "SapComponent":
        try:
            com = self.com.findById(relative_id)
        except Exception as exc:  # noqa: BLE001
            raise ElementNotFound(f"SAP component {relative_id!r} not found under {self.id!r} "
                                  f"({_com_message(exc)})") from exc
        return wrap(com, self.session)

    def set_focus(self) -> "SapComponent":
        self.session._run("focus", {"target": self.id}, self.session._ctx(self, activates=False),
                          f"focus {self.label or self.id!r}", lambda: self.com.SetFocus(), wait=False)
        return self

    def _act(self, tool: str, verb: str, action: Callable[[], Any], *, activates: bool = True,
             args: Optional[Dict[str, Any]] = None, secret: bool = False) -> "SapComponent":
        a = {"target": self.id}
        a.update(args or {})
        self.session._run(tool, a, self.session._ctx(self, activates=activates),
                          f"{verb} {self.label or self.id!r}", action, secret=secret)
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "type": self.type, "name": self.name, "text": self.text, "tooltip": self.tooltip}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id!r} {self.label!r}>"


class SapWindow(SapComponent):
    """``GuiMainWindow`` / ``GuiModalWindow``."""

    @property
    def title(self) -> str:
        return self.text

    @property
    def handle(self) -> Any:
        return _get(self.com, "Handle")

    def send_vkey(self, vkey: int) -> "SapStatusbar":
        return self.session.send_vkey(vkey, self.id or "wnd[0]")

    def close(self) -> "SapWindow":
        """Close this window (a popup's X; on ``wnd[0]`` SAP may ask to log off)."""
        return self._act("close_window", "close window", lambda: self.com.Close())  # type: ignore[return-value]


class SapTextField(SapComponent):
    """``GuiTextField``, ``GuiCTextField`` (with F4 help), ``GuiPasswordField``, ``GuiOkCodeField``."""

    @property
    def text(self) -> str:
        return str(_get(self.com, "Text", "") or "")

    @text.setter
    def text(self, value: str) -> None:
        self.set_text(value)

    @property
    def is_password(self) -> bool:
        return self.type == "GuiPasswordField"

    @property
    def max_length(self) -> Optional[int]:
        m = _get(self.com, "MaxLength")
        return m if isinstance(m, int) and m > 0 else None

    @property
    def label(self) -> str:
        left = _get(self.com, "LeftLabel")
        lt = str(_get(left, "Text", "") or "") if left is not None else ""
        return (lt or self.tooltip or _meaningful_name(self.name)).strip()

    def set_text(self, text: str) -> "SapTextField":
        """Set the field's text. Raises ``ValueError`` (nothing written) when it is longer than the
        field (``MaxLength``: SAP would cut it off silently)."""
        value = str(text)
        limit = self.max_length
        if limit is not None and len(value) > limit:
            raise ValueError(f"{len(value)} characters do not fit SAP field {self.id!r} (MaxLength {limit}); "
                             "SAP would truncate the value")
        if not self.changeable:
            raise ElementNotFound(f"SAP field {self.id!r} is not changeable (display mode or protected)")

        def write() -> None:
            self.com.Text = value

        self._act("set_value", "set text of", write, activates=False,
                  args={"text": value}, secret=self.is_password)
        return self

    def caret_to_end(self) -> "SapTextField":
        try:
            self.com.CaretPosition = len(self.text)
        except Exception:  # noqa: BLE001
            pass
        return self


class SapButton(SapComponent):
    """``GuiButton`` (toolbar and screen buttons)."""

    def press(self) -> "SapButton":
        return self._act("click", "press", lambda: self.com.press())  # type: ignore[return-value]


class SapComboBox(SapComponent):
    """``GuiComboBox``: ``key`` is the technical value, ``value`` the shown text."""

    @property
    def key(self) -> str:
        return str(_get(self.com, "Key", "") or "")

    @key.setter
    def key(self, value: str) -> None:
        self.set_key(value)

    @property
    def value(self) -> str:
        return str(_get(self.com, "Value", "") or "")

    @property
    def entries(self) -> List[Tuple[str, str]]:
        """``[(key, text), ...]``."""
        return [(str(_get(e, "Key", "") or ""), str(_get(e, "Value", "") or ""))
                for e in _items(_get(self.com, "Entries"))]

    def set_key(self, key: str) -> "SapComboBox":
        keys = [k for k, _ in self.entries]
        if keys and str(key) not in keys:
            raise ValueError(f"{key!r} is not an entry of SAP combo box {self.id!r}; keys: {', '.join(keys)}")

        def write() -> None:
            self.com.Key = str(key)

        self._act("select", f"select {key!r} in", write, args={"key": str(key)})
        return self


class SapCheckBox(SapComponent):
    """``GuiCheckBox``."""

    @property
    def selected(self) -> bool:
        return bool(_get(self.com, "Selected", False))

    @selected.setter
    def selected(self, value: bool) -> None:
        self.set_selected(value)

    def set_selected(self, value: bool) -> "SapCheckBox":
        if self.selected == bool(value):
            return self

        def write() -> None:
            self.com.Selected = bool(value)

        self._act("toggle", "check" if value else "uncheck", write)
        return self

    def check(self) -> "SapCheckBox":
        return self.set_selected(True)

    def uncheck(self) -> "SapCheckBox":
        return self.set_selected(False)


class SapRadioButton(SapComponent):
    """``GuiRadioButton``."""

    @property
    def selected(self) -> bool:
        return bool(_get(self.com, "Selected", False))

    def select(self) -> "SapRadioButton":
        return self._act("select", "select", lambda: self.com.Select())  # type: ignore[return-value]


class SapTab(SapComponent):
    """``GuiTab`` (one tab of a ``GuiTabStrip``)."""

    def select(self) -> "SapTab":
        return self._act("select", "select tab", lambda: self.com.Select())  # type: ignore[return-value]


class SapStatusbar(SapComponent):
    """``GuiStatusbar`` (``wnd[0]/sbar``). ``message_type``: ``S`` success, ``W`` warning,
    ``E`` error, ``A`` abort, ``I`` information, ``""`` none."""

    @property
    def message_type(self) -> str:
        return str(_get(self.com, "MessageType", "") or "").upper()

    @property
    def message_id(self) -> str:
        return str(_get(self.com, "MessageId", "") or "").strip()

    @property
    def message_number(self) -> str:
        return str(_get(self.com, "MessageNumber", "") or "").strip()

    @property
    def is_error(self) -> bool:
        return self.message_type in ("E", "A")

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update(message_type=self.message_type, message_id=self.message_id, message_number=self.message_number)
        return d


class SapTableControl(SapComponent):
    """``GuiTableControl`` (classic dynpro table). Rows are **absolute** (0 = first row of the
    table); rows outside the visible part are scrolled to first, which is a server round trip
    (the screen's PAI runs, like scrolling by hand)."""

    @property
    def row_count(self) -> int:
        return int(_get(self.com, "RowCount", 0) or 0)

    @property
    def visible_row_count(self) -> int:
        return int(_get(self.com, "VisibleRowCount", 0) or 0)

    @property
    def columns(self) -> List[Tuple[str, str]]:
        """``[(name, title), ...]`` in display order."""
        return [(str(_get(c, "Name", "") or ""), str(_get(c, "Title", "") or ""))
                for c in _items(_get(self.com, "Columns"))]

    def _column(self, col: Union[int, str]) -> int:
        if isinstance(col, int):
            return col
        cols = self.columns
        for i, (name, title) in enumerate(cols):
            if str(col) == name or str(col) == title:
                return i
        low = str(col).lower()
        for i, (name, title) in enumerate(cols):
            if low in (name.lower(), title.lower()):
                return i
        raise ElementNotFound(f"Column {col!r} not in SAP table {self.id!r}; columns: "
                              + ", ".join(t or n for n, t in cols))

    def _refind(self) -> None:
        self.com = self.session._find_raw(self.id)

    def _top(self) -> int:
        sb = _get(self.com, "VerticalScrollbar")
        return int(_get(sb, "Position", 0) or 0) if sb is not None else 0

    def scroll_to(self, row: int) -> "SapTableControl":
        """Make ``row`` the first visible row."""
        sb = _get(self.com, "VerticalScrollbar")
        if sb is None:
            raise NotSupported(f"SAP table {self.id!r} has no vertical scrollbar")

        def move() -> None:
            sb.Position = int(row)

        self._act("scroll", f"scroll to row {row} of", move, activates=False, args={"row": int(row)})
        self._refind()  # the table object is rebuilt after the round trip
        return self

    def _visible_row(self, row: int) -> int:
        if row < 0 or (self.row_count and row >= self.row_count):
            raise IndexError(f"Row {row} outside SAP table {self.id!r} ({self.row_count} rows)")
        top, vis = self._top(), self.visible_row_count
        if vis and not (top <= row < top + vis):
            self.scroll_to(row)
            top = self._top()
        return row - top

    def cell(self, row: int, col: Union[int, str]) -> SapComponent:
        """The cell component (a text field, check box, ...) at absolute ``row``."""
        c = self._column(col)
        r = self._visible_row(int(row))
        try:
            com = self.com.GetCell(r, c)
        except Exception as exc:  # noqa: BLE001
            raise ElementNotFound(f"No cell ({row}, {col!r}) in SAP table {self.id!r} ({_com_message(exc)})") from exc
        return wrap(com, self.session)

    def read(self, row: int, col: Union[int, str]) -> str:
        return self.cell(row, col).text

    def write(self, row: int, col: Union[int, str], text: str) -> "SapTableControl":
        cell = self.cell(row, col)
        setter = getattr(cell, "set_text", None)
        if setter is None:
            raise NotSupported(f"Cell ({row}, {col!r}) of {self.id!r} is a {cell.type}, not a text field")
        setter(text)
        return self


class SapGridView(SapComponent):
    """ALV grid (``GuiShell`` with ``SubType == "GridView"``). Columns are technical names
    (``"BUKRS"``); rows are absolute. Reading loads rows the grid has not fetched yet by moving
    ``FirstVisibleRow`` (client side, no PAI)."""

    @property
    def row_count(self) -> int:
        return int(_get(self.com, "RowCount", 0) or 0)

    @property
    def column_count(self) -> int:
        return int(_get(self.com, "ColumnCount", 0) or 0)

    @property
    def columns(self) -> List[str]:
        return [str(c) for c in _items(_get(self.com, "ColumnOrder"))]

    def column_title(self, col: str) -> str:
        try:
            return str(self.com.GetDisplayedColumnTitle(col))
        except Exception:  # noqa: BLE001
            return ""

    def _load(self, row: int) -> None:
        vis = int(_get(self.com, "VisibleRowCount", 0) or 0)
        first = int(_get(self.com, "FirstVisibleRow", 0) or 0)
        if vis and not (first <= row < first + vis):
            try:
                self.com.FirstVisibleRow = int(row)
            except Exception:  # noqa: BLE001
                pass

    def cell(self, row: int, col: str) -> str:
        """The cell's text."""
        if row < 0 or row >= self.row_count:
            raise IndexError(f"Row {row} outside ALV grid {self.id!r} ({self.row_count} rows)")
        self._load(int(row))
        try:
            return str(self.com.GetCellValue(int(row), str(col)))
        except Exception as exc:  # noqa: BLE001
            raise ElementNotFound(f"No cell ({row}, {col!r}) in ALV grid {self.id!r} ({_com_message(exc)})") from exc

    read = cell

    def rows(self, columns: Optional[Sequence[str]] = None, *, limit: Optional[int] = None) -> List[Dict[str, str]]:
        """Every row as ``{column: text}`` (``columns``: a subset, default all)."""
        cols = list(columns) if columns is not None else self.columns
        n = self.row_count if limit is None else min(self.row_count, int(limit))
        return [{c: self.cell(r, c) for c in cols} for r in range(n)]

    def write(self, row: int, col: str, text: str) -> "SapGridView":
        """Change an editable cell (``ModifyCell``)."""
        self._load(int(row))
        self._act("set_value", f"set cell ({row}, {col}) of", lambda: self.com.ModifyCell(int(row), str(col), str(text)),
                  activates=False, args={"row": int(row), "column": str(col), "text": str(text)})
        return self

    def set_current_cell(self, row: int, col: str) -> "SapGridView":
        self._load(int(row))
        self._act("focus", f"select cell ({row}, {col}) of", lambda: self.com.SetCurrentCell(int(row), str(col)),
                  activates=False, args={"row": int(row), "column": str(col)})
        return self

    def select_rows(self, rows: Sequence[int]) -> "SapGridView":
        spec = ",".join(str(int(r)) for r in rows)

        def write() -> None:
            self.com.SelectedRows = spec

        self._act("select", f"select rows {spec} of", write, activates=False, args={"rows": spec})
        return self

    def double_click_cell(self, row: int, col: str) -> "SapGridView":
        self._load(int(row))

        def go() -> None:
            self.com.SetCurrentCell(int(row), str(col))
            self.com.DoubleClickCurrentCell()

        self._act("double_click", f"double-click cell ({row}, {col}) of", go, args={"row": int(row), "column": str(col)})
        return self

    def press_toolbar_button(self, button_id: str) -> "SapGridView":
        """Press a grid toolbar button by function code (``"&MB_EXPORT"``, ``"&PC"``...)."""
        self._act("click", f"press toolbar button {button_id!r} of", lambda: self.com.PressToolbarButton(button_id),
                  args={"button": button_id})
        return self


_TYPES: Dict[str, type] = {
    "GuiTextField": SapTextField, "GuiCTextField": SapTextField, "GuiPasswordField": SapTextField,
    "GuiOkCodeField": SapTextField, "GuiButton": SapButton, "GuiComboBox": SapComboBox,
    "GuiCheckBox": SapCheckBox, "GuiRadioButton": SapRadioButton, "GuiTab": SapTab,
    "GuiStatusbar": SapStatusbar, "GuiTableControl": SapTableControl, "GuiMainWindow": SapWindow,
    "GuiModalWindow": SapWindow, "GuiFrameWindow": SapWindow,
}


def wrap(com: Any, session: SapSession) -> SapComponent:
    """The typed wrapper for a raw SAP GUI COM component."""
    kind = str(_get(com, "Type", "") or "")
    if kind == "GuiShell" and str(_get(com, "SubType", "") or "") == "GridView":
        return SapGridView(com, session)
    return _TYPES.get(kind, SapComponent)(com, session)


# ---------------------------------------------------------------------- accessibility adapter
_ROLES: Dict[str, str] = {
    "GuiMainWindow": "window", "GuiModalWindow": "dialog", "GuiFrameWindow": "window",
    "GuiButton": "button", "GuiTextField": "edit", "GuiCTextField": "edit", "GuiPasswordField": "edit",
    "GuiOkCodeField": "edit", "GuiComboBox": "combobox", "GuiCheckBox": "checkbox",
    "GuiRadioButton": "radiobutton", "GuiLabel": "text", "GuiTab": "tab", "GuiTabStrip": "tablist",
    "GuiTableControl": "table", "GuiStatusbar": "statusbar", "GuiTitlebar": "titlebar",
    "GuiToolbar": "toolbar", "GuiMenubar": "menubar", "GuiMenu": "menuitem", "GuiUserArea": "pane",
    "GuiBox": "group", "GuiSimpleContainer": "group", "GuiScrollContainer": "pane",
    "GuiContainerShell": "pane", "GuiCustomControl": "pane", "GuiSplitterContainer": "pane",
}
_TOP_TYPES = frozenset({"GuiSession", "GuiConnection", "GuiApplication"})


class SapNode(AccessibleNode):
    """A SAP GUI component as a locum :class:`~locum.backends.base.AccessibleNode`.

    It lets locum selectors (``session.find_all``) and policy allow rules
    (``Policy(allow=['button[name*="Save"]'])``) work on SAP screens. Roles:
    ``GuiButton`` -> ``button``, text fields -> ``edit``, ``GuiComboBox`` ->
    ``combobox``, ``GuiLabel`` -> ``text``, ``GuiTableControl`` / ALV grid ->
    ``table``, windows -> ``window``/``dialog``. ``automation_id`` is the
    session-relative id, ``class_name`` the SAP type; ``attributes()`` has
    ``sap_id``, ``sap_type``, ``sap_subtype`` and ``sap_name``.
    """

    def __init__(self, com: Any, session: Optional[SapSession] = None) -> None:
        self.com = com
        self.session = session
        self._kids: Optional[List["SapNode"]] = None

    @property
    def _type(self) -> str:
        return str(_get(self.com, "Type", "") or "")

    @property
    def role(self) -> str:
        t = self._type
        if t == "GuiShell":
            return "table" if str(_get(self.com, "SubType", "") or "") == "GridView" else "pane"
        return _ROLES.get(t, t[3:].lower() if t.startswith("Gui") else (t.lower() or "unknown"))

    @property
    def native_role(self) -> str:
        return self._type

    @property
    def _is_input(self) -> bool:
        return self._type in ("GuiTextField", "GuiCTextField", "GuiPasswordField", "GuiOkCodeField", "GuiComboBox")

    @property
    def name(self) -> str:
        if self._is_input:
            left = _get(self.com, "LeftLabel")
            lt = str(_get(left, "Text", "") or "") if left is not None else ""
            return (lt or str(_get(self.com, "Tooltip", "") or "")
                    or _meaningful_name(str(_get(self.com, "Name", "") or ""))).strip()
        text = str(_get(self.com, "Text", "") or "").strip()
        return text or str(_get(self.com, "Tooltip", "") or "").strip()             or _meaningful_name(str(_get(self.com, "Name", "") or ""))

    @property
    def value(self) -> Optional[str]:
        if self._type == "GuiPasswordField":
            return None
        if self._is_input:
            return str(_get(self.com, "Text", "") or "")
        return None

    @property
    def automation_id(self) -> str:
        return _relative_id(str(_get(self.com, "Id", "") or ""))

    @property
    def class_name(self) -> str:
        return self._type

    @property
    def description(self) -> str:
        return str(_get(self.com, "Tooltip", "") or "")

    @property
    def bounds(self) -> Optional[Rect]:
        x, y = _get(self.com, "ScreenLeft"), _get(self.com, "ScreenTop")
        w, h = _get(self.com, "Width"), _get(self.com, "Height")
        if not all(isinstance(v, int) for v in (x, y, w, h)):
            return None
        return Rect(x, y, w, h)

    @property
    def states(self) -> frozenset:
        st = {"visible"}
        changeable = bool(_get(self.com, "Changeable", True))
        if self._is_input:
            st.update({"enabled", "editable"} if changeable else {"enabled", "readonly"})
        else:
            st.add("enabled" if changeable else "disabled")
        if self._type == "GuiPasswordField":
            st.add("password")
        if self._type in ("GuiCheckBox", "GuiRadioButton"):
            st.add("checked" if _get(self.com, "Selected", False) else "unchecked")
        return frozenset(st)

    @property
    def handle(self) -> Any:
        return _get(self.com, "Handle")

    def attributes(self) -> Dict[str, Any]:
        return {"sap_id": self.automation_id, "sap_type": self._type,
                "sap_subtype": str(_get(self.com, "SubType", "") or ""),
                "sap_name": str(_get(self.com, "Name", "") or "")}

    def children(self) -> Sequence["SapNode"]:
        if self._kids is None:
            kids = _get(self.com, "Children") if _get(self.com, "ContainerType", True) else None
            self._kids = [SapNode(c, self.session) for c in _items(kids)]
            for k in self._kids:
                k._parent = self  # type: ignore[attr-defined]
        return self._kids

    @property
    def parent(self) -> Optional["SapNode"]:
        cached = getattr(self, "_parent", None)
        if cached is not None:
            return cached
        p = _get(self.com, "Parent")
        if p is None or str(_get(p, "Type", "") or "") in _TOP_TYPES:
            return None
        return SapNode(p, self.session)

    def same_as(self, other: "AccessibleNode") -> bool:
        if not isinstance(other, SapNode):
            return False
        a, b = _get(self.com, "Id"), _get(other.com, "Id")
        return bool(a) and a == b

    def invoke(self) -> bool:
        if self._type != "GuiButton":
            return False
        self.com.press()
        return True


def iter_tree(component: SapComponent) -> Iterator[SapComponent]:
    """Depth-first walk over a component and its descendants."""
    stack = [component]
    while stack:
        c = stack.pop()
        yield c
        stack.extend(reversed(c.children()))
