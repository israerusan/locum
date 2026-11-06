"""Sibling combinators (``A + B`` adjacent, ``A ~ B`` general) in the CSS selector language."""

from __future__ import annotations

from typing import List, Optional, Sequence

import pytest

from _stub_desktop import StubDesktop
from locum.backends.base import AccessibleNode
from locum.backends.mock import MockBackend
from locum.errors import SelectorSyntaxError
from locum.heal import heal
from locum.llm.safety import node_matches_selector
from locum.selectors import _COMBINATORS, parse
from locum.types import Rect


@pytest.fixture
def form():
    """Two windows; the Calc form has label/edit pairs, a group of buttons and a list."""
    be = MockBackend(width=1280, height=800)
    calc = be.add_window("Calc", (100, 100, 500, 500), app="calc.exe", pid=1)
    pane = calc.add("pane", "Form", (0, 0, 500, 300), automation_id="form")
    pane.add_text("Name:", (10, 10, 60, 20))
    pane.add_edit("name", (80, 10, 200, 20), automation_id="nameEdit")
    pane.add_text("Email:", (10, 40, 60, 20))
    pane.add_edit("email", (80, 40, 200, 20), automation_id="emailEdit")
    pane.add_text("Notes:", (10, 70, 60, 20))
    pane.add_checkbox("Urgent", (80, 70, 80, 20))
    pane.add_edit("notes", (170, 70, 200, 20), automation_id="notesEdit")
    calc.add("group", "Actions", (0, 300, 500, 40))
    calc.add_button("Save", (10, 350, 60, 25), automation_id="save")
    calc.add_button("Cancel", (80, 350, 60, 25), automation_id="cancel")
    calc.add_button("Help", (150, 350, 60, 25), automation_id="help", enabled=False)
    calc.add_list("Recent", (300, 350, 100, 100), items=["a", "b", "c", "d"])
    other = be.add_window("Other", (700, 100, 300, 200), app="other.exe", pid=2)
    other.add_text("Name:", (10, 10, 60, 20))
    other.add_button("OK", (80, 10, 60, 20))
    return be


def roots(be):
    return be.accessibility_roots()


def names(nodes: Sequence[AccessibleNode]) -> List[str]:
    return [f"{n.role}:{n.name}" for n in nodes]


def find(be, sel, **kw):
    return names(parse(sel).find_all(roots(be), **kw))


# ------------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    "text, canonical",
    [
        ("text + edit", "text + edit"),
        ("text+edit", "text + edit"),
        ("group ~ button", "group ~ button"),
        ("group~button", "group ~ button"),
        ('window "Calc" > pane text "Name:" + edit', 'window "Calc" > pane text "Name:" + edit'),
        ("group ~ button:nth(2)", "group ~ button:nth(2)"),
        ('"Name:" + edit', '"Name:" + edit'),
        ('[name~="^Na"] + edit', '[name~="^Na"] + edit'),
        ("a + b ~ c > d e", "a + b ~ c > d e"),
        ("text + edit, group ~ button", "text + edit, group ~ button"),
    ],
)
def test_parse_and_round_trip(text, canonical):
    sel = parse(text)
    assert str(sel) == canonical
    assert str(parse(str(sel))) == canonical


def test_steps_record_the_combinator():
    sel = parse('window "Calc" > pane text "Name:" + edit ~ button')
    assert [c for c, _ in sel.branches[0].steps] == [None, ">", " ", "+", "~"]
    assert set(_COMBINATORS) == {">", "+", "~"}


def test_regex_operator_still_works(form):
    # ``~=`` is an attribute operator; the lexer must not split it into ``~`` + ``=``.
    assert find(form, '[name~="^(Save|Cancel)$"]') == ["button:Save", "button:Cancel"]
    assert find(form, "button[name~=^Ca]") == ["button:Cancel"]
    assert find(form, '[name~="^Na"] + edit') == ["edit:name"]
    assert find(form, 'group ~ button[name~="^C"]') == ["button:Cancel"]
    assert find(form, '[name~="~"]') == []  # a tilde inside the regex value is just text
    assert find(form, "button[name~=a~b]") == []


@pytest.mark.parametrize(
    "text, reason",
    [
        ("+ edit", "Selector cannot start with '+'"),
        ("~ edit", "Selector cannot start with '~'"),
        ("text +", "Expected a selector after '+'"),
        ("text ~", "Expected a selector after '~'"),
        ("text + + edit", "Expected a selector after '+'"),
        ("text + ~ edit", "Expected a selector after '+'"),
        ("text > + edit", "Expected a selector after '>'"),
        ("text ~, edit", "Expected a selector after '~'"),
        ("pane:has(+ edit)", "':has()' can only start with '>', not the sibling combinator '+'"),
        ("pane:has(~ edit)", "':has()' can only start with '>', not the sibling combinator '~'"),
        ("pane:has(text +)", "Expected a selector after '+'"),
    ],
)
def test_syntax_errors(text, reason):
    with pytest.raises(SelectorSyntaxError) as ei:
        parse(text)
    assert ei.value.reason == reason


def test_tilde_inside_brackets_is_not_a_combinator():
    with pytest.raises(SelectorSyntaxError) as ei:
        parse('[name ~ "x"]')
    assert ei.value.reason.startswith("Expected an operator")
    with pytest.raises(SelectorSyntaxError) as ei:
        parse("[name + x]")
    assert ei.value.reason.startswith("Expected an operator")


def test_error_points_at_the_combinator():
    with pytest.raises(SelectorSyntaxError) as ei:
        parse("text + ")
    assert ei.value.position == len("text + ")
    with pytest.raises(SelectorSyntaxError) as ei:
        parse("  ~ edit")
    assert ei.value.position == 2


# ------------------------------------------------------------------ matching
def test_adjacent_sibling(form):
    assert find(form, "text + edit") == ["edit:name", "edit:email"]  # Notes: is followed by a checkbox
    assert find(form, 'text "Name:" + edit') == ["edit:name"]
    assert find(form, 'text "Notes:" + edit') == []
    assert find(form, 'text "Notes:" + checkbox + edit') == ["edit:notes"]
    assert find(form, "edit + text") == ["text:Email:", "text:Notes:"]
    assert find(form, 'text "Name:" + button') == ["button:OK"]  # in the Other window


def test_general_sibling(form):
    assert find(form, 'text "Notes:" ~ edit') == ["edit:notes"]
    assert find(form, 'text "Name:" ~ edit') == ["edit:name", "edit:email", "edit:notes"]
    assert find(form, "group ~ button") == ["button:Save", "button:Cancel", "button:Help"]
    assert find(form, "button ~ button") == ["button:Cancel", "button:Help"]
    assert find(form, "button ~ group") == []  # siblings are only the *later* ones
    assert find(form, 'edit "notes" ~ *') == []  # the last child has no later sibling


def test_siblings_need_the_same_parent(form):
    # The pane's edits and the window's buttons are cousins, not siblings.
    assert find(form, "edit ~ button") == []
    assert find(form, "pane ~ button") == ["button:Save", "button:Cancel", "button:Help"]
    assert find(form, "listitem ~ button") == []
    assert find(form, 'listitem "b" ~ listitem') == ["listitem:c", "listitem:d"]
    assert find(form, 'listitem "b" + listitem') == ["listitem:c"]


def test_roots_have_no_siblings(form):
    assert find(form, "window + window") == []
    assert find(form, "window ~ window") == []
    assert find(form, "window ~ *") == []


def test_composition_with_ancestry(form):
    assert find(form, 'window "Calc" > pane text "Name:" + edit') == ["edit:name"]
    assert find(form, 'window "Other" text "Name:" + edit') == []
    assert find(form, 'window "Other" text "Name:" + button') == ["button:OK"]
    assert find(form, 'window "Calc" > pane > text + edit') == ["edit:name", "edit:email"]
    # A sibling step in the middle, followed by descendant / child steps.
    assert find(form, "pane ~ list listitem") == ["listitem:a", "listitem:b", "listitem:c", "listitem:d"]
    assert find(form, 'button "Help" + list > listitem "c"') == ["listitem:c"]
    assert find(form, 'pane + list listitem') == []
    assert find(form, "group + button + button") == ["button:Cancel"]


def test_composition_with_pseudo_classes(form):
    assert find(form, "group ~ button:nth(2)") == ["button:Help"]
    assert find(form, "group ~ button:first") == ["button:Save"]
    assert find(form, "group ~ button:last") == ["button:Help"]
    assert find(form, "group ~ button:disabled") == ["button:Help"]
    assert find(form, "group ~ button:not(:disabled)") == ["button:Save", "button:Cancel"]
    assert find(form, "text:has-text(mail) + edit") == ["edit:email"]
    assert find(form, "text + edit:nth(-1)") == ["edit:email"]
    assert find(form, 'text "Name:" ~ edit:nth(1)') == ["edit:email"]  # positional after ``~``
    # Roots are in z-order: the Other window (added last) comes first.
    assert find(form, "text:first ~ edit") == []  # Other's "Name:" label has no edit after it
    assert find(form, 'window "Calc" text:first ~ edit') == ["edit:name", "edit:email", "edit:notes"]
    assert find(form, "text:nth(1) + edit") == ["edit:name"]  # nth(0) is the Other window's label
    assert find(form, "pane text:nth(1) + edit") == ["edit:email"]
    assert find(form, 'listitem:nth(1) + listitem') == ["listitem:c"]


def test_siblings_inside_has_and_not(form):
    assert find(form, "pane:has(text + checkbox)") == ["pane:Form"]
    assert find(form, "pane:has(> text + edit)") == ["pane:Form"]
    assert find(form, "window:has(group ~ button)") == ["window:Calc"]
    assert find(form, "window:has(text + button)") == ["window:Other"]
    assert find(form, "edit:not(checkbox + edit)") == ["edit:name", "edit:email"]
    assert find(form, "button:not(group + button)") == ["button:OK", "button:Cancel", "button:Help"]
    # :has() is scoped to the element: siblings of the pane itself are outside it.
    assert find(form, "pane:has(pane ~ button)") == []


def test_union_and_limits(form):
    assert find(form, 'text "Name:" + edit, group ~ button "Help"') == ["edit:name", "button:Help"]
    assert find(form, "text + edit", limit=1) == ["edit:name"]
    assert parse("group ~ button").find_first(roots(form)).name == "Save"
    # Positional union branches are merged in document order.
    assert find(form, "group ~ button:last, text + edit:first") == ["edit:name", "button:Help"]


def test_include_roots_false(form):
    win = next(r for r in roots(form) if r.name == "Calc")
    found = parse("text + edit").find_all([win], include_roots=False)
    assert names(found) == ["edit:name", "edit:email"]


def test_max_depth_still_bounds_siblings(form):
    # listitems are depth 2; with max_depth=1 they are never reached, via siblings or otherwise.
    assert find(form, 'listitem "a" ~ listitem', max_depth=1) == []
    assert find(form, "group ~ button", max_depth=1) == ["button:Save", "button:Cancel", "button:Help"]


def test_matches_ignores_siblings(form):
    sel = parse('text "Name:" + edit')
    edits = [n for n in parse("edit").find_all(roots(form))]
    assert all(sel.matches(e) for e in edits)  # only the last compound is tested


# ----------------------------------------------------------- integrations
def test_element_and_window_scoped_find(form):
    d = StubDesktop(form)
    pane = d.find("pane#form", timeout=0)
    assert pane.find('text "Email:" + edit', timeout=0).automation_id == "emailEdit"
    assert [e.automation_id for e in pane.find_all("checkbox ~ edit")] == ["notesEdit"]
    assert d.find('window "Calc" > pane text "Name:" + edit', timeout=0).automation_id == "nameEdit"


def test_safety_node_matches_selector(form):
    d = StubDesktop(form)
    email = d.find("#emailEdit", timeout=0).node
    name = d.find("#nameEdit", timeout=0).node
    assert node_matches_selector(email, 'text "Email:" + edit')
    assert not node_matches_selector(name, 'text "Email:" + edit')
    assert node_matches_selector(name, 'window "Calc" > pane text "Name:" + edit')
    assert not node_matches_selector(email, 'text "Name:" + edit')
    assert node_matches_selector(email, 'text "Name:" ~ edit')


def test_heal_with_a_sibling_first_step(form):
    # The edit was renamed; healing keeps the scope of the selector's first compound
    # (here a sibling, which still lies in the right window) and finds it by name.
    out = heal('text "Name:" + edit "nam"', roots(form), log=False)
    assert out.node is not None and out.node.name == "name"


# ----------------------------------------------------------- efficiency
class CountingNode(AccessibleNode):
    """A synthetic tree that counts property reads of ``name``."""

    name_reads = 0

    def __init__(self, role, name, kids=(), parent=None):
        self._role, self._name, self._kids, self._parent = role, name, list(kids), parent
        for k in self._kids:
            k._parent = self

    @property
    def role(self):
        return self._role

    @property
    def name(self):
        CountingNode.name_reads += 1
        return self._name

    @property
    def bounds(self) -> Optional[Rect]:
        return Rect(0, 0, 10, 10)

    def children(self):
        return self._kids

    @property
    def parent(self):
        return self._parent


def test_general_sibling_is_linear_in_the_number_of_siblings():
    n = 2000
    kids = [CountingNode("listitem", f"item{i}") for i in range(n)]
    root = CountingNode("list", "big", kids)
    CountingNode.name_reads = 0
    found = parse('listitem "item0" ~ listitem').find_all([root])
    assert len(found) == n - 1
    # Each node's name is read once (snapshot cache), not once per (node, earlier sibling) pair.
    assert CountingNode.name_reads <= n + 1
    CountingNode.name_reads = 0
    assert parse('listitem "nope" ~ listitem').find_all([root]) == []
    assert CountingNode.name_reads <= n + 1
