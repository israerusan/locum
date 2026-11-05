"""Regression tests for the sixth review round (interrupt safety, abort latency, hooks...)."""

from __future__ import annotations

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop


class _InterruptAfterPress(MockBackend):
    """key_down/mouse_down deliver the press, then a Ctrl+C lands while the call still waits."""

    interrupt = True

    def key_down(self, key):
        super().key_down(key)
        if self.interrupt:
            raise KeyboardInterrupt

    def mouse_down(self, button="left"):
        super().mouse_down(button)
        if self.interrupt:
            raise KeyboardInterrupt


@pytest.mark.parametrize("action", [
    lambda d: d.keyboard.down("shift"),
    lambda d: d.keyboard.press("a"),
    lambda d: d.keyboard.hotkey("ctrl", "c"),
    lambda d: d.type("Aa", mode="keys"),
])
def test_key_interrupted_inside_backend_is_released(action):
    be = _InterruptAfterPress()
    d = Desktop(be, failsafe=False)
    with pytest.raises(KeyboardInterrupt):
        action(d)
    be.interrupt = False
    d.close()
    assert be.pressed_keys == set()


@pytest.mark.parametrize("action", [
    lambda d: d.mouse.down(),
    lambda d: d.mouse.click((10, 10)),
    lambda d: d.mouse.drag((10, 10), (50, 50), duration=0),
])
def test_button_interrupted_inside_backend_is_released(action):
    be = _InterruptAfterPress()
    d = Desktop(be, failsafe=False)
    with pytest.raises(KeyboardInterrupt):
        action(d)
    be.interrupt = False
    d.close()
    assert be.pressed_buttons == set()


def test_keyboard_down_error_is_not_recorded():
    class Bad(MockBackend):
        def key_down(self, key):
            raise ValueError("unknown key")

    d = Desktop(Bad(), failsafe=False)
    with pytest.raises(ValueError):
        d.keyboard.down("f13")
    assert d.keyboard.pressed == []


def test_desktop_close_calls_backend_release_all():
    calls = []

    class Tracked(MockBackend):
        def release_all(self):
            calls.append("release_all")

    d = Desktop(Tracked(), failsafe=False)
    d.close()
    assert calls == ["release_all"]


# --------------------------------------------------------------------------- X11 after Ctrl+C
def _keys_down(display_name):
    from Xlib import display as xdisplay

    x = xdisplay.Display(display_name)
    try:
        km = x.query_keymap()
        return [i * 8 + j for i, byte in enumerate(km) for j in range(8) if byte & (1 << j)]
    finally:
        x.close()


def _run_bounded(fn, timeout=5.0):
    import threading

    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # pragma: no cover - reported below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "the X11 backend call blocked forever"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _interrupt_mid_reply(backend):
    """Make the next query_pointer behave like a Ctrl+C landing while python-xlib waits for the reply."""

    root = backend._root

    def interrupted(*a, **k):
        del root.query_pointer  # one shot
        # What an interrupted send_and_recv leaves behind: python-xlib believes another
        # thread is still sending/receiving, so every later request waits forever.
        backend._d.display.send_active = 1
        backend._d.display.recv_active = 1
        raise KeyboardInterrupt

    root.query_pointer = interrupted


def test_x11_backend_recovers_after_interrupted_round_trip(xvfb_display):
    pytest.importorskip("Xlib")
    from pypilot.backends.x11 import X11Backend

    b = X11Backend(xvfb_display, accessibility=False)
    try:
        b.key_down("shift")
        _interrupt_mid_reply(b)
        with pytest.raises(KeyboardInterrupt):
            b.mouse_position()
        # The next call works (fresh connection) instead of blocking forever.
        assert tuple(_run_bounded(lambda: (b.mouse_move(5, 6), b.mouse_position())[1])) == (5, 6)
    finally:
        _run_bounded(b.close)
    assert _keys_down(xvfb_display) == []  # the held shift was released by close()


def test_x11_close_after_interrupt_does_not_hang_and_releases_keys(xvfb_display):
    pytest.importorskip("Xlib")
    from pypilot.backends.x11 import X11Backend

    b = X11Backend(xvfb_display, accessibility=False)
    b.key_down("shift")
    _interrupt_mid_reply(b)
    with pytest.raises(KeyboardInterrupt):
        b.mouse_position()
    _run_bounded(b.close)  # also what the exit-time finalizer runs
    assert _keys_down(xvfb_display) == []


def test_x11_native_clipboard_recovers_after_interrupt(xvfb_display):
    pytest.importorskip("Xlib")
    from pypilot.backends._x11clip import X11Clipboard

    clip = X11Clipboard(xvfb_display)
    try:
        clip.set("first")
        d = clip._d
        real = d.get_selection_owner

        def interrupted(*a, **k):
            d.get_selection_owner = real
            d.display.send_active = d.display.recv_active = 1  # left mid-request
            raise KeyboardInterrupt

        d.get_selection_owner = interrupted
        with pytest.raises(KeyboardInterrupt):
            clip.set("second")
        _run_bounded(lambda: clip.set("third"))
        assert _run_bounded(clip.get) == "third"
    finally:
        _run_bounded(clip.close)


# --------------------------------------------------------------------------- clipboard paste vs. slow apps
class _ClipTracking(MockBackend):
    def __init__(self, read):
        super().__init__()
        self.read = read
        self.sets = []

    def set_clipboard(self, text):
        self.sets.append(text)
        super().set_clipboard(text)

    def wait_clipboard_read(self, since, timeout):
        return self.read


def test_clipboard_paste_default_settle_is_long_enough_and_configurable():
    from pypilot.keyboard import DEFAULT_CLIPBOARD_SETTLE

    slept = []
    be = _ClipTracking(None)  # the backend cannot tell when the app read it
    be.clipboard = "old"
    d = Desktop(be, failsafe=False, sleep=slept.append)
    d.type("new", mode="clipboard")
    assert DEFAULT_CLIPBOARD_SETTLE >= 0.5 and DEFAULT_CLIPBOARD_SETTLE in slept
    assert be.sets == ["new", "old"]
    d2 = Desktop(be, failsafe=False, sleep=slept.append, clipboard_settle=1.5)
    assert d2.keyboard.clipboard_settle == 1.5


def test_clipboard_not_restored_while_the_app_has_not_read_the_paste():
    be = _ClipTracking(False)  # the app has not fetched the text yet (busy)
    be.clipboard = "DE89 previous IBAN"
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    d.type("GB29 NWBK", mode="clipboard")
    assert be.sets == ["GB29 NWBK"]  # a late paste still inserts the typed text
    be2 = _ClipTracking(True)
    be2.clipboard = "old"
    Desktop(be2, failsafe=False, sleep=lambda s: None).type("new", mode="clipboard")
    assert be2.sets == ["new", "old"]


def test_x11_native_clipboard_paste_waits_for_slow_app(xvfb_display):
    pytest.importorskip("Xlib")
    import threading
    import time

    from Xlib import X
    from Xlib import display as xdisplay

    from pypilot.backends.x11 import X11Backend

    app = xdisplay.Display(xvfb_display)
    root = app.screen().root
    w = root.create_window(0, 0, 300, 100, 0, X.CopyFromParent, event_mask=X.KeyPressMask)
    w.map()
    app.sync()
    time.sleep(0.2)
    w.set_input_focus(X.RevertToParent, X.CurrentTime)
    app.sync()
    got = {}

    def slow_text_field():  # requests CLIPBOARD 0.4 s after Ctrl+V (a busy UI thread)
        clip, utf8, prop = (app.intern_atom(n) for n in ("CLIPBOARD", "UTF8_STRING", "PYPILOT_T"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ev = app.next_event()
            if ev.type == X.KeyPress and app.keycode_to_keysym(ev.detail, 0) == ord("v") and ev.state & X.ControlMask:
                time.sleep(0.4)
                w.convert_selection(clip, utf8, prop, X.CurrentTime)
                app.flush()
                while True:
                    e = app.next_event()
                    if e.type == X.SelectionNotify:
                        r = w.get_full_property(prop, X.AnyPropertyType)
                        got["text"] = r.value.decode() if r else None
                        return

    t = threading.Thread(target=slow_text_field, daemon=True)
    t.start()
    b = X11Backend(xvfb_display, accessibility=False, clipboard_tool="native")
    try:
        d = Desktop(b, failsafe=False, clipboard_settle=0.05)
        d.clipboard_set("DE89 previous payee IBAN")
        d.type("GB29 NWBK 6016 1331 9268 19", mode="clipboard")
        t.join(5)
        assert got.get("text") == "GB29 NWBK 6016 1331 9268 19"
        assert d.clipboard_get() == "DE89 previous payee IBAN"  # restored once the app had read it
    finally:
        b.close()
        app.close()


# --------------------------------------------------------------------------- abort during long sleeps
@pytest.fixture
def _clean_abort():
    from pypilot import failsafe

    failsafe.reset_abort()
    yield failsafe
    for t in _timers:
        t.cancel()
    _timers.clear()
    failsafe.reset_abort()


_timers = []


def _abort_soon(failsafe, after=0.15):
    import threading

    t = threading.Timer(after, lambda: failsafe.abort("kill hotkey pressed"))
    _timers.append(t)
    t.start()
    return t


def test_desktop_sleep_honours_abort_promptly(_clean_abort):
    import time

    from pypilot.errors import FailSafeTriggered

    d = Desktop(MockBackend(), failsafe=False)
    _abort_soon(_clean_abort)
    t0 = time.monotonic()
    with pytest.raises(FailSafeTriggered, match="kill hotkey"):
        d.sleep(5)
    assert time.monotonic() - t0 < 1.0


def test_desktop_sleep_with_injected_sleep_is_sliced(_clean_abort):
    from pypilot.errors import FailSafeTriggered

    calls = []

    def fake_sleep(s):
        calls.append(s)
        if len(calls) == 3:
            _clean_abort.abort("stop")

    d = Desktop(MockBackend(), failsafe=False, sleep=fake_sleep)
    with pytest.raises(FailSafeTriggered):
        d.sleep(60)
    assert len(calls) == 3 and max(calls) <= 0.1 + 1e-9


def test_queue_retry_wait_honours_abort_promptly(_clean_abort, tmp_path):
    import time

    from pypilot.errors import FailSafeTriggered
    from pypilot.rpa import WorkQueue
    from pypilot.rpa.queue import NEW

    q = WorkQueue("q", path=tmp_path / "q.json")
    q.add({"n": 1}, reference="A")

    def handler(item):
        raise RuntimeError("SAP timeout")

    _abort_soon(_clean_abort)
    t0 = time.monotonic()
    with pytest.raises(FailSafeTriggered):
        q.process(handler, retries=1, retry_delay=5)
    assert time.monotonic() - t0 < 1.0
    assert q.get("A").status == NEW  # aborted between attempts: nothing half done


def test_robot_sleep_without_desktop_honours_abort(_clean_abort):
    import time

    from pypilot.errors import FailSafeTriggered
    from pypilot.rpa import Robot

    r = Robot(name="r", save_reports=False)
    _abort_soon(_clean_abort)
    t0 = time.monotonic()
    with pytest.raises(FailSafeTriggered):
        r.sleep(5)
    assert time.monotonic() - t0 < 1.0


# --------------------------------------------------------------------------- recorder hooks
def test_two_backend_recorders_stop_in_any_order():
    from pypilot.rpa.recorder import Recorder

    be = MockBackend()
    original = be.mouse_down
    d = Desktop(be, failsafe=False)
    r1 = Recorder(d, use_selectors=False).start("backend")
    r2 = Recorder(d, use_selectors=False).start("backend")
    d.click((100, 100))
    r1.stop()
    d.click((200, 200))
    assert [a.kind for a in r1.actions] == ["click"]
    assert [a.kind for a in r2.actions] == ["click", "click"]
    r2.stop()
    d.click((300, 300))
    assert len(r1.actions) == 1 and len(r2.actions) == 2
    assert "mouse_down" not in vars(be) and be.mouse_down == original  # fully unhooked
    # A later wrapper is left alone when the recorder stops.
    r3 = Recorder(d, use_selectors=False).start("backend")
    hooked = be.mouse_down
    calls = []
    be.mouse_down = lambda button="left": (calls.append(button), hooked(button))[1]
    r3.stop()
    d.click((10, 10))
    assert calls and len(r3.actions) == 0


# --------------------------------------------------------------------------- kill hotkey health check
def _fake_pynput(monkeypatch, listener_cls):
    import sys
    import types

    kb = types.ModuleType("pynput.keyboard")
    kb.GlobalHotKeys = listener_cls
    pkg = types.ModuleType("pynput")
    pkg.keyboard = kb
    monkeypatch.setitem(sys.modules, "pynput", pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", kb)


class _FakeListener:
    ready_after_start = True
    alive_after_start = True
    trusted = None

    def __init__(self, mapping):
        self._ready = False
        self._alive = False
        self.stopped = False
        if self.trusted is not None:
            self.IS_TRUSTED = False

    def start(self):
        self._alive = self.alive_after_start
        self._ready = self.ready_after_start
        if self.trusted is not None:
            self.IS_TRUSTED = self.trusted

    def is_alive(self):
        return self._alive

    def stop(self):
        if not self._ready:  # like pynput: stop() of a never-ready listener blocks forever
            raise AssertionError("stop() on a listener that never became ready would hang")
        self.stopped = True


def test_kill_hotkey_reports_failure_when_listener_dies(monkeypatch):
    from pypilot import failsafe

    class Dead(_FakeListener):  # X server without RECORD: the thread dies before it is ready
        ready_after_start = False
        alive_after_start = False

    _fake_pynput(monkeypatch, Dead)
    try:
        assert failsafe.install_kill_hotkey() is False
    finally:
        failsafe.uninstall_kill_hotkey()


def test_kill_hotkey_reports_failure_when_untrusted_on_macos(monkeypatch):
    from pypilot import failsafe

    created = []

    class Untrusted(_FakeListener):  # macOS without Accessibility permission
        trusted = False

        def __init__(self, mapping):
            super().__init__(mapping)
            created.append(self)

    _fake_pynput(monkeypatch, Untrusted)
    try:
        assert failsafe.install_kill_hotkey() is False
        assert created[0].stopped
    finally:
        failsafe.uninstall_kill_hotkey()


def test_kill_hotkey_working_listener(monkeypatch):
    from pypilot import failsafe

    class Good(_FakeListener):
        trusted = True

    _fake_pynput(monkeypatch, Good)
    try:
        assert failsafe.install_kill_hotkey() is True
    finally:
        failsafe.uninstall_kill_hotkey()


# --------------------------------------------------------------------------- Windows mss grabbers per thread
def test_windows_mss_grabber_closed_when_its_thread_ends(monkeypatch):
    import gc
    import sys
    import threading
    import types

    from pypilot.backends.windows import WindowsBackend

    opened, closed = [], []

    class FakeMSS:
        def __init__(self):
            opened.append(self)

        def close(self):
            closed.append(self)

    fake = types.ModuleType("mss")
    fake.MSS = FakeMSS
    monkeypatch.setitem(sys.modules, "mss", fake)
    b = WindowsBackend(_api=object(), dpi_aware=False, accessibility=False)
    for _ in range(20):  # e.g. a service handling each job on a fresh worker thread
        t = threading.Thread(target=b._mss)
        t.start()
        t.join()
    gc.collect()
    assert len(opened) == 20 and len(closed) == 20  # nothing piles up
    main = b._mss()
    assert b._mss() is main  # one per thread, reused
    b.close()
    assert closed[-1] is main
