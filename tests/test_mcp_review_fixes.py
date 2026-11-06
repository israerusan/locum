"""MCP server review fixes: capped ``timeout`` extension; no progress after the final answer."""

from __future__ import annotations

import math
import threading
import time

import pytest

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.llm.mcp_server import MCPServer, _Job
from locum.llm.safety import Policy

from test_mcp_timeouts import Out, Pipe, call


def _server(**kw) -> MCPServer:
    return MCPServer(Desktop(MockBackend(), failsafe=False), **kw)


# ------------------------------------------------------- timeout extension cap
def test_huge_timeout_argument_does_not_disable_the_server_deadline():
    srv = _server(call_timeout=60)
    cap = MCPServer.MAX_TOOL_TIMEOUT + MCPServer.TIMEOUT_MARGIN
    assert srv._timeout_for({"timeout": 1e9}) == cap
    assert srv._timeout_for({"timeout": 10**12}) == cap
    assert srv._timeout_for({"timeout": 120}) == 120 + MCPServer.TIMEOUT_MARGIN


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan, -5, -1e9, True, "1e9", None, [100]])
def test_non_finite_negative_or_non_numeric_timeouts_are_ignored(bad):
    assert _server(call_timeout=60)._timeout_for({"timeout": bad}) == 60


def test_a_larger_server_limit_is_never_shortened():
    assert _server(call_timeout=600)._timeout_for({"timeout": 1e9}) == 600
    assert _server(call_timeout=None)._timeout_for({"timeout": 1e9}) is None


# ------------------------------------------------ progress after final answer
def test_progress_is_not_sent_once_the_call_was_answered():
    """Deterministic race: the worker answers right after the monitor's wait timed out."""
    srv = _server(call_timeout=None, progress_interval=0.05)
    out = Out()
    srv._out = out
    job = _Job(7, "spin", {}, None, "tok")
    job.started = time.monotonic() - 1.0
    real_done = job.done

    class RacingDone:
        calls = 0

        def wait(self, timeout=None):
            RacingDone.calls += 1
            if RacingDone.calls == 1:
                # The heartbeat wait times out, and before the monitor sends progress the
                # worker finishes and writes the final response.
                srv._finish(job, srv._result(7, {"content": [], "isError": False}))
                return False
            return real_done.wait(timeout)

        def set(self):
            real_done.set()

        def is_set(self):
            return real_done.is_set()

    job.done = RacingDone()
    srv._monitor(job)
    msgs = []
    while not out.q.empty():
        msgs.append(out.q.get())
    final = [i for i, m in enumerate(msgs) if m.get("id") == 7]
    assert final, msgs
    assert not [m for m in msgs[final[0]:] if m.get("method") == "notifications/progress"], msgs


def test_no_progress_after_the_final_response_under_a_tiny_interval():
    be = MockBackend(width=400, height=300)
    srv = MCPServer(Desktop(be, failsafe=False, sleep=lambda s: None), Policy(), call_timeout=5.0)
    srv.progress_interval = 0.0005  # far below the constructor's floor: maximise the race

    def nap(ms: int = 1) -> str:
        """Short call."""
        time.sleep(ms / 1000.0)
        return "ok"

    srv.toolkit.registry.register(nap, read_only=True)
    srv.toolkit.read_only_tools = set(getattr(srv.toolkit, "read_only_tools", set())) | {"nap"}
    inp, out = Pipe(), Out()
    t = threading.Thread(target=srv.serve, args=(inp, out), daemon=True)
    t.start()
    n = 60
    for i in range(n):
        inp.send(call(i, "nap", {"ms": i % 4}, meta={"progressToken": f"t{i}"}))
    msgs, answered = [], set()
    end = time.monotonic() + 20
    while len(answered) < n and time.monotonic() < end:
        m = out.q.get(timeout=5)
        msgs.append(m)
        if "id" in m:
            answered.add(m["id"])
    time.sleep(0.1)  # any straggling heartbeat would land now
    while not out.q.empty():
        msgs.append(out.q.get())
    inp.close()
    t.join(5)
    assert len(answered) == n
    done = set()
    for m in msgs:
        if "id" in m:
            done.add(m["id"])
        elif m.get("method") == "notifications/progress":
            tok = m["params"]["progressToken"]
            assert int(tok[1:]) not in done, f"progress for {tok} after its final response"
