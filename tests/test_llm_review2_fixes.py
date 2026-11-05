"""Regression tests for the second review round of the LLM layer (computer use, agent, MCP server)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.llm.agent import Agent, ScriptedClient, computer_block, scripted_response, tool_use_block
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.mcp_server import MCPServer, _claim_stdout
from locum.llm.safety import Policy, split_typed_text
from locum.llm.tools import DesktopToolkit


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def bank():
    """A transfer form: 'Amount' (focused) then a 'Pay' button that records payments."""
    be = MockBackend(width=1280, height=800)
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    win = be.add_window("Bank transfer", (100, 100, 800, 600), app="bank")
    amount = win.add_edit("Amount", (40, 60, 300, 30))
    paid = []
    pay = win.add_button("Pay", (40, 500, 100, 40), on_click=lambda *a: paid.append("PAID"))
    be._set_focus(amount)
    asked = []

    def decline(desc, details):
        asked.append(desc)
        return False

    return dict(be=be, d=d, amount=amount, pay=pay, paid=paid, asked=asked, policy=Policy(confirm=decline))


# ------------------------------------------- keyboard focus moves then activates


@pytest.mark.parametrize("text", ["Tab Return", "Tab space", "Tab Tab Tab Return"])
def test_key_sequence_rechecks_focus_before_activating(bank, text):
    cu = ComputerUseAdapter(bank["d"], policy=bank["policy"])
    res = cu.execute("key", {"text": text})
    assert res.is_error and "declined" in res.text
    assert bank["paid"] == []
    assert len(bank["asked"]) == 1


def test_type_with_tab_and_newline_rechecks_focus(bank):
    cu = ComputerUseAdapter(bank["d"], policy=bank["policy"])
    res = cu.execute("type", {"text": "1000\t\n"})
    assert res.is_error and "declined" in res.text
    assert bank["amount"].value == "1000"
    assert bank["paid"] == []
    assert bank["asked"]


def test_plain_keyboard_input_is_unaffected_and_counted_once(bank):
    policy = Policy.permissive()
    policy.max_actions = 2
    cu = ComputerUseAdapter(bank["d"], policy=policy)
    assert not cu.execute("type", {"text": "12\t34"}).is_error
    assert not cu.execute("key", {"text": "shift+Tab BackSpace Tab Tab"}).is_error
    assert policy.action_count == 2  # re-checks inside an action are not budgeted


def test_combo_mixing_tab_and_enter_is_rejected(bank):
    cu = ComputerUseAdapter(bank["d"], policy=Policy.permissive())
    for action, inp in (("key", {"text": "tab+Return"}), ("hold_key", {"text": "Tab+Return", "duration": 0.1})):
        res = cu.execute(action, inp)
        assert res.is_error and "one after another" in res.text
    assert bank["paid"] == []
    assert not cu.execute("key", {"text": "ctrl+Return"}).is_error


def test_toolkit_type_text_and_press_keys_recheck_focus(bank):
    tk = DesktopToolkit(bank["d"], bank["policy"])
    res = tk.call("type_text", {"text": "1000\t\n", "selector": 'edit[name="Amount"]'})
    assert res.is_error and "declined" in res.text
    bank["be"]._set_focus(bank["amount"])
    res = tk.call("press_keys", {"keys": ["tab", "enter"]})
    assert res.is_error and "declined" in res.text
    bank["be"]._set_focus(bank["amount"])
    res = tk.call("type_text", {"text": "5\t", "press_enter": True})
    assert res.is_error and "declined" in res.text
    assert bank["paid"] == []
    assert len(bank["asked"]) == 3


def test_toolkit_clear_with_tab_types_the_rest(bank):
    tk = DesktopToolkit(bank["d"], Policy.permissive())
    bank["amount"].value = "old"
    res = tk.call("type_text", {"text": "1000\t", "selector": 'edit[name="Amount"]', "clear": True})
    assert not res.is_error, res.text
    assert bank["amount"].value.rstrip("\t") == "1000"  # the old content was replaced
    assert bank["be"].focused_element().name == "Pay"  # the Tab was pressed, not stored


def test_split_typed_text():
    assert split_typed_text("1000\t\n") == ["1000\t", "\n"]
    assert split_typed_text("a\r\nb\rc") == ["a\r\n", "b\r", "c"]
    assert split_typed_text("plain") == ["plain"]
    assert split_typed_text("") == []


# ------------------------------------------------ done next to failed actions


def test_done_after_blocked_action_does_not_finish(bank):
    c = ScriptedClient([
        scripted_response(computer_block("left_click", {"coordinate": [190, 620]}),
                          tool_use_block("done", {"summary": "Payment sent", "success": True})),
        scripted_response("The payment was blocked; I could not pay."),
    ])
    r = Agent(bank["d"], client=c, mode="computer_use", policy=Policy(), initial_observation=False).run("pay")
    first = r.steps[0].tool_results
    assert first[0].is_error and first[1].is_error and "Not finished" in first[1].text
    assert r.stop_reason == "end_turn" and "blocked" in r.final_text
    results = r.messages[2]["content"]
    assert results[1]["is_error"] is True


def test_done_after_skipped_action_in_tools_mode(bank):
    c = ScriptedClient([
        scripted_response(tool_use_block("click", {"selector": "button[name=Pay]"}),
                          tool_use_block("done", {"summary": "Payment sent", "success": True})),
        scripted_response(tool_use_block("done", {"summary": "Blocked", "success": False})),
    ])
    r = Agent(bank["d"], client=c, mode="tools", policy=Policy(), initial_observation=False).run("pay")
    assert r.success is False and r.final_text == "Blocked"


def test_done_failure_after_error_still_finishes(bank):
    c = ScriptedClient([
        scripted_response(computer_block("left_click", {"coordinate": [9999, 5]}),
                          tool_use_block("done", {"summary": "Could not", "success": False})),
    ])
    r = Agent(bank["d"], client=c, mode="computer_use", policy=Policy.permissive(), initial_observation=False).run("t")
    assert r.success is False and r.stop_reason == "done"


def test_calls_after_done_are_not_executed(bank):
    c = ScriptedClient([
        scripted_response(tool_use_block("done", {"summary": "ok"}),
                          computer_block("type", {"text": "1000"})),
    ])
    r = Agent(bank["d"], client=c, mode="computer_use", policy=Policy.permissive(), initial_observation=False).run("t")
    assert r.success is True and r.stop_reason == "done"
    assert bank["amount"].value == ""
    res = r.messages[2]["content"][1]
    assert res["is_error"] is True and res["toolset_name"] == "computer"


# ------------------------------------------------ mouse button left pressed


@pytest.mark.parametrize("ending", ["error", "max_steps", "end_turn"])
def test_run_releases_mouse_button(bank, ending):
    def boom(kwargs):
        raise RuntimeError("HTTP 529 overloaded")

    first = scripted_response(computer_block("left_mouse_down", {"coordinate": [200, 200]}))
    nxt = {"error": boom, "max_steps": None, "end_turn": scripted_response("stopped")}[ending]
    responses = [first] + ([nxt] if nxt is not None else [])
    a = Agent(bank["d"], client=ScriptedClient(responses), mode="computer_use", policy=Policy.permissive(),
              initial_observation=False, max_steps=1 if ending == "max_steps" else 5)
    a.run("select")
    assert not bank["be"].pressed_buttons
    assert any(e.get("event") == "input_released" for e in a.audit.events)


def test_adapter_release_all_only_after_down(bank):
    cu = ComputerUseAdapter(bank["d"], policy=Policy.permissive())
    cu.execute("left_mouse_down", {"coordinate": [200, 200]})
    cu.execute("left_mouse_up", {})
    assert cu.release_all() == []
    cu.execute("left_mouse_down", {"coordinate": [200, 200]})
    assert cu.release_all() == ["left"] and not bank["be"].pressed_buttons


# ------------------------------------------------ create_kwargs / setup errors


class _FakeAPI:
    def __init__(self):
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                fake.requests.append((self.path, dict(self.headers), body))
                data = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                   "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                                   "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_betas_go_through_the_beta_endpoint_with_real_sdk(bank):
    anthropic = pytest.importorskip("anthropic")
    fake = _FakeAPI()
    try:
        client = anthropic.Anthropic(api_key="x", base_url=fake.url, max_retries=0)
        r = Agent(bank["d"], client=client, mode="computer_use", initial_observation=False,
                  create_kwargs={"betas": ["some-beta-2026-01-01"]}).run("t")
    finally:
        fake.close()
    assert r.success is True and r.final_text == "ok"
    path, headers, body = fake.requests[0]
    assert path.startswith("/v1/messages")
    assert "some-beta-2026-01-01" in {k.lower(): v for k, v in headers.items()}["anthropic-beta"]
    assert body["tools"] == [{"type": "computer_toolset_20260801"}, *body["tools"][1:]]


def test_setup_errors_on_first_request_are_raised(bank):
    def bad(**kwargs):
        raise TypeError("Messages.create() got an unexpected keyword argument 'bogus'")

    client = ScriptedClient([])
    client.create = bad
    a = Agent(bank["d"], client=client, initial_observation=False)
    with pytest.raises(TypeError):
        a.run("t")


def test_later_request_failures_are_still_reported(bank):
    def boom(kwargs):
        raise TypeError("odd")

    c = ScriptedClient([scripted_response(computer_block("screenshot")), boom])
    r = Agent(bank["d"], client=c, mode="computer_use", initial_observation=False).run("t")
    assert r.success is False and r.stop_reason == "error"


# ------------------------------------------------ MCP stdio encoding


_MCP_CHILD = textwrap.dedent("""
    import sys
    from locum.backends.mock import MockBackend
    from locum.desktop import Desktop
    from locum.llm.mcp_server import MCPServer
    from locum.llm.safety import Policy
    be = MockBackend(width=1280, height=800)
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    win = be.add_window("Vendor master", (100, 100, 800, 600), app="saplogon")
    ed = win.add_edit("Vendor name", (40, 60, 300, 30))
    MCPServer(d, Policy.permissive()).serve()
    sys.stderr.write("VALUE=" + ascii(ed.value) + "\\n")
""")


def test_mcp_stdio_is_utf8_whatever_the_locale():
    value = "Müller GmbH – 1.000 €"
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "set_value", "arguments": {"selector": 'edit[name="Vendor name"]', "value": value}}}
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
    proc = subprocess.run([sys.executable, "-c", _MCP_CHILD], input=(json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"),
                          capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "VALUE=" + ascii(value) in proc.stderr.decode("ascii", "replace")
    resp = json.loads(proc.stdout.decode("utf-8"))
    assert resp["result"]["isError"] is False and value in resp["result"]["content"][0]["text"]


def test_claim_stdout_fallback_writes_utf8(monkeypatch):
    raw = io.BytesIO()
    cp1252 = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", cp1252)  # BytesIO has no fileno: Python-level fallback
    proto, restore = _claim_stdout()
    try:
        proto.write("€\n")
        proto.flush()
    finally:
        restore()
    assert sys.stdout is cp1252
    cp1252.flush()
    assert raw.getvalue() == "€\n".encode("utf-8")


def test_serve_accepts_explicit_streams_unchanged():
    out = io.StringIO()
    MCPServer(Desktop(MockBackend(), failsafe=False), Policy.permissive()).serve(
        io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), out)
    assert json.loads(out.getvalue()) == {"jsonrpc": "2.0", "id": 1, "result": {}}
