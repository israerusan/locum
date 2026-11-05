"""Regression tests: container clicks, unknown targets, extra-tool arguments, computer-use
default button, plural confirmation patterns, password redaction, computer calls in tools mode."""

import json

import pytest

from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.cli import DemoBackend
from locum.desktop import Desktop
from locum.errors import PolicyViolation
from locum.llm import AuditLog, DesktopToolkit, Policy
from locum.llm.agent import Agent, ScriptedClient, scripted_response, tool_use_block
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.tools import ToolSpec
from locum.rpa.recorder import Recorder

from test_llm_tools_review3_fixes import _transfer


def _asker(answer=False):
    asked = []
    return asked, Policy(confirm=lambda desc, det: asked.append(desc) or answer)


# ------------------------------------------------------------------ container click


def _banking():
    be = MockBackend()
    w = be.add_window("Online Banking", (100, 100, 500, 300), app="chrome")
    grp = w.add("group", "Actions", (20, 150, 300, 80))
    pay = grp.add("button", "Pay now", (110, 170, 120, 40))  # covers the group's centre
    return be, Desktop(be), pay


def test_click_on_container_is_judged_by_the_child_under_the_mouse():
    be, d, pay = _banking()
    asked, pol = _asker(False)
    kit = DesktopToolkit(d, pol)
    r = kit.call("find_elements", {"selector": 'group "Actions"'})
    eid = int(r.text.split("[")[1].split("]")[0])
    r = kit.call("click", {"element_id": eid})
    assert r.is_error and asked and "Pay now" in asked[0]
    assert pay.clicks == 0
    r = DesktopToolkit(d, Policy()).call("click", {"selector": 'group "Actions"'})
    assert r.is_error and "confirmation" in r.text and pay.clicks == 0
    # Typing into the container (which clicks it to focus) is judged the same way.
    r = DesktopToolkit(d, Policy()).call("type_text", {"selector": 'group "Actions"', "text": "x"})
    assert r.is_error and pay.clicks == 0


def test_scroll_over_container_does_not_ask():
    be, d, pay = _banking()
    asked, pol = _asker(False)
    r = DesktopToolkit(d, pol).call("scroll", {"selector": 'group "Actions"'})
    assert not r.is_error and not asked


def test_click_on_button_with_own_label_child_needs_no_extra_label():
    be = MockBackend()
    w = be.add_window("Form", (0, 0, 400, 300), app="erp")
    btn = w.add("button", "OK", (20, 20, 100, 40))
    btn.add("text", "OK", (30, 25, 80, 30))
    r = DesktopToolkit(Desktop(be), Policy()).call("click", {"selector": 'button "OK"'})
    assert not r.is_error and "lands on" not in r.text


# ------------------------------------------------------------------ unknown targets


def _blind_demo():
    class Blind(DemoBackend):
        def focused_element(self):
            return None

    be = Blind()
    for k, v in (("vendor", "ACME"), ("invoice_no", "INV-9"), ("amount", "250000")):
        be._fields[k].value = v
    be._on_save(None)  # one unpaid invoice
    return be, Desktop(be)


def test_activating_keys_with_unreadable_focus_need_confirmation():
    be, d = _blind_demo()
    kit = DesktopToolkit(d, Policy())
    r = kit.call("press_keys", {"keys": ["tab", "space"]})
    assert r.is_error and "cannot be identified" in r.text
    assert kit.call("press_keys", {"keys": ["enter"]}).is_error
    assert kit.call("type_text", {"text": "1\n"}).is_error
    assert be.payments == []
    # Non-activating input still works.
    assert not kit.call("press_keys", {"keys": ["tab"]}).is_error
    assert not kit.call("type_text", {"text": "INV-10"}).is_error
    asked, pol = _asker(True)
    assert not DesktopToolkit(d, pol).call("press_keys", {"keys": ["escape", "enter"]}).is_error or asked
    assert asked
    with pytest.raises(PolicyViolation, match="block"):
        Policy(unknown_target="block").check("press_keys", {}, context={"target_unknown": True})
    Policy(unknown_target="allow").check("press_keys", {}, context={"target_unknown": True})
    Policy.permissive().check("press_keys", {}, context={"target_unknown": True})
    with pytest.raises(ValueError):
        Policy(unknown_target="maybe")


def test_computer_use_unknown_focus_and_unknown_click_point():
    be, d = _blind_demo()
    cu = ComputerUseAdapter(d, policy=Policy())
    r = cu.execute("key", {"text": "Tab space"})
    assert r.is_error and "cannot be identified" in r.text and be.payments == []
    assert not cu.execute("type", {"text": "abc"}).is_error
    assert cu.execute("type", {"text": "a b"}).is_error

    mb = MockBackend(width=800, height=600)
    md = Desktop(mb, failsafe=False, sleep=lambda s: None)
    mb.add_window("Form", (100, 100, 300, 200), app="erp")
    cu = ComputerUseAdapter(md, policy=Policy(), monitor=None)
    r = cu.execute("left_click", {"coordinate": list(cu.to_image(700, 550))})  # bare desktop
    assert r.is_error and "cannot be identified" in r.text
    assert not cu.execute("left_click", {"coordinate": list(cu.to_image(150, 150))}).is_error
    assert not cu.execute("mouse_move", {"coordinate": list(cu.to_image(700, 550))}).is_error
    cu.policy = Policy(unknown_target="allow")
    assert not cu.execute("left_click", {"coordinate": list(cu.to_image(700, 550))}).is_error


# ------------------------------------------------------------------ extra tools


def test_extra_tool_arguments_and_confirm_flag_need_confirmation():
    calls = []

    def erp_api(operation: str, document: str) -> str:
        """Run an ERP API operation.

        Args:
            operation: read, pay, delete...
            document: Document number.
        """
        calls.append((operation, document))
        return "done"

    def make_payments(batch: str) -> str:
        """Release a batch.

        Args:
            batch: Batch id.
        """
        calls.append(("make_payments", batch))
        return "ok"

    def close_period(period: str) -> str:
        """Close a period.

        Args:
            period: Period.
        """
        calls.append(("close_period", period))
        return "ok"

    asked, pol = _asker(False)
    client = ScriptedClient([
        scripted_response(tool_use_block("erp_api", {"operation": "pay", "document": "51"}),
                          tool_use_block("erp_api", {"operation": "delete", "document": "52"}),
                          tool_use_block("make_payments", {"batch": "B-77"}),
                          tool_use_block("close_period", {"period": "2026-10"}),
                          tool_use_block("erp_api", {"operation": "read", "document": "53"})),
        scripted_response("done"),
    ])
    agent = Agent(Desktop(DemoBackend()), client=client, policy=pol, initial_observation=False,
                  extra_tools=[erp_api, make_payments, ToolSpec.from_function(close_period, confirm=True)])
    agent.run("x")
    assert len(asked) == 4
    assert calls == [("read", "53")]


# ------------------------------------------------------------------ computer-use default button


@pytest.mark.parametrize("action,inp", [("key", {"text": "Return"}), ("type", {"text": "250000\n"})])
def test_computer_use_enter_is_judged_by_default_button(action, inp):
    be, d, amount, pay = _transfer()
    asked, pol = _asker(False)
    r = ComputerUseAdapter(d, policy=pol).execute(action, inp)
    assert r.is_error and asked and "Pay now" in asked[0]
    assert not [e for e in be.events if e[0] == "key_down" and e[1] == "enter"]
    be, d, amount, pay = _transfer(default_button=False)
    asked, pol = _asker(False)
    assert not ComputerUseAdapter(d, policy=pol).execute(action, inp).is_error and not asked


# ------------------------------------------------------------------ plural confirmation patterns


@pytest.mark.parametrize("label", ["Execute payments", "Make Payments", "Transfers", "Release", "Purchases",
                                   "Payouts", "Refunds", "Deletes", "make payments", "Buchen", "Überweisen",
                                   "Zahlung freigeben"])
def test_plural_and_german_labels_need_confirmation(label):
    assert Policy().confirmation_reason("click", {}, {"target": label}) is not None


@pytest.mark.parametrize("label", ["Payee name", "Deleted items", "Display", "Repay"])
def test_unrelated_labels_do_not_need_confirmation(label):
    assert Policy().confirmation_reason("click", {}, {"target": label}) is None


# ------------------------------------------------------------------ passwords


@pytest.fixture
def password_states(monkeypatch):
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))


def _login():
    be = MockBackend()
    w = be.add_window("Bank login", (100, 100, 500, 300), app="chrome")
    w.add("edit", "User", (20, 20, 200, 30))
    pwd = w.add("edit", "Password", (20, 60, 200, 30), attrs={"password": True})
    return be, Desktop(be), pwd


def test_recorder_never_saves_passwords(password_states, tmp_path):
    be, d, pwd = _login()
    rec = Recorder(d)
    rec.start(source="backend")
    d.find('edit "Password"').set_text("S3cr3t-Pa55!")
    d.find('edit "User"').set_text("alice")
    rec.stop()
    flow = json.dumps(rec.to_flow())
    script = rec.to_python()
    assert "S3cr3t" not in flow and "S3cr3t" not in script and "S3cr3t" not in repr(rec.actions)
    assert "${env.LOCUM_PASSWORD}" in flow and "alice" in flow
    assert "import os" in script and "os.environ['LOCUM_PASSWORD']" in script
    compile(script, "rec.py", "exec")
    assert rec.actions[0].to_dict()["secret"] is True
    # Typed (key events) into the focused password field.
    rec = Recorder(d)
    rec.start(source="manual")
    be._set_focus(pwd)
    for ch in "hunter2":
        rec.feed(("type", ch))
    rec.stop()
    assert "hunter2" not in json.dumps(rec.to_flow()) and "LOCUM_PASSWORD" in rec.to_python()


def test_toolkit_and_computer_use_redact_passwords(password_states):
    be, d, pwd = _login()
    audit = AuditLog()
    kit = DesktopToolkit(d, Policy(), audit)
    r = kit.call("type_text", {"selector": 'edit "Password"', "text": "S3cr3t-Pa55!", "clear": True})
    assert not r.is_error and "S3cr3t" not in r.text
    r = kit.call("set_value", {"selector": 'edit "Password"', "value": "S3cr3t-Pa55!"})
    assert not r.is_error and "S3cr3t" not in r.text
    be._set_focus(pwd)
    assert not kit.call("type_text", {"text": "S3cr3t-Pa55!"}).is_error
    cu = ComputerUseAdapter(d, policy=Policy(), audit=audit)
    assert not cu.execute("type", {"text": "S3cr3t-Pa55!"}).is_error
    assert "S3cr3t" not in json.dumps(audit.events)
    assert pwd.value.endswith("S3cr3t-Pa55!")
    # Other fields are still logged verbatim.
    kit.call("type_text", {"selector": 'edit "User"', "text": "alice"})
    assert audit.events[-1]["args"]["text"] == "alice"


# ------------------------------------------------------------------ computer calls in tools mode


def test_tools_mode_does_not_run_computer_actions():
    be = DemoBackend()
    d = Desktop(be)
    client = ScriptedClient([
        scripted_response(tool_use_block("computer", {"action": "type", "text": "ACME"}),
                          tool_use_block("key", {"text": "ctrl+a"}, toolset_name="computer")),
        scripted_response("ok"),
    ])
    agent = Agent(d, client=client, mode="tools", initial_observation=False,
                  policy=Policy(deny_tools=["type_text", "press_keys", "hotkey", "set_value"]))
    res = agent.run("look only")
    results = res.steps[0].tool_results
    assert all(r.is_error and "Unknown tool" in r.text for r in results)
    assert not [e for e in be.events if e[0] in ("type", "key_down")]
    sent = agent.messages[2]["content"]
    assert sent[1]["toolset_name"] == "computer" and "toolset_name" not in sent[0]
