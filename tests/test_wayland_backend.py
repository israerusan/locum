"""Tests of the Wayland backend (xdg-desktop-portal + AT-SPI) against a fake D-Bus.

Everything here runs on any OS: :class:`FakeBus` stands in for the session bus.
It records every method call and signal subscription and answers the portal's
asynchronous methods the way xdg-desktop-portal does -- a ``Response`` signal on
the request object path derived from the caller's unique name and the
``handle_token``. Like a real bus it delivers a signal only to watches that
exist when it is emitted, so a client that subscribed *after* calling would
miss the response and time out.

Only the tests marked ``wayland`` need a real Wayland session with
xdg-desktop-portal; they are skipped unless ``LOCUM_TEST_WAYLAND=1``. The
JeepneyBus adapter test needs ``jeepney`` and is skipped without it.
"""

from __future__ import annotations

import collections
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import pytest
from PIL import Image

from locum.backends import create_backend, is_wayland_session, select_platform_backend
from locum.backends.base import (
    CAP_ACCESSIBILITY,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOWS,
)
from locum.backends.wayland import (
    PortalBus,
    PortalClient,
    SignalWatch,
    TokenStore,
    WaylandAtspiNode,
    WaylandBackend,
    parse_streams,
)
from locum.backends.wayland import _portal as P
from locum.backends.wayland.backend import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, BTN_SIDE
from locum.errors import BackendError, BackendUnavailable, NotSupported, PermissionDenied, TargetLost
from locum.types import MouseButton, Point, Rect

UNIQUE = ":1.42"
SENDER = "1_42"
REQ_PREFIX = f"/org/freedesktop/portal/desktop/request/{SENDER}/"
SESSION_PREFIX = f"/org/freedesktop/portal/desktop/session/{SENDER}/"

ASYNC_METHODS = {
    (P.REMOTE_DESKTOP, "CreateSession"),
    (P.REMOTE_DESKTOP, "SelectDevices"),
    (P.REMOTE_DESKTOP, "Start"),
    (P.SCREEN_CAST, "SelectSources"),
    (P.SCREENSHOT, "Screenshot"),
}


# =============================================================================== fake bus
class FakeWatch(SignalWatch):
    def __init__(self, bus: "FakeBus", path: str, interface: str, member: str) -> None:
        self.bus, self.path, self.interface, self.member = bus, path, interface, member
        self.queue: collections.deque = collections.deque()
        self.closed = False

    def wait(self, timeout: Optional[float]) -> Tuple[Any, ...]:
        if not self.queue:
            pending = self.bus.deferred.pop(self.path, None)
            if pending is not None:
                self.queue.append(pending)
        if not self.queue:
            raise TimeoutError
        return self.queue.popleft()

    def poll(self) -> Optional[Tuple[Any, ...]]:
        return self.queue.popleft() if self.queue else None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.bus.events.append(("unwatch", self.path, self.member))
            self.bus.watches.remove(self)


class FakeBus(PortalBus):
    """A recording stand-in for the session bus with a scripted xdg-desktop-portal."""

    def __init__(self, *, versions: Optional[Dict[str, int]] = None, available: int = 3,
                 devices: int = 3, streams: Optional[List[Tuple[int, Dict[str, Any]]]] = None,
                 restore_token: Optional[str] = "token-2", screenshot_path: Optional[str] = None,
                 responses: Optional[Dict[str, int]] = None, silent: Tuple[str, ...] = (),
                 old_portal: bool = False) -> None:
        self.unique_name = UNIQUE
        self.versions = {P.REMOTE_DESKTOP: 2, P.SCREEN_CAST: 4, P.SCREENSHOT: 2} if versions is None else versions
        self.available = available
        self.devices = devices
        self.streams = streams if streams is not None else [
            (42, {"position": ("(ii)", (0, 0)), "size": ("(ii)", (1920, 1080)), "source_type": ("u", 1)})]
        self.restore_token = restore_token
        self.screenshot_path = screenshot_path
        self.responses = responses or {}  # method -> response code
        self.silent = silent  # methods that never answer
        self.old_portal = old_portal  # ignores handle_token, answers later on its own path
        self.calls: List[Tuple[str, str, str, str, Tuple[Any, ...]]] = []
        self.events: List[Tuple[Any, ...]] = []
        self.watches: List[FakeWatch] = []
        self.deferred: Dict[str, Tuple[Any, ...]] = {}
        self.closed = False
        self.n_old = 0

    # ------------------------------------------------------------- PortalBus
    def watch(self, path: str, interface: str, member: str) -> SignalWatch:
        w = FakeWatch(self, path, interface, member)
        self.watches.append(w)
        self.events.append(("watch", path, member))
        return w

    def emit(self, path: str, interface: str, member: str, body: Tuple[Any, ...]) -> None:
        for w in list(self.watches):
            if w.path == path and w.interface == interface and w.member == member:
                w.queue.append(body)

    def call(self, path: str, interface: str, member: str, signature: str, body: Any, *,
             destination: str = P.PORTAL_BUS_NAME, timeout: Optional[float] = 10.0) -> Tuple[Any, ...]:
        body = tuple(body)
        self.calls.append((path, interface, member, signature, body))
        self.events.append(("call", member))
        if interface == P.PROPERTIES and member == "Get":
            iface, prop = body
            if prop == "version":
                v = self.versions.get(iface, 0)
                if not v:
                    raise P.PortalError("org.freedesktop.DBus.Error.UnknownInterface", "UnknownInterface")
                return (("u", v),)
            if prop == "AvailableDeviceTypes":
                return (("u", self.available),)
            raise P.PortalError("org.freedesktop.DBus.Error.UnknownProperty")
        if (interface, member) in ASYNC_METHODS:
            return self._async(interface, member, body)
        if member.startswith("Notify") and body[0] != self.session_handle():
            raise P.PortalError("org.freedesktop.portal.Error.NotFound: no such session")
        return ()

    # --------------------------------------------------------------- helpers
    def session_handle(self) -> Optional[str]:
        for (_, _, member, _, body) in reversed(self.calls):
            if member == "CreateSession":
                return SESSION_PREFIX + body[-1]["session_handle_token"][1]
        return None

    def _async(self, interface: str, member: str, body: Tuple[Any, ...]) -> Tuple[Any, ...]:
        options = body[-1]
        token = options["handle_token"][1]
        if self.old_portal:
            self.n_old += 1
            path = f"/org/freedesktop/portal/desktop/request/legacy{self.n_old}"
        else:
            path = REQ_PREFIX + token
        if member in self.silent:
            return (path,)
        code = self.responses.get(member, P.RESPONSE_SUCCESS)
        results: Dict[str, Tuple[str, Any]] = {}
        if member == "CreateSession":
            results["session_handle"] = ("s", SESSION_PREFIX + options["session_handle_token"][1])
        elif member == "Start":
            results["devices"] = ("u", self.devices)
            results["streams"] = ("a(ua{sv})", self.streams)
            if self.restore_token:
                results["restore_token"] = ("s", self.restore_token)
        elif member == "Screenshot":
            results["uri"] = ("s", "file://" + _to_uri_path(self.screenshot_path or ""))
        signal = (code, results)
        if self.old_portal:
            self.deferred[path] = signal  # answered later, after the caller learned the path
        else:
            self.emit(path, P.REQUEST, "Response", signal)  # lost unless already subscribed
        return (path,)

    def notify_calls(self, member: Optional[str] = None) -> List[Tuple[Any, ...]]:
        return [c[4][2:] for c in self.calls if c[2].startswith("Notify") and (member is None or c[2] == member)]

    def members(self) -> List[str]:
        return [c[2] for c in self.calls if c[1] != P.PROPERTIES]

    def options_of(self, member: str) -> Dict[str, Any]:
        for c in self.calls:
            if c[2] == member:
                return P.unwrap_vardict(c[4][-1])
        raise AssertionError(f"{member} was not called")

    def close(self) -> None:
        self.closed = True


def _to_uri_path(path: str) -> str:
    from urllib.request import pathname2url

    return pathname2url(path) if path else ""


def make(bus: Optional[FakeBus] = None, tmp_path: Any = None, **kw: Any) -> Tuple[WaylandBackend, FakeBus]:
    bus = bus or FakeBus()
    kw.setdefault("state_path", str(tmp_path / "state.json") if tmp_path is not None else None)
    kw.setdefault("accessibility", False)
    kw.setdefault("clipboard_tool", "none")
    return WaylandBackend(bus=bus, **kw), bus


# ============================================================================ session setup
def test_session_setup_order_and_options(tmp_path):
    be, bus = make(tmp_path=tmp_path)
    session = be.start_session()
    assert session.startswith(SESSION_PREFIX)
    assert bus.members() == ["CreateSession", "SelectDevices", "SelectSources", "Start"]
    assert bus.options_of("CreateSession")["session_handle_token"].startswith("locum_session_")
    dev = bus.options_of("SelectDevices")
    assert dev["types"] == P.DEVICE_KEYBOARD | P.DEVICE_POINTER
    assert dev["persist_mode"] == 2
    assert "restore_token" not in dev  # nothing stored yet
    src = bus.options_of("SelectSources")
    assert src["types"] == P.SOURCE_MONITOR and src["multiple"] is True
    assert "persist_mode" not in src  # remote-desktop sessions persist via SelectDevices only
    start = next(c for c in bus.calls if c[2] == "Start")
    assert start[3] == "osa{sv}" and start[4][0] == session and start[4][1] == ""
    for c in bus.calls:
        if c[2] in ("SelectDevices", "SelectSources"):
            assert c[3] == "oa{sv}" and c[4][0] == session
    assert be.session_handle == session
    assert be.start_session() == session  # idempotent
    assert bus.members().count("CreateSession") == 1


def test_every_request_is_subscribed_before_the_call():
    be, bus = make()
    be.start_session()
    for i, ev in enumerate(bus.events):
        if ev[0] == "call" and ev[1] in {m for _, m in ASYNC_METHODS}:
            prev = bus.events[i - 1]
            assert prev[0] == "watch" and prev[2] == "Response" and prev[1].startswith(REQ_PREFIX), (ev, prev)
    # every request watch was closed again; only the session's Closed watch stays
    assert [(w.path, w.member) for w in bus.watches] == [(be.session_handle, "Closed")]


def test_handle_token_predicts_the_request_path():
    bus = FakeBus()
    client = PortalClient(bus)
    assert client.request_path("abc") == REQ_PREFIX + "abc"
    assert client.session_path("s1") == SESSION_PREFIX + "s1"
    assert P.sender_path_part(":1.42") == "1_42"
    res = client.request(P.SCREEN_CAST, "SelectSources", "oa{sv}", ("/s",), {"types": ("u", 1)})
    assert res == {}
    path, _, _, _, body = bus.calls[-1]
    assert body[-1]["handle_token"][1] and body[-1]["types"] == ("u", 1)


def test_old_portal_with_unpredictable_request_path_is_followed():
    be, bus = make(bus=FakeBus(old_portal=True))
    be.start_session()
    assert bus.members() == ["CreateSession", "SelectDevices", "SelectSources", "Start"]
    assert any(ev[0] == "watch" and "legacy" in ev[1] for ev in bus.events)


def test_dialog_cancelled_raises_permission_denied_and_closes_session():
    be, bus = make(bus=FakeBus(responses={"Start": P.RESPONSE_CANCELLED}))
    with pytest.raises(PermissionDenied):
        be.mouse_move(10, 10)
    assert be.session_handle is None
    assert ("Close" in bus.members()) and bus.calls[-1][0].startswith(SESSION_PREFIX)
    assert bus.watches == []


def test_portal_failure_raises_backend_error():
    be, _ = make(bus=FakeBus(responses={"SelectDevices": P.RESPONSE_OTHER}))
    with pytest.raises(BackendError, match="response 2"):
        be.start_session()


def test_request_timeout_closes_the_request():
    be, bus = make(bus=FakeBus(silent=("Start",)), approval_timeout=0.01)
    with pytest.raises(BackendError, match="no answer"):
        be.start_session()
    closes = [c for c in bus.calls if c[2] == "Close"]
    assert any(c[1] == P.REQUEST and c[0].startswith(REQ_PREFIX) for c in closes)
    assert any(c[1] == P.SESSION for c in closes)


def test_no_remote_desktop_portal_is_not_supported():
    be, bus = make(bus=FakeBus(versions={P.SCREENSHOT: 2}))
    with pytest.raises(NotSupported, match="RemoteDesktop"):
        be.mouse_move(1, 1)
    with pytest.raises(NotSupported):
        be.key_down("a")
    caps = be.capabilities
    assert CAP_MOUSE not in caps and CAP_KEYBOARD not in caps and CAP_SCREENSHOT in caps
    assert "CreateSession" not in bus.members()


def test_session_closed_signal_drops_the_session_and_next_call_restarts():
    be, bus = make()
    be.mouse_move(5, 5)
    first = be.session_handle
    bus.emit(first, P.SESSION, "Closed", ({},))
    with pytest.raises(BackendError, match="closed"):
        be.mouse_move(6, 6)
    assert be.session_handle is None
    with pytest.raises(NotSupported):
        be.mouse_position()  # the position is forgotten with the session
    be.mouse_move(7, 7)
    assert be.session_handle and be.session_handle != first
    assert bus.members().count("CreateSession") == 2


# ============================================================================ token persistence
def test_restore_token_is_saved_and_reused(tmp_path):
    state = tmp_path / "sub" / "state.json"
    be, bus = make(state_path=str(state))
    be.start_session()
    assert json.loads(state.read_text())["remote_desktop_restore_token"] == "token-2"
    if sys.platform != "win32":
        assert (state.stat().st_mode & 0o777) == 0o600
    be.close()

    bus2 = FakeBus(restore_token="token-3")
    be2, _ = make(bus=bus2, state_path=str(state))
    be2.start_session()
    assert bus2.options_of("SelectDevices")["restore_token"] == "token-2"
    assert json.loads(state.read_text())["remote_desktop_restore_token"] == "token-3"  # single-use: replaced
    be2.forget_approval()
    assert not state.exists()


def test_no_persistence_on_remote_desktop_v1(tmp_path):
    state = tmp_path / "state.json"
    TokenStore(str(state)).save("old")
    be, bus = make(bus=FakeBus(versions={P.REMOTE_DESKTOP: 1, P.SCREEN_CAST: 1, P.SCREENSHOT: 1}),
                   state_path=str(state))
    be.start_session()
    opts = bus.options_of("SelectDevices")
    assert "persist_mode" not in opts and "restore_token" not in opts


def test_token_store_tolerates_garbage_and_memory_mode(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert TokenStore(str(p)).load() is None
    p.write_text(json.dumps({"remote_desktop_restore_token": 5}))
    assert TokenStore(str(p)).load() is None
    mem = TokenStore(None)
    assert mem.load() is None
    mem.save("t")
    assert mem.load() == "t"
    mem.clear()
    assert mem.load() is None


def test_default_state_path(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCUM_WAYLAND_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert P.default_state_path() == os.path.join(str(tmp_path), "locum", "wayland-portal.json")
    monkeypatch.setenv("LOCUM_WAYLAND_STATE", str(tmp_path / "x.json"))
    assert P.default_state_path() == str(tmp_path / "x.json")


# ============================================================================ keyboard
def test_key_names_map_to_keysyms():
    be, bus = make()
    for key, keysym in [("a", 0x61), ("A", 0x41), ("enter", 0xFF0D), ("f5", 0xFFC2), ("é", 0xE9),
                        ("€", 0x010020AC), ("ctrl", 0xFFE3), ("cmd", 0xFFEB), ("num_enter", 0xFF8D),
                        ("volumeup", 0x1008FF13)]:
        be.key_down(key)
        be.key_up(key)
        assert bus.notify_calls("NotifyKeyboardKeysym")[-2:] == [(keysym, 1), (keysym, 0)], key
    for c in bus.calls:
        if c[2] == "NotifyKeyboardKeysym":
            assert c[3] == "oa{sv}iu" and c[4][0] == be.session_handle and c[4][1] == {}


def test_shortcut_letters_use_the_lowercase_keysym():
    be, bus = make()
    be.key_down("ctrl")
    be.key_down("A")  # ctrl+A must not become ctrl+shift+a
    be.key_up("A")
    be.key_up("ctrl")
    assert bus.notify_calls() == [(0xFFE3, 1), (0x61, 1), (0x61, 0), (0xFFE3, 0)]


def test_release_all_and_close_release_held_input():
    be, bus = make()
    be.key_down("shift")
    be.key_down("x")
    be.mouse_down(MouseButton.LEFT)
    be.release_all()
    released = bus.notify_calls()[3:]
    assert sorted(released) == sorted([(0xFFE1, 0), (0x78, 0), (BTN_LEFT, 0)])
    be.key_down("alt")
    be.close()
    assert bus.notify_calls()[-1] == (0xFFE9, 0)
    assert bus.calls[-1][1:3] == (P.SESSION, "Close")
    with pytest.raises(BackendError, match="closed"):
        be.key_down("a")


def test_keyboard_only_session_refuses_the_pointer():
    be, _ = make(bus=FakeBus(devices=P.DEVICE_KEYBOARD))
    be.key_down("a")
    with pytest.raises(NotSupported, match="pointer"):
        be.mouse_down()
    assert CAP_MOUSE not in be.capabilities and CAP_KEYBOARD in be.capabilities


def test_unknown_key_is_value_error_without_a_session():
    be, bus = make()
    with pytest.raises(ValueError):
        be.key_down("nosuchkey")
    assert "CreateSession" not in bus.members()


def test_type_text_is_not_advertised():
    be, _ = make()
    assert CAP_UNICODE_TYPING not in be.capabilities
    with pytest.raises(NotSupported):
        be.type_text("héllo")


# ============================================================================ pointer
def test_pointer_motion_is_relative_to_the_stream():
    streams = [
        (42, {"position": ("(ii)", (0, 0)), "size": ("(ii)", (1920, 1080))}),
        (43, {"position": ("(ii)", (1920, 0)), "size": ("(ii)", (1280, 1024))}),
    ]
    be, bus = make(bus=FakeBus(streams=streams))
    be.mouse_move(100, 50)
    be.mouse_move(2000, 10)
    be.mouse_move(5000, -30)  # outside: clamped to the nearest monitor
    assert bus.notify_calls("NotifyPointerMotionAbsolute") == [
        (42, 100.0, 50.0), (43, 80.0, 10.0), (43, 1279.0, 0.0)]
    sig = {c[3] for c in bus.calls if c[2] == "NotifyPointerMotionAbsolute"}
    assert sig == {"oa{sv}udd"}
    assert be.mouse_position() == Point(1920 + 1279, 0)
    mons = be.monitors()
    assert [m.rect for m in mons] == [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1280, 1024)]
    assert mons[0].is_primary and not mons[1].is_primary
    assert be.virtual_screen() == Rect(0, 0, 3200, 1080)


def test_multiple_streams_without_positions_are_refused():
    streams = [(1, {"size": ("(ii)", (800, 600))}), (2, {"size": ("(ii)", (800, 600))})]
    be, _ = make(bus=FakeBus(streams=streams))
    with pytest.raises(BackendError, match="where the shared monitors are"):
        be.mouse_move(1, 1)
    with pytest.raises(BackendError):
        be.monitors()


def test_single_stream_without_position_is_at_the_origin():
    st = parse_streams([(7, {"size": ("(ii)", (1024, 768))})])
    assert st[0].rect == Rect(0, 0, 1024, 768) and st[0].node_id == 7 and not st[0].pos_known
    assert parse_streams([(8, {"position": ("(ii)", (5, 5))})])[0].rect is None
    assert parse_streams(["garbage"]) == []


def test_no_streams_means_no_absolute_motion():
    be, _ = make(bus=FakeBus(streams=[]))
    with pytest.raises(NotSupported, match="stream"):
        be.mouse_move(1, 1)
    assert CAP_MOUSE not in be.capabilities  # session started: no stream, no absolute pointer


def test_buttons_use_evdev_codes():
    be, bus = make()
    for btn, code in [(MouseButton.LEFT, BTN_LEFT), (MouseButton.RIGHT, BTN_RIGHT),
                      (MouseButton.MIDDLE, BTN_MIDDLE), (MouseButton.X1, BTN_SIDE), ("x2", 0x114)]:
        be.mouse_down(btn)
        be.mouse_up(btn)
        assert bus.notify_calls("NotifyPointerButton")[-2:] == [(code, 1), (code, 0)]
    assert {c[3] for c in bus.calls if c[2] == "NotifyPointerButton"} == {"oa{sv}iu"}


def test_scroll_uses_discrete_axis_steps():
    be, bus = make()
    be.scroll(dy=2)  # locum: up
    be.scroll(dy=-3)  # down
    be.scroll(dx=4)  # right
    be.scroll(dx=-1, dy=1)
    assert bus.notify_calls("NotifyPointerAxisDiscrete") == [
        (P.AXIS_VERTICAL, -2), (P.AXIS_VERTICAL, 3), (P.AXIS_HORIZONTAL, 4),
        (P.AXIS_VERTICAL, -1), (P.AXIS_HORIZONTAL, -1)]
    assert {c[3] for c in bus.calls if c[2] == "NotifyPointerAxisDiscrete"} == {"oa{sv}ui"}


def test_pointer_position_is_unknown_until_moved_and_mouse_jumps():
    from locum.mouse import Mouse

    be, bus = make()
    with pytest.raises(NotSupported, match="pointer position"):
        be.mouse_position()
    m = Mouse(be, sleep=lambda s: None)
    m.move(300, 200, duration=0.5)  # no start point: a single jump, not a path from nowhere
    assert bus.notify_calls("NotifyPointerMotionAbsolute") == [(42, 300.0, 200.0)]
    m.move(310, 200, duration=0.0)
    assert bus.notify_calls("NotifyPointerMotionAbsolute")[-1] == (42, 310.0, 200.0)
    assert m.position() == Point(310, 200)


# ============================================================================ screenshots
def _png(tmp_path, size, name="shot.png"):
    w, h = size
    img = Image.new("RGB", size, (0, 0, 0))
    # four quadrants with distinct colours
    img.paste((255, 0, 0), (0, 0, w // 2, h // 2))
    img.paste((0, 255, 0), (w // 2, 0, w, h // 2))
    img.paste((0, 0, 255), (0, h // 2, w // 2, h))
    img.paste((255, 255, 0), (w // 2, h // 2, w, h))
    path = tmp_path / name
    img.save(path)
    return str(path)


def test_screenshot_crops_to_the_region_and_deletes_the_file(tmp_path):
    path = _png(tmp_path, (1920, 1080))
    be, bus = make(bus=FakeBus(screenshot_path=path))
    full = be.screenshot()
    assert full.size == (1920, 1080) and full.mode == "RGB"
    assert not os.path.exists(path)  # the portal's file is not left in ~/Pictures
    opts = bus.options_of("Screenshot")
    assert opts["interactive"] is False and opts["modal"] is False
    shot = next(c for c in bus.calls if c[2] == "Screenshot")
    assert shot[3] == "sa{sv}" and shot[4][0] == ""

    path = _png(tmp_path, (1920, 1080))
    part = be.screenshot(Rect(950, 530, 20, 20))
    assert part.size == (20, 20)
    assert part.getpixel((0, 0)) == (255, 0, 0) and part.getpixel((19, 0)) == (0, 255, 0)
    assert part.getpixel((0, 19)) == (0, 0, 255) and part.getpixel((19, 19)) == (255, 255, 0)


def test_screenshot_region_outside_the_layout_is_black(tmp_path):
    path = _png(tmp_path, (1920, 1080))
    be, _ = make(bus=FakeBus(screenshot_path=path))
    img = be.screenshot(Rect(-10, -10, 20, 20))
    assert img.size == (20, 20)
    assert img.getpixel((0, 0)) == (0, 0, 0) and img.getpixel((15, 15)) == (255, 0, 0)


def test_hidpi_screenshot_is_scaled_to_logical_coordinates(tmp_path):
    path = _png(tmp_path, (3840, 2160))  # a 2x monitor; the stream says 1920x1080 logical
    be, _ = make(bus=FakeBus(screenshot_path=path))
    assert be.screenshot().size == (1920, 1080)
    _png(tmp_path, (3840, 2160))
    part = be.screenshot(Rect(940, 520, 40, 40))
    assert part.size == (40, 40)
    assert part.getpixel((5, 5)) == (255, 0, 0) and part.getpixel((35, 35)) == (255, 255, 0)


def test_screenshot_not_matching_the_shared_layout_is_refused(tmp_path):
    path = _png(tmp_path, (3840, 1080))  # two monitors in the image, one shared
    be, _ = make(bus=FakeBus(screenshot_path=path))
    with pytest.raises(BackendError, match="share all monitors"):
        be.screenshot()


def test_keep_screenshots_and_capture_only_mode(tmp_path):
    path = _png(tmp_path, (800, 600))
    be, bus = make(bus=FakeBus(screenshot_path=path), remote_desktop=False, keep_screenshots=True)
    assert be.screenshot(Rect(0, 0, 10, 10)).getpixel((1, 1)) == (255, 0, 0)
    assert os.path.exists(path)
    assert [m.rect for m in be.monitors()] == [Rect(0, 0, 800, 600)]
    assert "CreateSession" not in bus.members()
    assert be.capabilities == frozenset({CAP_SCREENSHOT})
    with pytest.raises(NotSupported, match="remote_desktop=False"):
        be.mouse_move(1, 1)


def test_screenshot_portal_missing_or_denied(tmp_path):
    be, _ = make(bus=FakeBus(versions={P.REMOTE_DESKTOP: 2, P.SCREEN_CAST: 4}))
    with pytest.raises(NotSupported, match="Screenshot"):
        be.screenshot()
    be, _ = make(bus=FakeBus(responses={"Screenshot": P.RESPONSE_CANCELLED}))
    with pytest.raises(PermissionDenied):
        be.screenshot()


# ============================================================================ capabilities / info
def test_capabilities_before_the_session_show_no_dialog():
    be, bus = make()
    caps = be.capabilities
    assert caps == frozenset({CAP_MOUSE, CAP_HSCROLL, CAP_KEYBOARD, CAP_SCREENSHOT})
    assert CAP_WINDOWS not in caps  # windows cannot be activated or moved
    assert "CreateSession" not in bus.members()
    notes = be.limitations()
    assert any("not yet validated" in n for n in notes)
    assert any("not started" in n for n in notes)


def test_cli_info_reports_limitations(monkeypatch):
    import io

    from locum import cli

    be, _ = make()
    monkeypatch.setattr(cli, "_create_backend", lambda name: be)
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["--json", "info"], stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    data = json.loads(out.getvalue())
    assert data["backend"] == "wayland"
    assert "windows" not in data["capabilities"] and "mouse" in data["capabilities"]
    assert any("activated" in n for n in data["limitations"])
    assert "jeepney" in data["optional_dependencies"]


# ============================================================================ windows / AT-SPI
class _Nick:
    def __init__(self, nick):
        self.value_nick = nick


class _States:
    def __init__(self, states):
        self._s = states

    def get_states(self):
        return [_Nick(s) for s in self._s]


class _Ext:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class FakeAcc:
    def __init__(self, role, name="", rect=(0, 0, 0, 0), states=("enabled", "showing", "visible"),
                 children=(), pid=0, actions=()):
        self.role, self.nm, self.rect, self.st, self.pid = role, name, rect, list(states), pid
        self.kids = list(children)
        self.par = None
        for k in self.kids:
            k.par = self
        self.acts = list(actions)
        self.done: List[str] = []

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.nm

    def get_extents(self, coord):
        return _Ext(*self.rect)

    def get_state_set(self):
        return _States(self.st)

    def get_process_id(self):
        return self.pid

    def get_child_count(self):
        return len(self.kids)

    def get_child_at_index(self, i):
        return self.kids[i]

    def get_parent(self):
        return self.par

    def get_index_in_parent(self):
        return self.par.kids.index(self) if self.par else -1

    def get_action_iface(self):
        return self if self.acts else None

    def get_n_actions(self):
        return len(self.acts)

    def get_action_name(self, i):
        return self.acts[i]

    def do_action(self, i):
        self.done.append(self.acts[i])
        return True

    def get_component_iface(self):
        return self

    def get_accessible_at_point(self, x, y, coord):
        for k in self.kids:
            l, t, w, h = k.rect
            if l <= x < l + w and t <= y < t + h:
                return k
        return None


class FakeAtspi:
    class CoordType:
        SCREEN = 0

    def __init__(self, desktop):
        self.desktop = desktop

    def get_desktop(self, i):
        return self.desktop


def _desktop():
    ok = FakeAcc("push button", "OK", rect=(10, 10, 80, 30), actions=["click"])
    edit = FakeAcc("text", "Body", rect=(10, 50, 300, 200), states=("enabled", "showing", "visible", "focused",
                                                                    "editable", "focusable"))
    main = FakeAcc("frame", "Notes - Editor", rect=(0, 0, 400, 300),
                   states=("enabled", "showing", "visible", "active"), children=[edit, ok], pid=100)
    dlg = FakeAcc("dialog", "Save?", rect=(50, 50, 200, 100), pid=100)
    other = FakeAcc("frame", "Terminal", rect=(0, 0, 800, 600), pid=200)
    app1 = FakeAcc("application", "editor", children=[dlg, main], pid=100)
    app2 = FakeAcc("application", "terminal", children=[other], pid=200)
    desk = FakeAcc("desktop frame", "main", children=[app2, app1])
    return desk, main, dlg, other, ok, edit


def test_window_list_is_atspi_with_active_first_and_no_geometry():
    desk, *_ = _desktop()
    be, _ = make(accessibility=FakeAtspi(desk))
    wins = be.list_windows()
    assert [w.title for w in wins] == ["Notes - Editor", "Terminal", "Save?"]
    assert wins[0].is_active and not wins[1].is_active
    assert all(w.rect == Rect(0, 0, 0, 0) for w in wins)  # positions are unknown on Wayland
    assert all(isinstance(w.handle, str) and w.handle.startswith("atspi:") for w in wins)
    assert len({w.handle for w in wins}) == 3
    assert be.foreground_handle() == wins[0].handle
    assert CAP_ACCESSIBILITY in be.capabilities


def test_element_bounds_are_hidden_by_default_and_native_actions_work():
    desk, main, dlg, other, ok, edit = _desktop()
    be, _ = make(accessibility=FakeAtspi(desk))
    root = be.accessibility_roots()[0]
    assert isinstance(root, WaylandAtspiNode)
    kids = root.children()
    assert all(isinstance(k, WaylandAtspiNode) for k in kids)
    button = next(k for k in kids if k.name == "OK")
    assert button.bounds is None and button.is_visible
    assert button.attributes()["atspi_extents"] == "10,10,80,30"
    assert button.invoke() and ok.done == ["click"]
    assert button.parent.handle == root.handle  # owning window is found through the tree
    with pytest.raises(NotSupported, match="hit-testing"):
        be.element_at(20, 20)
    focused = be.focused_element()
    assert focused is not None and focused.name == "Body"


def test_screen_coords_mode_trusts_extents():
    desk, *_ = _desktop()
    be, _ = make(accessibility=FakeAtspi(desk), atspi_coords="screen")
    wins = be.list_windows()
    assert wins[0].rect == Rect(0, 0, 400, 300)
    hit = be.element_at(20, 20)
    assert hit is not None and hit.name == "OK" and hit.bounds == Rect(10, 10, 80, 30)
    with pytest.raises(ValueError):
        make(atspi_coords="sideways")


def test_window_belongs_to_same_app_dialog_only():
    desk, *_ = _desktop()
    be, _ = make(accessibility=FakeAtspi(desk))
    h = {w.title: w.handle for w in be.list_windows()}
    assert be.window_belongs_to(h["Notes - Editor"], h["Notes - Editor"])
    assert be.window_belongs_to(h["Save?"], h["Notes - Editor"])
    assert not be.window_belongs_to(h["Terminal"], h["Notes - Editor"])


def test_window_control_is_not_supported():
    desk, *_ = _desktop()
    be, _ = make(accessibility=FakeAtspi(desk))
    h = be.list_windows()[0].handle
    for call in (lambda: be.activate_window(h), lambda: be.move_window(h, Rect(0, 0, 10, 10)),
                 lambda: be.set_window_state(h, "minimized"), lambda: be.close_window(h)):
        with pytest.raises(NotSupported, match="Wayland"):
            call()
    with pytest.raises(NotSupported, match="where windows are"):
        be.window_handle_at(5, 5)
    with pytest.raises(NotSupported):
        be.windows_above(h)


def test_no_atspi_is_explicit():
    be, _ = make(accessibility=False)
    with pytest.raises(NotSupported, match="AT-SPI"):
        be.list_windows()
    assert CAP_ACCESSIBILITY not in be.capabilities


# ============================================================================ input targets
def test_lenient_targets_become_strict_on_wayland():
    from locum._target import InputTarget

    desk, *_ = _desktop()
    be, _ = make(accessibility=FakeAtspi(desk))
    wins = {w.title: w.handle for w in be.list_windows()}
    lenient = InputTarget(be, wins["Notes - Editor"], strict=False)
    with pytest.raises(TargetLost, match="cannot tell"):
        lenient.check("button", point=(10, 10))  # the window under the point is unknowable
    lenient.check("key", key="a")  # AT-SPI says the target is the active window: allowed
    other = InputTarget(be, wins["Terminal"], strict=False)
    with pytest.raises(TargetLost, match="foreground"):
        other.check("key", key="a")


def test_unknown_foreground_refuses_keys():
    from locum._target import InputTarget

    desk, main, *_ = _desktop()
    main.st.remove("active")  # nothing is reported active
    be, _ = make(accessibility=FakeAtspi(desk))
    assert be.foreground_handle() is None
    target = InputTarget(be, be.list_windows()[0].handle, strict=False)
    with pytest.raises(TargetLost, match="cannot tell"):
        target.check("text")


# ============================================================================ backend selection
@pytest.mark.parametrize("platform,env,jeepney,expected", [
    ("win32", {"XDG_SESSION_TYPE": "wayland"}, True, "windows"),
    ("darwin", {}, True, "macos"),
    ("linux", {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, True, "x11"),
    ("linux", {"DISPLAY": ":0"}, True, "x11"),
    ("linux", {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}, True, "wayland"),
    ("linux", {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}, False, "wayland"),
    # without jeepney, keep using XWayland through the x11 backend (the pre-Wayland behaviour)
    ("linux", {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, False, "x11"),
    ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True, "wayland"),
    ("linux", {"XDG_SESSION_TYPE": "tty", "WAYLAND_DISPLAY": "wayland-0"}, True, "x11"),
    ("freebsd13", {"XDG_SESSION_TYPE": "wayland"}, True, "wayland"),
])
def test_select_platform_backend(platform, env, jeepney, expected):
    assert select_platform_backend(platform, env, jeepney) == expected


def test_is_wayland_session():
    assert is_wayland_session({"XDG_SESSION_TYPE": "Wayland"})
    assert not is_wayland_session({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "w"})
    assert not is_wayland_session({})


def test_create_backend_by_name_and_env(monkeypatch):
    b = create_backend("wayland", bus=FakeBus(), state_path=None, accessibility=False)
    assert isinstance(b, WaylandBackend) and b.name == "wayland"
    monkeypatch.setenv("LOCUM_BACKEND", "wayland")
    b2 = create_backend(bus=FakeBus(), state_path=None, accessibility=False)
    assert isinstance(b2, WaylandBackend)


def test_missing_jeepney_is_backend_unavailable_with_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "jeepney" or name.startswith("jeepney."):
            raise ImportError("No module named 'jeepney'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(BackendUnavailable, match=r"locum\[wayland\]"):
        WaylandBackend(state_path=None)


# ============================================================================ jeepney adapter
def test_jeepney_bus_adapter_messages():
    jeepney = pytest.importorskip("jeepney")
    from jeepney.io.common import FilterHandle, MessageFilters
    from jeepney.low_level import HeaderFields, MessageType
    from jeepney.wrappers import new_error, new_method_return, new_signal, DBusAddress

    class FakeConn:
        unique_name = ":1.7"

        def __init__(self):
            self.sent = []
            self._filters = MessageFilters()
            self.incoming = []

        def send_and_get_reply(self, msg, *, timeout=None):
            self.sent.append(msg)
            member = msg.header.fields.get(HeaderFields.member)
            if member == "Fail":
                return new_error(msg, "org.example.Error.Nope", "s", ("nope",))
            if member == "Get":
                return new_method_return(msg, "v", (("u", 3),))
            return new_method_return(msg)

        def filter(self, rule, *, queue=None, bufsize=1):
            return FilterHandle(self._filters, rule, queue if queue is not None else collections.deque(maxlen=bufsize))

        def recv_until_filtered(self, queue, *, timeout=None):
            while not queue:
                if not self.incoming:
                    raise TimeoutError
                msg = self.incoming.pop(0)
                for f in self._filters.matches(msg):
                    f.queue.append(msg)
            return queue.popleft()

        def close(self):
            self.closed = True

    conn = FakeConn()
    bus = P.JeepneyBus(conn)
    assert bus.unique_name == ":1.7"
    assert PortalClient(bus).version(P.SCREENSHOT) == 3
    get = conn.sent[-1]
    assert get.header.fields[HeaderFields.destination] == P.PORTAL_BUS_NAME
    assert get.header.fields[HeaderFields.path] == P.PORTAL_PATH
    assert get.header.fields[HeaderFields.interface] == P.PROPERTIES
    assert get.header.fields[HeaderFields.signature] == "ss"
    assert get.body == (P.SCREENSHOT, "version")
    with pytest.raises(P.PortalError, match="Nope") as ei:
        bus.call(P.PORTAL_PATH, P.REMOTE_DESKTOP, "Fail", "", ())
    assert ei.value.name == "org.example.Error.Nope"

    path = "/org/freedesktop/portal/desktop/request/1_7/t1"
    w = bus.watch(path, P.REQUEST, "Response")
    add = conn.sent[-1]
    assert add.header.fields[HeaderFields.member] == "AddMatch"
    assert f"path='{path}'" in add.body[0] and "member='Response'" in add.body[0]
    emitter = DBusAddress(path, interface=P.REQUEST)
    conn.incoming.append(new_signal(DBusAddress("/other", interface=P.REQUEST), "Response", "ua{sv}", (2, {})))
    conn.incoming.append(new_signal(emitter, "Response", "ua{sv}", (0, {"uri": ("s", "file:///x.png")})))
    body = w.wait(1.0)
    assert body == (0, {"uri": ("s", "file:///x.png")})
    assert w.poll() is None
    with pytest.raises(TimeoutError):
        w.wait(0.01)
    w.close()
    assert conn.sent[-1].header.fields[HeaderFields.member] == "RemoveMatch"
    assert conn.sent[-1].header.message_type == MessageType.method_call


# ============================================================================ real session
@pytest.mark.wayland
@pytest.mark.skipif(os.environ.get("LOCUM_TEST_WAYLAND") != "1",
                    reason="needs a real Wayland session with xdg-desktop-portal (set LOCUM_TEST_WAYLAND=1); "
                           "it shows the remote-desktop approval dialog and moves the real pointer")
def test_real_portal_session():  # pragma: no cover - needs a real session
    be = WaylandBackend()
    try:
        assert be.capabilities
        mons = be.monitors()
        assert mons and mons[0].rect.width > 0
        img = be.screenshot(Rect(mons[0].rect.left, mons[0].rect.top, 64, 64))
        assert img.size == (64, 64)
        c = mons[0].rect.center
        be.mouse_move(c.x, c.y)
        assert be.mouse_position() == c
    finally:
        be.close()
