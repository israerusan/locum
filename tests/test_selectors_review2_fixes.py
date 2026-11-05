"""Regression tests for selector fixes (review round 2).

* ``selector_for`` must not start at AT-SPI/AX application or desktop nodes
  (they sit above the backend's accessibility roots, so such selectors never match);
* the CLI prints selectors that are unique across all windows;
* a nested UiPath ``<wnd>`` is a child window, not a second top-level window;
* ``[app ...]`` is case-insensitive for every operator;
* indeterminate tri-state boxes are neither ``:checked`` nor ``:unchecked``;
* dict selectors reject or normalise inputs that used to be misread silently.
"""

from __future__ import annotations

import io
import json
import os

import pytest

from locum import Desktop
from locum.backends.base import AccessibleNode
from locum.backends.mock import MockBackend
from locum.cli import main
from locum.errors import SelectorSyntaxError
from locum.selectors import describe_node, parse, selector_for


def _noop(_s: float) -> None:
    return None


# ----------------------------------------------------------------- selector_for above roots


class _N(AccessibleNode):
    def __init__(self, role, name="", parent=None, aid=""):
        self._role, self._name, self._parent, self._aid = role, name, parent, aid
        self._kids = []
        if parent is not None:
            parent._kids.append(self)

    role = property(lambda s: s._role)
    name = property(lambda s: s._name)
    automation_id = property(lambda s: s._aid)
    bounds = property(lambda s: None)
    parent = property(lambda s: s._parent)

    def children(self):
        return list(self._kids)


def _atspi_like_tree():
    desk = _N("desktop", "main")
    app = _N("application", "gedit", desk)
    win = _N("window", "Doc", app)
    pane = _N("pane", "", win)
    ok = _N("button", "OK", pane)
    other = _N("window", "Other", app)
    _N("button", "OK", other)
    return desk, [win, other], ok


def test_selector_for_stops_at_roots_not_application():
    _desk, roots, ok = _atspi_like_tree()
    s = selector_for(ok, roots=roots)
    assert not s.startswith(("desktop", "application")), s
    assert parse(s).find_first(roots) is ok
    assert s == 'window "Doc" button "OK"'


def test_selector_for_without_roots_drops_application_and_desktop():
    _desk, roots, ok = _atspi_like_tree()
    s = selector_for(ok)
    assert s == 'window "Doc" button "OK"'
    assert parse(s).find_first(roots) is ok


def test_selector_for_explicit_root_still_honoured():
    desk, _roots, ok = _atspi_like_tree()
    s = selector_for(ok, root=desk)
    assert s.startswith("desktop")
    assert parse(s).find_first([desk]) is ok


@pytest.mark.x11
def test_selector_for_round_trips_on_fake_atspi(xvfb_display):
    from Xlib import display as xdisplay

    from locum.backends.x11 import X11Backend
    from locum.rpa.recorder import Recorder
    from test_x11_backend import build_fake_tree, make_fake_atspi, make_window

    xc = xdisplay.Display(xvfb_display)
    win = make_window(xc, (100, 100, 400, 300), title="Fake App Window")
    desktop, _acc = build_fake_tree(pid=os.getpid(), title="Fake App Window", rect=(100, 100, 400, 300))
    b = X11Backend(xvfb_display, clipboard_tool="none", accessibility=make_fake_atspi(desktop))
    try:
        roots = b.accessibility_roots()
        for node in (b.element_at(115, 115), b.focused_element()):
            s = selector_for(node, roots=roots)
            assert s.startswith('window "Fake App Window"'), s
            assert parse(s).find_first(roots).same_as(node)
            assert parse(selector_for(node)).find_first(roots).same_as(node)
        assert Recorder(Desktop(backend=b))._selector(b.element_at(115, 115)) == 'window "Fake App Window" button "OK"'
    finally:
        b.close()
        win.destroy()
        xc.sync()
        xc.close()


def test_selector_for_round_trips_on_fake_ax():
    from test_macos_backend import _tree_setup

    be, _q, _appkit, ax, els = _tree_setup()
    roots = be.accessibility_roots()
    ax.at = els["ok"]
    node = be.element_at(300, 150)
    assert node is not None and node.parent.parent.role == "application"
    s = selector_for(node, roots=roots)
    assert s.startswith('window "Doc"'), s
    assert parse(s).find_first(roots).same_as(node)


# ----------------------------------------------------------------- CLI selectors are globally unique


def _two_sap_sessions():
    be = MockBackend()
    a = be.add_window("SAP Easy Access", (0, 0, 400, 300), app="saplogon.exe", pid=1)
    a.add_button("Post", (10, 10, 60, 25))
    b = be.add_window("SAP Easy Access", (500, 0, 400, 300), app="saplogon.exe", pid=2)
    b.add_button("Post", (10, 10, 60, 25))
    return Desktop(be, sleep=_noop, default_timeout=0.2, poll_interval=0.01)


def _run(argv, desktop):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), desktop=desktop, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_inspect_selector_finds_the_inspected_window_not_its_twin():
    d = _two_sap_sessions()
    code, out, _ = _run(["inspect", "--json", "15,15"], d)
    assert code == 0
    sel = json.loads(out)["robust_selector"]
    assert d.find(sel, timeout=0).node.pid == 1
    code, out, _ = _run(["inspect", "15,15"], d)
    line = next(ln for ln in out.splitlines() if ln.startswith("selector:"))
    assert d.find(line.split(":", 1)[1].strip(), timeout=0).node.pid == 1


def test_cli_find_selector_is_globally_unique():
    d = _two_sap_sessions()
    code, out, _ = _run(["find", "--json", "--all", 'button "Post"', "--timeout", "0"], d)
    assert code == 0
    items = json.loads(out)
    assert len(items) == 2
    sels = [item["robust_selector"] for item in items]
    assert len(set(sels)) == 2
    pids = {d.find(s, timeout=0).node.pid for s in sels}
    assert pids == {1, 2}


# ----------------------------------------------------------------- UiPath nested <wnd>


def test_uipath_nested_wnd_is_a_child_window():
    be = MockBackend()
    w = be.add_window("Untitled - Notepad", (0, 0, 400, 300), app="notepad.exe", pid=1, class_name="Notepad")
    w.add_edit("Text Editor", (0, 20, 400, 280), class_name="Edit")
    roots = be.accessibility_roots()
    sel = parse("<wnd app='notepad.exe' cls='Notepad' title='Untitled - Notepad' /><wnd cls='Edit' />")
    assert [n.class_name for n in sel.find_all(roots)] == ["Edit"]
    # The first <wnd> is still anchored to the top-level windows.
    assert parse("<wnd cls='Edit' />").find_all(roots) == []


# ----------------------------------------------------------------- [app ...] operators


@pytest.mark.parametrize(
    "sel",
    [
        'window[app="SAPLOGON.EXE"]',
        'window[app="saplogon"]',
        'window[app%="SAP*"]',
        'window[app*="SAP"]',
        'window[app^="SAP"]',
        'window[app$=".exe"]',
        'window[app$=".EXE"]',
        'window[app~="^saplogon\\.exe$"]',
        'window[app~="^saplogon$"]',
        {"role": "window", "app__contains": "SAP"},
        {"role": "window", "app__endswith": ".exe"},
    ],
)
def test_app_matching_is_case_insensitive_for_every_operator(sel):
    be = MockBackend()
    be.add_window("SAP Easy Access", (0, 0, 400, 300), app="saplogon.exe", pid=1)
    be.add_window("Notes", (0, 0, 400, 300), app="TextEdit", pid=2)
    assert [n.name for n in parse(sel).find_all(be.accessibility_roots())] == ["SAP Easy Access"]


def test_app_not_equal_and_render_unchanged():
    be = MockBackend()
    be.add_window("SAP Easy Access", (0, 0, 400, 300), app="saplogon.exe", pid=1)
    be.add_window("Book1 - Excel", (0, 0, 400, 300), app="EXCEL.EXE", pid=2)
    roots = be.accessibility_roots()
    assert [n.name for n in parse('window[app!="SAPLOGON.exe"]').find_all(roots)] == ["Book1 - Excel"]
    assert str(parse('window[app*="SAP"]')) == 'window[app*="SAP"]'


# ----------------------------------------------------------------- tri-state checkboxes


class _Box(AccessibleNode):
    def __init__(self, name, states):
        self._n, self._s = name, frozenset(states)

    role = property(lambda s: "checkbox")
    name = property(lambda s: s._n)
    bounds = property(lambda s: None)
    states = property(lambda s: s._s)

    def children(self):
        return []


def _boxes():
    return [
        _Box("Select all", {"enabled", "visible", "checkable", "mixed"}),  # WindowsBackend / macOS
        _Box("Partial", {"enabled", "visible", "indeterminate"}),  # AT-SPI nick
        _Box("Off", {"enabled", "visible", "checkable"}),
        _Box("On", {"enabled", "visible", "checkable", "checked"}),
    ]


def test_mixed_checkbox_is_neither_checked_nor_unchecked():
    boxes = _boxes()

    def names(sel):
        return [b.name for b in parse(sel).find_all(boxes)]

    assert names("checkbox:unchecked") == ["Off"]
    assert names("checkbox:checked") == ["On"]
    assert names("checkbox:mixed") == ["Select all", "Partial"]
    assert names("checkbox:indeterminate") == ["Select all", "Partial"]
    assert names({"role": "checkbox", "checked": False}) == ["Off"]
    assert names({"role": "checkbox", "mixed": True}) == ["Select all", "Partial"]
    assert names({"role": "checkbox", "states": ["mixed"]}) == ["Select all", "Partial"]
    assert "[mixed]" in describe_node(boxes[0])


# ----------------------------------------------------------------- dict selector validation


def _bank():
    be = MockBackend()
    w = be.add_window("Bank", (0, 0, 400, 300))
    w.add_button("Pay", (0, 0, 10, 10))
    w.add_button("Cancel", (20, 0, 10, 10), enabled=False)
    w.add("link", "button", (40, 0, 10, 10))
    return be.accessibility_roots()


def test_dict_any_requires_a_list():
    with pytest.raises(SelectorSyntaxError):
        parse({"any": "button"})
    assert len(parse({"any": ["button", "link"]}).branches) == 2
    assert len(parse({"any": ("button",)}).branches) == 1


def test_dict_role_values_are_normalised_for_every_operator():
    roots = _bank()
    css = [n.name for n in parse('window [role!="push button"]').find_all(roots)]
    assert css == ["button"]
    assert [n.name for n in parse({"role__ne": "push button", "within": "window"}).find_all(roots)] == css
    assert [n.name for n in parse({"role__iexact": "Push Button", "within": "window"}).find_all(roots)] == ["Pay", "Cancel"]


@pytest.mark.parametrize("val, expected", [("false", ["Cancel"]), ("False", ["Cancel"]), ("no", ["Cancel"]),
                                           ("true", ["Pay"]), ("yes", ["Pay"]), (0, ["Cancel"]), (1, ["Pay"])])
def test_dict_state_keys_accept_boolean_strings(val, expected):
    roots = _bank()
    assert [n.name for n in parse({"role": "button", "enabled": val}).find_all(roots)] == expected


@pytest.mark.parametrize("val", ["maybe", None, 2, "", [True]])
def test_dict_state_keys_reject_non_booleans(val):
    with pytest.raises(SelectorSyntaxError):
        parse({"role": "button", "enabled": val})
