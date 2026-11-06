"""observe(time_budget=...): one huge or slow application cannot stall the observation.

Live session 2026-11-05: ~3,800 nodes over 10 windows (Edge ~2,400); a full-desktop
observe must stay bounded and return what it has, naming what it skipped.
"""

from __future__ import annotations

import importlib

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.llm.tools import DesktopToolkit

observe_mod = importlib.import_module("locum.llm.observe")


def _desktop():
    be = MockBackend(width=1600, height=900)
    front = be.add_window("Calculator", (0, 0, 400, 400), app="calc")
    front.add_button("Equals", (10, 10, 50, 30))
    big = be.add_window("Edge", (420, 0, 800, 800), app="msedge")
    for i in range(200):
        big.add_button(f"Link {i}", (10 + (i % 10) * 70, 10 + (i // 10) * 35, 60, 30))
    be.add_window("Notes", (0, 420, 400, 400), app="notes").add_button("Save", (10, 10, 50, 30))
    return Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)


class FakeClock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def monotonic(self):
        self.t += self.step
        return self.t


def test_budget_cuts_a_slow_window_short_and_says_so(monkeypatch):
    d = _desktop()
    monkeypatch.setattr(observe_mod._time, "monotonic", FakeClock(0.05).monotonic)
    obs = observe_mod.observe(d, screenshot=False, time_budget=3.0, selectors=False)
    names = [e.name for e in obs.elements]
    assert "Equals" in names  # the front window is complete
    assert 0 < sum(n.startswith("Link") for n in names) < 200  # the huge one is partial
    assert obs.notes and "partial" in obs.notes[0] and '"Edge"' in obs.notes[0]
    assert "Note: Time budget of 3s ran out" in obs.text()
    assert obs.to_dict()["notes"] == obs.notes


def test_no_budget_and_ample_budget_are_complete():
    d = _desktop()
    for budget in (None, 60.0):
        obs = observe_mod.observe(d, screenshot=False, time_budget=budget, selectors=False)
        assert len(obs.elements) == 202 and obs.notes == []


def test_exhausted_budget_names_unscanned_windows(monkeypatch):
    d = _desktop()
    monkeypatch.setattr(observe_mod._time, "monotonic", FakeClock(10.0).monotonic)
    obs = observe_mod.observe(d, screenshot=False, time_budget=1.0, selectors=False)
    assert obs.notes and "not scanned" in obs.notes[0]


def test_toolkit_observe_uses_the_default_budget(monkeypatch):
    seen = {}
    real = observe_mod.observe

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr("locum.llm.tools.observe", spy)
    tk = DesktopToolkit(_desktop())
    tk.call("observe", {"include_screenshot": False})
    assert seen["time_budget"] == DesktopToolkit.OBSERVE_BUDGET
