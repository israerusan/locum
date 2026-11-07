"""Review 8: MCP wait/cancel/argument validation, CDP navigation, WebSocket Host, agent history, legacy computer tool."""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from locum import _cancel
from locum import failsafe as fs
from locum.backends.mock import MockBackend
from locum.browser import _ws
from locum.browser.cdp import CDPError, Page, PageInfo
from locum.desktop import Desktop
from locum.llm.agent import Agent, ScriptedClient, computer_block, scripted_response, tool_use_block
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.mcp_server import MCPServer
from locum.llm.safety import Policy


# ----------------------------------------------------------------------------- MCP plumbing
class Pipe:
    def __init__(self) -> None:
        self.q: "queue.Queue[str]" = queue.Queue()

    def send(self, obj) -> None:
        self.q.put(json.dumps(obj) + "\n")

    def close(self) -> None:
        self.q.put("")

    def readline(self) -> str:
        return self.q.get()


class Out:
    def __init__(self) -> None:
        self.q: "queue.Queue[dict]" = queue.Queue()
        self._buf = ""
        self.stash: list = []

    def write(self, s: str) -> None:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self.q.put(json.loads(line))

    def flush(self) -> None:
        pass

    def next(self, timeout=5.0, pred=lambda m: True):
        for i, m in enumerate(self.stash):
            if pred(m):
                return self.stash.pop(i)
        end = time.monotonic() + timeout
        while True:
            m = self.q.get(timeout=max(0.01, end - time.monotonic()))
            if pred(m):
                return m
            self.stash.append(m)

    def responses(self):
        while not self.q.empty():
            self.stash.append(self.q.get())
        return [m for m in self.stash if "id" in m]


def call(i, name, args=None):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


def by_id(i):
    return lambda m: m.get("id") == i


@pytest.fixture
def server():
    be = MockBackend(width=800, height=600)
    be.add_window("Calc", (0, 0, 300, 300), app="calc")
    d = Desktop(be, failsafe=False, default_timeout=0)  # real time.sleep: the MCP `wait` path
    srv = MCPServer(d, Policy(), call_timeout=0.4, progress_interval=0.1)
    release = threading.Event()

    def stuck() -> str:
        """Non-cooperative call."""
        release.wait(10)
        return "late"

    srv.toolkit.registry.register(stuck, read_only=True)
    inp, out = Pipe(), Out()
    t = threading.Thread(target=srv.serve, args=(inp, out), daemon=True)
    t.start()
    yield srv, inp, out, release
    release.set()
    inp.close()
    t.join(5)


# ----------------------------------------------------------------------------- 1. wait honours MCP timeouts
def test_abortable_sleep_stops_when_the_call_is_cancelled():
    with _cancel.CancelScope(0.2):
        started = time.monotonic()
        with pytest.raises(_cancel.CallCancelled):
            fs.abortable_sleep(5.0)
        assert time.monotonic() - started < 1.5
    calls = []
    with _cancel.CancelScope(0.15):
        with pytest.raises(_cancel.CallCancelled):
            fs.abortable_sleep(5.0, lambda s: (calls.append(s), time.sleep(s)))
    assert sum(calls) < 1.0
    assert fs.abortable_sleep(0.05) is False  # outside a scope: a plain sleep


def test_mcp_wait_times_out_and_frees_the_worker(server):
    srv, inp, out, _ = server
    inp.send(call(1, "wait", {"seconds": 10}))
    r = out.next(5, by_id(1))
    assert r["result"]["isError"] and "Timed out" in r["result"]["content"][0]["text"]
    inp.send(call(2, "list_windows"))
    r2 = out.next(5, by_id(2))
    assert r2["result"]["isError"] is False and "Calc" in r2["result"]["content"][0]["text"]


# ----------------------------------------------------------------------------- 2. no response after cancel
def test_cancelled_running_call_gets_no_late_timeout(server):
    srv, inp, out, release = server
    inp.send(call(1, "stuck"))
    time.sleep(0.1)
    inp.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    time.sleep(0.4 + 1.0 + 0.5)  # past the deadline + the monitor's grace
    release.set()
    inp.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    out.next(5, by_id(2))
    time.sleep(0.2)
    assert [m["id"] for m in out.responses()] == []  # nothing for id 1, ever


# ----------------------------------------------------------------------------- 3. unknown arguments
def test_unknown_argument_to_a_builtin_tool_is_reported(server):
    srv, inp, out, _ = server
    inp.send(call(1, "wait", {"secs": 1}))
    r = out.next(5, by_id(1))
    text = r["result"]["content"][0]["text"]
    assert r["result"]["isError"] is True
    assert "Unexpected argument(s) for wait: secs" in text and "Valid arguments: seconds" in text
    assert "TypeError" not in text


# ----------------------------------------------------------------------------- 4./5. CDP navigate
class FakeSession:
    broken = False

    def __init__(self, nav_result, *, origins):
        self.nav_result = nav_result
        self.origins = list(origins)  # timeOrigin values reported after the navigation
        self.navigated = False
        self.log = []

    def call(self, method, params=None, timeout=30.0):
        self.log.append(method)
        if method == "Page.navigate":
            self.navigated = True
            return dict(self.nav_result)
        ex = (params or {}).get("expression", "")
        if ex == "performance.timeOrigin":
            if not self.navigated:
                return {"result": {"value": 1000.0}}
            value = self.origins.pop(0) if len(self.origins) > 1 else self.origins[0]
            return {"result": {"value": value}}
        if ex == "document.readyState":
            return {"result": {"value": "complete"}}
        return {"result": {"value": None}}


def _page(session):
    pg = Page.__new__(Page)
    pg.info = PageInfo("1", "Old", "https://old/", "ws://127.0.0.1:1/x")
    pg.session = session
    return pg


def test_failed_navigation_raises():
    pg = _page(FakeSession({"frameId": "F", "loaderId": "L", "errorText": "net::ERR_NAME_NOT_RESOLVED"}, origins=[1000.0]))
    with pytest.raises(CDPError, match="ERR_NAME_NOT_RESOLVED"):
        pg.navigate("https://no-such-host.invalid/")


def test_navigate_waits_for_the_new_document():
    # the old document keeps answering for a few polls (same timeOrigin), then the new one appears
    s = FakeSession({"frameId": "F", "loaderId": "L"}, origins=[1000.0, 1000.0, 1000.0, 2000.0])
    _page(s).navigate("https://new/", timeout=5)
    assert s.origins == [2000.0]  # polled until the document changed


def test_fragment_navigation_does_not_wait():
    s = FakeSession({"frameId": "F"}, origins=[1000.0])  # same-document: no loaderId
    started = time.monotonic()
    _page(s).navigate("https://old/#part", timeout=5)
    assert time.monotonic() - started < 1.0


def test_navigation_that_never_replaces_the_document_times_out():
    s = FakeSession({"frameId": "F", "loaderId": "L"}, origins=[1000.0])
    with pytest.raises(CDPError, match="did not start loading"):
        _page(s).navigate("https://slow/", timeout=0.3)


# ----------------------------------------------------------------------------- 6. IPv6 Host header
def test_websocket_host_header_brackets_ipv6(monkeypatch):
    sent = []

    class FakeSock:
        def sendall(self, data):
            sent.append(data)
            raise OSError("stop after the request")

        def close(self):
            pass

    monkeypatch.setattr(_ws.socket, "create_connection", lambda addr, timeout=None: FakeSock())
    with pytest.raises(OSError):
        _ws.WebSocket("ws://[::1]:9222/devtools/page/1")
    assert b"\r\nHost: [::1]:9222\r\n" in sent[0]


# ----------------------------------------------------------------------------- 7. screenshot pruning
@pytest.fixture
def desk():
    be = MockBackend(width=1280, height=800)
    be.add_window("App", (0, 0, 400, 300), app="app")
    return Desktop(be, failsafe=False, default_timeout=0, sleep=lambda s: None)


def _images(messages):
    n = 0
    for m in messages:
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            if b.get("type") == "image":
                n += 1
            elif b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                n += sum(1 for x in b["content"] if x.get("type") == "image")
    return n


def _shots(n):
    return [scripted_response(computer_block("screenshot", {}, id=f"s{i}")) for i in range(n)] + [scripted_response("done")]


def test_old_screenshots_are_pruned_and_pairs_stay_valid(desk):
    client = ScriptedClient(_shots(6))
    agent = Agent(desk, mode="computer_use", model="claude-opus-5", client=client)
    assert agent.run("look").success
    last = client.requests[-1]["messages"]
    assert _images(last) == 3
    uses = [b["id"] for m in last if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
    results = [b["tool_use_id"] for m in last if m["role"] == "user" for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert uses == results == [f"s{i}" for i in range(6)]
    assert any("earlier screenshot removed" in json.dumps(m["content"]) for m in last if m["role"] == "user")


def test_observe_images_are_pruned_in_tools_mode(desk):
    resp = [scripted_response(tool_use_block("observe", {}, id=f"o{i}")) for i in range(5)] + [scripted_response("ok")]
    client = ScriptedClient(resp)
    assert Agent(desk, model="claude-sonnet-4-6", client=client).run("look").success
    assert _images(client.requests[-1]["messages"]) <= 3


def test_no_pruning_for_models_with_history_bound_thinking(desk):
    client = ScriptedClient(_shots(5))
    Agent(desk, mode="computer_use", client=client).run("look")  # default claude-opus-5-5
    assert _images(client.requests[-1]["messages"]) == 6  # initial + 5: editing history would break thinking


# ----------------------------------------------------------------------------- 8. legacy computer tool
def test_tool_definition_matches_the_model(desk):
    cu = ComputerUseAdapter(desk)
    iw, ih = cu.image_size()
    assert cu.tool_definition() == {"type": "computer_toolset_20260801"}
    assert cu.tool_definition("claude-opus-4-8") == {"type": "computer_toolset_20260801"}
    assert cu.tool_definition("claude-sonnet-5-5")["type"] == "computer_toolset_20260801"
    assert cu.tool_definition("claude-opus-4-6") == {"type": "computer_20251124", "name": "computer",
                                                    "display_width_px": iw, "display_height_px": ih,
                                                    "enable_zoom": True}
    assert cu.tool_definition("claude-sonnet-4-5-20250929")["type"] == "computer_20250124"
    assert cu.tool_version("us.anthropic.claude-haiku-4-5-20251001-v1:0") == ("computer_20250124", "computer-use-2025-01-24")
    assert cu.tool_version("claude-opus-4-5@20251101") == ("computer_20251124", "computer-use-2025-11-24")
    with pytest.raises(ValueError, match="computer use"):
        cu.tool_version("claude-3-5-sonnet-20241022")


def test_agent_sends_legacy_tool_with_its_beta_header(desk):
    client = ScriptedClient([scripted_response("ok")])
    Agent(desk, mode="computer_use", model="claude-sonnet-4-6", client=client,
          create_kwargs={"betas": ["other-beta"]}).run("x")
    req = client.requests[0]
    assert req["tools"][0]["type"] == "computer_20251124" and req["tools"][0]["name"] == "computer"
    assert req["betas"] == ["other-beta", "computer-use-2025-11-24"]
    client = ScriptedClient([scripted_response("ok")])
    Agent(desk, mode="computer_use", client=client).run("x")
    assert "betas" not in client.requests[0]
    with pytest.raises(ValueError):
        Agent(desk, mode="computer_use", model="claude-3-opus-20240229", client=ScriptedClient([]))
