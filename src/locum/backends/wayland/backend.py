"""The :class:`WaylandBackend`: input and capture through xdg-desktop-portal, UI through AT-SPI."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
import weakref
from typing import Any, Dict, List, Optional, Tuple

from ...errors import BackendError, NotSupported
from ...types import Monitor, MouseButton, Point, Rect, WindowInfo
from ..base import (
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    AccessibleNode,
    Backend,
)
from ..x11.atspi import load_atspi
from ..x11.keysyms import _COMMAND_MODIFIERS, key_to_keysym
from . import _portal as P
from .atspi import DIALOG_ROLES, WaylandAtspiNode

#: evdev button codes (linux/input-event-codes.h) for NotifyPointerButton.
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114
EVDEV_BUTTONS: Dict[MouseButton, int] = {
    MouseButton.LEFT: BTN_LEFT,
    MouseButton.RIGHT: BTN_RIGHT,
    MouseButton.MIDDLE: BTN_MIDDLE,
    MouseButton.X1: BTN_SIDE,  # "back", X11 button 8
    MouseButton.X2: BTN_EXTRA,  # "forward", X11 button 9
}

_NO_WINDOW_CONTROL = (
    "The Wayland backend cannot {what}: Wayland compositors do not let one application control "
    "another application's windows, and xdg-desktop-portal has no interface for it. Use the "
    "application's own UI (element actions), the compositor's keyboard shortcuts, or an X11 session."
)
_NO_GEOMETRY = (
    "Wayland does not tell applications where windows are or how they are stacked, so locum "
    "cannot tell which window is {what}"
)


class _Stream:
    """One ScreenCast stream of the session: a monitor in the compositor's logical layout."""

    __slots__ = ("node_id", "rect", "name", "pos_known")

    def __init__(self, node_id: int, rect: Optional[Rect], name: str = "", pos_known: bool = True) -> None:
        self.node_id = int(node_id)
        self.rect = rect
        self.name = name
        self.pos_known = pos_known

    def __repr__(self) -> str:
        return f"<stream {self.node_id} {self.rect}>"


def parse_streams(raw: Any) -> List[_Stream]:
    """``Start`` results ``streams`` (``a(ua{sv})``) -> :class:`_Stream` list.

    ``position`` is optional in the spec (a single monitor often has none: it is
    then at the origin); ``size`` is the stream's logical size.
    """
    out: List[_Stream] = []
    for item in raw or ():
        try:
            node_id, props = int(item[0]), P.unwrap_vardict(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        pos, size = props.get("position"), props.get("size")
        rect: Optional[Rect] = None
        try:
            if size is not None and int(size[0]) > 0 and int(size[1]) > 0:
                x, y = (int(pos[0]), int(pos[1])) if pos is not None else (0, 0)
                rect = Rect(x, y, int(size[0]), int(size[1]))
        except (TypeError, ValueError, IndexError):
            rect = None
        name = str(props.get("id") or props.get("mapping_id") or "")
        out.append(_Stream(node_id, rect, name, pos is not None))
    return out


def _cleanup(state: Dict[str, Any]) -> None:
    """Finalizer: release held input and close the portal session (holds no reference to the backend)."""
    bus = state.get("bus")
    if bus is None:
        return
    state["bus"] = None
    session = state.get("session")
    if session:
        for keysym in list(state.get("keys", {}).values()):
            try:
                bus.call(P.PORTAL_PATH, P.REMOTE_DESKTOP, "NotifyKeyboardKeysym", "oa{sv}iu",
                         (session, {}, int(keysym), P.STATE_RELEASED), timeout=2.0)
            except Exception:
                pass
        for btn in list(state.get("buttons", ())):
            try:
                bus.call(P.PORTAL_PATH, P.REMOTE_DESKTOP, "NotifyPointerButton", "oa{sv}iu",
                         (session, {}, int(btn), P.STATE_RELEASED), timeout=2.0)
            except Exception:
                pass
        try:
            bus.call(session, P.SESSION, "Close", "", (), timeout=2.0)
        except Exception:
            pass
    watch = state.get("closed_watch")
    if watch is not None:
        try:
            watch.close()
        except Exception:
            pass
    if state.get("owns_bus"):
        try:
            bus.close()
        except Exception:
            pass


class WaylandBackend(Backend):
    """Native Wayland backend over xdg-desktop-portal (experimental).

    * **Input**: one ``org.freedesktop.portal.RemoteDesktop`` session with a
      ``ScreenCast`` monitor selection on it (absolute pointer motion is only
      defined relative to a screen-cast stream). Starting it shows the
      compositor's approval dialog once; the ``restore_token`` it returns
      (``persist_mode=2``) is stored in a private state file so later runs are
      approved without asking until the user revokes it.
    * **Capture**: ``org.freedesktop.portal.Screenshot`` (non-interactive). It
      may show a permission prompt the first time (GNOME asks per application).
    * **Windows / elements**: AT-SPI (the same code as the X11 backend). Window
      listing, the element tree and native element actions work; activating,
      moving, resizing, minimizing and closing windows do not exist on Wayland
      and raise :class:`~locum.errors.NotSupported`.

    Coordinates are the compositor's *logical* layout (what the ScreenCast
    streams report): screenshots are scaled to it, so screenshot pixels and
    pointer coordinates always agree, also on HiDPI monitors.

    Args:
        bus: a :class:`~locum.backends.wayland._portal.PortalBus` (default: a
            ``jeepney`` session-bus connection).
        state_path: where the restore token is kept (default
            ``$XDG_STATE_HOME/locum/wayland-portal.json``; ``None``: memory only,
            the dialog is shown for every new backend).
        remote_desktop: False for a capture/AT-SPI-only backend that never opens
            the input session (no input; screenshot pixels are the coordinates).
        atspi_coords: ``"window"`` (default; AT-SPI extents are not trusted --
            see :mod:`locum.backends.wayland.atspi`) or ``"screen"``. Also read
            from ``$LOCUM_WAYLAND_ATSPI_COORDS``.
        accessibility: enable AT-SPI (or pass an AT-SPI module, for tests).
        keep_screenshots: keep the files the Screenshot portal writes (GNOME
            saves each one to ``~/Pictures/Screenshots``); default: delete them.
        approval_timeout: seconds to wait for the user to answer the session dialog.
        clipboard_tool: ``"wl-clipboard"`` (default when installed) or ``"none"``.
    """

    name = "wayland"
    #: Input whose destination cannot be verified is refused even by lenient input targets
    #: (see :mod:`locum._target`): Wayland gives no window geometry or stacking order.
    strict_input_targets = True

    def __init__(
        self,
        *,
        bus: Optional[P.PortalBus] = None,
        state_path: Any = "default",
        remote_desktop: bool = True,
        atspi_coords: Optional[str] = None,
        accessibility: Any = True,
        keep_screenshots: bool = False,
        approval_timeout: float = 180.0,
        clipboard_tool: Optional[str] = None,
    ) -> None:
        owns_bus = bus is None
        if bus is None:
            bus = P.JeepneyBus()
        self._bus = bus
        self._portal = P.PortalClient(bus)
        self._tokens = P.TokenStore(P.default_state_path() if state_path == "default" else state_path)
        self._remote_desktop = bool(remote_desktop)
        coords = (atspi_coords or os.environ.get("LOCUM_WAYLAND_ATSPI_COORDS") or "window").strip().lower()
        if coords not in ("window", "screen"):
            raise ValueError("atspi_coords must be 'window' or 'screen'")
        self._screen_coords = coords == "screen"
        self._keep_screenshots = bool(keep_screenshots)
        self._approval_timeout = float(approval_timeout)
        self._lock = threading.RLock()
        self._streams: List[_Stream] = []
        self._devices = 0
        self._pos: Optional[Point] = None
        self._versions: Dict[str, int] = {}
        self._available_devices: Optional[int] = None
        self._image_layout: Optional[Rect] = None  # remote_desktop=False: the last screenshot's size
        self._atspi_source: Any = "auto" if accessibility is True else (accessibility or None)
        self._atspi_mod: Any = None
        self._atspi_checked = False
        self._clip = self._detect_clipboard(clipboard_tool)
        # Everything the finalizer needs, without a reference to self.
        self._state: Dict[str, Any] = {"bus": bus, "owns_bus": owns_bus, "session": None, "keys": {},
                                       "buttons": set(), "closed_watch": None}
        self._finalizer = weakref.finalize(self, _cleanup, self._state)

    # ================================================================ lifecycle
    @property
    def closed(self) -> bool:
        return self._state["bus"] is None

    def close(self) -> None:
        """Release held keys/buttons and close the portal session."""
        self._finalizer()

    def __enter__(self) -> "WaylandBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _check(self) -> None:
        if self.closed:
            raise BackendError("Wayland backend has been closed")

    @property
    def session_handle(self) -> Optional[str]:
        return self._state["session"]

    def _version(self, interface: str) -> int:
        if interface not in self._versions:
            self._versions[interface] = self._portal.version(interface)
        return self._versions[interface]

    def _drop_session(self) -> None:
        st = self._state
        watch = st.get("closed_watch")
        if watch is not None:
            try:
                watch.close()
            except Exception:
                pass
        st["session"] = None
        st["closed_watch"] = None
        st["keys"].clear()
        st["buttons"].clear()
        self._streams = []
        self._devices = 0
        self._pos = None

    def _poll_closed(self) -> None:
        watch = self._state.get("closed_watch")
        if watch is not None and watch.poll() is not None:
            self._drop_session()
            raise BackendError(
                "The remote-desktop session was closed by the compositor or the user (sharing was "
                "stopped); the next input call starts a new session."
            )

    def start_session(self) -> str:
        """Open the RemoteDesktop session now (shows the approval dialog unless a restore
        token is accepted). Called automatically by the first input / monitor query."""
        with self._lock:
            self._check()
            if self._state["session"]:
                self._poll_closed()
                return self._state["session"]
            if not self._remote_desktop:
                raise NotSupported("This Wayland backend was created with remote_desktop=False (no input session)")
            rd_version = self._version(P.REMOTE_DESKTOP)
            if rd_version <= 0:
                raise NotSupported(
                    "xdg-desktop-portal offers no RemoteDesktop interface here: no input injection is possible. "
                    "It is implemented by xdg-desktop-portal-gnome and -kde; xdg-desktop-portal-wlr (Sway, "
                    "other wlroots compositors) does not implement it."
                )
            sc_version = self._version(P.SCREEN_CAST)
            available = self._available_device_types()
            want = (P.DEVICE_KEYBOARD | P.DEVICE_POINTER) & available if available else (
                P.DEVICE_KEYBOARD | P.DEVICE_POINTER)
            stoken = P.new_token("locum_session")
            res = self._portal.request(P.REMOTE_DESKTOP, "CreateSession", "a{sv}", (),
                                       {"session_handle_token": ("s", stoken)})
            session = str(res.get("session_handle") or self._portal.session_path(stoken))
            try:
                dev_opts: Dict[str, Tuple[str, Any]] = {"types": ("u", want)}
                if rd_version >= 2:
                    # Persistence of a remote-desktop session lives here (ScreenCast's own
                    # persist_mode must stay 0 on a remote-desktop session).
                    dev_opts["persist_mode"] = ("u", P.PERSIST_UNTIL_REVOKED)
                    token = self._tokens.load()
                    if token:
                        dev_opts["restore_token"] = ("s", token)
                self._portal.request(P.REMOTE_DESKTOP, "SelectDevices", "oa{sv}", (session,), dev_opts)
                if sc_version > 0:
                    self._portal.request(P.SCREEN_CAST, "SelectSources", "oa{sv}", (session,),
                                         {"types": ("u", P.SOURCE_MONITOR), "multiple": ("b", True)})
                watch = self._bus.watch(session, P.SESSION, "Closed")
                try:
                    res = self._portal.request(P.REMOTE_DESKTOP, "Start", "osa{sv}", (session, ""), {},
                                               timeout=self._approval_timeout)
                except BaseException:
                    watch.close()
                    raise
            except BaseException:
                self._portal.close_session(session)
                raise
            self._state["session"] = session
            self._state["closed_watch"] = watch
            try:
                self._devices = int(res.get("devices") or 0)
            except (TypeError, ValueError):
                self._devices = 0
            self._streams = parse_streams(res.get("streams"))
            token = res.get("restore_token")
            if isinstance(token, str) and token:
                try:
                    self._tokens.save(token)
                except OSError:
                    pass  # not fatal: the dialog will simply be shown again next time
            return session

    def _available_device_types(self) -> int:
        if self._available_devices is None:
            try:
                self._available_devices = int(self._portal.property(P.REMOTE_DESKTOP, "AvailableDeviceTypes", 0) or 0)
            except (TypeError, ValueError):
                self._available_devices = 0
        return self._available_devices

    def forget_approval(self) -> None:
        """Delete the stored restore token: the next session asks the user again."""
        self._tokens.clear()

    # ============================================================= capabilities
    @property
    def capabilities(self) -> frozenset:
        caps = set()
        if not self.closed:
            if self._remote_desktop:
                if self._state["session"]:
                    devices = self._devices
                    absolute = bool(self._streams)
                else:  # not started yet: what the portal offers (no dialog is shown for this)
                    devices = self._available_device_types() if self._version(P.REMOTE_DESKTOP) > 0 else 0
                    absolute = self._version(P.SCREEN_CAST) > 0
                if devices & P.DEVICE_POINTER and absolute:
                    caps |= {CAP_MOUSE, CAP_HSCROLL}
                if devices & P.DEVICE_KEYBOARD:
                    # Not CAP_UNICODE_TYPING: compositors map keysyms through the active layout and drop
                    # characters it lacks, so arbitrary text cannot be injected reliably.
                    caps.add(CAP_KEYBOARD)
            if self._version(P.SCREENSHOT) > 0:
                caps.add(CAP_SCREENSHOT)
            if self._clip is not None:
                caps.add(CAP_CLIPBOARD)
            if self._atspi() is not None:
                caps.add(CAP_ACCESSIBILITY)
        return frozenset(caps)

    def limitations(self) -> List[str]:
        """What this backend cannot do or verify (shown by ``locum info``)."""
        notes = [
            "experimental: not yet validated on a real compositor",
            "windows can be listed (AT-SPI) but not activated, moved, resized, minimized or closed",
            "no window geometry or stacking order: input bound to a window is refused when its "
            "destination cannot be verified",
            "the pointer position is only known after locum moved it (Wayland hides the global pointer)",
            "the corner fail-safe cannot see the user moving the mouse; use the kill hotkey or abort()",
            "typing is limited to characters on the active keyboard layout (others need clipboard mode)",
        ]
        if not self._screen_coords:
            notes.append("AT-SPI element bounds are hidden (toolkits report window-relative extents on "
                         "Wayland); elements are driven with native actions, not mouse clicks")
        if self._remote_desktop and not self._state["session"]:
            notes.append("input session not started yet: the first input asks for approval once")
        if self._version(P.SCREENSHOT) > 0:
            notes.append("screenshots go through the Screenshot portal (slow; may ask for permission)")
        if self._clip is None:
            notes.append("no clipboard: install wl-clipboard (wl-copy / wl-paste)")
        return notes

    # =================================================================== input
    def _session_for(self, device: int, what: str) -> str:
        session = self.start_session()
        if not self._devices & device:
            raise NotSupported(f"The remote-desktop session was not granted {what} access")
        return session

    def _notify(self, method: str, signature: str, args: Tuple[Any, ...]) -> None:
        session = self._state["session"]
        try:
            self._bus.call(P.PORTAL_PATH, P.REMOTE_DESKTOP, method, signature, (session, {}, *args))
        except P.PortalError as exc:
            # The session is gone (revoked, compositor restarted) or the call was rejected.
            self._poll_closed()
            raise BackendError(f"RemoteDesktop.{method} failed: {exc}") from exc

    def _stream_for(self, x: int, y: int) -> Tuple[_Stream, int, int]:
        streams = [s for s in self._streams if s.rect is not None]
        if not streams:
            raise NotSupported("The portal session has no screen-cast stream with a known size, so absolute "
                               "pointer motion is impossible")
        if len(streams) > 1 and not all(s.pos_known for s in streams):
            raise BackendError("The portal did not report where the shared monitors are; share a single "
                               "monitor (or all of them on a portal that reports positions)")
        for s in streams:
            if s.rect.contains((x, y)):  # type: ignore[union-attr]
                return s, x, y
        # Outside every monitor: clamp to the nearest one (like X11 clamps to the screen).
        def dist(s: _Stream) -> int:
            r = s.rect
            cx = min(max(x, r.left), r.right - 1)  # type: ignore[union-attr]
            cy = min(max(y, r.top), r.bottom - 1)  # type: ignore[union-attr]
            return (cx - x) ** 2 + (cy - y) ** 2

        s = min(streams, key=dist)
        r = s.rect
        return s, min(max(x, r.left), r.right - 1), min(max(y, r.top), r.bottom - 1)  # type: ignore[union-attr]

    def mouse_position(self) -> Point:
        """The position locum last moved the pointer to.

        Wayland gives no application the global pointer position; if the user
        moved the mouse since, this is stale. Before the first move it raises
        :class:`~locum.errors.NotSupported`.
        """
        self._check()
        if self._pos is None:
            raise NotSupported("Wayland does not let applications read the pointer position; it is known "
                               "only after locum has moved the pointer")
        return self._pos

    def mouse_move(self, x: int, y: int) -> None:
        with self._lock:
            self._check()
            self._session_for(P.DEVICE_POINTER, "pointer")
            s, cx, cy = self._stream_for(int(round(x)), int(round(y)))
            r = s.rect
            self._notify("NotifyPointerMotionAbsolute", "oa{sv}udd",
                         (s.node_id, float(cx - r.left), float(cy - r.top)))  # type: ignore[union-attr]
            self._pos = Point(cx, cy)

    def _button(self, button: Any) -> int:
        return EVDEV_BUTTONS[MouseButton.coerce(button)]

    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> None:
        btn = self._button(button)
        with self._lock:
            self._check()
            self._session_for(P.DEVICE_POINTER, "pointer")
            self._notify("NotifyPointerButton", "oa{sv}iu", (btn, P.STATE_PRESSED))
            self._state["buttons"].add(btn)

    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> None:
        btn = self._button(button)
        with self._lock:
            self._check()
            self._session_for(P.DEVICE_POINTER, "pointer")
            self._notify("NotifyPointerButton", "oa{sv}iu", (btn, P.STATE_RELEASED))
            self._state["buttons"].discard(btn)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Wheel notches (``dy > 0`` up, ``dx > 0`` right) via NotifyPointerAxisDiscrete.

        The portal's discrete steps follow evdev: positive vertical steps scroll
        *down*, positive horizontal steps scroll right.
        """
        dx, dy = int(dx), int(dy)
        with self._lock:
            self._check()
            self._session_for(P.DEVICE_POINTER, "pointer")
            if dy:
                self._notify("NotifyPointerAxisDiscrete", "oa{sv}ui", (P.AXIS_VERTICAL, -dy))
            if dx:
                self._notify("NotifyPointerAxisDiscrete", "oa{sv}ui", (P.AXIS_HORIZONTAL, dx))

    def _keysym(self, key: str) -> int:
        """Keysym for ``key``. Inside a shortcut (Ctrl/Alt/Super held) a letter uses its
        lowercase keysym: the compositor adds Shift for an uppercase keysym, which would
        turn ``ctrl+A`` into ``ctrl+shift+a``."""
        if len(key) == 1 and any(m in self._state["keys"] for m in _COMMAND_MODIFIERS):
            low = key.lower()
            if len(low) == 1:
                key = low
        return key_to_keysym(key)

    def key_down(self, key: str) -> None:
        with self._lock:
            self._check()
            held = self._state["keys"]
            keysym = held.get(key)
            if keysym is None:
                keysym = self._keysym(key)
            self._session_for(P.DEVICE_KEYBOARD, "keyboard")
            self._notify("NotifyKeyboardKeysym", "oa{sv}iu", (int(keysym), P.STATE_PRESSED))
            held[key] = keysym

    def key_up(self, key: str) -> None:
        with self._lock:
            self._check()
            held = self._state["keys"]
            keysym = held.get(key)
            if keysym is None:
                keysym = self._keysym(key)
            self._session_for(P.DEVICE_KEYBOARD, "keyboard")
            self._notify("NotifyKeyboardKeysym", "oa{sv}iu", (int(keysym), P.STATE_RELEASED))
            held.pop(key, None)

    def release_all(self) -> None:
        """Release every key and button this backend holds down."""
        with self._lock:
            if self.closed or not self._state["session"]:
                return
            first: Optional[BaseException] = None
            for key, keysym in list(self._state["keys"].items()):
                try:
                    self._notify("NotifyKeyboardKeysym", "oa{sv}iu", (int(keysym), P.STATE_RELEASED))
                    self._state["keys"].pop(key, None)
                except BackendError as exc:
                    first = first or exc
            for btn in list(self._state["buttons"]):
                try:
                    self._notify("NotifyPointerButton", "oa{sv}iu", (btn, P.STATE_RELEASED))
                    self._state["buttons"].discard(btn)
                except BackendError as exc:
                    first = first or exc
            if first is not None:
                raise first

    # ================================================================== screen
    def monitors(self) -> List[Monitor]:
        """The monitors shared in the session, in the compositor's logical layout."""
        with self._lock:
            self._check()
            if not self._remote_desktop:
                if self._image_layout is None:
                    self.screenshot()
                return [Monitor(0, self._image_layout, 1.0, True, "screenshot")]  # type: ignore[arg-type]
            self.start_session()
            streams = [s for s in self._streams if s.rect is not None]
            if not streams:
                raise NotSupported("The portal session reports no monitor (no screen-cast stream with a size)")
            if len(streams) > 1 and not all(s.pos_known for s in streams):
                raise BackendError("The portal did not report where the shared monitors are; share a single "
                                   "monitor (or all of them on a portal that reports positions)")
            # Primary first: the monitor at the origin if there is one (the portal does not say).
            streams.sort(key=lambda s: (not s.rect.contains((0, 0)), s.rect.top, s.rect.left))  # type: ignore[union-attr]
            return [Monitor(i, s.rect, 1.0, i == 0, s.name or f"stream {s.node_id}")  # type: ignore[arg-type]
                    for i, s in enumerate(streams)]

    def _capture(self) -> Any:
        """Full-desktop screenshot from the Screenshot portal, as an RGB image."""
        from PIL import Image

        if self._version(P.SCREENSHOT) <= 0:
            raise NotSupported("xdg-desktop-portal offers no Screenshot interface here")
        res = self._portal.request(P.SCREENSHOT, "Screenshot", "sa{sv}", ("",),
                                   {"interactive": ("b", False), "modal": ("b", False)},
                                   timeout=self._approval_timeout)
        uri = res.get("uri")
        if not isinstance(uri, str) or not uri:
            raise BackendError("The Screenshot portal returned no image")
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "file":
            raise BackendError(f"The Screenshot portal returned an unsupported URI {uri!r}")
        path = urllib.request.url2pathname(parsed.path)
        try:
            with Image.open(path) as im:
                img = im.convert("RGB")
                img.load()
        except OSError as exc:
            raise BackendError(f"Cannot read the portal screenshot {path!r}: {exc}") from exc
        finally:
            if not self._keep_screenshots:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return img

    def screenshot(self, region: Optional[Rect] = None) -> Any:
        """RGB image of ``region`` (default: every shared monitor) in logical coordinates.

        The portal image is scaled to the logical layout (HiDPI monitors) and
        cropped; parts of the region outside the layout are black.
        """
        from PIL import Image

        with self._lock:
            self._check()
            img = self._capture()
            if self._remote_desktop:
                layout = self.virtual_screen()
            else:
                layout = self._image_layout = Rect(0, 0, img.width, img.height)
        sx, sy = img.width / max(layout.width, 1), img.height / max(layout.height, 1)
        if abs(sx - sy) > 0.02 * max(sx, sy):
            raise BackendError(
                f"The portal screenshot ({img.width}x{img.height}) does not match the shared monitor layout "
                f"({layout.width}x{layout.height}); share all monitors in the remote-desktop dialog "
                "(forget_approval() asks again)"
            )
        r = Rect.coerce(region) if region is not None else layout
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"region must have a positive size, got {r}")
        part = r.intersect(layout)
        if part == r and r == layout and img.size == (r.width, r.height):
            return img
        out = Image.new("RGB", (r.width, r.height), (0, 0, 0))
        if part is not None:
            box = ((part.left - layout.left) * sx, (part.top - layout.top) * sy,
                   (part.right - layout.left) * sx, (part.bottom - layout.top) * sy)
            if abs(sx - 1.0) < 1e-9 and abs(sy - 1.0) < 1e-9:
                piece = img.crop(tuple(int(round(v)) for v in box))
            else:
                piece = img.resize((part.width, part.height), Image.BILINEAR, box=box)
            out.paste(piece, (part.left - r.left, part.top - r.top))
        return out

    # ============================================================= accessibility
    def _atspi(self) -> Any:
        if not self._atspi_checked:
            self._atspi_checked = True
            self._atspi_mod = load_atspi() if self._atspi_source == "auto" else self._atspi_source
        return self._atspi_mod

    def _require_atspi(self) -> Any:
        self._check()
        mod = self._atspi()
        if mod is None:
            raise NotSupported(
                "AT-SPI accessibility is unavailable: install PyGObject with the Atspi typelib "
                "(e.g. `apt install python3-gi gir1.2-atspi-2.0 at-spi2-core`)"
            )
        return mod

    def _window_nodes(self) -> List[WaylandAtspiNode]:
        atspi = self._require_atspi()
        try:
            desktop = atspi.get_desktop(0)
        except Exception as exc:
            raise NotSupported(f"AT-SPI registry unavailable: {exc}") from exc
        if desktop is None:
            raise NotSupported("AT-SPI registry unavailable (is at-spi2-registryd running?)")
        root = WaylandAtspiNode(desktop, atspi, screen_coords=self._screen_coords)
        out: List[WaylandAtspiNode] = []
        for app in root.children():
            for win in app.children():
                if isinstance(win, WaylandAtspiNode) and win.is_top_level():
                    out.append(win)
        # No stacking order on Wayland: the active window first, the rest in AT-SPI order.
        out.sort(key=lambda n: "active" not in n._raw_states())
        return out

    def accessibility_roots(self) -> List[AccessibleNode]:
        return list(self._window_nodes())

    def _info(self, node: WaylandAtspiNode) -> WindowInfo:
        raw = node._raw_states()
        st = node.states
        rect = node.bounds if self._screen_coords else None
        return WindowInfo(
            handle=node.handle,
            title=node.name,
            rect=rect or Rect(0, 0, 0, 0),
            app=node.app_name(),
            pid=node.pid,
            is_active="active" in raw,
            is_minimized="iconified" in raw,
            is_maximized=False,
            is_visible="visible" in st and "offscreen" not in st,
            class_name=node.native_role,
        )

    def list_windows(self) -> List[WindowInfo]:
        """Top-level windows of every AT-SPI application (active first; no Z-order on Wayland).

        Rectangles are empty unless ``atspi_coords="screen"``: window positions are unknown.
        Applications without accessibility support are missing.
        """
        return [self._info(n) for n in self._window_nodes()]

    def foreground_handle(self) -> Any:
        """The window AT-SPI reports as ``active``; ``None`` unless exactly one is."""
        active = [w for w in self.list_windows() if w.is_active]
        return active[0].handle if len(active) == 1 else None

    def window_belongs_to(self, handle: Any, target: Any) -> bool:
        """Same window, or a dialog/alert of the same application (same process)."""
        if handle == target:
            return True
        by_handle = {}
        for n in self._window_nodes():
            by_handle.setdefault(n.handle, n)
        win, tgt = by_handle.get(handle), by_handle.get(target)
        if win is None or tgt is None:
            raise NotSupported("window no longer known to AT-SPI")
        return bool(win.pid) and win.pid == tgt.pid and win.native_role.lower() in DIALOG_ROLES

    def window_handle_at(self, x: int, y: int) -> Any:
        raise NotSupported(_NO_GEOMETRY.format(what=f"at ({x}, {y})"))

    def windows_above(self, handle: Any) -> List[Rect]:
        raise NotSupported(_NO_GEOMETRY.format(what="in front of another"))

    def element_at(self, x: int, y: int) -> Optional[AccessibleNode]:
        """Hit-testing needs trusted screen coordinates (``atspi_coords="screen"``); with them
        the active window is searched first, other windows only when exactly one contains
        the point (there is no stacking order to break ties)."""
        if not self._screen_coords:
            raise NotSupported("AT-SPI hit-testing is off on Wayland: element extents are window-relative "
                               "(pass atspi_coords='screen' if this toolkit reports desktop coordinates)")
        pt = (int(x), int(y))
        roots = self._window_nodes()
        hits = [r for r in roots if r.bounds is not None and r.bounds.contains(pt) and r.is_visible]
        if not hits:
            return None
        active = [r for r in hits if "active" in r._raw_states()]
        if active:
            node = active[0]
        elif len(hits) == 1:
            node = hits[0]
        else:
            raise NotSupported(_NO_GEOMETRY.format(what=f"in front at ({x}, {y})"))
        for _ in range(256):
            child = node.child_at_point(*pt)
            if child is None:
                break
            node = child  # type: ignore[assignment]
        return node

    def focused_element(self) -> Optional[AccessibleNode]:
        roots = self._window_nodes()
        active = [r for r in roots if "active" in r._raw_states()] or roots
        for root in active:
            for node in root.walk(max_depth=48):
                if "focused" in node.states:
                    return node
        return None

    # ================================================================= windows
    def activate_window(self, handle: Any) -> None:
        raise NotSupported(_NO_WINDOW_CONTROL.format(what="activate (focus) windows"))

    def move_window(self, handle: Any, rect: Rect) -> None:
        raise NotSupported(_NO_WINDOW_CONTROL.format(what="move or resize windows"))

    def set_window_state(self, handle: Any, state: str) -> None:
        raise NotSupported(_NO_WINDOW_CONTROL.format(what=f"make windows {state}"))

    def close_window(self, handle: Any) -> None:
        raise NotSupported(_NO_WINDOW_CONTROL.format(what="close windows"))

    # =============================================================== clipboard
    @staticmethod
    def _detect_clipboard(tool: Optional[str]) -> Optional[Tuple[List[str], List[str]]]:
        t = (tool or "").strip().lower()
        if t in ("none",):
            return None
        if t not in ("", "wl-clipboard", "wl-copy", "wayland"):
            raise ValueError(f"Unknown clipboard tool {tool!r}; expected 'wl-clipboard' or 'none'")
        if shutil.which("wl-paste") and shutil.which("wl-copy"):
            return ["wl-paste", "--no-newline"], ["wl-copy"]
        return None

    def get_clipboard(self) -> str:
        self._check()
        if self._clip is None:
            raise NotSupported("No clipboard on the Wayland backend: install wl-clipboard (wl-copy / wl-paste)")
        try:
            proc = subprocess.run(self._clip[0], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise BackendError("Clipboard read timed out (wl-paste)") from exc
        except OSError as exc:
            raise BackendError(f"Cannot run wl-paste: {exc}") from exc
        if proc.returncode != 0:
            return ""  # wl-paste exits non-zero when the clipboard holds no text
        return proc.stdout.decode("utf-8", "replace")

    def set_clipboard(self, text: str) -> None:
        self._check()
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        if self._clip is None:
            raise NotSupported("No clipboard on the Wayland backend: install wl-clipboard (wl-copy / wl-paste)")
        try:
            # wl-copy forks a process that serves the selection: never capture its output.
            proc = subprocess.run(self._clip[1], input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=5, start_new_session=True)
        except subprocess.TimeoutExpired as exc:
            raise BackendError("Clipboard write timed out (wl-copy)") from exc
        except OSError as exc:
            raise BackendError(f"Cannot run wl-copy: {exc}") from exc
        if proc.returncode != 0:
            raise BackendError(f"wl-copy failed with exit code {proc.returncode}")

    def __repr__(self) -> str:
        state = " closed" if self.closed else (" session" if self._state["session"] else "")
        return f"<WaylandBackend{state}>"


__all__ = ["BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA", "EVDEV_BUTTONS",
           "WaylandBackend", "parse_streams"]
