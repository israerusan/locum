"""Review item 9: selector_for cost (shared read-once tree) and fragility (volatile window titles)."""

from __future__ import annotations

import pytest

import locum.selectors as sel_mod
from locum._nodeinfo import owning_window, stable_title
from locum.backends import mock
from locum.backends.base import AccessibleNode
from locum.backends.mock import MockBackend
from locum.selectors import _as_roots, parse, selector_for
from locum.types import Rect, WindowInfo


def retitle(w, title):
    w.title = w.root.name = title


def hit(sel, be):
    return parse(sel).find_all(be.accessibility_roots())


@pytest.fixture
def walks(monkeypatch):
    """Counts MockNode.children() calls (one per node visited by a tree walk)."""
    counter = {"n": 0}
    orig = mock.MockNode.children

    def counting(self):
        counter["n"] += 1
        return orig(self)

    monkeypatch.setattr(mock.MockNode, "children", counting)
    return counter


# ------------------------------------------------------------------ stable_title

@pytest.mark.parametrize("title, core", [
    ("◐ Pypilot library", "Pypilot library"),
    ("◑ Pypilot library", "Pypilot library"),
    ("✳ Claude Code", "Claude Code"),
    ("⠋ building", "building"),
    ("*doc.txt - Notepad", "doc.txt - Notepad"),
    ("Untitled - Notepad*", "Untitled - Notepad"),
    ("● main.py - Visual Studio Code", "main.py - Visual Studio Code"),
    ("doc.txt - Notepad (Not Responding)", "doc.txt - Notepad"),
    ("(3) Inbox - Gmail", "Inbox - Gmail"),
    (" zsh", "zsh"),
    # kept: real content, not decoration
    ("Mail (1 unread)", "Mail (1 unread)"),
    ("~/src - bash", "~/src - bash"),
    ("Calculator", "Calculator"),
    ("*", "*"),
    ("", ""),
])
def test_stable_title(title, core):
    assert stable_title(title) == core


# ------------------------------------------------------------------ (b) volatile titles

def test_spinner_title_selector_survives_the_next_frame():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("◐ Pypilot library", (0, 0, 400, 300), app="WindowsTerminal.exe")
    btn = w.add_button("Run", (10, 10, 80, 30))
    be.add_window("Other", (500, 0, 300, 300)).add_button("Run", (10, 10, 80, 30))
    node = be.accessibility_roots()[1].children()[0]
    assert node.widget is btn
    sel = selector_for(node, roots=be.accessibility_roots())
    assert sel == 'window "Pypilot library" button "Run"'
    for frame in ("◑ Pypilot library", "◒ Pypilot library", "Pypilot library"):
        retitle(w, frame)
        found = hit(sel, be)
        assert len(found) == 1 and found[0].widget is btn, frame


def test_dirty_marker_added_after_the_selector_was_built():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("doc.txt - Notepad", (0, 0, 400, 300), app="notepad.exe")
    edit = w.add_edit("Text editor", (0, 0, 400, 300))
    be.add_window("other.txt - Notepad", (500, 0, 300, 300), app="notepad.exe").add_edit("Text editor", (0, 0, 300, 300))
    roots = be.accessibility_roots()
    node = [r for r in roots if r.name == "doc.txt - Notepad"][0].children()[0]
    sel = selector_for(node, roots=roots)
    assert sel == 'window "doc.txt - Notepad" edit "Text editor"'  # clean titles keep the exact form
    for title in ("*doc.txt - Notepad", "doc.txt - Notepad (Not Responding)", "doc.txt - Notepad"):
        retitle(w, title)
        found = hit(sel, be)
        assert len(found) == 1 and found[0].widget is edit, title


def test_two_similar_windows_stay_unique():
    be = MockBackend(width=1280, height=800)
    a = be.add_window("◐ Pypilot library", (0, 0, 400, 300), app="WindowsTerminal.exe")
    a_btn = a.add_button("Run", (10, 10, 80, 30))
    b = be.add_window("Pypilot library", (500, 0, 400, 300), app="notepad.exe")
    b_btn = b.add_button("Run", (10, 10, 80, 30))
    roots = be.accessibility_roots()
    sels = {}
    for widget in (a_btn, b_btn):
        node = next(r for r in roots if r.widget is widget.window.root).children()[0]
        sels[widget.window.app] = s = selector_for(node, roots=roots, prefix_cache=[])
        found = hit(s, be)
        assert len(found) == 1 and found[0].widget is widget
    # Same stable title: told apart by the process, not by the spinner glyph.
    assert sels["WindowsTerminal.exe"] == 'window "Pypilot library"[app="WindowsTerminal.exe"] button "Run"'
    assert sels["notepad.exe"] == 'window "Pypilot library"[app="notepad.exe"] button "Run"'
    retitle(a, "◑ Pypilot library")
    for widget in (a_btn, b_btn):
        found = hit(sels[widget.window.app], be)
        assert len(found) == 1 and found[0].widget is widget


def test_two_similar_windows_of_one_app_fall_back_to_a_position():
    be = MockBackend(width=1280, height=800)
    a = be.add_window("*doc.txt - Notepad", (0, 0, 400, 300), app="notepad.exe")
    a.add_button("Save", (10, 10, 80, 30))
    b = be.add_window("doc.txt - Notepad", (500, 0, 400, 300), app="notepad.exe")
    b.add_button("Save", (10, 10, 80, 30))
    roots = be.accessibility_roots()
    cache = []
    out = []
    for r in roots:
        node = r.children()[0]
        s = selector_for(node, roots=roots, prefix_cache=cache)
        found = parse(s).find_all(roots)
        assert len(found) == 1 and found[0].same_as(node), s
        out.append(s)
    assert out[0] != out[1]


def test_title_tolerance_is_limited_to_windows_and_plain_tests():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("*doc.txt - Notepad", (0, 0, 400, 300))
    w.add_button("* Required", (10, 10, 80, 30))
    roots = be.accessibility_roots()
    assert len(parse('window "doc.txt - Notepad"').find_all(roots)) == 1
    assert len(parse('window[name^="doc.txt"]').find_all(roots)) == 1
    assert len(parse('window[name="DOC.TXT - notepad" i]').find_all(roots)) == 1
    assert parse('window[name!="doc.txt - Notepad"]').find_all(roots) == []
    # The written form still matches, and patterns are matched as written.
    assert len(parse('window "*doc.txt - Notepad"').find_all(roots)) == 1
    assert parse('window[name~="^doc"]').find_all(roots) == []
    assert parse('window[name%="doc*"]').find_all(roots) == []
    # Not for controls: a button's leading "*" is content.
    assert parse('button "Required"').find_all(roots) == []
    assert len(parse('button "* Required"').find_all(roots)) == 1


def test_owning_window_matches_an_animated_title():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("◐ Pypilot library", (0, 0, 400, 300), pid=7)
    w.add_button("Run", (10, 10, 80, 30))
    be.add_window("Other", (500, 0, 300, 300), pid=7)
    node = be.accessibility_roots()[1].children()[0]
    # list_windows() saw the previous spinner frame; the node reads the next one.
    wins = [WindowInfo(handle=1, title="◑ Pypilot library", pid=7, rect=Rect(0, 0, 10, 10)),
            WindowInfo(handle=2, title="Other", pid=7, rect=Rect(600, 0, 10, 10))]
    got = owning_window(be, node, wins, guess=False)
    assert got is not None and got.handle == 1


# ------------------------------------------------------------------ (a) cost

def busy_desktop(windows=5, buttons=30):
    be = MockBackend(width=1600, height=900)
    for i in range(windows):
        w = be.add_window(f"Window {i}", (0, 0, 1000, 800), app=f"app{i}", activate=False)
        pane = w.add("pane", "Body", (0, 0, 1000, 800))
        for j in range(buttons):
            pane.add_button(f"B{j}", (10 * j, 10, 9, 9))
        pane.add_button("OK", (10, 50, 9, 9))
        pane.add_button("OK", (30, 50, 9, 9))  # duplicates: forces ancestor / :nth fallbacks
    return be


def targets(be):
    out = []
    for r in be.accessibility_roots():
        pane = r.children()[0]
        out.extend(pane.children()[::7])
    return out


def test_shared_cache_reads_each_tree_once_and_keeps_the_selectors(walks):
    be = busy_desktop()
    roots = be.accessibility_roots()
    nodes = targets(be)
    expected = [selector_for(n, roots=roots) for n in nodes]  # no cache: proven on the live tree
    walks["n"] = 0
    cache = []
    got = [selector_for(n, roots=roots, prefix_cache=cache) for n in nodes]
    assert got == expected
    total = sum(1 for r in roots for _ in parse("*").find_all([r]))
    walks["n"] = 0
    got_again = [selector_for(n, roots=roots, prefix_cache=cache) for n in nodes]
    assert got_again == expected
    assert walks["n"] == 0  # everything came from the shared tree
    for s, n in zip(got, nodes):
        found = parse(s).find_all(roots)
        assert len(found) == 1 and found[0].same_as(n), s
    assert total > 100
    # The cache keeps its documented shape: (top, prefix, unique) entries only.
    assert all(isinstance(e, tuple) and len(e) == 3 and isinstance(e[0], AccessibleNode) for e in cache)


def test_first_selector_walks_the_desktop_once(walks):
    be = busy_desktop()
    roots = be.accessibility_roots()
    nodes = targets(be)
    total = walks["n"]
    for n in nodes:
        selector_for(n, roots=roots, prefix_cache=[])  # a fresh cache each time: no sharing
    unshared = walks["n"] - total
    nodes_total = sum(1 for _ in parse("*").find_all(roots))
    walks["n"] = 0
    cache = []
    for n in nodes:
        selector_for(n, roots=roots, prefix_cache=cache)
    assert walks["n"] <= nodes_total  # each node's children read at most once
    assert walks["n"] * 5 < unshared


def test_node_added_after_the_tree_was_read_gets_a_correct_selector():
    be = busy_desktop(windows=2)
    roots = be.accessibility_roots()
    cache = []
    selector_for(targets(be)[0], roots=roots, prefix_cache=cache)  # reads the tree
    w = be.windows[0]
    pane = w.root.children[0]
    pane.add_button("Late", (500, 500, 9, 9))
    roots = be.accessibility_roots()
    node = next(r for r in roots if r.widget is w.root).children()[0].children()[-1]
    assert node.name == "Late"
    s = selector_for(node, roots=roots, prefix_cache=cache)
    found = parse(s).find_all(roots)
    assert len(found) == 1 and found[0].same_as(node), s
    assert s.endswith('button "Late"')


def test_tree_expires_so_later_duplicates_are_seen(monkeypatch):
    be = busy_desktop(windows=2)
    roots = be.accessibility_roots()
    cache = []
    w = be.windows[0]
    node = next(r for r in roots if r.widget is w.root).children()[0].children()[3]
    assert selector_for(node, roots=roots, prefix_cache=cache).endswith('button "B3"')
    # A twin appears later; once the tree is older than the TTL it is read again.
    w.root.children[0].add_button("B3", (900, 500, 9, 9))
    clock = [sel_mod.time.monotonic() + sel_mod._TREE_TTL + 1]
    monkeypatch.setattr(sel_mod.time, "monotonic", lambda: clock[0])
    s = selector_for(node, roots=be.accessibility_roots(), prefix_cache=cache)
    found = parse(s).find_all(be.accessibility_roots())
    assert len(found) == 1 and found[0].same_as(node), s
    assert s != 'window "Window 1" button "B3"'


def test_without_a_cache_nothing_is_shared(walks):
    be = busy_desktop(windows=2)
    roots = be.accessibility_roots()
    node = targets(be)[1]
    selector_for(node, roots=roots)
    first = walks["n"]
    walks["n"] = 0
    selector_for(node, roots=roots)
    assert walks["n"] > first // 2 > 0  # the live tree is walked again


# ------------------------------------------------------------------ _as_roots

class NodeWithNodeAttr(mock.MockNode):
    """An AccessibleNode that happens to carry an unrelated ``node`` attribute."""

    node = "raw handle"


def test_as_roots_never_unwraps_a_node():
    be = MockBackend()
    w = be.add_window("W", (0, 0, 100, 100))
    w.add_button("OK", (1, 1, 9, 9))
    n = NodeWithNodeAttr(be, w.root)
    assert _as_roots([n]) == [n]
    assert len(parse('button "OK"').find_all([n])) == 1


def test_as_roots_unwraps_element_wrappers():
    from locum import Desktop

    be = MockBackend()
    w = be.add_window("W", (0, 0, 100, 100))
    w.add_button("OK", (1, 1, 9, 9))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    el = d.find('window "W"')
    assert _as_roots([el])[0] is el.node
    assert _as_roots(el)[0] is el.node  # a single wrapper, not only a list of them
    assert len(parse('button "OK"').find_all(el)) == 1
