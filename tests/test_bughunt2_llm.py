"""Regression tests for bug-hunt round 2 findings in locum.llm (tools / computer_use / agent).

Mock backend only: no real input, clipboard or windows.
"""
import enum
import json

import pytest

from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.llm import AuditLog, DesktopToolkit
from locum.llm.agent import Agent, ScriptedClient
from locum.llm.computer_use import ComputerUseAdapter
from locum.llm.safety import READ_ONLY_TOOLS, Policy


class _BrokenAudit(AuditLog):
    """An audit log whose writes fail (locked file, full disk)."""

    def record(self, event):  # noqa: D401
        raise PermissionError(13, "Permission denied", "audit.jsonl")


def _desktop(be=None):
    return Desktop(be or MockBackend(), failsafe=False, sleep=lambda s: None)


@pytest.fixture
def password_states(monkeypatch):
    orig = mock.MockNode.states
    monkeypatch.setattr(
        mock.MockNode, "states",
        property(lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())),
    )


# ---------------------------------------------------------------- finding 2
def test_read_text_masks_password_field(password_states):
    be = MockBackend()
    w = be.add_window("Bank login", (100, 100, 500, 300), app="chrome")
    w.add("edit", "User", (20, 20, 200, 30))
    pwd = w.add("edit", "Password", (20, 60, 200, 30), attrs={"password": True})
    pwd.value = "S3cr3t-Pa55!"
    audit = AuditLog()
    kit = DesktopToolkit(_desktop(be), Policy(), audit)
    kit.call("observe", {"include_screenshot": False})
    eid = next(e.id for e in kit.last_observation.elements if e.name == "Password")
    r = kit.call("read_text", {"element_id": eid})
    assert not r.is_error
    assert "S3cr3t" not in r.text and "***" in r.text
    assert "S3cr3t" not in json.dumps(r.data)
    assert "S3cr3t" not in json.dumps(audit.events)


def test_read_text_still_returns_non_password_value(password_states):
    be = MockBackend()
    w = be.add_window("Form", (100, 100, 500, 300), app="chrome")
    user = w.add("edit", "User", (20, 20, 200, 30))
    user.value = "alice"
    kit = DesktopToolkit(_desktop(be), Policy())
    kit.call("observe", {"include_screenshot": False})
    eid = next(e.id for e in kit.last_observation.elements if e.name == "User")
    assert "value: alice" in kit.call("read_text", {"element_id": eid}).text


# ---------------------------------------------------------------- finding 3
def test_query_tool_removed_from_read_only_is_still_policy_checked():
    be = MockBackend()
    be.clipboard = "IBAN DE89 3704 0044 0532 0130 00"
    pol = Policy(read_only_tools=READ_ONLY_TOOLS - {"read_clipboard"}, deny_tools=["read_clipboard"])
    kit = DesktopToolkit(_desktop(be), pol)
    r = kit.call("read_clipboard", {})
    assert r.is_error and "not allowed" in r.text
    assert "DE89" not in r.text


def test_query_tool_removed_from_read_only_counts_against_budget():
    be = MockBackend()
    be.clipboard = "secret"
    pol = Policy(read_only_tools=READ_ONLY_TOOLS - {"read_clipboard"}, max_actions=1)
    kit = DesktopToolkit(_desktop(be), pol)
    first = kit.call("read_clipboard", {})
    assert not first.is_error and pol.action_count == 1
    second = kit.call("read_clipboard", {})
    assert second.is_error and "secret" not in second.text


def test_read_only_query_tools_do_not_count():
    be = MockBackend()
    be.clipboard = "x"
    pol = Policy(max_actions=0)
    kit = DesktopToolkit(_desktop(be), pol)
    assert not kit.call("read_clipboard", {}).is_error
    assert not kit.call("list_windows", {}).is_error
    assert pol.action_count == 0


# ---------------------------------------------------------------- finding 4
def test_computer_use_execute_does_not_raise_when_audit_write_fails():
    be = MockBackend(width=800, height=600)
    w = be.add_window("Bank", (0, 0, 800, 600), app="chrome")
    pay = w.add_button("Pay", (10, 10, 100, 30))
    cu = ComputerUseAdapter(_desktop(be), policy=Policy(confirm=lambda *a: True), audit=_BrokenAudit())
    ip = cu.to_image(60, 25)
    r = cu.execute("left_click", {"coordinate": [ip.x, ip.y]})
    assert pay.clicks == 1
    assert not r.is_error
    assert "AUDIT LOG WRITE FAILED" in r.text and "do not repeat" in r.text


# ---------------------------------------------------------------- finding 5
def test_agent_extra_tool_not_reported_as_error_when_audit_write_fails():
    sent = []

    def post_journal(amount: int) -> str:
        """Post a journal entry.

        Args:
            amount: Amount in cents.
        """
        sent.append(amount)
        return "posted"

    ag = Agent(_desktop(), client=ScriptedClient([]), policy=Policy.permissive(), audit=_BrokenAudit(),
               extra_tools=[post_journal], initial_observation=False)
    r = ag.registry.call("post_journal", {"amount": 100})
    assert sent == [100]
    assert not r.is_error
    assert r.text.startswith("posted") and "AUDIT LOG WRITE FAILED" in r.text


# ---------------------------------------------------------------- finding 6
class Priority(enum.Enum):
    LOW = "low"
    HIGH = "high"


def test_agent_extra_tool_receives_enum_members():
    got = []

    def file_ticket(title: str, priority: Priority = Priority.LOW) -> str:
        """File a ticket.

        Args:
            title: Ticket title.
            priority: Ticket priority.
        """
        got.append(priority)
        return f"filed with {priority.name}"

    ag = Agent(_desktop(), client=ScriptedClient([]), policy=Policy.permissive(), extra_tools=[file_ticket],
               initial_observation=False)
    r = ag.registry.call("file_ticket", {"title": "x", "priority": "high"})
    assert not r.is_error, r.text
    assert got == [Priority.HIGH]
    bad = ag.registry.call("file_ticket", {"title": "x", "priority": "urgent"})
    assert bad.is_error


# ---------------------------------------------------------------- finding 7
def test_agent_rebinding_policy_keeps_caller_registry_changes():
    d = _desktop()
    kit = DesktopToolkit(d)
    kit.registry.remove("launch_app")
    kit.registry.remove("write_clipboard")

    @kit.registry.register(name="erp_lookup", read_only=True)
    def erp_lookup(doc: str) -> str:
        """Look up an ERP document.

        Args:
            doc: Document number.
        """
        return "ok"

    pol = Policy(max_actions=50, read_only_tools=READ_ONLY_TOOLS - {"screenshot"})
    ag = Agent(d, client=ScriptedClient([]), toolkit=kit, policy=pol, initial_observation=False)
    names = [t["name"] for t in ag.tools()]
    assert "launch_app" not in names and "write_clipboard" not in names
    assert "erp_lookup" in names
    assert kit.policy is pol
    specs = {s.name: s for s in kit.registry.specs()}
    assert specs["screenshot"].read_only is False  # read-only flags follow the new policy
    assert specs["observe"].read_only is True
    assert specs["erp_lookup"].read_only is True
