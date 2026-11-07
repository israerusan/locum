"""Self-repairing selectors (locum.heal), end-to-end on the MockBackend."""

from __future__ import annotations

import logging

import pytest

from locum import heal as heal_mod
from locum._cancel import CallCancelled, CancelScope
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import ElementNotFound, WorkflowError
from locum.heal import Fingerprint, HealOptions
from locum.rpa import run_flow, validate_flow
from locum.selectors import parse
from locum.types import Rect


def _desk(be, **kw):
    kw.setdefault("default_timeout", 0)
    return Desktop(be, sleep=lambda s: None, **kw)


@pytest.fixture
def renamed():
    """'Save' was renamed to 'Save as'; unrelated buttons around it."""
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Editor", (100, 100, 600, 400), app="editor")
    save = w.add_button("Save as", (20, 160, 80, 30))
    w.add_button("Cancel", (120, 160, 80, 30))
    w.add_button("Open", (220, 160, 80, 30))
    w.add_text("Save your work often", (20, 300, 300, 24))
    return be, w, save


# ---------------------------------------------------------------- off by default

def test_heal_is_off_by_default_and_errors_are_unchanged(renamed, caplog):
    be, _w, save = renamed
    plain = _desk(be)
    assert plain.heal_selectors is False
    with pytest.raises(ElementNotFound) as e1:
        plain.find('button "Save"')
    # Exactly the pre-existing message: no self-healing note, same text as an explicit heal=False.
    with pytest.raises(ElementNotFound) as e2:
        _desk(be, heal_selectors=True).find('button "Save"', heal=False)
    assert str(e1.value) == str(e2.value)
    assert str(e1.value).startswith("No element matching 'button \"Save\"' within 0s")
    assert "self-healing" not in str(e1.value)
    with pytest.raises(ElementNotFound) as e3:
        plain.wait('button "Save"')
    assert "self-healing" not in str(e3.value)
    with pytest.raises(ElementNotFound):
        plain.find('button "Save"', root=plain.window("Editor"))
    assert not [r for r in caplog.records if r.name == "locum.heal"]
    assert save.clicks == 0


def test_matching_selector_is_never_marked_healed(renamed):
    be, _w, _save = renamed
    el = _desk(be, heal_selectors=True).find('button "Save as"')
    assert el.healed is None
    assert el.selector == 'button "Save as"'


def test_heal_selectors_must_be_bool_or_options():
    with pytest.raises(TypeError):
        Desktop(MockBackend(), heal_selectors="yes")


# ---------------------------------------------------------------- renamed

def test_renamed_button_heals_per_call(renamed, caplog):
    be, _w, save = renamed
    d = _desk(be)
    with caplog.at_level(logging.WARNING, logger="locum.heal"):
        el = d.find('window "Editor" button "Save"', heal=True)
    assert el.node.widget is save
    info = el.healed
    assert info is not None
    assert info.old_selector == 'window "Editor" button "Save"'
    assert "name 'Save' -> 'Save as'" in info.diff
    assert info.score >= heal_mod.DEFAULT_THRESHOLD
    # The new selector finds the element again and the element tracks it.
    assert parse(info.new_selector).find_first(d.roots()).widget is save
    assert el.selector == info.new_selector
    assert el.refresh().node.widget is save
    # Logged once at WARNING on locum.heal, with the diff and structured data.
    recs = [r for r in caplog.records if r.name == "locum.heal"]
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING
    assert "name 'Save' -> 'Save as'" in recs[0].getMessage()
    assert recs[0].locum_heal["new_selector"] == info.new_selector
    el.click()
    assert save.clicks == 1


def test_desktop_level_heal_and_click_by_selector(renamed):
    be, _w, save = renamed
    d = _desk(be, heal_selectors=True)
    d.click('button "Save"')
    assert save.clicks == 1


def test_exists_never_heals(renamed):
    be, w, _save = renamed
    d = _desk(be, heal_selectors=True)
    assert d.exists('button "Save"') is False
    assert d.find("window").exists('button "Save"') is False


def test_heal_respects_the_window_scope(renamed):
    be, _w, _save = renamed
    other = be.add_window("Other", (300, 300, 300, 200), app="other")
    other.add_button("Close", (10, 10, 80, 30))
    d = _desk(be)
    with pytest.raises(ElementNotFound, match="self-healing"):
        d.find('window "Other" button "Save"', heal=True)
    with pytest.raises(ElementNotFound, match="self-healing"):
        d.find('window "Nope" button "Save"', heal=True)


def test_unrelated_role_is_not_a_candidate(renamed):
    be, _w, _save = renamed
    d = _desk(be)
    # Only a text label mentions "work": a checkbox selector must not heal onto it.
    with pytest.raises(ElementNotFound, match="self-healing"):
        d.find('checkbox "Save your work"', heal=True)


def test_selector_without_name_or_id_never_heals(renamed):
    be, _w, _save = renamed
    with pytest.raises(ElementNotFound, match="no name or automation id"):
        _desk(be).find("checkbox", heal=True)


# ---------------------------------------------------------------- moved

def test_moved_button_heals_and_reports_distance():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Editor", (100, 100, 600, 400), app="editor")
    w.add("pane", "Toolbar", (0, 0, 600, 40))
    status = w.add("pane", "Status", (0, 360, 600, 40))
    save = status.add_button("Save", (50, 10, 80, 30))
    d = _desk(be)
    old = Fingerprint(role="button", name="Save", bounds=Rect(110, 110, 80, 30), window="Editor")
    el = d.find('window "Editor" > pane "Toolbar" > button "Save"', heal=True, fingerprint=old)
    assert el.node.widget is save
    assert "moved 40px" in el.healed.diff
    assert "name" not in el.healed.diff
    # Without a fingerprint the heal still works, and reports a structural change.
    el2 = d.find('window "Editor" > pane "Toolbar" > button "Save"', heal=True)
    assert el2.node.widget is save
    assert el2.healed.diff == "position in the tree changed"


# ---------------------------------------------------------------- automation id

def test_automation_id_kept_with_a_new_name():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Editor", (100, 100, 600, 400))
    w.add_button("Save all", (120, 160, 80, 30))
    target = w.add_button("Speichern", (20, 160, 80, 30), automation_id="saveBtn")
    d = _desk(be)
    fp = {"role": "button", "name": "Save", "automation_id": "saveBtn"}
    el = d.find('button "Save"', heal=True, fingerprint=fp)
    assert el.node.widget is target
    assert "name 'Save' -> 'Speichern'" in el.healed.diff
    assert "#saveBtn" in el.healed.new_selector


def test_selector_automation_id_heals_a_role_change():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Editor", (100, 100, 600, 400))
    target = w.add("togglebutton", "Bold", (20, 20, 30, 30), automation_id="boldBtn")
    w.add_button("Bold text", (60, 20, 30, 30))
    el = _desk(be).find("button#boldBtn", heal=True)
    assert el.node.widget is target
    assert "role 'button' -> 'togglebutton'" in el.healed.diff


# ---------------------------------------------------------------- ambiguity

@pytest.fixture
def twins():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Editor", (100, 100, 600, 400))
    a = w.add_button("Save as", (20, 160, 80, 30))
    b = w.add_button("Save all", (400, 300, 80, 30))
    return be, a, b


def test_ambiguous_candidates_fail(twins, caplog):
    be, a, b = twins
    d = _desk(be)
    with pytest.raises(ElementNotFound, match="ambiguous") as ei:
        d.find('button "Save"', heal=True)
    msg = str(ei.value)
    assert msg.startswith("No element matching 'button \"Save\"' within 0s")
    assert "Save as" in msg and "Save all" in msg
    assert not [r for r in caplog.records if r.name == "locum.heal"]
    assert a.clicks == b.clicks == 0


def test_fingerprint_position_breaks_the_tie(twins):
    be, _a, b = twins
    d = _desk(be)
    near_b = Fingerprint(role="button", name="Save", bounds=Rect(505, 395, 80, 30))
    el = d.find('button "Save"', heal=True, fingerprint=near_b)
    assert el.node.widget is b
    assert "moved 7px" in el.healed.diff


def test_threshold_and_margin_are_configurable(renamed):
    be, _w, _save = renamed
    strict = _desk(be, heal_selectors=HealOptions(threshold=0.99))
    with pytest.raises(ElementNotFound, match=r"scored .* < 0.99"):
        strict.find('button "Save"')
    with pytest.raises(ValueError):
        HealOptions(threshold=0)


# ---------------------------------------------------------------- scoped lookups / wait

def test_element_and_window_scoped_heal(renamed):
    be, _w, save = renamed
    d = _desk(be)
    win_el = d.find('window "Editor"')
    el = win_el.find('button "Save"', heal=True)
    assert el.node.widget is save and el.healed is not None
    assert el.refresh().node.widget is save  # the new selector is relative to the scope
    el2 = d.find('button "Save"', root=d.window("Editor"), heal=True)
    assert el2.node.widget is save and el2.healed is not None
    with pytest.raises(ElementNotFound, match="inside"):
        win_el.find('button "Save"')  # still off by default


def test_wait_heals_only_when_nothing_matched(renamed):
    be, _w, save = renamed
    d = _desk(be)
    el = d.wait('button "Save"', state="enabled", heal=True)
    assert el.node.widget is save and el.healed is not None
    assert d.wait('button "Save"', state="gone", heal=True) is None  # gone never heals
    save.enabled = False
    with pytest.raises(ElementNotFound, match="is not 'enabled'"):
        d.wait('button "Save"', state="enabled", heal=True)


def test_suppressed_overrides_every_switch(renamed):
    be, _w, _save = renamed
    d = _desk(be, heal_selectors=True)
    with heal_mod.suppressed():
        with pytest.raises(ElementNotFound) as ei:
            d.find('button "Save"', heal=True)
        assert "self-healing" not in str(ei.value)
    assert d.find('button "Save"').healed is not None


# ---------------------------------------------------------------- selector analysis / fingerprints

def test_uipath_and_dict_selectors_heal(renamed):
    be, _w, save = renamed
    d = _desk(be)
    el = d.find("<wnd app='editor.exe' title='Editor' /><ctrl name='Save' role='push button' />", heal=True)
    assert el.node.widget is save
    el = d.find({"role": "button", "name": "Save", "within": {"role": "window", "title": "Editor"}}, heal=True)
    assert el.node.widget is save


def test_fingerprint_round_trip(renamed):
    be, _w, save = renamed
    d = _desk(be)
    el = d.find('button "Save as"')
    fp = el.fingerprint()
    assert fp == Fingerprint.from_element(el)
    assert fp.role == "button" and fp.name == "Save as" and fp.window == "Editor"
    assert fp.bounds == Rect(120, 260, 80, 30)
    data = fp.to_dict()
    assert data["bounds"] == [120, 260, 80, 30]
    assert Fingerprint.from_dict(data) == fp
    with pytest.raises(ValueError, match="unknown fingerprint key"):
        Fingerprint.from_dict({"nmae": "x"})


def test_name_similarity():
    sim = heal_mod.name_similarity
    assert sim("Save", "save") == 1.0
    assert 0.7 <= sim("Save", "Save as") < 0.85  # an added word earns no bonus
    assert sim("Save as...", "Save as") >= 0.9
    assert sim("Save", "Cancel") < 0.5
    assert sim("", "x") == 0.0


def test_heal_scan_honours_cancellation(renamed):
    be, _w, _save = renamed
    roots = be.accessibility_roots()
    with CancelScope() as scope:
        scope.cancel("stop")
        with pytest.raises(CallCancelled):
            heal_mod.heal('button "Save"', roots)


# ---------------------------------------------------------------- flows

def test_flow_level_heal(renamed):
    be, _w, save = renamed
    d = _desk(be)
    report = run_flow({"settings": {"heal": True}, "steps": [{"click": 'button "Save"'}]}, d)
    assert report.ok, report.error
    assert save.clicks == 1
    healed = report.steps[0].data["healed"]
    assert "name 'Save' -> 'Save as'" in healed[0]["diff"]
    assert any(log["level"] == "warning" and "healed selector" in log["message"] for log in report.logs)


def test_flow_without_heal_still_fails(renamed):
    be, _w, save = renamed
    report = run_flow({"steps": [{"click": 'button "Save"'}]}, _desk(be))
    assert not report.ok and "ElementNotFound" in report.error
    assert "self-healing" not in report.error
    assert save.clicks == 0


def test_step_level_heal_and_override(renamed):
    be, _w, save = renamed
    d = _desk(be)
    assert run_flow({"steps": [{"click": 'button "Save"', "heal": True}]}, d).ok
    assert save.clicks == 1
    off = run_flow({"settings": {"heal": True}, "steps": [{"click": 'button "Save"', "heal": False}]}, d)
    assert not off.ok
    # heal: false in a flow also beats Desktop(heal_selectors=True).
    assert not run_flow({"steps": [{"click": 'button "Save"', "heal": "false"}]}, _desk(be, heal_selectors=True)).ok
    # A control step passes heal down to the steps inside it.
    nested = run_flow({"steps": [{"repeat": {"times": 1, "steps": [{"invoke": 'button "Save"'}]}, "heal": True}]}, d)
    assert nested.ok, nested.error
    assert save.clicks == 2


def test_flow_step_fingerprint_and_set_text():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Form", (100, 100, 600, 400))
    a = w.add_edit("Customer name", (20, 40, 200, 28))
    w.add_edit("Customer id", (20, 300, 200, 28))
    d = _desk(be)
    doc = {"steps": [{"set_text": {"target": 'edit "Customer"', "text": "ACME"}, "heal": True,
                      "fingerprint": {"role": "edit", "name": "Customer", "bounds": [120, 140, 200, 28]}}]}
    report = run_flow(doc, d)
    assert report.ok, report.error
    assert a.value == "ACME"
    # Without the fingerprint the two fields are too close to call.
    doc["steps"][0].pop("fingerprint")
    assert not run_flow(doc, d).ok


def test_flow_existence_checks_never_heal(renamed):
    be, _w, save = renamed
    d = _desk(be)
    report = run_flow({"settings": {"heal": True}, "steps": [
        {"if_exists": {"target": 'button "Save"', "then": [{"fail": "should not heal"}]}},
        {"assert_exists": 'button "Save"', "continue_on_error": True},
        {"wait_gone": 'button "Save"'},
    ]}, d)
    assert report.ok or report.steps[1].handled
    assert report.steps[0].data["result"]["exists"] is False
    assert report.steps[1].status == "failed"
    assert save.clicks == 0


def test_flow_validation_of_heal_keys():
    assert validate_flow({"settings": {"heal": True}, "steps": [{"click": "x", "heal": "${h}"}]}) == []
    problems = validate_flow({"settings": {"heal": "maybe"}, "steps": [
        {"click": "x", "heal": 3},
        {"click": "x", "fingerprint": {"nmae": "x"}},
        {"click": "x", "fingerprint": "button"},
        {"repeat": {"times": 1, "steps": []}, "fingerprint": {"name": "x"}},
    ]})
    text = "\n".join(problems)
    assert "settings.heal must be true or false" in text
    assert "steps[0]: 'heal' must be true or false" in text
    assert "unknown key 'nmae'" in text and "did you mean 'name'" in text
    assert "steps[2].fingerprint: must be a mapping" in text
    assert "steps[3]: 'fingerprint' belongs on the step" in text
    with pytest.raises(WorkflowError):
        run_flow({"settings": {"heal": "maybe"}, "steps": [{"click": "x"}]}, _desk(MockBackend()), raise_on_error=True)


def test_llm_tools_never_heal_even_on_a_healing_desktop():
    from locum import Desktop as _D
    from locum.backends.mock import MockBackend as _M
    from locum.llm.tools import DesktopToolkit as _TK

    be = _M()
    w = be.add_window("App", (0, 0, 400, 300))
    w.add_button("Save as", (10, 10, 80, 30))
    d = _D(be, failsafe=False, sleep=lambda s: None, default_timeout=0, heal_selectors=True)
    assert d.find('button "Save"', timeout=0).name == "Save as"  # the desktop itself heals
    r = _TK(d, find_timeout=0).call("click", {"selector": 'button "Save"'})
    assert r.is_error  # the tool layer does not act on a guessed element
    assert w.find("Save as").clicks == 0
