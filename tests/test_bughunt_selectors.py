"""Regression tests for bug-hunt findings in selectors.py, _nodeinfo.py and llm/observe.py."""

import pytest

from locum import Desktop
from locum._nodeinfo import node_app, owning_window
from locum.backends.mock import MockBackend
from locum.errors import SelectorSyntaxError
from locum.llm.observe import Observation, observe
from locum.selectors import parse
from locum.types import Point, Rect, WindowInfo


def _two_invoices():
    be = MockBackend(width=1280, height=800)
    a = be.add_window("Invoice", (0, 0, 400, 300), app="erp.exe", pid=1)
    a.add_button("Save", (10, 10, 80, 30))
    b = be.add_window("Invoice", (500, 0, 400, 300), app="erp.exe", pid=2)
    b.add_button("Save", (10, 10, 80, 30))
    return Desktop(be, sleep=lambda s: None, default_timeout=0)


class _Node:
    def __init__(self, role, name, children=(), **kw):
        self.role, self.name, self.kw = role, name, kw
        self.parent = None
        self._children = list(children)
        for c in self._children:
            c.parent = self

    def children(self):
        return list(self._children)

    def __getattr__(self, item):
        try:
            return self.__dict__["kw"][item]
        except KeyError:
            raise AttributeError(item) from None


# 27: add_element selectors are proven unique among all windows
def test_add_element_selector_unique_across_same_titled_windows():
    d = _two_invoices()
    obs = observe(d, screenshot=False, max_elements=0)
    back_save = parse('button "Save"').find_all(d.roots())[-1]
    entry = obs.add_element(back_save)
    found = parse(entry.selector).find_first(d.roots())
    assert found.same_as(back_save)


# 28: observe(window=...) selectors are proven unique among all windows
def test_window_scoped_observe_selector_finds_its_own_window():
    d = _two_invoices()
    target = d.windows()[-1]
    obs = observe(d, screenshot=False, window=target)
    el = [e for e in obs.elements if e.name == "Save"][0]
    found = parse(el.selector).find_first(d.roots())
    assert found.same_as(el.node)


# 29: a known pid never matches another process's window
def test_owning_window_known_pid_does_not_match_other_process():
    wins = [WindowInfo(handle=1, title="Save As", rect=Rect(0, 0, 500, 400), app="notepad.exe", pid=9)]
    btn = _Node("button", "Save", pid=5)
    _Node("dialog", "Save As", [btn], pid=5, bounds=Rect(0, 0, 500, 400))
    assert owning_window(None, btn, wins, guess=False) is None
    assert node_app(None, btn, wins) == ""


def test_owning_window_unknown_window_pid_still_matches_by_title():
    wins = [WindowInfo(handle=1, title="Save As", rect=Rect(0, 0, 500, 400), app="notepad.exe", pid=None)]
    btn = _Node("button", "Save", pid=5)
    _Node("dialog", "Save As", [btn], pid=5)
    assert owning_window(None, btn, wins, guess=False) is wins[0]


# 30: dict "not" rejects positional selectors like CSS :not() does
def test_dict_not_rejects_positional():
    with pytest.raises(SelectorSyntaxError):
        parse({"role": "button", "not": "button:first"})
    with pytest.raises(SelectorSyntaxError):
        parse("button:not(button:first)")
    sel = parse({"role": "button", "not": 'button "A"'})
    assert str(parse(str(sel))) == str(sel)


def _bare_obs():
    return Observation(screenshot=None, elements=[], windows=[], active_window=None, cursor=Point(0, 0),
                       screen_size=(100, 100), scale=1.0, annotated=None, region=Rect(0, 0, 100, 100))


# 31: refresh keeps the old value when the live value can't be read
def test_refresh_keeps_value_when_unreadable():
    class Flaky(_Node):
        broken = False

        @property
        def value(self):
            if self.broken:
                raise RuntimeError("COM error")
            return "123.45"

    node = Flaky("edit", "Display")
    obs = _bare_obs()
    el = obs.add_element(node)
    assert el.value == "123.45"
    node.broken = True
    assert obs.add_element(node).value == "123.45"


# 32: selectors=False stays empty after a refresh that changes the name
def test_selectors_false_stays_empty_after_rename():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("Calc", (0, 0, 400, 300), app="calc.exe", pid=1)
    disp = w.add_edit("Display is 0", (10, 10, 200, 30))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    obs = observe(d, screenshot=False, selectors=False)
    el = [e for e in obs.elements if e.name == "Display is 0"][0]
    assert el.selector == ""
    disp.name = "Display is 42"
    again = obs.add_element(el.node)
    assert again.name == "Display is 42"
    assert again.selector == ""


def test_selector_rebuilt_after_rename_when_selectors_on():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("Calc", (0, 0, 400, 300), app="calc.exe", pid=1)
    disp = w.add_edit("Display is 0", (10, 10, 200, 30))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    obs = observe(d, screenshot=False)
    el = [e for e in obs.elements if e.name == "Display is 0"][0]
    assert "Display is 0" in el.selector
    disp.name = "Display is 42"
    assert "Display is 42" in obs.add_element(el.node).selector


# 33: observe(screenshot=False) of an empty window region does not crash
def test_observe_no_screenshot_empty_region():
    be = MockBackend(width=1280, height=800)
    be.add_window("Tray helper", (0, 0, 0, 0), app="helper.exe", pid=1)
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    info = [x for x in d.windows() if x.title == "Tray helper"][0]
    obs = observe(d, screenshot=False, window=info)
    assert obs.scale == 1.0
    assert obs.screenshot is None
