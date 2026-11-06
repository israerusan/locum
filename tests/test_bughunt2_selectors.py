"""Regression tests for bug hunt round 2: selectors, _nodeinfo, llm/observe (mock backend only)."""

import re

import pytest

from locum import Desktop
from locum._nodeinfo import owning_window
from locum.backends.mock import MockBackend
from locum.errors import SelectorSyntaxError
from locum.llm.observe import observe
from locum.selectors import parse, selector_for
from locum.types import Rect, WindowInfo


def _desk(be):
    return Desktop(be, sleep=lambda s: None, default_timeout=0)


# ---------------------------------------------------------------- #31 stale prefix cache

def test_rebuilt_selector_after_window_retitle_uses_live_title():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("Untitled - Notepad", (0, 0, 400, 300), app="notepad.exe", pid=1)
    status = w.add_edit("Ln 1", (10, 10, 200, 30))
    d = _desk(be)
    obs = observe(d, screenshot=False)
    el = [e for e in obs.elements if e.name == "Ln 1"][0]
    assert el.selector == 'window "Untitled - Notepad" edit "Ln 1"'  # fills the prefix cache
    w.title = "notes.txt - Notepad"
    w.root.name = w.title
    status.name = "Ln 2"
    again = obs.add_element(el.node)
    hit = parse(again.selector).find_first(d.roots())
    assert hit is not None and hit.same_as(el.node)
    assert "notes.txt" in again.selector


def test_prefix_cache_still_shared_while_windows_unchanged(monkeypatch):
    import locum.selectors as sel_mod

    be = MockBackend(width=1280, height=800)
    w = be.add_window("Calc", (0, 0, 400, 300), app="calc.exe", pid=1)
    for i in range(4):
        w.add_button(f"B{i}", (10 + 40 * i, 10, 30, 30))
    obs = observe(_desk(be), screenshot=False)
    calls = []
    real = sel_mod._top_prefix
    monkeypatch.setattr(sel_mod, "_top_prefix", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    for el in obs.elements:
        assert el.selector
    assert len(calls) == 1


# ---------------------------------------------------------------- #32 stale roots

def test_lazy_selector_is_unique_among_windows_opened_after_observe():
    be = MockBackend(width=1280, height=800)
    a = be.add_window("Invoice", (0, 0, 400, 300), app="erp.exe", pid=1)
    a.add_button("Save", (10, 10, 80, 30))
    d = _desk(be)
    obs = observe(d, screenshot=False)
    el = [e for e in obs.elements if e.name == "Save"][0]
    assert not el.selector_ready
    b = be.add_window("Invoice", (500, 0, 400, 300), app="erp.exe", pid=1)
    b.add_button("Save", (10, 10, 80, 30))
    found = parse(el.selector).find_all(d.roots())
    assert len(found) == 1 and found[0].same_as(el.node)


# ---------------------------------------------------------------- #33 Z-order

class _AppOrderBackend(MockBackend):
    def accessibility_roots(self):
        return list(reversed(super().accessibility_roots()))


def test_observe_numbers_and_caps_in_window_z_order():
    be = _AppOrderBackend(width=1280, height=800)
    be.add_window("Background", (0, 0, 400, 300), app="old.exe", pid=1).add_button("Old", (10, 10, 80, 30))
    be.add_window("Dialog", (600, 0, 400, 300), app="new.exe", pid=2).add_button("Confirm", (10, 10, 80, 30))
    d = _desk(be)
    capped = observe(d, screenshot=False, max_elements=1)
    assert [(e.id, e.name, e.window_title) for e in capped.elements] == [(1, "Confirm", "Dialog")]
    full = observe(d, screenshot=False)
    assert [(e.id, e.name) for e in full.elements] == [(1, "Confirm"), (2, "Old")]


# ---------------------------------------------------------------- #34 owning_window same title

class _N:
    handle = None

    def __init__(self, role, name, children=(), **kw):
        self.role, self.name, self.parent = role, name, None
        self.__dict__.update(kw)
        for c in children:
            c.parent = self


def _two_sap_windows():
    return [
        WindowInfo(handle=101, title="SAP Easy Access", rect=Rect(0, 0, 800, 600), app="saplogon.exe", pid=7),
        WindowInfo(handle=202, title="SAP Easy Access", rect=Rect(900, 0, 800, 600), app="saplogon.exe", pid=7),
    ]


def test_owning_window_same_title_uses_bounds():
    btn = _N("button", "Execute", pid=7, bounds=Rect(950, 50, 80, 30))
    _N("window", "SAP Easy Access", [btn], pid=7, bounds=Rect(900, 0, 800, 600))
    assert owning_window(None, btn, _two_sap_windows(), guess=False).handle == 202


def test_owning_window_same_title_uses_node_center_without_top_bounds():
    btn = _N("button", "Execute", pid=7, bounds=Rect(950, 50, 80, 30))
    _N("window", "SAP Easy Access", [btn], pid=7, bounds=None)
    assert owning_window(None, btn, _two_sap_windows(), guess=False).handle == 202


def test_owning_window_same_title_ambiguous():
    btn = _N("button", "Execute", pid=7, bounds=None)
    _N("window", "SAP Easy Access", [btn], pid=7, bounds=None)
    assert owning_window(None, btn, _two_sap_windows(), guess=False) is None
    assert owning_window(None, btn, _two_sap_windows(), guess=True).handle == 101


# ---------------------------------------------------------------- #35 role aliases

@pytest.mark.parametrize("role", ["label", "input", "check", "img"])
def test_selector_for_alias_role_finds_its_node(role):
    be = MockBackend(width=800, height=600)
    w = be.add_window("W", (0, 0, 400, 300), app="a.exe", pid=1)
    w.root.add(role, "Name", (10, 10, 50, 20))
    w.root.add(role, "", (10, 40, 50, 20))
    roots = list(be.accessibility_roots())
    for node in [n for n in roots[0].walk() if n.role == role]:
        s = selector_for(node, roots=roots)
        found = parse(s).find_first(roots)
        assert found is not None and found.same_as(node), s


def test_selector_for_canonical_role_unchanged():
    be = MockBackend(width=800, height=600)
    w = be.add_window("W", (0, 0, 400, 300), app="a.exe", pid=1)
    w.add_button("OK", (10, 10, 50, 20))
    roots = list(be.accessibility_roots())
    node = [n for n in roots[0].walk() if n.name == "OK"][0]
    assert selector_for(node, roots=roots) == 'window "W" button "OK"'


# ---------------------------------------------------------------- #36 pattern flags

def test_dict_pattern_keeps_multiline_and_dotall_flags():
    be = MockBackend(width=800, height=600)
    w = be.add_window("Log", (0, 0, 400, 300))
    w.add_text("Status:\nDONE", (10, 10, 200, 40))
    roots = be.accessibility_roots()
    assert parse({"role": "text", "name": re.compile(r"^DONE$", re.M)}).find_first(roots) is not None
    assert parse({"role": "text", "name": re.compile(r"Status:.DONE", re.S)}).find_first(roots) is not None
    assert parse({"role": "text", "name": re.compile(r"^done$", re.M | re.I)}).find_first(roots) is not None
    assert parse({"role": "text", "name": re.compile(r"^DONE$")}).find_first(roots) is None
    # The rendered selector round-trips.
    sel = parse({"role": "text", "name": re.compile(r"^DONE$", re.M)})
    assert parse(str(sel)).find_first(roots) is not None


# ---------------------------------------------------------------- #37 nth type errors

@pytest.mark.parametrize("d", [{"role": "button", "nth": "last"}, {"role": "button", "index": None},
                               {"role": "button", "nth": [1]}])
def test_dict_bad_nth_raises_selector_syntax_error(d):
    with pytest.raises(SelectorSyntaxError):
        parse(d)


def test_dict_integer_nth_still_works():
    parse({"role": "button", "nth": "1"})
    parse({"role": "button", "index": 0})
