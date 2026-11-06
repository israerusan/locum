"""Regression tests for bug-hunt findings in locum.llm (safety + tools). Mock backend only."""
import enum
import json
import sys
import textwrap
import threading
import time
from typing import Literal

import pytest

from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import PolicyViolation
from locum.llm import AuditLog, DesktopToolkit
from locum.llm.safety import GuardedDesktop, Policy
from locum.llm.tools import ToolRegistry, schema_from_function


# ---------------------------------------------------------- guarded click with offset
def _erp():
    be = MockBackend(width=1280, height=800)
    win = be.add_window("ERP", (100, 100, 500, 400), app="erp")
    clicked = []
    grp = win.add("group", "Details", (10, 10, 480, 380))
    grp.add_button("Pay now", (400, 340, 80, 30), on_click=lambda *a: clicked.append("pay"))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    el = d.find('group "Details"')
    off = (540 - el.center.x, 455 - el.center.y)  # centre of "Pay now"
    return d, clicked, off


@pytest.mark.parametrize("policy_kw", [{}, {"allow": ['group "Details"'], "confirm_patterns": ()}])
@pytest.mark.parametrize("how", ["desktop", "element", "element_double"])
def test_offset_click_is_judged_at_the_real_click_point(policy_kw, how):
    d, clicked, off = _erp()
    asked = []
    g = GuardedDesktop(d, Policy(confirm=lambda desc, det: asked.append(desc) or False, **policy_kw))
    with pytest.raises(PolicyViolation):
        if how == "desktop":
            g.click('group "Details"', offset=off)
        elif how == "element":
            g.find('group "Details"').click(offset=off)
        else:
            g.find('group "Details"').double_click(offset=off)
    assert clicked == []
    assert asked and "Pay now" in asked[0]


def test_offset_click_on_harmless_spot_still_runs():
    d, clicked, _ = _erp()
    asked = []
    g = GuardedDesktop(d, Policy(confirm=lambda desc, det: asked.append(desc) or False))
    g.click('group "Details"', offset=(-100, -100))  # empty area of the group
    assert clicked == [] and asked == []


# ---------------------------------------------------------------- budget race
@pytest.mark.parametrize("kw", [{"max_actions": 1}, {"max_actions_per_minute": 1}])
def test_concurrent_checks_cannot_overrun_budget(kw):
    def slow_confirm(desc, details):
        time.sleep(0.2)
        return True

    p = Policy(confirm=slow_confirm, **kw)
    ok = []

    def run():
        try:
            p.check("click", {}, context={"target": "Pay now"})
            ok.append(1)
        except PolicyViolation:
            pass

    ts = [threading.Thread(target=run) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(ok) == 1 and p.action_count == 1


def test_declined_action_gives_its_budget_slot_back():
    answers = iter([False, True])
    p = Policy(confirm=lambda d, det: next(answers), max_actions=1, max_actions_per_minute=1)
    with pytest.raises(PolicyViolation):
        p.check("click", {}, context={"target": "Pay now"})
    assert p.action_count == 0
    p.check("click", {}, context={"target": "Pay now"})
    assert p.action_count == 1
    with pytest.raises(PolicyViolation, match="budget"):
        p.check("click", {}, context={"target": "Pay now"})


# ------------------------------------------------------ password after Tab
@pytest.fixture
def password_states(monkeypatch):
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))


def _login():
    be = MockBackend()
    w = be.add_window("Bank login", (100, 100, 500, 300), app="chrome")
    user = w.add("edit", "User", (20, 20, 200, 30))
    pwd = w.add("edit", "Password", (20, 60, 200, 30), attrs={"password": True})
    return be, Desktop(be, failsafe=False, sleep=lambda s: None), user, pwd


@pytest.mark.parametrize("args", [{}, {"selector": 'edit "User"'}])
def test_type_text_redacts_password_typed_after_tab(password_states, args):
    be, d, user, pwd = _login()
    be._set_focus(user)
    audit = AuditLog()
    kit = DesktopToolkit(d, Policy(), audit)
    r = kit.call("type_text", dict(args, text="alice\tS3cr3t-Pa55!"))
    assert not r.is_error
    assert pwd.value == "S3cr3t-Pa55!"
    assert "S3cr3t" not in r.text
    assert "S3cr3t" not in json.dumps(audit.events)


def test_type_text_without_password_is_not_redacted(password_states):
    be, d, user, pwd = _login()
    be._set_focus(user)
    audit = AuditLog()
    kit = DesktopToolkit(d, Policy(), audit)
    r = kit.call("type_text", {"text": "alice"})
    assert "alice" in r.text and "alice" in json.dumps(audit.events)


# ------------------------------------------------- unresolvable return annotation
def test_schema_keeps_param_types_when_return_annotation_unresolvable(tmp_path, monkeypatch):
    (tmp_path / "bh_mod_unresolved.py").write_text(textwrap.dedent('''
        from __future__ import annotations
        from typing import TYPE_CHECKING, List, Optional
        if TYPE_CHECKING:
            from decimal import Decimal

        def book(amount_cents: int, lines: List[str], urgent: bool = False, note: Optional[str] = None,
                 extra: Missing = None) -> Decimal:
            return f"{amount_cents!r} {lines!r} {urgent!r}"
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    from bh_mod_unresolved import book  # type: ignore

    _, schema = schema_from_function(book)
    props = schema["properties"]
    assert props["amount_cents"]["type"] == "integer"
    assert props["lines"] == {"type": "array", "items": {"type": "string"}}
    assert props["urgent"]["type"] == "boolean"
    assert props["note"]["type"] == "string"
    assert "type" not in props["extra"]  # still unresolvable: skipped, not fatal
    reg = ToolRegistry()
    reg.register(book)
    assert reg.call("book", {"amount_cents": "12.5", "lines": ["a"]}).is_error
    r = reg.call("book", {"amount_cents": "12", "lines": "single", "urgent": "false"})
    assert r.text == "12 ['single'] False"
    sys.modules.pop("bh_mod_unresolved", None)


# --------------------------------------------------------- Enum / Literal validation
class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


def test_enum_and_untyped_literal_params_are_validated():
    reg = ToolRegistry()
    got = {}

    @reg.register
    def paint(color: Color) -> str:
        got["c"] = color
        return "ok"

    @reg.register
    def level(n: Literal[1.5, 2.5]) -> str:
        return repr(n)

    @reg.register
    def flag(b: Literal[True]) -> str:
        return repr(b)

    assert reg.call("paint", {"color": "purple"}).is_error
    assert not reg.call("paint", {"color": "red"}).is_error
    assert got["c"] is Color.RED
    assert reg.call("level", {"n": 99}).is_error
    assert reg.call("level", {"n": 2.5}).text == "2.5"
    assert reg.call("flag", {"b": "nope"}).is_error


def test_find_elements_without_observe_gives_distinct_working_selectors():
    """find_elements before any observe: selectors must be unique across all windows."""
    from locum import Desktop
    from locum.backends.mock import MockBackend
    from locum.llm.tools import DesktopToolkit
    from locum.selectors import parse as _parse

    be = MockBackend(width=1280, height=800)
    for i, left in enumerate((0, 500), 1):
        be.add_window("Invoice", (left, 0, 400, 300), app="erp.exe", pid=i).add_button("Save", (10, 10, 80, 30))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    tk = DesktopToolkit(d)
    res = tk.call("find_elements", {"selector": 'button "Save"'})
    sels = [line.split("selector: ", 1)[1] for line in res.text.splitlines() if "selector: " in line]
    assert len(sels) == 2 and sels[0] != sels[1], res.text
    nodes = _parse('button "Save"').find_all(d.roots())
    for sel, node in zip(sels, nodes):
        assert _parse(sel).find_first(d.roots()).same_as(node), sel
