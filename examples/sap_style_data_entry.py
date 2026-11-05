"""An unattended RPA robot: enter invoices from a CSV work queue into an ERP form.

This is the classic "SAP-style" back-office robot, written code-first:

* a :class:`~pypilot.rpa.WorkQueue` (Orchestrator-style transactions) is loaded
  from ``data/invoices.csv``; it is persisted to JSON, so an interrupted run
  resumes where it stopped and re-running never enters an invoice twice;
* every transaction fills the form through **accessibility selectors** (no
  coordinates), clicks *Save* and reads the status bar;
* bad data (a negative amount, a duplicate, a validation message from the app)
  raises a :class:`~pypilot.rpa.BusinessException` -- recorded, never retried;
  technical failures (element not found, app hiccup) are retried;
* the :class:`~pypilot.rpa.Robot` times every step, saves a screenshot when
  something fails and writes ``report.json`` + a self-contained ``report.html``.

The selectors target a generic data-entry window titled "Invoice Entry*" with
fields whose automation ids are vendor / invoice_no / amount / due_date. Point
them at your own application with ``pypilot inspect`` (prints a robust selector
for the element under the cursor) or ``pypilot tree --window "<title>"``.

Pairing with SAP GUI: for SAP GUI for Windows, pypilot drives the window
through UI Automation like any other app. If your SAP basis team has enabled
**SAP GUI Scripting**, you can additionally talk to the scripting API from the
same robot (``pip install pywin32``) for fields UIA cannot see, e.g.::

    import win32com.client
    sap = win32com.client.GetObject("SAPGUI").GetScriptingEngine
    session = sap.Children(0).Children(0)
    session.findById("wnd[0]/tbar[0]/okcd").text = "/nFB60"   # your transaction
    session.findById("wnd[0]").sendVKey(0)

and keep pypilot for everything around it (logon dialogs, pop-ups, Excel,
e-mail, screenshots, reports, queues, retries).

Run against the demo desktop (no real input is sent):

    PYPILOT_BACKEND=mock python examples/sap_style_data_entry.py

Run for real (the ERP window must be open or ``--launch`` must start it):

    python examples/sap_style_data_entry.py --csv invoices.csv --launch "erp.exe"
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from typing import Any, Dict, Optional

from pypilot import Desktop
from pypilot.rpa import BusinessException, QueueItem, Robot, WorkQueue

HERE = os.path.dirname(os.path.abspath(__file__))

# CSS-like selectors. The UiPath-XML equivalent of the amount field also works:
#   "<wnd app='erp' title='Invoice Entry*' /><ctrl automationid='amount' role='editable text' />"
FORM = 'window[name%="Invoice Entry*"]'
SEL = {
    "vendor": f"{FORM} edit#vendor",
    "invoice_no": f"{FORM} edit#invoice_no",
    "amount": f"{FORM} edit#amount",
    "due_date": f"{FORM} edit#due_date",
    "urgent": f"{FORM} checkbox#urgent",
    "save": f'{FORM} button#save',
    "clear": f'{FORM} button#clear',
    "status": f"{FORM} text#status",
}


def make_desktop() -> Desktop:
    if os.environ.get("PYPILOT_BACKEND", "").lower() in ("mock", "demo"):
        from pypilot.cli import DemoBackend

        return Desktop(DemoBackend(), sleep=lambda s: None, default_timeout=2)
    return Desktop(default_timeout=15)


def parse_amount(raw: Any) -> float:
    """Business validation before touching the UI (cheap, and keeps bad data out of the ERP)."""
    try:
        amount = float(str(raw).replace(",", "").strip())
    except ValueError:
        raise BusinessException(f"amount {raw!r} is not a number") from None
    if amount <= 0:
        raise BusinessException(f"amount must be positive, got {amount:g}")
    return amount


def build_robot(desktop: Desktop, csv_path: str, queue_path: str, run_dir: str, launch: Optional[str] = None) -> Robot:
    robot = Robot(desktop, name="invoice-entry", run_dir=run_dir)
    queue = WorkQueue("invoices", path=queue_path)
    robot.vars["queue"] = queue

    def status_text() -> str:
        return desktop.find(SEL["status"], timeout=0).text

    def enter_invoice(item: QueueItem) -> Dict[str, Any]:
        amount = parse_amount(item["amount"])
        robot.log("entering invoice", invoice=item["invoice_no"], attempt=item.attempts)
        desktop.click(SEL["clear"])                      # known state; status becomes "Cleared"
        desktop.find(SEL["vendor"]).set_text(item["vendor"])
        desktop.find(SEL["invoice_no"]).set_text(item["invoice_no"])
        desktop.find(SEL["amount"]).set_text(f"{amount:.2f}")
        desktop.find(SEL["due_date"]).set_text(item.get("due_date", ""))
        urgent = desktop.find(SEL["urgent"])
        if str(item.get("urgent", "")).strip().lower() in ("yes", "y", "true", "1"):
            urgent.check()
        else:
            urgent.uncheck()
        desktop.click(SEL["save"])
        # Wait for the application to answer in its status bar.
        def answered() -> Optional[str]:
            text = status_text()
            return text if text not in ("", "Cleared") else None

        status = desktop.wait_until(answered, timeout=10)
        if status.lower().startswith("error"):
            raise BusinessException(status)              # the app rejected the data: do not retry
        return {"status": status, "amount": amount}

    @robot.setup
    def open_application() -> None:
        with robot.step("open the ERP window"):
            if launch and not desktop.windows("Invoice Entry*"):
                desktop.launch(launch, wait_window="Invoice Entry*", timeout=60)
            desktop.window("Invoice Entry*").activate()

    @robot.task(retries=1, retry_delay=2)
    def load_queue() -> None:
        """Load the CSV into the persistent queue (idempotent: references are invoice numbers)."""
        items = queue.load_csv(csv_path, reference_field="invoice_no")
        robot.log(f"queue has {len(queue)} item(s) ({len(items)} from {os.path.basename(csv_path)})")

    @robot.task
    def process_queue() -> None:
        """Enter every pending invoice; business exceptions are recorded, system errors retried twice."""
        stats = queue.process(enter_invoice, retries=2, retry_delay=1, sleep=desktop.sleep,
                              on_item=lambda it: robot.log(f"{it.reference}: {it.status}", error=it.error))
        robot.vars["stats"] = stats
        robot.log("queue processed", success=stats["success"], business=stats["business_exception"],
                  failed=stats["failed"])
        queue.export_csv(os.path.join(str(robot.run_path), "results.csv"))

    return robot


def main(argv: Optional[list] = None) -> int:
    mock = os.environ.get("PYPILOT_BACKEND", "").lower() in ("mock", "demo")
    scratch = tempfile.mkdtemp(prefix="pypilot-invoices-") if mock else "."
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--csv", default=os.path.join(HERE, "data", "invoices.csv"))
    p.add_argument("--queue", default=os.path.join(scratch, "invoices.queue.json"), help="queue state (resume) file")
    p.add_argument("--run-dir", default=os.path.join(scratch, "pypilot_runs"))
    p.add_argument("--launch", help="command that starts the ERP client if its window is not open")
    args = p.parse_args(argv)

    with make_desktop() as desktop:
        robot = build_robot(desktop, args.csv, args.queue, args.run_dir, args.launch)
        report = robot.run()
        print(report.summary())
        stats = robot.vars.get("stats", {})
        for item in robot.vars["queue"].items:
            print(f"  {item.reference:10} {item.status:20} {item.error or item.output.get('status', '')}")
        if stats:
            print(f"success {stats['success']}, business exceptions {stats['business_exception']}, failed {stats['failed']}")
        print(f"report: {os.path.join(report.run_dir or str(robot.run_path), 'report.html')}")
        if mock:
            print("invoices stored by the demo ERP:", [inv["invoice_no"] for inv in desktop.backend.invoices])
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
