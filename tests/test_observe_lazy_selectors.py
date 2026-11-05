"""observe() must not build a selector for every element up front.

Seen through the MCP server on a real Windows desktop with ~3,900 accessible
elements: building and proving unique a selector for each of 300 observed
elements took ~5 s apiece (the whole observation effectively hung). Selectors are
now built on first use, and elements of one window share the proof that the
window's prefix is unique.
"""

from __future__ import annotations

import pytest

from locum import Desktop
from locum.cli import DemoBackend
import importlib

observe_mod = importlib.import_module("locum.llm.observe")  # locum.llm.observe is also a function name

from locum.llm.observe import LAZY_SELECTOR, ObservedElement, observe
from locum.llm.tools import DesktopToolkit
from locum.selectors import parse, selector_for
from locum.types import Rect


@pytest.fixture
def d():
    return Desktop(DemoBackend(), sleep=lambda s: None, default_timeout=0)


def test_observe_builds_no_selector_until_one_is_read(d, monkeypatch):
    built = []
    real = observe_mod._selector
    monkeypatch.setattr(observe_mod, "_selector", lambda *a, **k: built.append(a[0]) or real(*a, **k))
    obs = observe(d, screenshot=False)
    assert obs.elements and built == []
    obs.text()  # what the model reads: no selectors needed
    assert built == []
    first = obs.elements[0]
    assert not first.selector_ready
    sel = first.selector
    assert len(built) == 1 and first.selector_ready
    assert first.selector == sel and len(built) == 1  # cached


def test_lazy_selectors_find_their_element_and_match_eager_ones(d):
    obs = observe(d, screenshot=False)
    roots = d.roots()
    for el in obs.elements:
        found = parse(el.selector).find_first(roots)
        assert found is not None and found.same_as(el.node), el.selector
        assert el.selector == selector_for(el.node, roots=roots)  # the cache changes cost, not results


def test_prefix_cache_shares_the_window_proof(d, monkeypatch):
    from locum import selectors as sel_mod

    calls = []
    real = sel_mod._top_prefix
    monkeypatch.setattr(sel_mod, "_top_prefix", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    obs = observe(d, screenshot=False, window="Invoice Entry*")
    obs2 = observe(d, screenshot=False)
    same_window = [e for e in obs2.elements if e.window_title.startswith("Invoice Entry")]
    assert len(same_window) >= 3
    for e in same_window:
        e.selector
    assert len(calls) == 1  # one desktop-wide uniqueness proof for the whole window
    assert obs  # (window-scoped observation builds no cross-window proof at all)


def test_selectors_false_still_means_empty(d):
    obs = observe(d, screenshot=False, selectors=False)
    assert obs.elements and all(e.selector == "" for e in obs.elements)


def test_explicit_selector_is_kept_and_dataclass_api_unchanged():
    el = ObservedElement(id=1, role="button", name="OK", value=None, bounds=Rect(0, 0, 10, 10),
                         states=frozenset(), selector='button "OK"', node=None)
    assert el.selector == 'button "OK"' and el.selector_ready
    assert el.to_dict()["selector"] == 'button "OK"'
    lazy = ObservedElement(id=2, role="button", name="X", value=None, bounds=Rect(0, 0, 1, 1),
                           states=frozenset(), selector=LAZY_SELECTOR, node=None,
                           _selector_source=lambda: "computed")
    assert "computed" not in repr(lazy) and not lazy.selector_ready  # repr does not trigger it
    assert lazy.selector == "computed"


def test_find_elements_stops_building_selectors_past_its_budget(d, monkeypatch):
    tk = DesktopToolkit(d)
    monkeypatch.setattr(DesktopToolkit, "SELECTOR_BUDGET", 0.0)
    res = tk.call("find_elements", {"selector": "edit"})
    assert not res.is_error, res.text
    assert "selectors omitted" in res.text
    tk2 = DesktopToolkit(d)
    monkeypatch.setattr(DesktopToolkit, "SELECTOR_BUDGET", 60.0)
    res2 = tk2.call("find_elements", {"selector": "edit"})
    assert "selector:" in res2.text and "omitted" not in res2.text
