from __future__ import annotations

import json

import pytest

from locum.errors import PolicyViolation
from locum.llm.safety import DEFAULT_CONFIRM, AuditLog, Policy
from locum.types import Point, Rect


def test_default_policy_allows_plain_actions():
    p = Policy()
    p.check("click", {"element_id": 1}, context={"target": "OK", "app": "erp"})
    p.check("type_text", {"text": "please send the report"}, context={})  # typed text is not a commit
    assert p.action_count == 2


def test_allow_and_deny_tools():
    p = Policy(allow_tools=["observe", "click*"], deny_tools=["click_danger"])
    p.check("observe", {}, context={})
    p.check("click", {}, context={})
    with pytest.raises(PolicyViolation, match="not allowed"):
        p.check("type_text", {}, context={})
    with pytest.raises(PolicyViolation):
        p.check("click_danger", {}, context={})
    assert not p.tool_allowed("launch_app")


@pytest.mark.parametrize("target", ["Pay now", "Transfer funds", "Wire", "Approve invoice", "Post", "Submit", "Delete row", "Send"])
def test_finance_actions_need_confirmation(target):
    p = Policy()
    with pytest.raises(PolicyViolation, match="confirmation"):
        p.check("click", {}, context={"target": target})
    assert p.action_count == 0


def test_confirm_callback_approves_and_declines():
    asked = []

    def confirm(description, details):
        asked.append((description, details["reason"]))
        return details["target"] == "Pay invoice"

    p = Policy(confirm=confirm)
    p.check("click", {}, context={"target": "Pay invoice", "description": "click button 'Pay invoice'"})
    with pytest.raises(PolicyViolation, match="declined"):
        p.check("click", {}, context={"target": "Delete everything"})
    assert asked[0][0] == "click button 'Pay invoice'"
    assert "pay" in asked[0][1].lower()
    assert p.confirmations == [("click button 'Pay invoice'", True), (asked[1][0], False)]
    # confirm callback raising -> blocked, never executed
    p2 = Policy(confirm=lambda d, x: 1 / 0)
    with pytest.raises(PolicyViolation, match="Confirmation failed"):
        p2.check("click", {}, context={"target": "Submit"})


def test_confirm_matches_selector_and_hotkey_args():
    p = Policy()
    with pytest.raises(PolicyViolation):
        p.check("click", {"selector": 'button "Submit order"'}, context={})
    p.check("hotkey", {"combo": "ctrl+s"}, context={})
    # word boundaries: "Postcode"/"Sender" are not "post"/"send"
    p.check("click", {}, context={"target": "Postcode"})
    p.check("click", {}, context={"target": "Sender name"})


def test_dry_run_skips_confirmation_but_flags_it():
    p = Policy(dry_run=True)
    ctx = {"target": "Transfer"}
    p.check("click", {}, context=ctx)
    assert "transfer" in ctx["needs_confirmation"].lower()


def test_permissive_and_strict_presets():
    Policy.permissive().check("click", {}, context={"target": "Delete"})
    s = Policy.strict()
    with pytest.raises(PolicyViolation):
        s.check("launch_app", {"command": "notepad"}, context={})
    with pytest.raises(PolicyViolation, match="blocked"):
        s.check("click", {}, context={"app": "powershell.exe"})
    with pytest.raises(PolicyViolation):
        s.check("type_text", {"text": "4111 1111 1111 1111"}, context={})
    with pytest.raises(PolicyViolation):
        s.check("click", {}, context={"target": "Save"})
    s.check("click", {}, context={"target": "Sign in"})


def test_app_allow_and_block_lists():
    p = Policy(allowed_apps=["excel", "erp*"], blocked_apps=["regedit"])
    p.check("click", {}, context={"app": "EXCEL.EXE"})
    p.check("click", {}, context={"app": "erp-client"})
    with pytest.raises(PolicyViolation, match="not allowed"):
        p.check("click", {}, context={"app": "chrome"})
    with pytest.raises(PolicyViolation, match="not allowed"):
        p.check("click", {}, context={})
    with pytest.raises(PolicyViolation, match="blocked"):
        p.check("click", {}, context={"app": "excel", "target_app": "regedit.exe"})
    with pytest.raises(PolicyViolation, match="blocked"):
        p.check("launch_app", {"command": "regedit /s x.reg"}, context={})
    # read-only tools are not app-restricted
    p.check("observe", {}, context={"app": "chrome"})


def test_deny_text_patterns():
    p = Policy(deny_text_patterns=[r"rm\s+-rf", r"password"])
    with pytest.raises(PolicyViolation):
        p.check("type_text", {"text": "rm -rf /"}, context={})
    with pytest.raises(PolicyViolation):
        p.check("click", {}, context={"window_title": "Change Password"})
    p.check("type_text", {"text": "ls -la"}, context={})


def test_region_confinement():
    p = Policy(region=Rect(0, 0, 100, 100))
    p.check("click", {}, context={"point": Point(50, 50)})
    with pytest.raises(PolicyViolation, match="outside"):
        p.check("click", {}, context={"point": (150, 50)})
    with pytest.raises(PolicyViolation):
        p.check("drag", {}, context={"start_point": (10, 10), "end_point": (500, 500)})


def test_rate_limits():
    now = [0.0]
    p = Policy(max_actions=3, max_actions_per_minute=2, clock=lambda: now[0])
    p.check("click", {}, context={})
    p.check("click", {}, context={})
    with pytest.raises(PolicyViolation, match="Rate limit"):
        p.check("click", {}, context={})
    p.check("observe", {}, context={})  # read-only tools are not rate limited
    now[0] = 61.0
    p.check("click", {}, context={})
    with pytest.raises(PolicyViolation, match="budget"):
        p.check("click", {}, context={})
    p.reset()
    p.check("click", {}, context={})
    with pytest.raises(ValueError):
        Policy(max_actions_per_minute=0)


def test_audit_log_jsonl(tmp_path):
    path = tmp_path / "logs" / "audit.jsonl"
    log = AuditLog(path)
    log.record({"event": "tool_call", "tool": "click", "args": {"x": 1}, "point": Point(3, 4)})
    log.record({"event": "screenshot", "image": "A" * 50000, "rect": Rect(1, 2, 3, 4)})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(l) for l in lines)
    assert first["tool"] == "click" and "ts" in first
    assert first["point"] == [3, 4]
    assert len(second["image"]) < 300
    assert second["rect"] == {"left": 1, "top": 2, "width": 3, "height": 4}
    assert log.read()[0]["tool"] == "click"
    assert len(log) == 2
    mem = AuditLog()
    mem.record({"a": 1})
    assert mem.read() == mem.events


def test_default_confirm_patterns_cover_finance_verbs():
    joined = " ".join(DEFAULT_CONFIRM)
    for verb in ("pay", "transfer", "wire", "approve", "post", "submit", "delete", "send"):
        assert verb in joined
