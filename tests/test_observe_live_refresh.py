"""find_elements / wait_for must describe an element as it is *now*.

Seen on Windows Calculator: after observe, the display's entry said "Display is 0";
a later wait_for / find_elements re-used that snapshot and printed "Display is 0"
while the display read 56,154 (read_text, which reads the live node, was right).
"""

from __future__ import annotations

import pytest

from locum import Desktop
from locum.cli import DemoBackend
from locum.llm.observe import observe
from locum.types import Rect


class LiveNode:
    def __init__(self, name):
        self.role = "text"
        self.name = name
        self.value = None
        self.automation_id = "CalculatorResults"
        self.bounds = Rect(10, 10, 200, 40)
        self.states = frozenset()

    def same_as(self, other):
        return other is self


@pytest.fixture
def obs():
    d = Desktop(DemoBackend(), sleep=lambda s: None, default_timeout=0)
    return observe(d, screenshot=False)


def test_existing_entry_is_refreshed_from_the_live_node(obs):
    node = LiveNode("Display is 0")
    first = obs.add_element(node, selector='text "Display is 0"')
    assert "Display is 0" in obs.describe_element(first)

    node.name = "Display is 56,154"
    node.bounds = Rect(12, 10, 220, 40)
    again = obs.add_element(node)
    assert again is first and again.id == first.id  # same element, same id
    text = obs.describe_element(again)
    assert "Display is 56,154" in text and "Display is 0" not in text
    assert again.bounds == Rect(12, 10, 220, 40)
    assert not again.selector_ready  # the old selector named the old text


def test_refresh_keeps_observe_computed_occlusion_and_unreadable_fields(obs):
    node = LiveNode("OK")
    entry = obs.add_element(node)
    entry.states = frozenset({"occluded"})

    class Broken(LiveNode):
        @property
        def name(self):
            raise RuntimeError("element gone")

        @name.setter
        def name(self, v):
            pass

    node.__class__ = Broken
    obs.add_element(node)
    assert entry.name == "OK"  # an unreadable field keeps its last value
    assert "occluded" in entry.states


def test_unchanged_name_keeps_the_built_selector(obs):
    node = LiveNode("Equals")
    entry = obs.add_element(node, selector='button "Equals"')
    obs.add_element(node)
    assert entry.selector_ready and entry.selector == 'button "Equals"'
