"""Polish pass (2026-11) on locum.llm: secrets in logs, prompt caching, needless observations."""

from __future__ import annotations

import json

import pytest

from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.llm.agent import Agent, ScriptedClient, scripted_response, tool_use_block
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.safety import AuditLog, Policy


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


# ------------------------------------------------ computer use: password after Tab
def test_computer_type_redacts_password_after_tab_in_confirmation_and_audit(password_states):
    """The description of the piece typed after a Tab into a password field (shown to the human,
    kept in Policy.confirmations, echoed in the declined-action error and the audit log) must
    not contain the password."""
    be, d, user, pwd = _login()
    be._set_focus(user)
    asked = []

    def decline(description, details):
        asked.append(description)
        return False

    policy = Policy(confirm_patterns=[r"password"], confirm=decline)
    audit = AuditLog()
    cu = ComputerUseAdapter(d, policy=policy, audit=audit)
    r = cu.execute("type", {"text": "alice\tS3cr3t-Pa55!\n"})
    assert r.is_error and asked  # the Enter in the password field needed (and was refused) confirmation
    assert "S3cr3t" not in r.text
    assert not any("S3cr3t" in a for a in asked)
    assert not any("S3cr3t" in str(c) for c in policy.confirmations)
    assert "S3cr3t" not in json.dumps(audit.events)


def test_computer_type_without_password_is_not_redacted(password_states):
    be, d, user, pwd = _login()
    be._set_focus(user)
    audit = AuditLog()
    cu = ComputerUseAdapter(d, policy=Policy(), audit=audit)
    assert not cu.execute("type", {"text": "alice"}).is_error
    assert "alice" in json.dumps(audit.events)


# ------------------------------------------------ agent extra tools: secret arguments
def _desktop():
    be = MockBackend(width=1280, height=800)
    return Desktop(be, failsafe=False, sleep=lambda s: None)


def test_extra_tool_secret_arguments_are_kept_out_of_the_audit_log():
    seen = {}

    def erp_login(username: str, password: str) -> str:
        """Log in to the ERP."""
        seen["password"] = password
        return "logged in"

    client = ScriptedClient([
        scripted_response(tool_use_block("erp_login", {"username": "alice", "password": "S3cr3t-Pa55!"})),
        scripted_response("done"),
    ])
    audit = AuditLog()
    agent = Agent(_desktop(), client=client, audit=audit, extra_tools=[erp_login], initial_observation=False,
                  policy=Policy(confirm_patterns=()))
    res = agent.run("log in")
    assert res.success and seen["password"] == "S3cr3t-Pa55!"  # the tool still gets the real value
    dumped = json.dumps(audit.events)
    assert "S3cr3t" not in dumped
    assert "alice" in dumped  # ordinary arguments are still logged


def test_extra_tool_secret_arguments_redacted_in_dry_run_description():
    def erp_login(username: str, api_key: str) -> str:
        """Log in to the ERP."""
        return "logged in"

    client = ScriptedClient([
        scripted_response(tool_use_block("erp_login", {"username": "alice", "api_key": "sk-live-123456"})),
        scripted_response("done"),
    ])
    audit = AuditLog()
    agent = Agent(_desktop(), client=client, audit=audit, extra_tools=[erp_login], initial_observation=False,
                  policy=Policy(dry_run=True, confirm_patterns=()))
    res = agent.run("log in")
    assert "sk-live" not in res.steps[0].tool_results[0].text
    assert "sk-live" not in json.dumps(audit.events)


# ------------------------------------------------ prompt caching
def test_requests_enable_prompt_caching_by_default():
    client = ScriptedClient([scripted_response("ok")])
    Agent(_desktop(), client=client, initial_observation=False).run("hi")
    assert client.requests[0]["cache_control"] == {"type": "ephemeral"}


def test_prompt_caching_can_be_turned_off_or_overridden():
    client = ScriptedClient([scripted_response("ok")])
    Agent(_desktop(), client=client, initial_observation=False, prompt_cache=False).run("hi")
    assert "cache_control" not in client.requests[0]
    client = ScriptedClient([scripted_response("ok")])
    Agent(_desktop(), client=client, initial_observation=False,
          create_kwargs={"cache_control": {"type": "ephemeral", "ttl": "1h"}}).run("hi")
    assert client.requests[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# ------------------------------------------------ no needless first observation
@pytest.mark.parametrize("mode", ["tools", "computer_use"])
def test_system_prompt_does_not_ask_for_an_observation_already_attached(mode):
    with_obs = Agent(_desktop(), client=ScriptedClient([]), mode=mode, initial_observation=True).system()
    without = Agent(_desktop(), client=ScriptedClient([]), mode=mode, initial_observation=False).system()
    assert "already" in with_obs.lower() and "first message" in with_obs.lower()
    assert "first message" not in without.lower()


# ------------------------------------------------ polish 2 (2026-11b): fresh review
def test_extra_tool_secret_arguments_never_reach_the_confirmation_callback():
    asked = []

    def confirm(description, details):
        asked.append((description, json.dumps(details, default=str)))
        return False

    def pay_vendor(vendor: str, pin: str) -> str:
        """Pay a vendor."""
        return "paid"

    client = ScriptedClient([
        scripted_response(tool_use_block("pay_vendor", {"vendor": "ACME", "pin": "8812"})),
        scripted_response("stopped"),
    ])
    audit = AuditLog()
    agent = Agent(_desktop(), client=client, audit=audit, extra_tools=[pay_vendor], initial_observation=False,
                  policy=Policy(confirm_patterns=[r"\bpay\b"], confirm=confirm))
    res = agent.run("pay ACME")
    assert asked, "paying must ask the human"
    assert not any("8812" in d or "8812" in desc for desc, d in asked)
    assert "ACME" in asked[0][0]
    assert "8812" not in json.dumps(audit.events)
    assert "8812" not in res.steps[0].tool_results[0].text


def test_underscored_secret_argument_is_not_in_the_confirmation_details():
    asked = []

    def erp_login(username: str, password: str) -> str:
        """Log in to the ERP and pay."""
        return "ok"

    client = ScriptedClient([
        scripted_response(tool_use_block("erp_login", {"username": "pay_clerk", "password": "Sup3r_Secret_99"})),
        scripted_response("stopped"),
    ])
    agent = Agent(_desktop(), client=client, extra_tools=[erp_login], initial_observation=False,
                  policy=Policy(confirm_patterns=[r"\bpay\b"], confirm=lambda d, det: asked.append(
                      json.dumps(det, default=str)) or False))
    res = agent.run("log in")
    assert asked, res.steps[0].tool_results[0].text  # "pay clerk" (underscores read as spaces) still triggers the confirmation
    assert not any("Secret" in a for a in asked)


def test_deny_pattern_error_does_not_echo_the_card_number():
    from locum.errors import PolicyViolation
    from locum.llm.safety import CARD_NUMBER

    policy = Policy(deny_text_patterns=[CARD_NUMBER, r"rm\s+-rf"])
    with pytest.raises(PolicyViolation) as ei:
        policy.check("type_text", {"text": "card 4111 1111 1111 1111 exp 12/29"})
    msg = str(ei.value)
    assert "4111" not in msg and "1111" not in msg
    assert "card" in msg and "exp 12/29" in msg  # the context around the match is still shown
    with pytest.raises(PolicyViolation) as ei:
        policy.check("type_text", {"text": "sudo rm -rf /"})
    assert "rm -rf" not in str(ei.value) and "sudo" in str(ei.value)


def test_empty_pause_turn_is_not_sent_back_as_an_empty_assistant_message():
    client = ScriptedClient([
        scripted_response(stop_reason="pause_turn"),
        scripted_response("all done"),
    ])
    res = Agent(_desktop(), client=client, initial_observation=False).run("hi")
    assert res.success and res.final_text == "all done"
    second = client.requests[1]["messages"]
    assert all(m["content"] for m in second)
    assert second[-1]["role"] == "user"  # not an (empty) assistant prefill


def test_tool_use_stop_without_tool_blocks_does_not_send_an_empty_user_message():
    client = ScriptedClient([scripted_response("thinking about it", stop_reason="tool_use")])
    res = Agent(_desktop(), client=client, initial_observation=False).run("hi")
    assert not res.success and res.stop_reason == "tool_use"
    assert client.remaining == 0 and len(client.requests) == 1


def test_on_step_exception_still_records_agent_end():
    def boom(step):
        raise RuntimeError("progress UI crashed")

    client = ScriptedClient([scripted_response(tool_use_block("observe", {})), scripted_response("ok")])
    audit = AuditLog()
    agent = Agent(_desktop(), client=client, audit=audit, initial_observation=False, on_step=boom)
    with pytest.raises(RuntimeError, match="progress UI"):
        agent.run("look")
    ends = [e for e in audit.events if e["event"] == "agent_end"]
    assert len(ends) == 1 and ends[0]["success"] is False and ends[0]["stop_reason"] == "error"


def test_setup_error_records_agent_end_once():
    class Bad:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise TypeError("unexpected keyword 'foo'")

    audit = AuditLog()
    with pytest.raises(TypeError):
        Agent(_desktop(), client=Bad(), audit=audit, initial_observation=False).run("hi")
    assert [e["event"] for e in audit.events].count("agent_end") == 1


def test_old_observation_screenshots_are_released():
    seen = []
    client = ScriptedClient([
        scripted_response(tool_use_block("observe", {})),
        scripted_response(tool_use_block("observe", {})),
        scripted_response(tool_use_block("observe", {})),
        scripted_response("done looking"),
    ])

    def on_step(step):
        if step.tool_results:
            seen.append(step.tool_results[0].data.screenshot is not None)  # the callback still sees the pixels

    agent = Agent(_desktop(), client=client, initial_observation=False, on_step=on_step)
    res = agent.run("look")
    assert res.success and seen == [True, True, True]
    obs = [s.tool_results[0].data for s in res.steps[:3]]
    assert obs[0].screenshot is None and obs[0].annotated is None and obs[1].screenshot is None
    assert obs[2] is agent.toolkit.last_observation and obs[2].screenshot is not None
    assert obs[0].text()  # elements/text survive
