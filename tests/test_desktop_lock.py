"""The desktop input lock: compound input actions from different threads/processes never interleave."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid

import pytest

from _stub_desktop import StubDesktop
from locum import _inputlock
from locum.backends.mock import MockBackend
from locum.errors import DesktopBusy
from locum.keyboard import Keyboard

HERE = os.path.dirname(os.path.abspath(__file__))


def _typed_runs(be):
    """The characters typed, in order, from the mock event log."""
    return "".join(e[1] for e in be.events if e[0] == "type")


def _race(be, n=20):
    kbs = [Keyboard(be, sleep=time.sleep, clipboard_settle=0) for _ in range(2)]
    start = threading.Barrier(2)
    errors = []

    def worker(kb, ch):
        try:
            start.wait(5)
            kb.type(ch * n, interval=0.005, mode="unicode")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(kb, ch)) for kb, ch in zip(kbs, "AB")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not errors
    return _typed_runs(be)


def test_concurrent_typing_is_serialised():
    be = MockBackend(width=800, height=600)
    typed = _race(be)
    assert sorted(typed) == sorted("A" * 20 + "B" * 20)
    assert typed in ("A" * 20 + "B" * 20, "B" * 20 + "A" * 20), typed


def test_without_the_lock_the_same_race_interleaves(monkeypatch):
    # Sanity check that the test above is meaningful.
    monkeypatch.setenv("LOCUM_DESKTOP_LOCK", "0")
    be = MockBackend(width=800, height=600)
    typed = _race(be)
    assert typed not in ("A" * 20 + "B" * 20, "B" * 20 + "A" * 20)


def test_element_set_text_is_one_input_action():
    """Thread B's typing cannot land between A's select-all, delete and typing."""
    be = MockBackend(width=800, height=600)
    win = be.add_window("Form", (0, 0, 600, 400), app="app.exe")
    win.add_edit("Amount", (10, 10, 200, 24))
    d = StubDesktop(be)
    el = d.find('edit "Amount"')
    other = Keyboard(be, sleep=time.sleep, clipboard_settle=0)
    in_a = threading.Event()
    orig = be.type_text

    def slow_type(text):
        in_a.set()
        time.sleep(0.05)
        orig(text)

    be.type_text = slow_type
    got = []

    def b():
        in_a.wait(5)
        t0 = time.monotonic()
        with _inputlock.hold(other):
            got.append(time.monotonic() - t0)

    tb = threading.Thread(target=b)
    tb.start()
    el.set_text("1250", native=False)
    tb.join(10)
    assert got and got[0] > 0.02  # B waited for A's whole set_text


def test_reentrant_within_a_thread():
    be = MockBackend(width=800, height=600)
    d = StubDesktop(be)
    with _inputlock.desktop_input(d):
        with _inputlock.desktop_input(d):
            d.keyboard.press("a")  # takes it a third time
        lock = _inputlock._lock_for(d)
        assert lock.held_here and lock.depth == 1
    assert _inputlock._lock_for(d).owner is None


def test_busy_lock_times_out_and_try_never_waits():
    be = MockBackend(width=800, height=600)
    d = StubDesktop(be)
    held, done = threading.Event(), threading.Event()

    def owner():
        with _inputlock.hold(d):
            held.set()
            done.wait(10)

    t = threading.Thread(target=owner, name="owner-thread")
    t.start()
    held.wait(5)
    try:
        t0 = time.monotonic()
        with _inputlock.try_desktop_input(d) as got:
            assert got is False
        assert time.monotonic() - t0 < 0.1
        with pytest.raises(DesktopBusy, match="busy for 0.1 s"):
            with _inputlock.hold(d, timeout=0.1):
                pass
        # Releases never wait for the lock.
        t0 = time.monotonic()
        d.keyboard.release_all()
        d.mouse.release_all()
        assert time.monotonic() - t0 < 0.5
    finally:
        done.set()
        t.join(5)
    with _inputlock.try_desktop_input(d) as got:
        assert got is True


def test_desktop_timeout_reaches_keyboard_and_mouse():
    from locum.desktop import Desktop

    d = Desktop(MockBackend(width=800, height=600), failsafe=False, input_lock_timeout=0.05)
    assert d.keyboard.input_lock_timeout == d.mouse.input_lock_timeout == 0.05
    with pytest.raises(ValueError):
        Desktop(MockBackend(width=800, height=600), input_lock_timeout=-1)
    with pytest.raises(ValueError):
        Desktop(MockBackend(width=800, height=600), decimal_separator=";")


def test_simulated_desktops_do_not_contend():
    a, b = StubDesktop(MockBackend(width=800, height=600)), StubDesktop(MockBackend(width=800, height=600))
    assert _inputlock._lock_for(a) is not _inputlock._lock_for(b)


def test_real_backends_share_one_session_lock():
    class Fake:
        name = "windows"

    assert _inputlock._lock_name(Fake()) == _inputlock.LOCK_NAME


def test_disabled_by_environment(monkeypatch):
    monkeypatch.setenv("LOCUM_DESKTOP_LOCK", "0")
    d = StubDesktop(MockBackend(width=800, height=600))
    with _inputlock.hold(d):
        assert _inputlock._lock_for(d).owner is None


# ------------------------------------------------------------ across processes
def _child(name, ready, seconds, abandon=False):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([HERE, os.environ.get("PYTHONPATH", "")]))
    code = f"import _lock_workers as w; w.hold_lock({name!r}, {ready!r}, {seconds}, {abandon})"
    return subprocess.Popen([sys.executable, "-c", code], env=env)


def _wait_for(path, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise AssertionError(f"child never acquired the lock ({path})")
        time.sleep(0.02)


class _Named:
    def __init__(self, name):
        self.desktop_lock_name = name


def test_lock_is_shared_across_processes(tmp_path):
    name = f"locum-test-{uuid.uuid4().hex}"
    ready = str(tmp_path / "ready")
    p = _child(name, ready, 1.5)
    try:
        _wait_for(ready)
        with _inputlock.try_desktop_input(_Named(name)) as got:
            assert got is False
        with pytest.raises(DesktopBusy) as exc:
            with _inputlock.hold(_Named(name), timeout=0.2):
                pass
        with open(ready, encoding="utf-8") as fh:
            assert f"pid {fh.read().strip()}" in str(exc.value)  # names the holder
        with _inputlock.hold(_Named(name), timeout=20):  # waits for the child to finish
            pass
    finally:
        p.wait(20)
    assert p.returncode == 0


def test_lock_of_a_dead_process_is_released(tmp_path):
    name = f"locum-test-{uuid.uuid4().hex}"
    ready = str(tmp_path / "ready")
    p = _child(name, ready, 0.2, abandon=True)
    _wait_for(ready)
    p.wait(20)
    with _inputlock.hold(_Named(name), timeout=5):
        pass
