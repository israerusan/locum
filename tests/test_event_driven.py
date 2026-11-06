"""Event-driven waits and observe reuse (P1 #6 of the 2026-11-05 live-session report).

A fake change tracker stands in for UI Automation events on the mock backend.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.llm.observe import observe


class FakeTracker:
    def __init__(self):
        self.generation = 0
        self.alive = True
        self.log = []
        self.cond = threading.Condition()

    def bump(self, pid):
        with self.cond:
            self.generation += 1
            self.log.append((self.generation, pid))
            self.cond.notify_all()

    def wait(self, since, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.generation > since, timeout)
            return self.generation

    def dirty_since(self, since):
        return {p for g, p in self.log if g > since}


@pytest.fixture
def env(monkeypatch):
    be = MockBackend(width=1280, height=800)
    a = be.add_window("Calculator", (0, 0, 400, 400), app="calc", pid=11)
    a.add_button("Seven", (10, 100, 80, 40))
    b = be.add_window("Notes", (500, 0, 400, 400), app="notes", pid=22)
    b.add_button("Save", (10, 100, 80, 40))
    tracker = FakeTracker()
    monkeypatch.setattr(be, "change_tracker", lambda: tracker)
    return be, a, b, tracker


def _nodes(obs, title):
    return [e.node for e in obs.elements if e.window_title == title]


def test_unchanged_windows_are_reused_and_dirty_ones_rewalked(env):
    be, a, b, tracker = env
    d = Desktop(be, failsafe=False)
    o1 = observe(d, screenshot=False)
    o2 = observe(d, screenshot=False, reuse=o1)
    assert o2.reused_windows == 2
    assert _nodes(o2, "Calculator")[0] is _nodes(o1, "Calculator")[0]  # not re-walked
    a.find("Seven").name = "Eight"
    tracker.bump(11)  # the calculator process raised an event
    o3 = observe(d, screenshot=False, reuse=o2)
    assert o3.reused_windows == 1
    assert [e.name for e in o3.elements if e.window_title == "Calculator"] == ["Eight"]
    assert _nodes(o3, "Notes")[0] is _nodes(o1, "Notes")[0]
    assert [e.id for e in o3.elements] == list(range(1, len(o3.elements) + 1))


def test_moved_window_or_other_options_are_not_reused(env):
    be, a, b, tracker = env
    d = Desktop(be, failsafe=False)
    o1 = observe(d, screenshot=False)
    d.window("Notes").move(600, 50)
    o2 = observe(d, screenshot=False, reuse=o1)
    assert o2.reused_windows == 1  # only the calculator
    o3 = observe(d, screenshot=False, reuse=o2, interactive_only=False)
    assert o3.reused_windows == 0


def test_occlusion_is_recomputed_for_reused_elements(env):
    be, a, b, tracker = env
    d = Desktop(be, failsafe=False)
    o1 = observe(d, screenshot=False)
    assert not any(e.occluded for e in o1.elements)
    be.add_window("Cover", (0, 0, 300, 300), app="cover", pid=33)  # covers Seven's center
    o2 = observe(d, screenshot=False, reuse=o1)
    seven = next(e for e in o2.elements if e.name == "Seven")
    assert seven.occluded and seven.node is _nodes(o1, "Calculator")[0]


def test_no_tracker_means_no_reuse():
    be = MockBackend()
    be.add_window("W", (0, 0, 300, 300)).add_button("B", (10, 10, 50, 30))
    d = Desktop(be, failsafe=False)
    o1 = observe(d, screenshot=False)
    assert observe(d, screenshot=False, reuse=o1).reused_windows == 0


def test_wait_wakes_on_an_event_and_polls_slowly_otherwise(env):
    be, a, b, tracker = env
    d = Desktop(be, failsafe=False)  # real clock and sleep: event-driven
    calls = []
    real_roots = be.accessibility_roots
    be.accessibility_roots = lambda: calls.append(1) or real_roots()

    def later():
        time.sleep(0.6)
        a.add_button("Later", (100, 100, 80, 40))
        tracker.bump(11)

    threading.Thread(target=later, daemon=True).start()
    t = time.monotonic()
    d.find('button "Later"', timeout=5)
    took = time.monotonic() - t
    assert took < 1.0  # woke on the event, not at the next slow poll
    calls.clear()
    with pytest.raises(Exception):
        d.find('button "Never"', timeout=1.2)
    assert len(calls) <= 5  # ~every 0.5 s instead of every 0.1 s (12+ searches)


def test_event_storm_never_polls_faster_than_the_interval(env):
    be, a, b, tracker = env
    d = Desktop(be, failsafe=False)
    stop = threading.Event()

    def storm():
        while not stop.is_set():
            tracker.bump(22)
            time.sleep(0.001)

    calls = []
    real_roots = be.accessibility_roots
    be.accessibility_roots = lambda: calls.append(1) or real_roots()
    threading.Thread(target=storm, daemon=True).start()
    try:
        with pytest.raises(Exception):
            d.find('button "Never"', timeout=1.0)
    finally:
        stop.set()
    assert len(calls) <= 13  # bounded by poll_interval (0.1 s), not by the event rate


def test_injected_sleep_keeps_plain_polling(env):
    be, a, b, tracker = env
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    pred, sleep, interval = d._event_wait(lambda: True, 0.1)
    assert interval == 0.1 and sleep == d.sleep


@pytest.mark.skipif(sys.platform != "win32", reason="real UI Automation events")
def test_real_uia_change_tracker_starts_and_stops():
    pytest.importorskip("comtypes")
    from locum.backends.windows import WindowsBackend

    be = WindowsBackend()
    try:
        tr = be.change_tracker()
        if tr is None:
            pytest.skip("UI Automation events unavailable here")
        assert tr.alive and isinstance(tr.generation, int)
        assert tr.dirty_since(tr.generation) == set()
    finally:
        be.close()
    assert not tr.alive
