"""Polish pass 3, round 2: regressions an adversarial review found in the first round's fixes."""
from __future__ import annotations

import tempfile
import time
from typing import Any, Dict

import pytest

import locum
from locum.backends.mock import MockBackend, MockNode
from locum.browser import cdp
from locum.errors import FailSafeTriggered
from locum.rpa.robot import Robot


# ------------------------------------------- drag onto an element in another window
def _two_windows():
    be = MockBackend()
    be.add_window("Source", rect=(0, 0, 400, 400), app="explorer").add_button("File", (20, 20, 80, 30))
    be.add_window("Target", rect=(500, 0, 400, 400), app="app").add_button("Drop", (20, 20, 120, 40))
    return be, locum.Desktop(backend=be, sleep=lambda s: None, failsafe=False)


def test_drag_from_a_point_onto_an_element_in_another_window():
    be, d = _two_windows()
    d.drag((50, 35), 'button "Drop"', duration=0)


def test_drag_from_the_cursor_onto_an_element_in_another_window():
    be, d = _two_windows()
    be.mouse_move(50, 35)
    d.drag(None, 'button "Drop"', duration=0)


def test_drag_from_an_element_onto_a_point_in_another_window():
    be, d = _two_windows()
    d.drag('button "File"', (700, 300), duration=0)


# ----------------------------------------------------- placeholder: failed reads
def test_a_failed_placeholder_read_is_not_cached(monkeypatch):
    be = MockBackend()
    e = be.add_window("App").add_edit("Search", (10, 10, 200, 24), value="Type to search")
    e.attrs["placeholder"] = "Type to search"
    d = locum.Desktop(backend=be)
    el = d.find('edit "Search"')
    orig = MockNode.attributes
    n = {"c": 0}

    def flaky(self):
        n["c"] += 1
        if n["c"] == 1:
            raise OSError("COM busy (transient)")
        return orig(self)

    monkeypatch.setattr(MockNode, "attributes", flaky)
    el.text  # the failed read: the value cannot be told from the placeholder
    assert el.text == ""


def test_the_placeholder_cache_expires(monkeypatch):
    be = MockBackend()
    e = be.add_window("App").add_edit("Search", (10, 10, 200, 24), value="Find")
    d = locum.Desktop(backend=be)
    el = d.find('edit "Search"')
    assert el.text == "Find"
    e.attrs["placeholder"] = "Find"  # the app sets a placeholder later (e.g. on focus)
    clock = {"t": 1000.0}
    monkeypatch.setattr(locum.element.time, "monotonic", lambda: clock["t"])
    el.__dict__.pop("_placeholder_cache", None)
    e.attrs.pop("placeholder")
    assert el.text == "Find"
    e.attrs["placeholder"] = "Find"
    clock["t"] += locum.element._PLACEHOLDER_TTL + 0.01
    assert el.text == ""


# ------------------------------------------- a returned report's save error raises
def test_fail_safe_run_save_error_still_raises():
    r = Robot("r", run_dir=tempfile.mkdtemp(), save_reports=True)

    @r.task
    def t():
        raise FailSafeTriggered("corner")

    def boom(report=None):
        raise OSError("disk full")

    r.save_report = boom
    with pytest.raises(OSError):
        r.run()


# ------------------------------------- CDP wait_for: busy page at the deadline
class _BusySession:
    broken = False

    def call(self, method: str, params: Any = None, timeout: float = 30.0) -> Dict[str, Any]:
        # Like Session.call: raise only once the monotonic clock has passed the per-call deadline
        # (time.sleep alone can return a little early on Windows' coarse monotonic clock).
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            time.sleep(0.01)
        raise cdp.CDPError(f"DevTools call {method} timed out after {timeout:g}s")


def test_wait_for_on_a_busy_page_raises_element_not_found_at_the_deadline():
    p = cdp.Page.__new__(cdp.Page)
    p.session = _BusySession()
    with pytest.raises(cdp.ElementNotFound):
        p.wait_for("#x", timeout=0.15)


def test_wait_for_still_raises_other_devtools_errors():
    class Broken(_BusySession):
        def call(self, method, params=None, timeout=30.0):
            raise cdp.CDPError("DevTools connection lost")

    p = cdp.Page.__new__(cdp.Page)
    p.session = Broken()
    with pytest.raises(cdp.CDPError, match="connection lost"):
        p.wait_for("#x", timeout=5)


# --------------------------- click: "enabled" is re-checked once the lock is held
def test_click_rechecks_enabled_after_waiting_for_the_input_lock():
    import threading

    from locum import _inputlock

    be = MockBackend()
    hits = []
    btn = be.add_window("App").add_button("Post", (10, 10, 80, 24), on_click=lambda *a: hits.append(1))
    d = locum.Desktop(backend=be, default_timeout=0.3, failsafe=False, sleep=lambda s: None)
    el = d.find('button "Post"')
    held, release = threading.Event(), threading.Event()

    def other_worker():
        with _inputlock.hold(d):
            held.set()
            release.wait(5)
            btn.enabled = False  # its input disabled the button before letting go

    t = threading.Thread(target=other_worker)
    t.start()
    held.wait(5)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    with pytest.raises(locum.ElementNotFound, match="disabled"):
        el.click()
    t.join()
    assert hits == []


# ------------------------------------------- tracer: concurrent instrumentation
def test_tracer_instruments_an_object_once_under_concurrency_and_restores_it(monkeypatch):
    import threading

    from locum import trace as trace_mod
    from locum.trace import Tracer

    real_ref = trace_mod.weakref.ref

    class SlowWeakref:  # widen the check-then-register window so every thread is inside it
        @staticmethod
        def ref(*a, **kw):
            time.sleep(0.05)
            return real_ref(*a, **kw)

    monkeypatch.setattr(trace_mod, "weakref", SlowWeakref)

    be = MockBackend()
    be.add_window("App").add_button("OK", (10, 10, 50, 20))
    d = locum.Desktop(backend=be)
    el = d.find('button "OK"')
    original = {name: el.__dict__.get(name) for name in ("click", "type")}
    tr = Tracer(d).start()
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        tr._instrument_result(el)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    tr.stop()
    for name, value in original.items():
        assert el.__dict__.get(name) is value  # the original, not a leftover wrapper


def test_tracer_patches_nothing_after_stop():
    from locum.trace import Tracer

    be = MockBackend()
    be.add_window("App").add_button("OK", (10, 10, 50, 20))
    d = locum.Desktop(backend=be)
    el = d.find('button "OK"')
    tr = Tracer(d).start()
    tr.stop()
    tr._instrument_result(el)
    assert "click" not in el.__dict__


# ---------------------------------------------- observe: unusable runtime ids
@pytest.mark.parametrize("rid", [None, [], (), "abc", 42, [[1], [2]], object()])
def test_unusable_runtime_ids_are_ignored(rid):
    from locum.llm.observe import _rid_key

    assert _rid_key(rid) is None


def test_runtime_id_with_an_ambiguous_truth_value_is_ignored():
    np = pytest.importorskip("numpy")
    from locum.llm.observe import _rid_key

    assert _rid_key(np.array([1, 2])) == (1, 2)
    assert _rid_key([1, 2]) == (1, 2)
