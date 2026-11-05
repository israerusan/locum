from __future__ import annotations

import re
from typing import List, Optional, Sequence

import pytest

from pypilot.backends.base import AccessibleNode
from pypilot.backends.mock import MockBackend
from pypilot.errors import SelectorSyntaxError
from pypilot.selectors import describe_node, parse, quote, selector_for, tree_text
from pypilot.types import Rect


@pytest.fixture
def scene():
    be = MockBackend(width=1280, height=800)
    other = be.add_window("Other", (700, 100, 300, 200), app="other.exe", pid=7)
    other.add_button("OK", (10, 10, 60, 25))
    calc = be.add_window("Calculator", (100, 100, 400, 400), app="CALC.EXE", pid=42)
    keys = calc.add("pane", "Keypad", (0, 40, 400, 300), automation_id="keypad", class_name="KeyPad")
    keys.add_button("7", (0, 40, 40, 40), automation_id="num7")
    keys.add_button("8", (40, 40, 40, 40), automation_id="num8")
    keys.add_button("9", (80, 40, 40, 40), automation_id="num9", enabled=False)
    calc.add_button("OK", (10, 350, 80, 30), automation_id="okBtn", class_name="Button")
    calc.add_button("OK", (100, 350, 80, 30), enabled=False)
    calc.add_edit("Display", (10, 5, 300, 30), value="123.45", automation_id="display")
    calc.add_checkbox("Scientific", (320, 5, 70, 30), checked=True, attrs={"data-mode": "sci"})
    calc.add_list("History", (300, 40, 100, 100), items=["1+1", "2*3", "Sum total"])
    return be


def roots(be):
    return be.accessibility_roots()


def names(nodes: Sequence[AccessibleNode]) -> List[str]:
    return [f"{n.role}:{n.name}" for n in nodes]


def find(be, sel, **kw):
    return names(parse(sel).find_all(roots(be), **kw))


# ------------------------------------------------------------------- basics
def test_role_and_star(scene):
    assert find(scene, "button") == ["button:7", "button:8", "button:9", "button:OK", "button:OK", "button:OK"]
    assert len(find(scene, "*")) == 15
    assert find(scene, "window") == ["window:Calculator", "window:Other"]
    assert find(scene, "BUTTON") == find(scene, "button")
    assert find(scene, "textbox") == ["edit:Display"]  # role alias


def test_id_class_and_attributes(scene):
    assert find(scene, "#num8") == ["button:8"]
    assert find(scene, "button#okBtn") == ["button:OK"]
    assert find(scene, ".KeyPad") == ["pane:Keypad"]
    assert find(scene, "[class=Button]") == ["button:OK"]
    assert find(scene, '[name="7"]') == ["button:7"]
    assert find(scene, "[title=Calculator]") == ["window:Calculator"]
    assert find(scene, "[id=display]") == ["edit:Display"]
    assert find(scene, "[automation_id^=num]") == ["button:7", "button:8", "button:9"]
    assert find(scene, "[data-mode=sci]") == ["checkbox:Scientific"]
    assert find(scene, "[role=edit]") == ["edit:Display"]


def test_attribute_operators(scene):
    assert find(scene, 'listitem[name*="tot"]') == ["listitem:Sum total"]
    assert find(scene, 'listitem[name^="2"]') == ["listitem:2*3"]
    assert find(scene, 'listitem[name$="1"]') == ["listitem:1+1"]
    assert find(scene, 'listitem[name~="^\\d\\*\\d$"]') == ["listitem:2*3"]
    assert find(scene, 'listitem[name%="?+?"]') == ["listitem:1+1"]
    assert find(scene, 'listitem[name%="S*"]') == ["listitem:Sum total"]
    assert find(scene, 'listitem[name!="1+1"]') == ["listitem:2*3", "listitem:Sum total"]
    assert find(scene, 'listitem[name="sum TOTAL" i]') == ["listitem:Sum total"]
    assert find(scene, 'listitem[name="sum TOTAL"]') == []
    assert find(scene, 'listitem[name*=SUM i]') == ["listitem:Sum total"]
    assert find(scene, 'listitem[name~="^sum" i]') == ["listitem:Sum total"]
    assert find(scene, "button[id]") == ["button:7", "button:8", "button:9", "button:OK"]
    assert find(scene, "edit[value]") == ["edit:Display"]


def test_text_attribute_matches_name_or_value(scene):
    assert find(scene, '[text="123.45"]') == ["edit:Display"]
    assert find(scene, '[text="Display"]') == ["edit:Display"]
    assert find(scene, 'edit[text!="123.45"]') == []
    assert find(scene, '"123.45"') == ["edit:Display"]


def test_app_and_pid_attributes(scene):
    assert find(scene, "window[app=calc]") == ["window:Calculator"]
    assert find(scene, "window[app=calc.exe]") == ["window:Calculator"]
    assert find(scene, "window[app%=CA*]") == ["window:Calculator"]
    assert find(scene, "window[pid=7]") == ["window:Other"]
    h = scene.windows[0].handle
    assert find(scene, f"[handle={h}]:root") == ["window:Calculator"]


def test_shorthand_and_bare_string(scene):
    assert find(scene, 'button "7"') == ["button:7"]
    assert find(scene, "button '7'") == ["button:7"]
    assert find(scene, 'button"7"') == ["button:7"]
    assert find(scene, '"Keypad"') == ["pane:Keypad"]
    # second string after a shorthand is a descendant selector
    assert find(scene, 'window "Calculator" "OK"') == ["button:OK", "button:OK"]
    assert find(scene, 'button "OK":enabled') == ["button:OK", "button:OK"]
    assert find(scene, 'button#okBtn "OK"') == ["button:OK"]
    assert find(scene, '* "Keypad"') == ["pane:Keypad"]


def test_state_pseudo_classes(scene):
    assert find(scene, "button:disabled") == ["button:9", "button:OK"]
    assert len(find(scene, "button:enabled")) == 4
    assert find(scene, ":checked") == ["checkbox:Scientific"]
    assert find(scene, "checkbox:unchecked") == []
    assert find(scene, ":editable") == ["edit:Display"]
    assert find(scene, "window:active") == ["window:Calculator"]
    assert find(scene, ":root") == ["window:Calculator", "window:Other"]
    assert find(scene, "button:visible:enabled#num7") == ["button:7"]
    scene.windows[1].minimized = True  # Other
    assert find(scene, "window:hidden") == ["window:Other"]
    assert "button:OK" in find(scene, "window:visible button")
    scene.windows[0].root.find("Display").attrs["selected"] = True
    assert find(scene, ":selected") == ["edit:Display"]


def test_focused_pseudo(scene):
    edit = scene.windows[0].find("Display")
    scene._set_focus(edit)
    assert find(scene, ":focused") == ["edit:Display"]


def test_has_text(scene):
    assert find(scene, 'listitem:has-text("TOTAL")') == ["listitem:Sum total"]
    assert find(scene, "listitem:has-text(sum tot)") == ["listitem:Sum total"]
    assert find(scene, "edit:has-text(23.4)") == ["edit:Display"]
    assert find(scene, "listitem:contains('2*')") == ["listitem:2*3"]


def test_has_and_not(scene):
    assert find(scene, "pane:has(#num7)") == ["pane:Keypad"]
    assert find(scene, "window:has(edit)") == ["window:Calculator"]
    assert find(scene, "window:has(> button:disabled)") == ["window:Calculator"]
    assert find(scene, "window:has(> #num7)") == []
    assert find(scene, "window:has(pane > #num7)") == ["window:Calculator"]
    assert find(scene, "button:not(:disabled):not(#okBtn)") == ["button:7", "button:8", "button:OK"]
    assert find(scene, "button:not(pane button)") == ["button:OK", "button:OK", "button:OK"]
    assert find(scene, "window:not(:has(edit))") == ["window:Other"]
    assert find(scene, 'window:has(button "8", edit)') == ["window:Calculator"]


def test_positional(scene):
    assert find(scene, "button:nth(0)") == ["button:7"]
    assert find(scene, "button:nth(1)") == ["button:8"]
    assert find(scene, "button:nth(-1)") == ["button:OK"]
    assert find(scene, "button:nth(99)") == []
    assert find(scene, "button:first") == ["button:7"]
    assert find(scene, "pane button:last") == ["button:9"]
    assert find(scene, 'window "Calculator" button:nth(0)') == ["button:7"]
    # positional applies to matches *so far*: filter after pick vs pick after filter
    assert find(scene, "button:nth(2):enabled") == []  # button 9 is disabled
    assert find(scene, "button:enabled:nth(2)") == ["button:OK"]
    assert names(parse("button:enabled:nth(2)").find_all(roots(scene)))[0] == "button:OK"
    assert parse("button:enabled:nth(2)").find_first(roots(scene)).automation_id == "okBtn"
    assert find(scene, "pane:nth(0) > button:nth(1)") == ["button:8"]
    assert find(scene, "listitem:nth(1)") == ["listitem:2*3"]


def test_combinators(scene):
    assert find(scene, "window > button") == ["button:OK", "button:OK", "button:OK"]
    assert find(scene, "window button") == find(scene, "button")
    assert find(scene, "window>pane>button#num8") == ["button:8"]
    assert find(scene, 'window "Other" button') == ["button:OK"]
    assert find(scene, "pane > listitem") == []
    assert find(scene, "window list listitem") == ["listitem:1+1", "listitem:2*3", "listitem:Sum total"]
    assert find(scene, "window window") == []


def test_union_document_order_and_dedup(scene):
    out = find(scene, "edit, pane, #num7, button#num7")
    assert out == ["pane:Keypad", "button:7", "edit:Display"]
    assert find(scene, 'button "7", button "8"', limit=1) == ["button:7"]


def test_limit_and_max_depth(scene):
    assert find(scene, "button", limit=2) == ["button:7", "button:8"]
    assert find(scene, "button", limit=0) == []
    assert find(scene, "button", max_depth=1) == ["button:OK", "button:OK", "button:OK"]
    assert find(scene, "*", max_depth=0) == ["window:Calculator", "window:Other"]
    assert find(scene, "button:nth(1)", max_depth=1) == ["button:OK"]


def test_find_first_and_include_roots(scene):
    sel = parse("button")
    first = sel.find_first(roots(scene))
    assert first.name == "7"
    assert parse("listitem").find_first(roots(scene), max_depth=1) is None
    calc_root = roots(scene)[0]
    assert parse("window").find_first([calc_root]).name == "Calculator"
    assert parse("window").find_first([calc_root], include_roots=False) is None
    assert parse("window > pane").find_first(calc_root, include_roots=False).name == "Keypad"
    assert parse("*:nth(0)").find_all(calc_root, include_roots=False)[0].name == "Keypad"
    assert parse("button").find_all([]) == []


def test_matches_ignores_ancestry(scene):
    seven = parse("#num7").find_first(roots(scene))
    assert parse('window "Nope" button#num7').matches(seven)
    assert parse("button:nth(5)").matches(seven)  # positional ignored
    assert not parse("edit").matches(seven)
    assert parse("edit, button").matches(seven)
    assert parse(":root").matches(seven)  # a standalone node has no parent in its own scope


# ------------------------------------------------------------- UiPath XML
def test_uipath_basic(scene):
    sel = "<wnd app='calc.exe' title='Calc*' /><ctrl name='OK' role='push button' />"
    assert find(scene, sel) == ["button:OK", "button:OK"]
    assert find(scene, "<wnd title='Calculator'/><ctrl automationid='num8'/>") == ["button:8"]
    assert find(scene, "<wnd cls='MockWindow' title='Other'/>") == ["window:Other"]
    assert find(scene, "<wnd title='*'/><ctrl role='editable text'/>") == ["edit:Display"]
    assert find(scene, "<wnd title='Calculator'/><ctrl role='push button' idx='2'/>") == ["button:8"]
    assert find(scene, "<wnd title='Calculator'/><ctrl role='list item' aaname='Sum*'/>") == ["listitem:Sum total"]
    assert find(scene, "<wnd title='Calculator'/><ctrl role='client'/><ctrl aaname='9'/>") == ["button:9"]
    assert find(scene, '<wnd title="Calculator"></wnd><ctrl name="7" />') == ["button:7"]
    assert find(scene, "<wnd title='Calculator &amp; co'/>") == []


def test_uipath_wnd_only_matches_top_level(scene):
    w = scene.windows[0]
    w.add("window", "Calculator", (0, 0, 10, 10))  # a nested node with the same title
    assert find(scene, "<wnd title='Calculator'/>") == ["window:Calculator"]
    assert len(find(scene, 'window "Calculator"')) == 2


def test_uipath_round_trips_to_css(scene):
    sel = parse("<wnd app='calc.exe' title='Calc*'/><ctrl role='push button' idx='1'/>")
    text = str(sel)
    assert text.startswith(":root")
    assert names(parse(text).find_all(roots(scene))) == names(sel.find_all(roots(scene)))


@pytest.mark.parametrize(
    "bad",
    [
        "<wnd title='x'",
        "<wnd title=x/>",
        "<wnd/><ctrl idx='0'/>",
        "<wnd/> junk",
    ],
)
def test_uipath_errors(bad):
    with pytest.raises(SelectorSyntaxError):
        parse(bad)


# ------------------------------------------------------------------- dicts
def test_dict_selectors(scene):
    assert find(scene, {"role": "button", "name": "OK", "within": {"role": "window", "title": "Other"}}) == ["button:OK"]
    assert find(scene, {"id": "num8"}) == ["button:8"]
    assert find(scene, {"role": "button", "enabled": False}) == ["button:9", "button:OK"]
    assert find(scene, {"role": "button", "focused": False, "within": "pane"}) == ["button:7", "button:8", "button:9"]
    assert find(scene, {"role": "listitem", "name__contains": "tot"}) == ["listitem:Sum total"]
    assert find(scene, {"role": "listitem", "name__istartswith": "SUM"}) == ["listitem:Sum total"]
    assert find(scene, {"role": "listitem", "name": re.compile(r"\d\*")}) == ["listitem:2*3"]
    assert find(scene, {"role": "button", "nth": -1}) == ["button:OK"]
    assert find(scene, {"role": "button", "child_of": {"role": "pane"}, "index": 1}) == ["button:8"]
    assert find(scene, {"has_text": "sum"}) == ["listitem:Sum total"]
    assert find(scene, {"role": "window", "has": "edit"}) == ["window:Calculator"]
    assert find(scene, {"role": "window", "not": {"has": "edit"}}) == ["window:Other"]
    assert find(scene, {"states": ["checked"]}) == ["checkbox:Scientific"]
    assert find(scene, {"selector": "#num7"}) == ["button:7"]
    assert find(scene, {"any": ["#num7", {"id": "num8"}]}) == ["button:7", "button:8"]
    assert find(scene, {"role": "edit", "value__regex": r"^\d+\.\d+$"}) == ["edit:Display"]


@pytest.mark.parametrize(
    "bad",
    [{}, {"name__bogus": "x"}, {"states": ["weird"]}, {"name": None}, {"name__regex": "("}, {"within": "a", "child_of": "b"}],
)
def test_dict_errors(bad):
    with pytest.raises(SelectorSyntaxError):
        parse(bad)


def test_dict_round_trip(scene):
    d = {"role": "button", "name__icontains": "o", "within": {"title": "Other"}, "enabled": True}
    sel = parse(d)
    assert find(scene, str(sel)) == find(scene, d) == ["button:OK"]


# ----------------------------------------------------------- parse / str
@pytest.mark.parametrize(
    "text",
    [
        "button",
        "*",
        'button "OK"',
        '"OK"',
        "#okBtn",
        ".KeyPad",
        'window "Calc" > pane button:nth(-1)',
        '[name*="a\\"b" i]',
        "edit:has-text(\"x y\"):not(:disabled)",
        "pane:has(> button#num7), list listitem:last",
        '[name~="^\\d+$"]',
        "[data-mode=sci]",
        "button[id]",
        '* "x"',
        ":root[app%=\"calc*\"]",
    ],
)
def test_str_round_trips(text):
    sel = parse(text)
    again = parse(str(sel))
    assert str(again) == str(sel)
    assert again == sel and hash(again) == hash(sel)


def test_str_canonical_forms():
    assert str(parse("button[title=OK]")) == 'button[name="OK"]'
    assert str(parse("[id='a.b']")) == '[automation_id="a.b"]'
    assert str(parse("#\"a b\"")) == '[automation_id="a b"]'
    assert str(parse("  window  >  button  ")) == "window > button"
    assert str(parse("button:first")) == "button:first"
    assert str(parse("textbox")) == "edit"
    assert repr(parse("edit")) == "Selector('edit')"


def test_quote_and_escapes():
    assert quote('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'
    sel = parse('"line1\\nline2"')
    assert sel.branches[0].steps[0][1].conds[0].value == "line1\nline2"
    assert parse('"\\u00e9"').branches[0].steps[0][1].conds[0].value == "é"
    assert parse("'it\\'s'").branches[0].steps[0][1].conds[0].value == "it's"


def test_parse_cache_and_passthrough():
    a = parse("button#x")
    assert parse("button#x") is a
    assert parse(a) is a
    with pytest.raises(SelectorSyntaxError):
        parse(42)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad,pos",
    [
        ("", 0),
        ("button[", 7),
        ("button[name", 11),
        ('button[name="x"', 15),
        ("button[name=]", 12),
        ('button "x', 7),
        (":nth(a)", 5),
        (":nth(1", 6),
        ("a >", 3),
        ("> a", 0),
        ("a > > b", 4),
        (":bogus", 1),
        ("a + b", 2),
        ("a ~ b", 2),
        ("a,", 2),
        (",a", 0),
        ("a)", 1),
        ("button & x", 7),
        ('[name~="("]', 7),
        ("#", 1),
        (". x", 2),
        (":has()", 5),
        (":has", 4),
        (":not(:first)", 1),
        ("a\"x\" \"y\"z", 8),
        ("a[name=x foo]", 9),
    ],
)
def test_syntax_errors_have_positions(bad, pos):
    with pytest.raises(SelectorSyntaxError) as ei:
        parse(bad)
    err = ei.value
    assert err.position == pos, str(err)
    assert err.selector == bad
    assert "^" in str(err)
    assert isinstance(err, ValueError)


# -------------------------------------------------------- selector_for
def all_nodes(be) -> List[AccessibleNode]:
    return parse("*").find_all(roots(be))


def test_selector_for_round_trips_every_node(scene):
    rs = roots(scene)
    for node in all_nodes(scene):
        s = selector_for(node, roots=rs)
        found = parse(s).find_first(rs)
        assert found is not None and found.same_as(node), (s, node)


def test_selector_for_preferences(scene):
    rs = roots(scene)
    seven = parse("#num7").find_first(rs)
    assert selector_for(seven) == 'window "Calculator" button#num7'
    lst = parse("list").find_first(rs)
    assert selector_for(lst) == 'window "Calculator" list "History"'
    ok_disabled = parse("button:disabled").find_all(rs)[1]
    s = selector_for(ok_disabled)
    assert s.endswith(":nth(1)") and parse(s).find_first(rs).same_as(ok_disabled)
    assert selector_for(rs[0]) == 'window "Calculator"'


def test_selector_for_without_parents_and_with_root(scene):
    rs = roots(scene)
    # element_at builds a full parent chain
    hit = scene.element_at(100 + 45, 100 + 45)
    assert hit.name == "8"
    s = selector_for(hit)
    assert parse(s).find_first(rs).same_as(hit)
    # focused_element() returns a node without parent links -> selector for the node alone
    scene._set_focus(scene.windows[0].find("Display"))
    lonely = scene.focused_element()
    s2 = selector_for(lonely)
    assert s2 == "edit#display"
    assert parse(s2).find_first(rs).same_as(lonely)
    # relative to a root
    pane = parse("pane").find_first(rs)
    eight = parse("#num8").find_first(rs)
    s3 = selector_for(eight, root=pane)
    assert s3.startswith('pane "Keypad"') or s3.startswith("pane#keypad")
    assert parse(s3).find_first([pane]).same_as(eight)


def test_selector_for_duplicate_windows_and_unnamed_nodes():
    be = MockBackend()
    for app in ("a.exe", "b.exe"):
        w = be.add_window("Same", (0, 0, 300, 300), app=app)
        g = w.add("group", "", (0, 0, 300, 300))
        g.add("group", "", (0, 0, 100, 100)).add_button("", (0, 0, 10, 10))
        g.add("group", "", (100, 0, 100, 100)).add_button("", (0, 0, 10, 10))
    rs = be.accessibility_roots()
    for node in all_nodes(be):
        s = selector_for(node, roots=rs)
        assert parse(s).find_first(rs).same_as(node), s


def test_selector_for_long_names(scene):
    long = "x" * 120
    scene.windows[0].add_text(long, (0, 0, 10, 10))
    node = parse("text").find_first(roots(scene))
    s = selector_for(node)
    assert "^=" in s and len(s) < 120
    assert parse(s).find_first(roots(scene)).same_as(node)


def test_selector_for_odd_identifiers():
    be = MockBackend()
    w = be.add_window('Say "hi"\n2', (0, 0, 200, 200))
    w.add("custom role", "x", (0, 0, 5, 5), automation_id="1.weird id", class_name="Win.Forms")
    rs = be.accessibility_roots()
    for node in all_nodes(be):
        s = selector_for(node, roots=rs)
        assert parse(s).find_first(rs).same_as(node), s


# ----------------------------------------------------------------- efficiency
class CountingNode(AccessibleNode):
    """A synthetic tree that counts children() calls."""

    calls = 0

    def __init__(self, role, name, kids=(), parent=None):
        self._role, self._name, self._kids, self._parent = role, name, list(kids), parent
        for k in self._kids:
            k._parent = self

    @property
    def role(self):
        return self._role

    @property
    def name(self):
        return self._name

    @property
    def bounds(self) -> Optional[Rect]:
        return Rect(0, 0, 10, 10)

    def children(self):
        CountingNode.calls += 1
        return self._kids

    @property
    def parent(self):
        return self._parent


def make_tree(width=6, depth=4, prefix="n"):
    if depth == 0:
        return CountingNode("button", prefix)
    return CountingNode("group", prefix, [make_tree(width, depth - 1, f"{prefix}.{i}") for i in range(width)])


def test_each_node_children_fetched_once_per_query():
    root = make_tree()
    total = sum(1 for _ in root.walk())
    CountingNode.calls = 0
    parse("group group:has(button) > group button:nth(-1), group button:not(:disabled)").find_all([root])
    assert CountingNode.calls <= total


def test_find_first_stops_early():
    root = make_tree(width=8, depth=4)
    CountingNode.calls = 0
    first = parse("button").find_first([root])
    assert first.name == "n.0.0.0.0"
    assert CountingNode.calls <= 5


def test_union_find_first_stops_early():
    root = make_tree(width=8, depth=4)
    CountingNode.calls = 0
    first = parse('[name="n.7.7.7.7"], button').find_first([root])
    assert first.name == "n.0.0.0.0"
    assert CountingNode.calls <= 5
    mixed = parse("button:nth(1), group > button:first").find_all([root])
    assert [n.name for n in mixed][:2] == ["n.0.0.0.0", "n.0.0.0.1"]


def test_deep_descendant_chain_is_not_exponential():
    # 30 nested groups; "group group group group button" must not backtrack exponentially
    node = CountingNode("button", "leaf")
    for i in range(30):
        node = CountingNode("group", f"g{i}", [node])
    found = parse("group group group group group group button").find_all([node])
    assert names(found) == ["button:leaf"]


def test_stale_children_are_ignored():
    class Broken(CountingNode):
        def children(self):
            raise RuntimeError("stale")

    root = CountingNode("window", "w", [Broken("pane", "p"), CountingNode("button", "b")])
    assert names(parse("button").find_all(root)) == ["button:b"]


# ------------------------------------------------------------------ tree_text
def test_tree_text(scene):
    text = tree_text(roots(scene))
    lines = text.splitlines()
    assert lines[0].startswith('window "Calculator" (100,100 400x400) [active]')
    assert '  pane "Keypad" #keypad (100,140 400x300)' in lines
    assert '    button "7" #num7 (100,140 40x40)' in lines
    assert any(line.strip().startswith('button "9" #num9') and line.endswith("[disabled]") for line in lines)
    assert any('edit "Display" #display value="123.45"' in line for line in lines)
    assert any(line.strip().startswith('checkbox "Scientific"') and "[checked]" in line for line in lines)


def test_tree_text_options(scene):
    no_bounds = tree_text(roots(scene), include_bounds=False)
    assert "(" not in no_bounds.replace("(", "", 0).split("\n")[0]
    shallow = tree_text(roots(scene), max_depth=1)
    assert "listitem" not in shallow and "list" in shallow
    inter = tree_text(roots(scene), interactive_only=True)
    assert "pane" not in inter and "list " not in inter
    assert '  button "7" #num7' in inter  # promoted to window-child indentation
    assert '  listitem "Sum total"' in inter
    trunc = tree_text(roots(scene), max_nodes=3)
    assert len(trunc.splitlines()) == 4 and trunc.endswith("(truncated after 3 nodes)")
    assert tree_text([]) == ""
    assert tree_text(roots(scene)[0]).startswith('window "Calculator"')


def test_describe_node_escapes_and_truncates():
    be = MockBackend()
    w = be.add_window("T", (0, 0, 100, 100))
    w.add_edit('a "quoted"\nname', (0, 0, 10, 10), value="v" * 200)
    node = parse("edit").find_first(be.accessibility_roots())
    d = describe_node(node)
    assert d.startswith('edit "a \\"quoted\\"\\nname" value="vvv')
    assert "…" in d
