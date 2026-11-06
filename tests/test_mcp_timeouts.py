"""MCP server: per-call time limits, cancellation and progress (live-session report 2026-11-05).

A full-desktop ``screenshot`` ran for minutes and ``list_windows`` queued behind it;
a call that runs too long must come back as a clear timeout error while the
server keeps serving.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

import pytest

from locum import _cancel
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.llm.mcp_server import MCPServer
from locum.llm.safety import Policy


class Pipe:
    """Blocking line-oriented stdin for serve()."""

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

    def write(self, s: str) -> None:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self.q.put(json.loads(line))

    def flush(self) -> None:
        pass

    def next(self, timeout: float = 5.0, pred=lambda m: True):
        """First message matching ``pred``; others are kept for later calls."""
        if not hasattr(self, "stash"):
            self.stash = []
        for i, m in enumerate(self.stash):
            if pred(m):
                return self.stash.pop(i)
        end = time.monotonic() + timeout
        while True:
            m = self.q.get(timeout=max(0.01, end - time.monotonic()))
            if pred(m):
                return m
            self.stash.append(m)

    def idle(self) -> bool:
        return self.q.empty() and not [m for m in getattr(self, "stash", []) if "id" in m]


@pytest.fixture
def running():
    be = MockBackend(width=800, height=600)
    be.add_window("Calc", (0, 0, 300, 300), app="calc").add_button("Equals", (10, 10, 50, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    srv = MCPServer(d, Policy(), call_timeout=0.4, progress_interval=0.1)
    release = threading.Event()

    def spin() -> str:
        """Cooperative long call (like a huge tree walk)."""
        while True:
            _cancel.check()
            time.sleep(0.01)

    def stuck() -> str:
        """Non-cooperative call (stuck inside one native call)."""
        release.wait(10)
        return "late"

    reg = srv.toolkit.registry
    reg.register(spin, read_only=True)
    reg.register(stuck, read_only=True)
    srv.toolkit.read_only_tools = set(getattr(srv.toolkit, "read_only_tools", set())) | {"spin", "stuck"}
    inp, out = Pipe(), Out()
    t = threading.Thread(target=srv.serve, args=(inp, out), daemon=True)
    t.start()
    yield srv, inp, out, release
    release.set()
    inp.close()
    t.join(5)


def call(id, name, args=None, meta=None):
    params = {"name": name, "arguments": args or {}}
    if meta:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": params}


def by_id(i):
    return lambda m: m.get("id") == i


def test_long_call_times_out_and_server_keeps_serving(running):
    srv, inp, out, _ = running
    inp.send(call(1, "spin"))
    inp.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert out.next(1, by_id(2))["result"] == {}  # answered while the call runs
    r = out.next(5, by_id(1))
    assert r["result"]["isError"] is True
    assert "Timed out" in r["result"]["content"][0]["text"] and "still serving" in r["result"]["content"][0]["text"]
    inp.send(call(3, "list_windows"))
    r3 = out.next(5, by_id(3))
    assert r3["result"]["isError"] is False and "Calc" in r3["result"]["content"][0]["text"]


def test_stuck_native_call_is_answered_by_the_watchdog(running):
    srv, inp, out, release = running
    inp.send(call(1, "stuck"))
    inp.send(call(2, "list_windows"))  # queued behind the stuck call
    r = out.next(5, by_id(1))
    assert r["result"]["isError"] and "Timed out" in r["result"]["content"][0]["text"]
    r2 = out.next(5, by_id(2))
    assert r2["result"]["isError"] and "never started" in r2["result"]["content"][0]["text"]
    release.set()  # the native call returns: its late result is dropped, the next call runs
    inp.send(call(3, "list_windows"))
    assert out.next(5, by_id(3))["result"]["isError"] is False
    time.sleep(0.2)
    assert out.idle()  # no late duplicate for id 1


def test_cancelled_call_stops_without_a_response(running):
    srv, inp, out, _ = running
    srv.call_timeout = 30.0
    inp.send(call(1, "spin"))
    time.sleep(0.1)
    inp.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1, "reason": "user"}})
    inp.send(call(2, "list_windows"))
    r = out.next(5, lambda m: "id" in m)
    assert r["id"] == 2 and r["result"]["isError"] is False  # 1 never answers, 2 runs right after


def test_progress_notifications_for_a_progress_token(running):
    srv, inp, out, _ = running
    inp.send(call(1, "spin", meta={"progressToken": "tok"}))
    p = out.next(3, lambda m: m.get("method") == "notifications/progress")
    assert p["params"]["progressToken"] == "tok" and p["params"]["total"] == pytest.approx(0.4)
    out.next(5, by_id(1))


def test_tool_timeout_argument_extends_the_limit():
    srv = MCPServer(Desktop(MockBackend(), failsafe=False), call_timeout=60)
    assert srv._timeout_for({"timeout": 120}) == 120 + MCPServer.TIMEOUT_MARGIN
    assert srv._timeout_for({"timeout": 5}) == 60
    assert MCPServer(Desktop(MockBackend(), failsafe=False), call_timeout=None)._timeout_for({"timeout": 5}) is None
    with pytest.raises(ValueError):
        MCPServer(call_timeout=0)


def test_cancel_scope_outside_a_call_is_a_no_op():
    _cancel.check()
    with _cancel.CancelScope(0.0) as s:
        with pytest.raises(_cancel.CallCancelled):
            _cancel.check()
    _cancel.check()
    assert s.expired()


@pytest.mark.parametrize("argv", [[sys.executable, "-m", "locum.mcp", "--help"],
                                  [sys.executable, "-m", "locum", "mcp", "--help"]])
def test_both_module_forms_start_the_server_cli(argv):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "--call-timeout" in r.stdout and "claude mcp add locum" in r.stdout


def test_python_m_locum_mcp_serves_initialize():
    p = subprocess.run([sys.executable, "-m", "locum.mcp", "--backend", "mock"],
                       input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n",
                       capture_output=True, text=True, timeout=60)
    msg = json.loads(p.stdout.strip().splitlines()[0])
    assert msg["result"]["serverInfo"]["name"] == "locum"
