"""Regressions of the review-8 fixes found in review 9 (2026-11-06).

* Confirmation prompts ran while the desktop input lock was held (a human confirm
  blocked every other worker; a confirm UI needing the desktop deadlocked).
* A flow's exempt rule counted ``[name*="e"]``, ``^=``, ``~=``, ``%=`` and ``:has-text``
  as naming a control.
* CSV export prefixed ``'`` to plain negative/signed numbers.
"""

from __future__ import annotations

import csv
import threading

import pytest

from locum import TargetLost, _inputlock
from locum.backends.mock import MockBackend, MockNode
from locum.desktop import Desktop
from locum.llm.safety import GuardedDesktop, Policy
from locum.rpa.flow import flow_exempt_rules
from locum.rpa.queue import WorkQueue


class FocusBackend(MockBackend):
    def focused_element(self):
        return self._node_with_parents(self._focus)


def _shop():
    be = FocusBackend(width=1280, height=800)
    a = be.add_window("Shop", (0, 0, 600, 400), app="shop")
    card = a.add_edit("Card", (10, 10, 200, 30))
    pay = a.add_button("Pay now", (10, 60, 120, 30))
    cancel = a.add_button("Cancel", (10, 110, 120, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    d.window("Shop").activate()
    return be, d, card, pay, cancel


def _other_thread_can_take_the_lock(d) -> bool:
    got = []
    t = threading.Thread(target=lambda: got.append(_inputlock.try_desktop_input(d).__enter__()))
    t.start()
    t.join(5)
    return got == [True]


def _keys(be):
    return [e[1] for e in be.events if e[0] == "key_down"]


# ============================================================ 3: confirm without the input lock
def test_confirm_after_a_tab_runs_without_the_input_lock():
    be, d, card, pay, _ = _shop()
    seen = []

    def confirm(desc, details):
        seen.append((desc, _inputlock.holder(d), _other_thread_can_take_the_lock(d)))
        return True

    GuardedDesktop(d, Policy.default(), confirm=confirm).find('edit "Card"').type("4111\t\n", verify=False)
    assert seen and all(holder == "" and free for _, holder, free in seen), seen
    assert card.value == "4111" and pay.clicks == 1  # the input still went through
    assert not _inputlock._lock_for(d).held_here


def test_confirm_before_a_focusing_click_runs_without_the_input_lock(monkeypatch):
    be, d, _, pay, _ = _shop()

    def no_native_focus(self):
        raise NotImplementedError

    monkeypatch.setattr(MockNode, "focus", no_native_focus)  # forces the focusing click
    seen = []

    def confirm(desc, details):
        seen.append((desc, _inputlock.holder(d)))
        return True

    GuardedDesktop(d, Policy.default(), confirm=confirm).find('button "Pay now"').press("space")
    clicks = [s for s in seen if s[0].startswith("click")]
    assert clicks and all(h == "" for _, h in seen), seen
    assert pay.clicks >= 1


def test_a_lock_held_by_the_caller_is_released_for_confirm_and_restored():
    be, d, card, pay, _ = _shop()
    seen = []

    def confirm(desc, details):
        seen.append(_inputlock.holder(d))
        return True

    g = GuardedDesktop(d, Policy.default(), confirm=confirm)
    lock = _inputlock._lock_for(d)
    with _inputlock.desktop_input(d), _inputlock.desktop_input(d):
        g.find('edit "Card"').type("4111\t\n", verify=False)
        assert lock.held_here and lock.depth == 2
    assert seen == [""] and not lock.held_here and lock.owner is None


def test_focus_moved_during_confirm_stops_the_rest_of_the_input():
    be, d, card, pay, cancel = _shop()

    def confirm(desc, details):
        be._focus = cancel  # another worker moves the focus while the human decides
        return True

    with pytest.raises(TargetLost, match="focus moved"):
        GuardedDesktop(d, Policy.default(), confirm=confirm).find('edit "Card"').type("4111\t\n", verify=False)
    assert pay.clicks == 0 and cancel.clicks == 0
    assert "enter" not in _keys(be)
    assert not _inputlock._lock_for(d).held_here


def test_window_that_took_the_foreground_during_confirm_gets_nothing():
    be, d, card, pay, _ = _shop()
    b = be.add_window("Chat", (700, 0, 400, 400), app="chat")
    chat = b.add_edit("Message", (10, 10, 200, 30))

    def confirm(desc, details):
        be._raise(b)
        be._focus = chat
        return True

    with pytest.raises(TargetLost):
        GuardedDesktop(d, Policy.default(), confirm=confirm).find('edit "Card"').type("4111\t\n", verify=False)
    assert (chat.value or "") == "" and pay.clicks == 0
    assert "enter" not in _keys(be)


# ============================================================ 5: exempt rules need an exact name
@pytest.mark.parametrize("rule", ['button[name*="e"]', 'button[name%="*e*"]', 'button[name^="D"]',
                                  'button[name$="e"]', 'button[name~="e"]', 'button:has-text("e")',
                                  '[automation_id*="_"]', 'button "*"', 'button "S*"', 'button[name="Sa?e"]',
                                  'button[name="  "]', 'button[class_name="Btn"]', ".Btn",
                                  'button[name="Save"], button[name*="e"]'])
def test_flow_exempt_rejects_non_exact_target_names(rule):
    with pytest.raises(ValueError, match="too broad"):
        flow_exempt_rules({"exempt": [rule]})


@pytest.mark.parametrize("rule", ['button "Save"', '"Save"', "#saveBtn", 'button[name="Save" i]',
                                  'edit[text="Total"]', '[automation_id="btnSave"]',
                                  'button[name="Save"][name^="Sa"]', 'button "Save":has-text("Save")'])
def test_flow_exempt_accepts_exact_target_names(rule):
    assert len(flow_exempt_rules({"exempt": [rule]})) == 1


# ============================================================ 7: CSV keeps plain numbers
def test_export_csv_leaves_plain_numbers_alone(tmp_path):
    q = WorkQueue("x")
    q.add({"neg": "-12.50", "phone": "+15551234", "exp": "-1.5e-3", "frac": "-.5", "int": "-7",
           "f1": "-1+2", "f2": "+A1", "f3": "-12.5.1", "f4": "--1", "f5": "-", "f6": "-1,000"}, reference="r")
    out = q.export_csv(tmp_path / "out.csv")
    with open(out, encoding="utf-8", newline="") as fh:
        r = list(csv.DictReader(fh))[0]
    assert (r["neg"], r["phone"], r["exp"], r["frac"], r["int"]) == ("-12.50", "+15551234", "-1.5e-3", "-.5", "-7")
    assert [r[k] for k in ("f1", "f2", "f3", "f4", "f5", "f6")] == [
        "'-1+2", "'+A1", "'-12.5.1", "'--1", "'-", "'-1,000"]
