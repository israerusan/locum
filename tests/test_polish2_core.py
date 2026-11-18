"""Regression tests for the second 2026-11 polish pass over the core modules."""

from __future__ import annotations

import pytest

from _stub_desktop import StubDesktop
from locum import Desktop
from locum.backends.mock import MockBackend, MockNode
from locum.errors import ElementNotFound
from locum.selectors import parse


def _desk(be: MockBackend) -> Desktop:
    return Desktop(be, failsafe=False, sleep=lambda s: None)


# ---------------------------------------------------------------- C1: :root first step


@pytest.fixture
def counted(monkeypatch):
    """A desktop of 20 windows x 50 buttons, counting every children() call."""
    calls = [0]
    orig = MockNode.children

    def children(self):
        calls[0] += 1
        return orig(self)

    monkeypatch.setattr(MockNode, "children", children)
    be = MockBackend()
    for i in range(20):
        w = be.add_window(f"Win {i}", app=f"app{i}")
        for j in range(50):
            w.add_button(f"b{j}", (0, 0, 10, 10))
    return _desk(be), be, calls


def test_uipath_wnd_root_step_walks_only_the_matched_window(counted):
    d, be, calls = counted
    calls[0] = 0
    el = d.find("<wnd title='Win 4' /><ctrl name='b9' role='push button' />", timeout=0)
    assert el.node.widget.window.title == "Win 4"
    assert calls[0] <= 51  # was every node of every window (~1000)


def test_root_step_find_all_walks_only_the_matched_window(counted):
    d, be, calls = counted
    calls[0] = 0
    found = d.find_all('window:root[name="Win 4"] button')
    assert len(found) == 50 and {e.node.widget.window.title for e in found} == {"Win 4"}
    assert calls[0] <= 51


def test_union_with_root_branches_skips_unmatched_windows(counted):
    d, be, calls = counted
    calls[0] = 0
    found = d.find_all(':root[name="Win 3"] button "b1", :root[name="Win 5"] button "b2"')
    assert sorted((e.node.widget.window.title, e.name) for e in found) == [("Win 3", "b1"), ("Win 5", "b2")]
    assert calls[0] <= 2 * 51


def test_root_step_semantics_unchanged():
    be = MockBackend()
    outer = be.add_window("W")
    inner = outer.add("window", "W", (0, 0, 50, 50))
    inner.add_button("deep", (0, 0, 10, 10))
    be.add_window("V").add_button("other", (0, 0, 10, 10))
    roots = be.accessibility_roots()
    assert [n.name for n in parse(':root[name="W"] button').find_all(roots)] == ["deep"]
    assert parse(':root[name="Nope"] button').find_all(roots) == []
    # Only the roots themselves are :root, never the nested "W".
    assert [n.name for n in parse(':root[name="W"] > button').find_all(roots)] == []


# ---------------------------------------------------------------- C2: quoted glob hint


def test_quoted_glob_in_selector_hints_at_the_glob_operator():
    be = MockBackend()
    be.add_window("Invoice Entry - ERP").add_button("Save", (0, 0, 10, 10))
    d = _desk(be)
    assert d.window("Invoice Entry*").title == "Invoice Entry - ERP"  # window() takes a glob
    with pytest.raises(ElementNotFound) as exc:
        d.find('window "Invoice Entry*" button "Save"', timeout=0)  # a selector's quoted name is exact
    assert '[name%="Invoice Entry*"]' in str(exc.value)
    assert d.find('window[name%="Invoice Entry*"] button "Save"', timeout=0).name == "Save"


def test_glob_hint_not_given_for_glob_operator():
    be = MockBackend()
    be.add_window("Main").add_button("Save", (0, 0, 10, 10))
    d = _desk(be)
    with pytest.raises(ElementNotFound) as exc:
        d.find('window[name%="Nope*"] button', timeout=0)
    assert "quoted name matches exactly" not in str(exc.value)


# ---------------------------------------------------------------- C3: placeholders


@pytest.fixture
def ph(monkeypatch):
    """An edit whose app reports its placeholder as the value while the field is empty."""
    orig = MockNode.value

    def value(self):
        v = orig.fget(self)
        p = self.widget.attrs.get("placeholder")
        return p if p and not v else v

    monkeypatch.setattr(MockNode, "value", property(value))
    be = MockBackend(width=1280, height=800)
    d = StubDesktop(be)
    win = be.add_window("Form", (100, 100, 500, 400))
    box = win.add_edit("Search", (20, 40, 200, 28), automation_id="q", attrs={"placeholder": "Search mail"})
    return d, box


def test_placeholder_is_not_the_text_of_an_empty_field(ph):
    d, box = ph
    e = d.find("#q", timeout=0)
    assert e.value == "Search mail"  # raw value: what the app reports
    assert e._placeholder() == "Search mail"
    assert e.text == ""
    box.value = "hello"
    assert e.text == "hello"


def test_set_text_append_does_not_keep_the_placeholder(ph):
    d, box = ph
    d.find("#q", timeout=0).set_text("abc", clear=False)
    assert box.value == "abc"  # was "Search mailabc"


def test_clearing_a_field_that_shows_its_placeholder_verifies(ph):
    d, box = ph
    box.value = "old"
    e = d.find("#q", timeout=0).set_text("")
    assert box.value == "" and e.last_verification.verified


def test_text_equal_to_the_placeholder_still_verifies(ph):
    d, box = ph
    e = d.find("#q", timeout=0).set_text("Search mail", verify="exact")
    assert box.value == "Search mail" and e.last_verification.verified


def test_type_into_a_field_showing_its_placeholder(ph):
    d, box = ph
    e = d.find("#q", timeout=0)
    e.focus()
    e.type("abc", verify="exact")  # before reads "Search mail": the field was empty
    assert box.value == "abc" and e.last_verification.verified


def test_amount_type_into_a_field_showing_its_placeholder(ph):
    d, box = ph
    e = d.find("#q", timeout=0)
    e.focus()
    e.type("12.50", verify="amount", decimal=".")  # was refused: "types into an empty field only"
    assert box.value == "12.50"


def test_no_placeholder_attribute_keeps_old_behaviour():
    be = MockBackend(width=1280, height=800)
    d = StubDesktop(be)
    win = be.add_window("Form", (100, 100, 500, 400))
    win.add_edit("Search", (20, 40, 200, 28), automation_id="q", value="x")
    e = d.find("#q", timeout=0)
    assert e._placeholder() == "" and e.text == "x"


# ---------------------------------------------------------------- fresh review


def test_coerce_point_accepts_a_numpy_xy_array():
    np = pytest.importorskip("numpy")
    from locum.types import Point, coerce_point

    assert coerce_point(np.array([150, 151])) == Point(150, 151)
    assert coerce_point(np.array([150.4, 150.6])) == Point(150, 151)
    be = MockBackend()
    be.add_window("A")
    d = _desk(be)
    assert d.click(np.array([150, 150])) == Point(150, 150)
    with pytest.raises(TypeError):
        coerce_point(np.array([1, 2, 3]))


def test_window_find_after_close_says_the_window_is_gone():
    be = MockBackend()
    w = be.add_window("A")
    w.add_button("x", (0, 0, 10, 10))
    d = _desk(be)
    win = d.window("A")
    be.remove_window(w)
    with pytest.raises(ElementNotFound) as exc:
        win.find("button", timeout=0)
    assert "no longer exists" in str(exc.value)
    assert "no accessibility root at all" not in str(exc.value)


def test_heal_reads_no_bounds_or_class_for_rejected_roles(monkeypatch):
    from locum.heal import heal

    reads = {"bounds": 0, "class_name": 0}
    for prop in reads:
        orig = getattr(MockNode, prop)

        def counted(self, _orig=orig, _prop=prop):
            reads[_prop] += 1
            return _orig.fget(self)

        monkeypatch.setattr(MockNode, prop, property(counted))
    be = MockBackend()
    w = be.add_window("Main")
    for i in range(200):
        w.add_text(f"label {i}", (0, 0, 10, 10))
    w.add_button("Save now", (0, 0, 10, 10))
    out = heal('button "Save"', be.accessibility_roots(), log=False,
               options=__import__("locum.heal", fromlist=["HealOptions"]).HealOptions(threshold=0.5))
    assert out and out.node.widget.name == "Save now"
    # No fingerprint: neither bounds nor class name is scored, and 200 text nodes are the wrong role.
    reads_during_scan = dict(reads)
    assert reads_during_scan["bounds"] <= 5 and reads_during_scan["class_name"] <= 5, reads_during_scan
