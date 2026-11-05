from __future__ import annotations

import re
import sys
from types import SimpleNamespace

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.errors import FailSafeTriggered
from pypilot.llm.agent import (
    Agent,
    AgentStep,
    ScriptedClient,
    computer_block,
    scripted_response,
    text_block,
    thinking_block,
    tool_use_block,
)
from pypilot.llm.computer_use import NOT_EXECUTED
from pypilot.llm.safety import Policy


def fake_time():
    """A sleep that advances a fake clock, so polling waits are instant and deterministic."""
    now = [0.0]

    def sleep(seconds):
        now[0] += max(0.0, float(seconds))

    return {"sleep": sleep, "clock": lambda: now[0]}


@pytest.fixture
def app():
    be = MockBackend(width=1920, height=1080)
    d = Desktop(be, failsafe=False, default_timeout=0.2, **fake_time())
    win = be.add_window("New Customer", (200, 150, 600, 400), app="crm")
    w = {
        "name": win.add_edit("Customer name", (20, 60, 300, 28)),
        "email": win.add_edit("Email", (20, 100, 300, 28)),
        "ok": win.add_button("OK", (20, 340, 90, 32)),
        "pay": win.add_button("Pay invoice", (130, 340, 120, 32)),
    }
    return be, d, w


def _last_tool_results(kwargs):
    """The tool_result blocks of the last user message in a request."""
    msg = kwargs["messages"][-1]
    assert msg["role"] == "user"
    return msg["content"]


def _text_of(content):
    out = []
    for b in content:
        if b.get("type") == "text":
            out.append(b["text"])
        elif b.get("type") == "tool_result":
            c = b["content"]
            out.extend(x["text"] for x in (c if isinstance(c, list) else [{"type": "text", "text": c}]) if x.get("type") == "text")
    return "\n".join(out)


def _id_for(kwargs, role, name):
    text = _text_of(_last_tool_results(kwargs))
    m = re.search(r"\[(\d+)\] " + role + ' "' + re.escape(name) + '"', text)
    assert m, text
    return int(m.group(1))


def test_tools_mode_fills_form_and_clicks_ok(app):
    be, d, w = app

    def fill(kwargs):
        name_id = _id_for(kwargs, "edit", "Customer name")
        email_id = _id_for(kwargs, "edit", "Email")
        # several tools in one turn -> all results must come back in one user message
        return scripted_response(
            thinking_block("planning"),
            "Filling in the form.",
            tool_use_block("type_text", {"text": "ACME Corp", "element_id": name_id}, id="t_name"),
            tool_use_block("type_text", {"text": "ap@acme.test", "element_id": email_id}, id="t_mail"),
            tool_use_block("observe", {}, id="t_obs"),
        )

    def click_ok(kwargs):
        results = _last_tool_results(kwargs)
        assert [r["tool_use_id"] for r in results] == ["t_name", "t_mail", "t_obs"]
        assert all(r["type"] == "tool_result" and not r.get("is_error") for r in results)
        assert any(c["type"] == "image" and c["source"]["media_type"] == "image/png" for c in results[2]["content"])
        return scripted_response(tool_use_block("click", {"element_id": _id_for(kwargs, "button", "OK")}, id="t_ok"))

    client = ScriptedClient([
        scripted_response(tool_use_block("observe", {}, id="t0")),
        fill,
        click_ok,
        scripted_response(tool_use_block("done", {"summary": "Customer ACME Corp created.", "success": True}, id="t_done")),
    ])
    steps = []
    agent = Agent(d, client=client, policy=Policy(), on_step=steps.append, max_steps=10)
    result = agent.run("Create the customer ACME Corp with email ap@acme.test and confirm.")

    assert result.success is True
    assert result.final_text == "Customer ACME Corp created."
    assert result.stop_reason == "done"
    assert w["name"].value == "ACME Corp"
    assert w["email"].value == "ap@acme.test"
    assert w["ok"].clicks == 1 and w["pay"].clicks == 0
    assert len(result.steps) == 4 and len(steps) == 4 and isinstance(steps[0], AgentStep)
    assert result.steps[1].text == "Filling in the form."
    assert result.usage == {"input_tokens": 40, "output_tokens": 20}

    # API contract of every request
    for req in client.requests:
        assert req["model"] == "claude-opus-5-5"
        assert req["max_tokens"] == 16000
        assert req["output_config"] == {"effort": "high"}
        assert isinstance(req["system"], str) and "observe" in req["system"]
        for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
            assert forbidden not in req
        assert req["messages"][-1]["role"] == "user"  # never an assistant prefill
        names = [t["name"] for t in req["tools"]]
        assert "done" in names and "observe" in names and "click" in names
    # initial message carries the task + the current observation (text and image)
    first = client.requests[0]["messages"][0]["content"]
    assert first[0] == {"type": "text", "text": "Create the customer ACME Corp with email ap@acme.test and confirm."}
    assert any(b["type"] == "image" for b in first)
    # the full assistant content (thinking included) is replayed unchanged
    second_turn = client.requests[2]["messages"][3]
    assert second_turn["role"] == "assistant"
    assert second_turn["content"][0] == {"type": "thinking", "thinking": "planning", "signature": "sig"}
    assert [m["role"] for m in client.requests[3]["messages"]] == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]


def test_failed_tool_is_error_and_agent_recovers(app):
    be, d, w = app

    def after_error(kwargs):
        res = _last_tool_results(kwargs)[0]
        assert res["is_error"] is True and "ElementNotFound" in _text_of([res])
        return scripted_response(tool_use_block("click", {"selector": 'button "OK"'}))

    client = ScriptedClient([
        scripted_response(tool_use_block("click", {"selector": 'button "Missing"'})),
        after_error,
        scripted_response("All done: clicked OK."),
    ])
    result = Agent(d, client=client, initial_observation=False).run("Click OK")
    assert result.success and result.final_text == "All done: clicked OK."
    assert result.stop_reason == "end_turn"
    assert w["ok"].clicks == 1


def test_policy_block_reaches_model_and_done_failure(app):
    be, d, w = app

    def react(kwargs):
        res = _last_tool_results(kwargs)[0]
        assert res["is_error"] and "Blocked by policy" in _text_of([res])
        return scripted_response(tool_use_block("done", {"summary": "Payment needs human approval.", "success": False}))

    client = ScriptedClient([scripted_response(tool_use_block("click", {"text": "Pay invoice"})), react])
    agent = Agent(d, client=client, initial_observation=False)
    result = agent.run("Pay the invoice")
    assert result.success is False and result.final_text == "Payment needs human approval."
    assert w["pay"].clicks == 0
    assert any(e.get("status") == "blocked" and e.get("tool") == "click" for e in agent.audit.events)
    assert agent.audit.events[-1]["event"] == "agent_end"


def test_confirm_callback_allows_sensitive_action(app):
    be, d, w = app
    asked = []
    policy = Policy(confirm=lambda desc, details: asked.append(desc) or True)
    client = ScriptedClient([
        scripted_response(tool_use_block("click", {"text": "Pay invoice"})),
        scripted_response("Paid."),
    ])
    result = Agent(d, client=client, policy=policy, initial_observation=False).run("Pay the invoice")
    assert result.success and w["pay"].clicks == 1 and len(asked) == 1


def test_dry_run_agent_never_touches_the_backend(app):
    be, d, w = app
    client = ScriptedClient([
        scripted_response(tool_use_block("type_text", {"text": "x", "selector": 'edit "Email"'}),
                          tool_use_block("click", {"selector": 'button "OK"'})),
        scripted_response("Would have filled the form."),
    ])
    be.clear_events()
    agent = Agent(d, client=client, policy=Policy(dry_run=True))
    result = agent.run("fill")
    assert result.success
    assert "DRY-RUN" in client.requests[0]["system"]
    results = client.requests[1]["messages"][-1]["content"]
    assert all("[dry-run] Would" in _text_of([r]) for r in results)
    assert [e for e in be.events if e[0] != "screenshot"] == []
    assert w["email"].value == "" and w["ok"].clicks == 0


def test_refusal_reports_stop_details(app):
    _, d, _ = app
    client = ScriptedClient([
        scripted_response("I can't help with that.", stop_reason="refusal",
                          stop_details={"type": "refusal", "category": "cyber", "explanation": "Declined by policy."}),
    ])
    result = Agent(d, client=client, initial_observation=False).run("do something")
    assert result.success is False and result.stop_reason == "refusal"
    assert "category: cyber" in result.final_text and "Declined by policy." in result.final_text


def test_max_tokens_continuation_and_truncated_tool_call(app):
    _, d, w = app

    def check_truncated(kwargs):
        res = _last_tool_results(kwargs)
        assert res[0]["is_error"] and "max_tokens" in res[0]["content"]
        return scripted_response("Partial answer, continued")

    def check_continue(kwargs):
        msg = kwargs["messages"][-1]
        assert msg["role"] == "user" and "output token limit" in msg["content"][0]["text"]
        return scripted_response(tool_use_block("click", {"selector": 'button "OK"'}), stop_reason="max_tokens")

    client = ScriptedClient([
        scripted_response("Partial answer", stop_reason="max_tokens"),
        check_continue,
        check_truncated,
    ])
    result = Agent(d, client=client, initial_observation=False).run("x")
    assert result.success and result.final_text == "Partial answer, continued"
    assert w["ok"].clicks == 0  # a truncated tool call is never executed

    endless = ScriptedClient([scripted_response("...", stop_reason="max_tokens")] * 5)
    r2 = Agent(d, client=endless, initial_observation=False, max_continuations=2).run("x")
    assert r2.success is False and r2.stop_reason == "max_tokens"
    assert len(endless.requests) == 3


def test_max_steps_and_request_errors(app):
    _, d, _ = app
    loop = ScriptedClient([scripted_response(tool_use_block("wait", {"seconds": 0}))] * 3)
    r = Agent(d, client=loop, max_steps=3, initial_observation=False).run("x")
    assert r.success is False and r.stop_reason == "max_steps" and len(r.steps) == 3

    class Broken:
        class messages:
            @staticmethod
            def create(**kw):
                raise ConnectionError("network down")

    r = Agent(d, client=Broken(), initial_observation=False).run("x")
    assert r.success is False and r.stop_reason == "error" and "network down" in r.final_text


def test_failsafe_aborts_run(app):
    _, d, _ = app

    def abort(kwargs):
        raise FailSafeTriggered("mouse in corner")

    r = Agent(d, client=ScriptedClient([abort]), initial_observation=False).run("x")
    assert r.success is False and r.stop_reason == "aborted" and "corner" in r.final_text


def test_sdk_style_objects_are_supported(app):
    _, d, w = app
    tool = SimpleNamespace(type="tool_use", id="toolu_x", name="click", input={"selector": 'button "OK"'})
    thinking = SimpleNamespace(type="thinking", thinking="", signature="abc")
    first = SimpleNamespace(content=[thinking, tool], stop_reason="tool_use", stop_details=None,
                            usage=SimpleNamespace(input_tokens=7, output_tokens=3, cache_read_input_tokens=2))
    second = SimpleNamespace(content=[SimpleNamespace(type="text", text="Clicked.")], stop_reason="end_turn",
                             usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    client = ScriptedClient([first, second])
    r = Agent(d, client=client, initial_observation=False).run("Click OK")
    assert r.success and r.final_text == "Clicked." and w["ok"].clicks == 1
    assert r.usage == {"input_tokens": 8, "output_tokens": 4, "cache_read_input_tokens": 2}
    assert client.requests[1]["messages"][1]["content"][0] is not None
    assert r.messages[1]["content"][0] is thinking  # appended unchanged


def test_computer_use_mode_batches(app):
    be, d, w = app
    agent = Agent(d, mode="computer_use", client=None, initial_observation=False)
    cu = agent.computer
    name_pt = list(cu.to_image(*w["name"].screen_rect.center))
    ok_pt = list(cu.to_image(*w["ok"].screen_rect.center))

    def check_first_batch(kwargs):
        results = _last_tool_results(kwargs)
        assert [r["tool_use_id"] for r in results] == ["c1", "c2", "c3", "c4"]
        assert all(r["toolset_name"] == "computer" for r in results)
        assert not results[0].get("is_error") and not results[1].get("is_error")
        assert results[2]["is_error"] is True and "outside the screenshot bounds" in results[2]["content"]
        assert results[3]["content"] == NOT_EXECUTED and results[3]["is_error"] is True
        return scripted_response(computer_block("left_click", {"coordinate": ok_pt}, id="c5"),
                                 computer_block("screenshot", {}, id="c6"))

    def check_second(kwargs):
        results = _last_tool_results(kwargs)
        assert results[0]["content"] == [{"type": "text", "text": "OK"}]
        assert results[1]["content"][0]["type"] == "image"
        return scripted_response("Form submitted.")

    agent._client = ScriptedClient([
        scripted_response(
            computer_block("left_click", {"coordinate": name_pt}, id="c1"),
            computer_block("type", {"text": "Zed"}, id="c2"),
            computer_block("left_click", {"coordinate": [99999, 1]}, id="c3"),
            computer_block("key", {"text": "Return"}, id="c4"),
        ),
        check_first_batch,
        check_second,
    ])
    result = agent.run("Enter Zed and press OK")
    assert result.success and result.final_text == "Form submitted."
    assert w["name"].value == "Zed" and w["ok"].clicks == 1
    req = agent.client.requests[0]
    assert req["tools"][0] == {"type": "computer_toolset_20260801"}
    assert [t.get("name") for t in req["tools"][1:]] == ["done"]
    assert "Screenshots are 1920x1080" in req["system"]


def test_computer_use_initial_screenshot_and_legacy(app):
    be, d, w = app
    ok_pt = list(Agent(d, mode="computer_use", client=ScriptedClient([])).computer.to_image(*w["ok"].screen_rect.center))
    client = ScriptedClient([
        scripted_response(tool_use_block("computer", {"action": "left_click", "coordinate": ok_pt}, id="legacy")),
        scripted_response("ok"),
    ])
    agent = Agent(d, mode="computer_use", client=client)
    r = agent.run("click ok")
    assert r.success and w["ok"].clicks == 1
    first = client.requests[0]["messages"][0]["content"]
    assert first[1]["type"] == "image"
    legacy_result = client.requests[1]["messages"][-1]["content"][0]
    assert legacy_result["tool_use_id"] == "legacy" and "toolset_name" not in legacy_result


def test_constructor_validation_and_missing_sdk(app, monkeypatch):
    _, d, _ = app
    with pytest.raises(ValueError):
        Agent(d, mode="vision", client=ScriptedClient([]))
    with pytest.raises(ValueError):
        Agent(d, client=ScriptedClient([]), create_kwargs={"temperature": 0.2})
    with pytest.raises(ValueError):
        Agent(d, client=ScriptedClient([]), max_steps=0)
    with pytest.raises(ValueError):
        Agent(d, client=ScriptedClient([])).run("   ")
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(ImportError, match="pypilot\\[llm\\]"):
        Agent(d).run("x")


def test_scripted_client_helpers():
    c = ScriptedClient([[text_block("hi")], lambda kw: scripted_response("echo " + kw["model"])])
    assert c.messages.create(model="m", messages=[])["stop_reason"] == "end_turn"
    assert c.messages.create(model="m2", messages=[])["content"][0]["text"] == "echo m2"
    assert c.remaining == 0
    with pytest.raises(RuntimeError):
        c.messages.create(model="m", messages=[])
    assert scripted_response(tool_use_block("x"))["stop_reason"] == "tool_use"
    assert computer_block("screenshot")["toolset_name"] == "computer"
