from __future__ import annotations

import base64
import io
import json

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.llm.mcp_server import LATEST_PROTOCOL_VERSION, MCPServer, serve
from pypilot.llm.safety import Policy


@pytest.fixture
def server():
    be = MockBackend(width=1280, height=800)
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    win = be.add_window("Login", (100, 100, 400, 300), app="portal")
    w = {"user": win.add_edit("Username", (20, 40, 200, 28)), "go": win.add_button("Go", (20, 200, 80, 30))}
    return MCPServer(d, Policy()), be, w


def req(method, params=None, id=1):
    m = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        m["params"] = params
    return m


def test_initialize_and_notifications(server):
    srv, _, _ = server
    r = srv.handle_message(req("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}))
    assert r["jsonrpc"] == "2.0" and r["id"] == 1
    res = r["result"]
    assert res["protocolVersion"] == "2025-03-26"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"]["name"] == "pypilot" and res["serverInfo"]["version"]
    assert srv.client_info == {"name": "t", "version": "1"}
    unknown_version = srv.handle_message(req("initialize", {"protocolVersion": "1999-01-01"}))
    assert unknown_version["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION == "2025-06-18"
    assert srv.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert srv.initialized
    assert srv.handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}) is None
    assert srv.handle_message(req("ping", id="p"))["result"] == {}


def test_errors(server):
    srv, _, _ = server
    r = srv.handle_message(req("resources/list", id=7))
    assert r["id"] == 7 and r["error"]["code"] == -32601
    assert srv.handle_message("nope")["error"]["code"] == -32600
    assert srv.handle_message({"jsonrpc": "2.0", "id": 2})["error"]["code"] == -32600
    assert srv.handle_message({"jsonrpc": "2.0", "id": 3, "result": {}}) is None  # a stray response
    assert srv.handle_message(req("tools/call", {"name": "teleport"}))["error"]["code"] == -32602
    assert srv.handle_message(req("tools/call", {}))["error"]["code"] == -32602
    assert srv.handle_message(req("tools/call", {"name": "click", "arguments": [1]}))["error"]["code"] == -32602
    assert srv.handle_message(req("tools/list", params=[1]))["error"]["code"] == -32602
    assert srv.handle_line("{not json")["error"]["code"] == -32700
    assert srv.handle_line("   ") is None


def test_tools_list(server):
    srv, _, _ = server
    tools = srv.handle_message(req("tools/list"))["result"]["tools"]
    names = [t["name"] for t in tools]
    assert {"observe", "click", "type_text", "screenshot", "set_value"} <= set(names)
    click = next(t for t in tools if t["name"] == "click")
    assert click["inputSchema"]["type"] == "object"
    assert "element_id" in click["inputSchema"]["properties"]
    assert all(isinstance(t["description"], str) and t["description"] for t in tools)


def test_tools_call_with_image_content_and_actions(server):
    srv, be, w = server
    r = srv.handle_message(req("tools/call", {"name": "observe", "arguments": {}}, id=10))["result"]
    assert r["isError"] is False
    kinds = [c["type"] for c in r["content"]]
    assert kinds == ["text", "image"]
    img = r["content"][1]
    assert img["mimeType"] == "image/png"
    assert base64.b64decode(img["data"])[:4] == b"\x89PNG"
    assert '[1] edit "Username"' in r["content"][0]["text"]

    r = srv.handle_message(req("tools/call", {"name": "type_text", "arguments": {"element_id": 1, "text": "alice"}}))["result"]
    assert r["isError"] is False and w["user"].value == "alice"
    r = srv.handle_message(req("tools/call", {"name": "click", "arguments": {"element_id": 2}}))["result"]
    assert r["isError"] is False and w["go"].clicks == 1

    r = srv.handle_message(req("tools/call", {"name": "click", "arguments": {"element_id": 42}}))["result"]
    assert r["isError"] is True and "No element with id 42" in r["content"][0]["text"]
    r = srv.handle_message(req("tools/call", {"name": "screenshot"}))["result"]
    assert [c["type"] for c in r["content"]] == ["text", "image"]


def test_policy_applies_to_mcp_calls(server):
    _, be, w = server
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    srv = MCPServer(d, Policy(dry_run=True, deny_tools=["launch_app"]))
    srv.handle_message(req("tools/call", {"name": "observe", "arguments": {"include_screenshot": False}}))
    r = srv.handle_message(req("tools/call", {"name": "click", "arguments": {"element_id": 2}}))["result"]
    assert r["isError"] is False and r["content"][0]["text"].startswith("[dry-run]")
    assert w["go"].clicks == 0
    r = srv.handle_message(req("tools/call", {"name": "launch_app", "arguments": {"command": "x"}}))["result"]
    assert r["isError"] is True and "not allowed" in r["content"][0]["text"]


def test_serve_over_streams(server):
    srv, _, _ = server
    lines = [
        json.dumps(req("initialize", {"protocolVersion": "2025-06-18"}, id=1)),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
        json.dumps(req("tools/list", id=2)),
        json.dumps([req("ping", id=3), {"jsonrpc": "2.0", "method": "notifications/initialized"}, req("bogus", id=4)]),
        json.dumps(req("tools/call", {"name": "read_clipboard", "arguments": {}}, id=5)),
    ]
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()
    srv.serve(stdin, stdout)
    out = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert len(out) == 4  # notifications and blank lines produce nothing
    assert out[0]["id"] == 1 and out[0]["result"]["protocolVersion"] == "2025-06-18"
    assert out[1]["id"] == 2 and out[1]["result"]["tools"]
    assert isinstance(out[2], list) and [m["id"] for m in out[2]] == [3, 4]
    assert out[2][1]["error"]["code"] == -32601
    assert out[3]["result"]["content"][0]["text"] == "(the clipboard is empty)"


def test_module_level_serve(server):
    _, be, _ = server
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    stdout = io.StringIO()
    serve(d, Policy.strict(), stdin=io.StringIO(json.dumps(req("ping", id=9)) + "\n"), stdout=stdout)
    assert json.loads(stdout.getvalue()) == {"jsonrpc": "2.0", "id": 9, "result": {}}
