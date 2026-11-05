"""Let an LLM operate the desktop -- with a safety policy, human confirmation and an audit log.

    pip install "pypilot[llm]"
    export ANTHROPIC_API_KEY=...
    python examples/llm_agent.py "Enter invoice INV-7 from ACME Corp for 120.50 and pay it"

Offline demo (no API key, no real input): a scripted model drives the demo
desktop, and the confirmation prompt fires when it tries to submit a payment:

    PYPILOT_BACKEND=mock python examples/llm_agent.py

How it stays safe:

* the :class:`~pypilot.llm.Policy` lets the agent act only in the allowed apps,
  blocks card numbers / ``rm -rf`` style text, caps the number and rate of
  actions, and asks ``confirm()`` before anything that looks like it commits
  money or data (pay, submit, delete, send, approve, post, ...);
* every tool call -- allowed, blocked or declined -- is written to a JSON-lines
  :class:`~pypilot.llm.AuditLog`;
* moving the mouse into a screen corner (fail-safe) aborts the run instantly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

from pypilot import Desktop
from pypilot.llm import Agent, AgentStep, AuditLog, Policy, ScriptedClient, scripted_response, tool_use_block


def make_desktop() -> Desktop:
    if os.environ.get("PYPILOT_BACKEND", "").lower() in ("mock", "demo"):
        from pypilot.cli import DemoBackend

        return Desktop(DemoBackend(), sleep=lambda s: None, default_timeout=2)
    return Desktop()


def confirm(description: str, details: Dict[str, Any]) -> bool:
    """Ask the human before consequential actions. Non-interactive runs decline (fail closed)."""
    print(f"\n[confirm] The agent wants to: {description}\n          reason: {details.get('reason')}")
    if not sys.stdin.isatty():
        print("[confirm] no terminal to ask on -> declined")
        return False
    return input("[confirm] Allow? [y/N] ").strip().lower() in ("y", "yes")


def print_step(step: AgentStep) -> None:
    if step.text:
        print(f"[{step.index + 1}] {step.text}")
    for call, result in zip(step.tool_calls, step.tool_results):
        args = json.dumps(call.get("input") or {}, ensure_ascii=False)
        outcome = ("BLOCKED/ERROR: " if result.is_error else "") + (result.text.splitlines()[0] if result.text else "")
        print(f"[{step.index + 1}]   {call['name']}({args[:100]}) -> {outcome[:140]}")


def scripted_demo_client() -> ScriptedClient:
    """Canned model turns for the offline demo (same message shapes as the real API)."""
    turns: List[Dict[str, Any]] = [
        scripted_response(
            "The invoice form is open. I'll fill it in using the field selectors.",
            tool_use_block("set_value", {"selector": "edit#vendor", "value": "ACME Corp"}),
            tool_use_block("set_value", {"selector": "edit#invoice_no", "value": "INV-7"}),
            tool_use_block("set_value", {"selector": "edit#amount", "value": "120.50"}),
            tool_use_block("click", {"selector": 'button "Save"'}),
        ),
        scripted_response(
            "Saved. Now submitting the payment.",
            tool_use_block("click", {"selector": 'button "Submit Payment"'}),
        ),
        scripted_response(
            tool_use_block("done", {"summary": "Invoice INV-7 (ACME Corp, 120.50) was saved. The payment step "
                                               "required human approval; see the audit log for the decision.",
                                    "success": True}),
        ),
    ]
    return ScriptedClient(turns)


def main(argv: Optional[List[str]] = None) -> int:
    mock = os.environ.get("PYPILOT_BACKEND", "").lower() in ("mock", "demo")
    p = argparse.ArgumentParser(description="Run a desktop task with an LLM agent.")
    p.add_argument("task", nargs="?", default="Enter invoice INV-7 from ACME Corp for 120.50 and pay it.")
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--mode", choices=("tools", "computer_use"), default="tools")
    p.add_argument("--allow-app", action="append", help="apps the agent may drive (default: any; demo: erp)")
    p.add_argument("--audit", default=os.path.join(tempfile.gettempdir(), "pypilot_agent_audit.jsonl"))
    p.add_argument("--dry-run", action="store_true", help="simulate every action")
    p.add_argument("--offline", action="store_true", help="use the scripted demo model instead of the API")
    args = p.parse_args(argv)

    offline = args.offline or (mock and not os.environ.get("ANTHROPIC_API_KEY"))
    allowed = args.allow_app or (["erp"] if mock else None)
    policy = Policy(
        allowed_apps=allowed,                                       # None = any application
        blocked_apps=("cmd", "powershell", "terminal", "regedit"),  # never drive shells/registry
        deny_text_patterns=(r"\b(?:\d[ -]?){13,19}\b",),            # no card numbers anywhere
        confirm=confirm,                                            # human in the loop for pay/submit/delete/...
        dry_run=args.dry_run,
        max_actions=100,
        max_actions_per_minute=60,
    )
    audit = AuditLog(args.audit)

    with make_desktop() as desktop:
        agent = Agent(
            desktop,
            model=args.model,
            client=scripted_demo_client() if offline else None,    # None = anthropic.Anthropic()
            mode=args.mode,
            policy=policy,
            audit=audit,
            max_steps=30,
            on_step=print_step,
        )
        result = agent.run(args.task)
        print("\nresult:", "success" if result.success else "FAILED", "-", result.final_text)
        print(f"confirmations: {policy.confirmations}")
        print(f"audit log: {args.audit} ({len(audit)} events)")
        if mock:
            print("demo ERP invoices:", desktop.backend.invoices, "payments:", desktop.backend.payments)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
