"""Review fixes for observe / changes_only / observe reuse (2026-11-06).

* 4a: element ids are never reused within a chain of observations -- an id reported
  "gone" must not come back meaning a different control (find_elements / wait_for).
* 4b: obsdiff.match never pairs two elements whose runtime ids are both known and
  differ, and matches by position alone only for content displays (or same automation id).
* 8: Store/UWP windows (ApplicationFrameHost.exe) are not reused while the process whose
  content they host (Calculator, SystemSettings) raised change events.
"""

from __future__ import annotations

import sys
import threading

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.llm.obsdiff import compare, match
from locum.llm.observe import Observation, ObservedElement, observe
from locum.llm.tools import DesktopToolkit
from locum.types import Point, Rect


# ------------------------------------------------------------------ 4a: id reuse
@pytest.fixture
def calc():
    be = MockBackend(width=1280, height=800)
    win = be.add_window("Calculator", (0, 0, 400, 400), app="calc", pid=1)
    w = {
        "seven": win.add_button("Seven", (10, 100, 80, 40)),
        "equals": win.add_button("Equals", (100, 100, 80, 40)),
        "clear": win.add_button("Clear", (190, 100, 80, 40)),
    }
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, win, w


def test_gone_id_is_not_reused_by_find_elements(calc):
    be, d, win, w = calc
    tk = DesktopToolkit(d)
    tk.call("observe", {})
    clear_id = {e.name: e.id for e in tk.last_observation.elements}["Clear"]
    assert clear_id == max(e.id for e in tk.last_observation.elements)
    w["clear"].parent.children.remove(w["clear"])
    r = tk.call("observe", {"changes_only": True})
    assert f"[{clear_id}] button \"Clear\"" in r.text and "no longer valid" in r.text
    win.add_button("Delete", (190, 100, 80, 40))
    tk.call("find_elements", {"text": "Delete"})
    ids = {e.name: e.id for e in tk.last_observation.elements}
    assert ids["Delete"] != clear_id
    assert ids["Delete"] > clear_id
    with pytest.raises(LookupError):
        tk.last_observation.element(clear_id)


def test_ids_added_after_a_diff_stay_unique_in_the_next_diff(calc):
    be, d, win, w = calc
    tk = DesktopToolkit(d)
    tk.call("observe", {})
    w["clear"].parent.children.remove(w["clear"])
    tk.call("observe", {"changes_only": True})
    win.add_button("Delete", (190, 100, 80, 40))
    tk.call("find_elements", {"text": "Delete"})
    found_id = {e.name: e.id for e in tk.last_observation.elements}["Delete"]
    win.add_button("Undo", (280, 100, 80, 40))
    tk.call("observe", {"changes_only": True})
    ids = [e.id for e in tk.last_observation.elements]
    assert len(ids) == len(set(ids))
    by_name = {e.name: e.id for e in tk.last_observation.elements}
    assert by_name["Delete"] == found_id
    assert by_name["Undo"] > found_id


def test_add_element_respects_max_id_on_a_bare_observation():
    be = MockBackend(width=800, height=600)
    win = be.add_window("A", (0, 0, 400, 400), pid=1)
    btn = win.add_button("Go", (10, 10, 80, 30))
    obs = Observation(None, [], [], None, Point(0, 0), (800, 600), 1.0, None, region=Rect(0, 0, 800, 600))
    obs._max_id = 7
    node = be.accessibility_roots()[0].children()[0]
    assert node.name == "Go" or btn
    e = obs.add_element(node)
    assert e.id == 8 and obs._max_id == 8


# ------------------------------------------------------------------ 4b: match()
class _Node:
    def __init__(self, rid=None, aid=""):
        self.runtime_id = rid
        self.automation_id = aid


def _el(id_, role, name, bounds, rid=None, aid="", title="Win"):
    return ObservedElement(id=id_, role=role, name=name, value=None, bounds=Rect(*bounds),
                           states=frozenset(), selector="", node=_Node(rid, aid), window_title=title)


def test_different_runtime_ids_never_pair_even_at_the_same_place():
    old = _el(5, "button", "Cancel", (10, 10, 80, 30), rid=(42, 1, 100))
    new = _el(1, "button", "Delete", (10, 10, 80, 30), rid=(42, 1, 200))
    assert match([old], [new]) == []


def test_different_runtime_ids_do_not_pair_by_name_and_bounds_either():
    old = _el(5, "button", "OK", (10, 10, 80, 30), rid=(42, 1, 100))
    new = _el(1, "button", "OK", (10, 10, 80, 30), rid=(42, 1, 200))
    assert match([old], [new]) == []
    # one side without a runtime id: still matched by name + bounds
    new2 = _el(1, "button", "OK", (10, 10, 80, 30), rid=None)
    assert match([old], [new2]) == [(old, new2)]


def test_compare_reports_replaced_button_as_new_and_gone():
    prev = Observation(None, [_el(5, "button", "Cancel", (10, 10, 80, 30), rid=(42, 1, 100))], [], None,
                       Point(0, 0), (800, 600), 1.0, None, region=Rect(0, 0, 800, 600))
    new = Observation(None, [_el(1, "button", "Delete", (10, 10, 80, 30), rid=(42, 1, 200))], [], None,
                      Point(0, 0), (800, 600), 1.0, None, region=Rect(0, 0, 800, 600))
    diff = compare(prev, new)
    assert [e.name for e in diff.removed] == ["Cancel"]
    assert [e.name for e in diff.added] == ["Delete"]
    assert new.elements[0].id != 5


def test_position_only_match_is_not_used_for_buttons_without_runtime_ids():
    old = _el(5, "button", "Cancel", (10, 10, 80, 30))
    new = _el(1, "button", "Delete", (10, 10, 80, 30))
    assert match([old], [new]) == []


def test_position_only_match_still_pairs_text_displays():
    old = _el(5, "text", "Display is 0", (10, 10, 300, 40))
    new = _el(1, "text", "Display is 15", (10, 10, 300, 40))
    assert match([old], [new]) == [(old, new)]
    old_e = _el(6, "edit", "", (10, 60, 300, 30))
    new_e = _el(2, "edit", "Search", (10, 60, 300, 30))
    assert match([old_e], [new_e]) == [(old_e, new_e)]


def test_position_only_match_pairs_buttons_with_the_same_automation_id():
    old = _el(5, "button", "Play", (10, 10, 80, 30), aid="playPause")
    new = _el(1, "button", "Pause", (10, 10, 80, 30), aid="playPause")
    assert match([old], [new]) == [(old, new)]
    other = _el(1, "button", "Delete", (10, 10, 80, 30), aid="deleteButton")
    assert match([old], [other]) == []


# ------------------------------------------------------------------ 8: UWP host windows
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

    def dirty_since(self, since):
        return {p for g, p in self.log if g > since}


FRAME_PID, CALC_PID = 100, 200


@pytest.fixture
def uwp(monkeypatch):
    be = MockBackend(width=1280, height=800)
    calc_win = be.add_window("Calculator", (0, 0, 400, 400), app="ApplicationFrameHost.exe", pid=FRAME_PID)
    display = calc_win.add_text("Display is 0", (10, 40, 380, 40))
    calc_win.add_button("Seven", (10, 100, 80, 40))
    notes = be.add_window("Notes", (500, 0, 400, 400), app="notes", pid=22)
    notes.add_button("Save", (10, 100, 80, 40))
    tracker = FakeTracker()
    monkeypatch.setattr(be, "change_tracker", lambda: tracker)
    return be, calc_win, display, tracker


def test_hosted_process_events_dirty_the_frame_window(uwp, monkeypatch):
    be, calc_win, display, tracker = uwp
    hosted = {calc_win.handle: frozenset({FRAME_PID, CALC_PID})}
    monkeypatch.setattr(be, "window_content_pids", lambda h: hosted.get(h, None), raising=False)
    d = Desktop(be, failsafe=False)
    o1 = observe(d, screenshot=False, interactive_only=False)
    o2 = observe(d, screenshot=False, interactive_only=False, reuse=o1)
    assert o2.reused_windows == 2  # nothing changed: both reused
    display.name = "Display is 15"
    tracker.bump(CALC_PID)  # the event comes from Calculator's process, not the frame host
    o3 = observe(d, screenshot=False, interactive_only=False, reuse=o2)
    assert o3.reused_windows == 1  # only Notes
    assert "Display is 15" in [e.name for e in o3.elements]


def test_host_window_without_hosted_pid_info_is_never_reused(uwp):
    be, calc_win, display, tracker = uwp
    d = Desktop(be, failsafe=False)
    o1 = observe(d, screenshot=False, interactive_only=False)
    o2 = observe(d, screenshot=False, interactive_only=False, reuse=o1)
    assert o2.reused_windows == 1  # Notes only; the frame host's content is unattributable
    display.name = "Display is 15"
    tracker.bump(CALC_PID)
    o3 = observe(d, screenshot=False, interactive_only=False, reuse=o2)
    assert "Display is 15" in [e.name for e in o3.elements]


def test_hosted_pids_seen_at_the_previous_observe_still_count(uwp, monkeypatch):
    """The CoreWindow can detach (suspended app): pids hosted *before* still dirty the window."""
    be, calc_win, display, tracker = uwp
    hosted = {calc_win.handle: frozenset({FRAME_PID, CALC_PID})}
    monkeypatch.setattr(be, "window_content_pids", lambda h: hosted.get(h, None), raising=False)
    d = Desktop(be, failsafe=False)
    o1 = observe(d, screenshot=False, interactive_only=False)
    hosted[calc_win.handle] = frozenset({FRAME_PID, 300})
    tracker.bump(CALC_PID)
    o2 = observe(d, screenshot=False, interactive_only=False, reuse=o1)
    assert o2.reused_windows == 1


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 windows")
def test_real_application_frame_window_reports_its_hosted_process():
    pytest.importorskip("comtypes")
    from locum.backends.windows import WindowsBackend

    be = WindowsBackend()
    try:
        frames = [w for w in be.list_windows() if (w.app or "").lower() == "applicationframehost.exe"]
        if not frames:
            pytest.skip("no Store/UWP window (Calculator, Settings) is open")
        for w in frames:
            pids = be.window_content_pids(w.handle)
            assert pids is not None and w.pid in pids
            assert any(p != w.pid for p in pids), f"{w.title!r}: no hosted process found"
        # an ordinary window reports at least its own process
        other = [w for w in be.list_windows() if (w.app or "").lower() != "applicationframehost.exe"]
        if other:
            assert other[0].pid in be.window_content_pids(other[0].handle)
    finally:
        be.close()
