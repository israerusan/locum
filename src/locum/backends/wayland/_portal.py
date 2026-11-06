"""xdg-desktop-portal plumbing for the Wayland backend.

Three layers, so everything above the socket can be tested without a session bus:

* :class:`PortalBus` -- the minimal D-Bus surface the backend needs: a method
  call that returns the reply body, and signal watches. :class:`JeepneyBus`
  implements it with ``jeepney`` (pure Python, imported lazily); the tests use
  a fake that records calls and emits ``Response`` signals.
* :class:`PortalClient` -- the portal *Request/Response* pattern: every
  asynchronous portal method takes a ``handle_token``, returns the object path
  of a ``org.freedesktop.portal.Request`` and later emits ``Response(u, a{sv})``
  on that path. The client subscribes to the *predicted* request path
  (``/org/freedesktop/portal/desktop/request/SENDER/TOKEN``) **before** calling,
  so a fast response can never be missed, and re-subscribes if an old portal
  returns a different path.
* :class:`TokenStore` -- the RemoteDesktop ``restore_token`` (``persist_mode=2``)
  kept in a private per-user state file, so the approval dialog is shown once.

Variants (``v`` / ``a{sv}`` values) use jeepney's convention everywhere: a
``(signature, value)`` tuple, e.g. ``{"types": ("u", 3)}``.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from typing import Any, Dict, Optional, Sequence, Tuple

from ...errors import BackendError, BackendUnavailable, PermissionDenied

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
SCREEN_CAST = "org.freedesktop.portal.ScreenCast"
SCREENSHOT = "org.freedesktop.portal.Screenshot"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"
PROPERTIES = "org.freedesktop.DBus.Properties"

# RemoteDesktop device types (SelectDevices "types", Start "devices").
DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2
DEVICE_TOUCHSCREEN = 4
# ScreenCast source types.
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
SOURCE_VIRTUAL = 4
# persist_mode: 0 = do not persist, 1 = while the app runs, 2 = until explicitly revoked.
PERSIST_UNTIL_REVOKED = 2
# Response codes of org.freedesktop.portal.Request::Response.
RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1
RESPONSE_OTHER = 2
# Pointer axes of NotifyPointerAxisDiscrete.
AXIS_VERTICAL = 0
AXIS_HORIZONTAL = 1
# Key / button states.
STATE_RELEASED = 0
STATE_PRESSED = 1

INSTALL_HINT = "install it with `pip install locum[wayland]` (or `pip install jeepney`)"


class PortalError(BackendError):
    """A D-Bus error reply from the portal (``name`` is the D-Bus error name)."""

    def __init__(self, message: str, name: str = "") -> None:
        super().__init__(message)
        self.name = name


# =============================================================================== bus
class SignalWatch:
    """A subscription to one signal on one object path."""

    def wait(self, timeout: Optional[float]) -> Tuple[Any, ...]:
        """Block until the signal arrives; return its body. Raises :class:`TimeoutError`."""
        raise NotImplementedError

    def poll(self) -> Optional[Tuple[Any, ...]]:
        """The body of a signal that already arrived (without blocking), else ``None``."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class PortalBus:
    """The D-Bus operations the Wayland backend uses (see module docstring)."""

    unique_name: str = ""

    def call(self, path: str, interface: str, member: str, signature: str, body: Sequence[Any],
             *, destination: str = PORTAL_BUS_NAME, timeout: Optional[float] = 10.0) -> Tuple[Any, ...]:
        """Call a method and return the reply body; a D-Bus error raises :class:`PortalError`."""
        raise NotImplementedError

    def watch(self, path: str, interface: str, member: str) -> SignalWatch:
        """Subscribe to ``interface.member`` signals emitted on ``path`` by the portal."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def _load_jeepney() -> Any:
    try:
        import jeepney  # type: ignore
        from jeepney.io import blocking  # type: ignore  # noqa: F401
    except ImportError as exc:
        raise BackendUnavailable(f"The Wayland backend needs jeepney ({exc}); {INSTALL_HINT}") from exc
    return jeepney


class _JeepneyWatch(SignalWatch):
    def __init__(self, bus: "JeepneyBus", rule: Any) -> None:
        self._bus = bus
        self._rule = rule
        self._handle = bus._conn.filter(rule, bufsize=16)
        self._queue = self._handle.queue
        self._closed = False
        bus._add_match(rule)

    def wait(self, timeout: Optional[float]) -> Tuple[Any, ...]:
        with self._bus._lock:
            if self._queue:
                return tuple(self._queue.popleft().body)
            # Reads the socket, routing other messages to their filters, until one lands here.
            msg = self._bus._conn.recv_until_filtered(self._queue, timeout=timeout)
            return tuple(msg.body)

    def poll(self) -> Optional[Tuple[Any, ...]]:
        if self._queue:
            return tuple(self._queue.popleft().body)
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.close()
        finally:
            self._bus._remove_match(self._rule)


class JeepneyBus(PortalBus):
    """:class:`PortalBus` over a blocking ``jeepney`` session-bus connection.

    jeepney's blocking connection is not thread-safe; every operation holds one lock.
    Signals that arrive while waiting for a method reply are routed into the
    matching watch queues by jeepney itself.
    """

    def __init__(self, connection: Any = None) -> None:
        self._jeepney = _load_jeepney()
        if connection is None:
            from jeepney.io.blocking import open_dbus_connection  # type: ignore

            try:
                connection = open_dbus_connection(bus="SESSION")
            except Exception as exc:
                raise BackendUnavailable(f"Cannot connect to the D-Bus session bus: {exc}") from exc
        self._conn = connection
        self._lock = threading.RLock()
        self.unique_name = str(getattr(connection, "unique_name", "") or "")

    def _send(self, msg: Any, timeout: Optional[float]) -> Tuple[Any, ...]:
        jp = self._jeepney
        with self._lock:
            reply = self._conn.send_and_get_reply(msg, timeout=timeout)
        if reply.header.message_type == jp.MessageType.error:
            name = str(reply.header.fields.get(jp.HeaderFields.error_name, "") or "")
            detail = reply.body[0] if reply.body else ""
            raise PortalError(f"{name}: {detail}" if detail else name or "D-Bus error", name)
        return tuple(reply.body)

    def call(self, path: str, interface: str, member: str, signature: str, body: Sequence[Any],
             *, destination: str = PORTAL_BUS_NAME, timeout: Optional[float] = 10.0) -> Tuple[Any, ...]:
        jp = self._jeepney
        addr = jp.DBusAddress(path, bus_name=destination, interface=interface)
        msg = jp.new_method_call(addr, member, signature or None, tuple(body))
        try:
            return self._send(msg, timeout)
        except TimeoutError as exc:
            raise BackendError(f"D-Bus call {interface}.{member} timed out") from exc
        except PortalError:
            raise
        except OSError as exc:
            raise BackendError(f"D-Bus connection failed during {interface}.{member}: {exc}") from exc

    def _bus_call(self, member: str, rule: Any) -> None:
        jp = self._jeepney
        msg = getattr(jp.message_bus, member)(rule)
        try:
            self._send(msg, 5.0)
        except Exception:
            if member == "AddMatch":
                raise

    def _add_match(self, rule: Any) -> None:
        self._bus_call("AddMatch", rule)

    def _remove_match(self, rule: Any) -> None:
        self._bus_call("RemoveMatch", rule)

    def watch(self, path: str, interface: str, member: str) -> SignalWatch:
        jp = self._jeepney
        rule = jp.MatchRule(type="signal", interface=interface, member=member, path=path)
        with self._lock:
            return _JeepneyWatch(self, rule)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ============================================================================ helpers
def variant_value(v: Any) -> Any:
    """Unwrap a jeepney variant ``(signature, value)``; other values pass through."""
    if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str):
        return v[1]
    return v


def unwrap_vardict(d: Any) -> Dict[str, Any]:
    """``a{sv}`` (dict of variants) -> plain dict."""
    if not isinstance(d, dict):
        return {}
    return {str(k): variant_value(v) for k, v in d.items()}


def sender_path_part(unique_name: str) -> str:
    """``":1.42"`` -> ``"1_42"`` (how portals spell the caller in request/session paths)."""
    return unique_name.lstrip(":").replace(".", "_")


def new_token(prefix: str = "locum") -> str:
    """A fresh ``handle_token`` / ``session_handle_token`` (letters, digits and ``_`` only)."""
    return f"{prefix}_{secrets.token_hex(8)}"


# ============================================================================= client
class PortalClient:
    """Portal method calls, including the Request/Response round trip."""

    def __init__(self, bus: PortalBus) -> None:
        self.bus = bus

    # ------------------------------------------------------------------ paths
    def request_path(self, token: str) -> str:
        return f"{PORTAL_PATH}/request/{sender_path_part(self.bus.unique_name)}/{token}"

    def session_path(self, token: str) -> str:
        return f"{PORTAL_PATH}/session/{sender_path_part(self.bus.unique_name)}/{token}"

    # -------------------------------------------------------------- properties
    def version(self, interface: str) -> int:
        """The ``version`` property of a portal interface (0: unavailable / unknown)."""
        try:
            (v,) = self.bus.call(PORTAL_PATH, PROPERTIES, "Get", "ss", (interface, "version"), timeout=5.0)
        except (BackendError, ValueError):
            return 0
        try:
            return int(variant_value(v))
        except (TypeError, ValueError):
            return 0

    def property(self, interface: str, name: str, default: Any = None) -> Any:
        try:
            (v,) = self.bus.call(PORTAL_PATH, PROPERTIES, "Get", "ss", (interface, name), timeout=5.0)
        except (BackendError, ValueError):
            return default
        return variant_value(v)

    # ---------------------------------------------------------------- requests
    def request(self, interface: str, method: str, signature: str, args: Sequence[Any],
                options: Optional[Dict[str, Tuple[str, Any]]] = None, *,
                timeout: Optional[float] = 60.0) -> Dict[str, Any]:
        """Call an asynchronous portal method and wait for its ``Response``.

        ``signature`` covers ``args`` followed by the trailing ``a{sv}`` options
        (a ``handle_token`` is added to them). Returns the unwrapped ``results``.

        Raises:
            PermissionDenied: the user dismissed / denied the dialog (response 1).
            BackendError: the portal reported another failure (response 2), or no
                response came within ``timeout`` (the request is then closed).
        """
        opts: Dict[str, Tuple[str, Any]] = dict(options or {})
        token = new_token()
        opts["handle_token"] = ("s", token)
        predicted = self.request_path(token)
        watch = self.bus.watch(predicted, REQUEST, "Response")  # subscribe *before* calling
        handle = predicted
        try:
            reply = self.bus.call(PORTAL_PATH, interface, method, signature, (*args, opts))
            if reply and isinstance(reply[0], str) and reply[0] and reply[0] != predicted:
                # Portals older than 0.9 ignore handle_token; their path is only known now.
                handle = reply[0]
                watch.close()
                watch = self.bus.watch(handle, REQUEST, "Response")
            try:
                body = watch.wait(timeout)
            except TimeoutError:
                self._close_request(handle)
                raise BackendError(
                    f"{interface.rsplit('.', 1)[-1]}.{method}: no answer from the portal within {timeout:g}s "
                    "(was a permission dialog left open?)"
                ) from None
        finally:
            watch.close()
        response = int(body[0]) if body else RESPONSE_OTHER
        results = unwrap_vardict(body[1]) if len(body) > 1 else {}
        what = f"{interface.rsplit('.', 1)[-1]}.{method}"
        if response == RESPONSE_CANCELLED:
            raise PermissionDenied(f"{what} was cancelled or denied in the portal dialog")
        if response != RESPONSE_SUCCESS:
            raise BackendError(f"{what} failed in the portal (response {response})")
        return results

    def _close_request(self, handle: str) -> None:
        try:
            self.bus.call(handle, REQUEST, "Close", "", (), timeout=2.0)
        except Exception:
            pass

    def close_session(self, session_handle: str) -> None:
        try:
            self.bus.call(session_handle, SESSION, "Close", "", (), timeout=2.0)
        except Exception:
            pass


# ======================================================================== token store
def default_state_path() -> str:
    """``$LOCUM_WAYLAND_STATE`` or ``$XDG_STATE_HOME/locum/wayland-portal.json`` (``~/.local/state``)."""
    env = os.environ.get("LOCUM_WAYLAND_STATE")
    if env:
        return os.path.expanduser(env)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "locum", "wayland-portal.json")


class TokenStore:
    """The RemoteDesktop restore token, persisted in a private JSON file.

    Restore tokens are single-use: every successful ``Start`` returns a new one,
    which replaces the stored value. A token the portal no longer accepts is
    simply ignored by it (the dialog is shown again), so a stale file is harmless.
    ``path=None`` keeps the token in memory only.
    """

    KEY = "remote_desktop_restore_token"

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._mem: Optional[str] = None

    def load(self) -> Optional[str]:
        if self.path is None:
            return self._mem
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        tok = data.get(self.KEY) if isinstance(data, dict) else None
        return tok if isinstance(tok, str) and tok else None

    def save(self, token: Optional[str]) -> None:
        if self.path is None:
            self._mem = token or None
            return
        if not token:
            self.clear()
            return
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".wayland-portal-", dir=d)
        try:
            try:
                os.chmod(tmp, 0o600)
            except OSError:  # pragma: no cover - e.g. Windows
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({self.KEY: token}, fh)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        if self.path is None:
            self._mem = None
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass


__all__ = [
    "JeepneyBus",
    "PortalBus",
    "PortalClient",
    "PortalError",
    "SignalWatch",
    "TokenStore",
    "default_state_path",
    "new_token",
    "sender_path_part",
    "unwrap_vardict",
    "variant_value",
]
