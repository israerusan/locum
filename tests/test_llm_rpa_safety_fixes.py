"""Regression tests for the "llm-rpa-safety" review findings."""

from __future__ import annotations

import pytest

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import PolicyViolation
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.safety import Policy
from locum.llm.tools import DesktopToolkit
from locum._nodeinfo import target_label


@pytest.fixture
def bank():
    be = MockBackend(width=1920, height=1080)
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    win = be.add_window("Bank", (400, 300, 800, 600), app="bank")
    clicked = []
    pay = win.add_button("Pay now", (200, 500, 100, 40), on_click=lambda *a: clicked.append("pay"))
    icon = pay.add("image", "", (205, 505, 20, 20))
    group = win.add("group", "Payments", (10, 10, 400, 200))
    opener = group.add("button", "Open", (20, 20, 80, 30), on_click=lambda *a: clicked.append("open"))
    return be, d, win, pay, icon, opener, clicked


# --------------------------------------------------------------- finding 1: keyboard / child bypass
def test_keyboard_activation_of_focused_pay_button_needs_confirmation(bank):
    be, d, win, pay, icon, opener, clicked = bank
    cu = ComputerUseAdapter(d, policy=Policy())
    tk = DesktopToolkit(d, Policy())
    be._set_focus(pay)
    assert cu.execute("key", {"text": "Return"}).is_error
    be._set_focus(pay)
    assert tk.call("press_keys", {"keys": ["space"]}).is_error
    be._set_focus(pay)
    assert tk.call("type_text", {"text": "\n"}).is_error
    be._set_focus(pay)
    assert tk.call("hotkey", {"keys": "enter"}).is_error
    assert clicked == []


def test_unnamed_child_inside_pay_button_needs_confirmation(bank):
    be, d, win, pay, icon, opener, clicked = bank
    node = be.element_at(*icon.screen_rect.center)
    assert node is not None and node.role == "image"
    assert "Pay now" in target_label(node)
    cu = ComputerUseAdapter(d, policy=Policy())
    x, y = cu.to_image(*icon.screen_rect.center)
    r = cu.execute("left_click", {"coordinate": [x, y]})
    assert r.is_error and "confirmation" in r.text
    tk = DesktopToolkit(d, Policy())
    c = icon.screen_rect.center
    r = tk.call("click", {"x": c.x, "y": c.y})  # no observation yet: screenshot pixels == screen pixels
    assert r.is_error and "confirmation" in r.text
    assert clicked == []


def test_label_stops_at_first_actionable_control(bank):
    be, d, win, pay, icon, opener, clicked = bank
    node = be.element_at(*opener.screen_rect.center)
    # the "Payments" group above the "Open" button is not part of the target
    assert target_label(node) == "Open"
    tk = DesktopToolkit(d, Policy())
    r = tk.call("click", {"selector": 'button "Open"'})
    assert not r.is_error, r.text
    assert clicked == ["open"]


# --------------------------------------------------------------- finding 2: app under the pointer
@pytest.fixture
def two_apps():
    be = MockBackend(width=1920, height=1080)
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    term = be.add_window("Terminal", (0, 0, 600, 400), app="xterm")
    tb = term.add_button("Run", (10, 10, 100, 30))
    notes = be.add_window("Notes", (900, 300, 600, 400), app="notes")
    be.element_at = lambda x, y: None  # no accessibility under the pointer
    return be, d, term, tb, notes


@pytest.mark.parametrize("policy", [lambda: Policy.permissive(blocked_apps=("xterm",)),
                                    lambda: Policy.permissive(allowed_apps=("notes",))])
def test_pointer_app_is_window_under_point_not_active_app(two_apps, policy):
    be, d, term, tb, notes = two_apps
    be.activate_window(notes.handle)
    cu = ComputerUseAdapter(d, policy=policy())
    x, y = cu.to_image(*tb.screen_rect.center)
    be.events.clear()
    r = cu.execute("left_click", {"coordinate": [x, y]})
    assert r.is_error and "xterm" in r.text
    assert not [e for e in be.events if e[0] in ("down", "up")]
    be.activate_window(notes.handle)
    tk = DesktopToolkit(d, policy())
    r = tk.call("click", {"x": tb.screen_rect.center.x, "y": tb.screen_rect.center.y})
    assert r.is_error and "xterm" in r.text


def test_node_without_window_still_checks_window_under_point(two_apps):
    be, d, term, tb, notes = two_apps

    class Orphan:
        name = "Run"
        role = "button"
        value = None
        handle = None
        pid = None
        parent = None
        bounds = None

        def attributes(self):
            return {"app": "notes"}  # node resolves to another app than the window under the point

        def actions(self):
            return []

    be.element_at = lambda x, y: Orphan()
    be.activate_window(notes.handle)
    cu = ComputerUseAdapter(d, policy=Policy.permissive(blocked_apps=("xterm",)))
    x, y = cu.to_image(*tb.screen_rect.center)
    assert cu.execute("left_click", {"coordinate": [x, y]}).is_error


def test_allowed_apps_refuses_point_with_unknown_app():
    p = Policy.permissive(allowed_apps=("notes",))
    with pytest.raises(PolicyViolation, match="Cannot determine"):
        p.check("click", {}, context={"app": "notes", "point": (5, 5)})
    # keyboard actions (no point) still use the active app
    p.check("type_text", {"text": "hi"}, context={"app": "notes"})


# --------------------------------------------------------------- finding 4: MCP stdio hygiene
_MCP_SCRIPT = r"""
import os, sys
from locum.backends.base import Backend
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.llm.mcp_server import MCPServer
from locum.llm.safety import Policy

class B(MockBackend):
    def launch(self, command, **kw):  # the real implementation every OS backend uses
        return Backend.launch(self, command, **kw).wait()

d = Desktop(B(), failsafe=False, sleep=lambda s: None)
srv = MCPServer(d, Policy.permissive())
@srv.toolkit.registry.register(name="noisy")
def noisy() -> str:
    print("stray-print")
    os.write(1, b"stray-fd-write\n")
    return "ok"
srv.serve()
print("after-serve")
"""


def test_mcp_server_keeps_children_and_strays_off_protocol_stdout(tmp_path):
    import json
    import subprocess
    import sys

    script = tmp_path / "srv.py"
    script.write_text(_MCP_SCRIPT)
    child = f"{sys.executable} -c \"import sys; print('hello-' + 'from-child'); sys.stdin.read()\""
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "launch_app", "arguments": {"command": child}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "noisy", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]
    proc = subprocess.run([sys.executable, str(script)], input="".join(json.dumps(m) + "\n" for m in lines),
                          capture_output=True, text=True, timeout=60)
    out = proc.stdout.splitlines()
    # every line before the server returned is a JSON-RPC frame; the child read nothing from our stdin
    assert out[-1] == "after-serve"
    frames = [json.loads(line) for line in out[:-1]]
    # tool calls run on a worker thread, so a ping may be answered before them (JSON-RPC
    # responses are matched by id, not order)
    assert sorted(f["id"] for f in frames) == [1, 2, 3, 4], proc.stdout
    frames = {f["id"]: f for f in frames}
    assert frames[2]["result"]["isError"] is False
    assert "hello-from-child" not in proc.stdout
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "stray-print" in proc.stderr and "stray-fd-write" in proc.stderr


def test_mcp_server_sets_launch_redirection_on_supplied_toolkit():
    import subprocess

    from locum.llm.mcp_server import MCPServer

    be = MockBackend()
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    tk = DesktopToolkit(d, Policy.permissive(), launch_kwargs={"stdout": subprocess.PIPE})
    MCPServer(toolkit=tk)
    assert tk.launch_kwargs == {"stdout": subprocess.PIPE, "stdin": subprocess.DEVNULL}
    srv = MCPServer(d, Policy.permissive())
    assert srv.toolkit.launch_kwargs == {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL}


# --------------------------------------------------------------- finding 5: Agent extra_tools behind the policy
def _agent_with(policy, *extra, script):
    from locum.llm.agent import Agent, ScriptedClient

    d = Desktop(MockBackend(), failsafe=False, sleep=lambda s: None)
    client = ScriptedClient(script)
    return Agent(d, client=client, policy=policy, extra_tools=list(extra), initial_observation=False), client


def _make_tools(ran):
    def delete_record(record_id: str) -> str:
        """Delete a record in the ERP.

        Args:
            record_id: Record id.
        """
        ran.append(("delete", record_id))
        return "deleted"

    def lookup_record(record_id: str) -> str:
        """Look up a record.

        Args:
            record_id: Record id.
        """
        ran.append(("lookup", record_id))
        return "found"

    return delete_record, lookup_record


def test_extra_tools_obey_deny_list_and_are_audited():
    from locum.llm.agent import tool_use_block

    ran = []
    delete_record, _ = _make_tools(ran)
    agent, client = _agent_with(Policy(deny_tools=("delete_*",)), delete_record,
                                script=[[tool_use_block("delete_record", {"record_id": "42"})], ["done"]])
    agent.run("x")
    assert ran == []
    last = client.requests[1]["messages"][-1]["content"][0]
    assert last["is_error"] and "not allowed" in last["content"][0]["text"]
    events = [e for e in agent.audit.events if e.get("event") == "tool_call"]
    assert events and events[0]["tool"] == "delete_record" and events[0]["status"] == "blocked"


def test_extra_tools_dry_run_and_confirmation():
    from locum.llm.agent import tool_use_block

    ran = []
    delete_record, lookup_record = _make_tools(ran)
    agent, client = _agent_with(Policy(dry_run=True), delete_record,
                                script=[[tool_use_block("delete_record", {"record_id": "42"})], ["done"]])
    agent.run("x")
    assert ran == []
    text = client.requests[1]["messages"][-1]["content"][0]["content"][0]["text"]
    assert text.startswith("[dry-run]") and "confirmation" in text
    assert [e["status"] for e in agent.audit.events if e.get("event") == "tool_call"] == ["dry_run"]

    # real run: "delete" in the tool name needs a human; lookups do not
    asked = []
    pol = Policy(confirm=lambda desc, det: asked.append(desc) or False)
    agent, client = _agent_with(pol, delete_record, lookup_record, script=[
        [tool_use_block("lookup_record", {"record_id": "1"}), tool_use_block("delete_record", {"record_id": "42"})],
        ["done"]])
    agent.run("x")
    assert ran == [("lookup", "1")]
    assert len(asked) == 1 and "delete_record" in asked[0]
    assert pol.action_count == 1


def test_extra_tools_keep_argument_validation():
    from locum.llm.agent import tool_use_block

    ran = []
    _, lookup_record = _make_tools(ran)
    agent, client = _agent_with(Policy(), lookup_record,
                                script=[[tool_use_block("lookup_record", {"record_id": "1", "bogus": 2})], ["done"]])
    agent.run("x")
    assert ran == []
    assert "Unexpected argument" in client.requests[1]["messages"][-1]["content"][0]["content"][0]["text"]


# --------------------------------------------------------------- finding 3 / 8: flow reports, call vars
def test_flow_report_never_contains_interpolated_secrets(tmp_path, monkeypatch):
    from locum.rpa.flow import run_flow

    monkeypatch.setenv("APP_PASSWORD", "hunter2-secret")
    be = MockBackend()
    win = be.add_window("Login", (0, 0, 400, 300), app="erp")
    win.add_edit("Password", (10, 10, 200, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    report = run_flow({"vars": {"pw": "${env.APP_PASSWORD}"},
                       "steps": [{"set_text": {"target": 'edit "Password"', "text": "${env.APP_PASSWORD}"}},
                                 {"type": "${pw}"}]},
                      d, run_dir=tmp_path)
    assert report.ok, report.error
    assert win.find("Password").value.startswith("hunter2-secret")
    files = list(tmp_path.rglob("report.*"))
    assert {f.suffix for f in files} >= {".json", ".html"}
    for f in files:
        content = f.read_text(encoding="utf-8")
        assert "hunter2-secret" not in content, f
    assert "${env.APP_PASSWORD}" in next(f for f in files if f.suffix == ".json").read_text(encoding="utf-8")


def test_call_vars_defaults_see_overrides_and_each_other():
    from locum.rpa.flow import run_flow

    d = Desktop(MockBackend(), failsafe=False, sleep=lambda s: None)
    sub = {"vars": {"host": "erp", "url": "https://${host}/login"}, "steps": [{"log": "${url}"}]}
    doc = {"vars": {"host": "parent-host"}, "flows": {"login": sub},
           "steps": [{"call": {"flow": "login", "vars": {"host": "staging"}}}, {"call": "login"}]}
    r = run_flow(doc, d)
    assert r.ok, r.error
    msgs = [l["message"] for l in r.logs]
    assert "https://staging/login" in msgs  # override wins and is visible to later defaults
    assert "https://erp/login" in msgs  # without override, the sub-flow default applies


# --------------------------------------------------------------- finding 6 / 9: queue references & abandoned items
def test_queue_same_basename_in_other_folder_is_not_a_duplicate(tmp_path):
    from locum.rpa.queue import WorkQueue

    (tmp_path / "jan").mkdir()
    (tmp_path / "feb").mkdir()
    (tmp_path / "jan" / "invoices.csv").write_text("no,amount\nJ1,10\nJ2,20\n")
    (tmp_path / "feb" / "invoices.csv").write_text("no,amount\nF1,30\nF2,40\n")
    q = WorkQueue()
    q.load(tmp_path / "jan" / "invoices.csv")
    q.process(lambda it: None)
    feb = q.load(tmp_path / "feb" / "invoices.csv")
    assert len(q) == 4
    assert [it["no"] for it in q.pending()] == ["F1", "F2"]
    assert all(it.status == "new" for it in feb)


def test_queue_rows_inserted_into_source_are_picked_up_once(tmp_path):
    from locum.rpa.queue import WorkQueue

    src = tmp_path / "data.csv"
    src.write_text("no\nR1\nR2\n")
    q = WorkQueue(path=tmp_path / "q.json")
    q.load(src)
    seen = []
    q.process(lambda it: seen.append(it["no"]))
    src.write_text("no\nR0\nR1\nR2\nR2\n")  # a new row at the top and a genuine duplicate at the end
    q2 = WorkQueue.open(tmp_path / "q.json")
    q2.load(src)
    assert [it["no"] for it in q2.pending()] == ["R0", "R2"]
    q2.process(lambda it: seen.append(it["no"]))
    assert seen == ["R1", "R2", "R0", "R2"]
    q2.load(src)  # idempotent
    assert len(q2) == 4 and not q2.pending()


def _crash_file(tmp_path, attempts):
    import json

    from locum.rpa.queue import WorkQueue

    path = tmp_path / "q.json"
    q = WorkQueue("pay", path)
    q.add({"invoice": 1}, reference="inv1")
    data = json.loads(path.read_text())
    data["items"][0]["status"] = "in_progress"
    data["items"][0]["attempts"] = attempts
    path.write_text(json.dumps(data))
    return path


def test_abandoned_in_progress_items_are_failed_by_default(tmp_path):
    from locum.rpa.queue import FAILED, WorkQueue

    path = _crash_file(tmp_path, 1)
    q = WorkQueue.open(path)
    it = q.get("inv1")
    assert it.status == FAILED and it.error_type == "Abandoned"
    ran = []
    q.process(lambda item: ran.append(item.reference))
    assert ran == []  # never replayed automatically
    q.retry_items()  # a human decided it is safe
    q.process(lambda item: ran.append(item.reference))
    assert ran == ["inv1"]
    with pytest.raises(ValueError):
        WorkQueue(on_abandoned="bogus")


def test_requeued_abandoned_items_respect_retry_budget_across_runs(tmp_path):
    from locum.rpa.queue import FAILED, SUCCESS, WorkQueue

    # attempts already used up by crashing runs -> failed without running again
    q = WorkQueue.open(_crash_file(tmp_path, 2), on_abandoned="requeue")
    assert q.get("inv1").status == "new"
    ran = []
    stats = q.process(lambda item: ran.append(1), retries=1)
    assert ran == [] and q.get("inv1").status == FAILED and "retry limit" in q.get("inv1").error
    assert stats["processed"] == 1

    # one crash with retries=1 -> exactly one more attempt
    (tmp_path / "q.json").unlink()
    q = WorkQueue.open(_crash_file(tmp_path, 1), on_abandoned="requeue")

    def boom(item):
        ran.append(item.attempts)
        raise RuntimeError("app crashed")

    q.process(boom, retries=1)
    assert ran == [2] and q.get("inv1").status == FAILED and q.get("inv1").attempts == 2

    # an interruption by the human while the handler runs: a human checks the item
    # (its side effect may have happened); retrying it then starts from scratch
    (tmp_path / "q.json").unlink()
    from locum.errors import FailSafeTriggered

    q = WorkQueue("x", tmp_path / "q.json")
    q.add({"n": 1}, reference="a")

    def stop(item):
        raise FailSafeTriggered("abort")

    with pytest.raises(FailSafeTriggered):
        q.process(stop, retries=0)
    assert q.get("a").status == FAILED and q.get("a").error_type == "Interrupted"
    q.retry_items()
    assert q.get("a").attempts == 0
    q.process(lambda item: None, retries=0)
    assert q.get("a").status == SUCCESS


# --------------------------------------------------------------- finding 7: recorder escapes ${
def test_recorded_dollar_brace_text_replays_literally(monkeypatch):
    from locum.rpa.flow import run_flow
    from locum.rpa.recorder import Recorder

    monkeypatch.setenv("LOCUM_TEST_SECRET", "s3cr3t")
    be = MockBackend()
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    w = be.add_window("T", (100, 100, 400, 300))
    e = w.add_edit("Name", (10, 50, 200, 28))
    b = w.add_button("OK", (10, 100, 80, 30))
    rec = Recorder(d, trust_unknown_focus=True)  # manual feed: no real focus
    c = e.screen_rect.center
    rec.feed(("down", "left", c.x, c.y))
    rec.feed(("up", "left", c.x, c.y))
    typed = "cost ${total} key=${env.LOCUM_TEST_SECRET} keep $${x}"
    rec.feed(("type", typed))
    c2 = b.screen_rect.center
    rec.feed(("down", "left", c2.x, c2.y))
    rec.feed(("up", "left", c2.x, c2.y))
    flow = rec.to_flow()
    r = run_flow(flow, d)
    assert r.ok, r.error
    assert e.value == typed
    assert "s3cr3t" not in e.value
