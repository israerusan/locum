"""An unattended RPA robot: enter invoices from a CSV work queue into an ERP form.

This is the classic "SAP-style" back-office robot, written code-first:

* a :class:`~locum.rpa.WorkQueue` (Orchestrator-style transactions) is loaded
  from ``data/invoices.csv``; it is persisted to JSON, so an interrupted run
  resumes where it stopped and re-running never enters an invoice twice;
* an invoice is identified by vendor + invoice number (invoice numbers are
  only unique per vendor); the same row exported twice is entered once, while
  two *different* rows for the same vendor and invoice number are both
  recorded as business exceptions for a human to resolve (neither is entered);
* every transaction fills the form through **accessibility selectors** (no
  coordinates), clicks *Save* and reads the status bar;
* bad data (an amount that is not a plain positive number with at most two
  decimals, a duplicate, a validation message from the app) raises a
  :class:`~locum.rpa.BusinessException` -- recorded, never retried. Amounts
  are read as ``1250.00`` / ``1,250.00`` (``--decimal-comma`` for
  ``1.250,00`` / ``1250,00``); anything ambiguous (``1,250`` or ``1.250``
  without decimals) is rejected rather than guessed;
* technical failures *before* Save (element not found, app hiccup) are
  retried. Once Save has been clicked the outcome is unknown, so a missing or
  late answer is never retried (that could post the invoice twice): the item
  is recorded as "needs review" -- check the ERP, then re-queue it if needed;
* the :class:`~locum.rpa.Robot` times every step, saves a screenshot when
  something fails and writes ``report.json`` + a self-contained ``report.html``.

The selectors target a generic data-entry window titled "Invoice Entry*" with
fields whose automation ids are vendor / invoice_no / amount / due_date. Point
them at your own application with ``locum inspect`` (prints a robust selector
for the element under the cursor) or ``locum tree --window "<title>"``.

Pairing with SAP GUI: for SAP GUI for Windows, locum drives the window
through UI Automation like any other app. If your SAP basis team has enabled
**SAP GUI Scripting**, you can additionally talk to the scripting API from the
same robot (``pip install pywin32``) for fields UIA cannot see, e.g.::

    import win32com.client
    sap = win32com.client.GetObject("SAPGUI").GetScriptingEngine
    session = sap.Children(0).Children(0)
    session.findById("wnd[0]/tbar[0]/okcd").text = "/nFB60"   # your transaction
    session.findById("wnd[0]").sendVKey(0)

and keep locum for everything around it (logon dialogs, pop-ups, Excel,
e-mail, screenshots, reports, queues, retries).

Run against the demo desktop (no real input is sent):

    LOCUM_BACKEND=mock python examples/sap_style_data_entry.py

Run for real (the ERP window must be open or ``--launch`` must start it):

    python examples/sap_style_data_entry.py --csv invoices.csv --launch "erp.exe"
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
from decimal import Decimal
from typing import Any, Dict, List, Optional

from locum import Desktop
from locum.errors import FailSafeTriggered
from locum.rpa import BusinessException, QueueItem, Robot, WorkQueue

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
    if os.environ.get("LOCUM_BACKEND", "").lower() in ("mock", "demo"):
        from locum.cli import DemoBackend

        return Desktop(DemoBackend(), sleep=lambda s: None, default_timeout=2)
    return Desktop(default_timeout=15)


# A plain amount: digits with optional thousands groups and an optional decimal part.
_AMOUNT_RE = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_AMBIGUOUS_RE = re.compile(r"[+-]?\d{1,3},\d{3}")  # 1,250: 1250 (US) or 1.25 (DE)?


def parse_amount(raw: Any, decimal_comma: bool = False) -> Decimal:
    """Business validation before touching the UI (cheap, and keeps bad data out of the ERP).

    Never guesses: ``1250.00`` and ``1,250.00`` are accepted (with
    ``decimal_comma``: ``1250,00`` and ``1.250,00``); a different separator
    style, ``1,250`` (1250 or 1.25?), ``NaN``/``inf``, a non-positive amount or
    more than two decimals raise :class:`BusinessException`.
    """
    text = str(raw).strip()
    if decimal_comma:
        text = text.translate(str.maketrans(",.", ".,"))
    if not _AMOUNT_RE.fullmatch(text):
        example = "1250,00 or 1.250,00" if decimal_comma else "1250.00 or 1,250.00"
        raise BusinessException(f"amount {raw!r} is not a plain amount (expected e.g. {example})")
    if _AMBIGUOUS_RE.fullmatch(text):
        raise BusinessException(f"amount {raw!r} is ambiguous (thousands or decimal separator?); "
                                "write it with two decimals")
    amount = Decimal(text.replace(",", ""))
    if amount <= 0:
        raise BusinessException(f"amount must be positive, got {raw!r}")
    if amount != amount.quantize(Decimal("0.01")):
        raise BusinessException(f"amount {raw!r} has more than two decimals")
    return amount.quantize(Decimal("0.01"))


def invoice_key(row: Dict[str, Any]) -> Optional[str]:
    """Queue reference of an invoice: vendor + invoice number (numbers are unique per vendor only)."""
    vendor = " ".join(str(row.get("vendor", "")).split())
    number = str(row.get("invoice_no", "")).strip()
    return f"{vendor} | {number}" if vendor and number else None


def queue_invoices(queue: WorkQueue, csv_path: str) -> List[QueueItem]:
    """Add the CSV rows to ``queue`` (idempotent: re-loading the same file adds nothing).

    An exact duplicate row is entered once. Rows that share vendor + invoice
    number but differ otherwise are all queued as conflicts, so each one is
    reported as a business exception instead of one of them silently winning.
    """
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = [{str(k).strip(): (v or "").strip() for k, v in row.items() if k is not None}
                for row in csv.DictReader(fh)]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for line, row in enumerate(rows, 2):  # line 1 is the header
        if any(row.values()):
            key = invoice_key(row) or f"{os.path.basename(csv_path)} line {line}"  # reported by enter_invoice
            group = groups.setdefault(key, [])
            if row not in group:
                group.append(row)
    items = []
    for key, distinct in groups.items():
        if len(distinct) == 1:
            items.append(queue.add(distinct[0], reference=key))
            continue
        for n, row in enumerate(distinct, 1):
            items.append(queue.add(dict(row, _conflicts=len(distinct)), reference=f"{key} #{n}"))
    return items


def build_robot(desktop: Desktop, csv_path: str, queue_path: str, run_dir: str, launch: Optional[str] = None,
                decimal_comma: bool = False) -> Robot:
    robot = Robot(desktop, name="invoice-entry", run_dir=run_dir)
    queue = WorkQueue("invoices", path=queue_path)
    robot.vars["queue"] = queue

    def status_text() -> str:
        return desktop.find(SEL["status"], timeout=0).text

    def enter_invoice(item: QueueItem) -> Dict[str, Any]:
        if not item.get("vendor") or not item.get("invoice_no"):
            raise BusinessException("vendor and invoice_no are required")
        if item.get("_conflicts"):
            raise BusinessException(f"invoice {item['invoice_no']} of {item['vendor']} appears "
                                    f"{item['_conflicts']} times in the CSV with different data; none was entered")
        amount = parse_amount(item["amount"], decimal_comma)
        robot.log("entering invoice", invoice=item["invoice_no"], attempt=item.attempts)
        # 1. Fill the form. Nothing is posted yet: a failure here is safe to retry.
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
        save = desktop.find(SEL["save"])

        # 2. Commit. From the click on, the ERP may have posted the invoice even if
        #    we never see its answer, so nothing after this point is retried.
        def answered() -> Optional[str]:
            text = status_text()
            return text if text not in ("", "Cleared") else None

        try:
            save.click()
            status = desktop.wait_until(answered, timeout=10)  # the application answers in its status bar
        except FailSafeTriggered:
            raise  # the operator stopped the robot
        except Exception as exc:
            raise BusinessException(
                f"needs review: Save was clicked but the ERP gave no answer ({type(exc).__name__}: {exc}); "
                f"check whether invoice {item['invoice_no']} was posted before re-queuing it") from exc
        if status.lower().startswith("error"):
            raise BusinessException(status)              # the app rejected the data: do not retry
        return {"status": status, "amount": f"{amount:.2f}"}

    @robot.setup
    def open_application() -> None:
        with robot.step("open the ERP window"):
            if launch and not desktop.windows("Invoice Entry*"):
                desktop.launch(launch, wait_window="Invoice Entry*", timeout=60)
            desktop.window("Invoice Entry*").activate()

    @robot.task(retries=1, retry_delay=2)
    def load_queue() -> None:
        """Load the CSV into the persistent queue (idempotent: references are vendor + invoice number)."""
        items = queue_invoices(queue, csv_path)
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
    mock = os.environ.get("LOCUM_BACKEND", "").lower() in ("mock", "demo")
    scratch = tempfile.mkdtemp(prefix="locum-invoices-") if mock else "."
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--csv", default=os.path.join(HERE, "data", "invoices.csv"))
    p.add_argument("--queue", default=os.path.join(scratch, "invoices.queue.json"), help="queue state (resume) file")
    p.add_argument("--run-dir", default=os.path.join(scratch, "locum_runs"))
    p.add_argument("--launch", help="command that starts the ERP client if its window is not open")
    p.add_argument("--decimal-comma", action="store_true",
                   help="amounts are written 1.250,00 / 1250,00 (default: 1,250.00 / 1250.00)")
    args = p.parse_args(argv)

    with make_desktop() as desktop:
        robot = build_robot(desktop, args.csv, args.queue, args.run_dir, args.launch, args.decimal_comma)
        report = robot.run()
        print(report.summary())
        stats = robot.vars.get("stats", {})
        for item in robot.vars["queue"].items:
            print(f"  {item.reference:24} {item.status:20} {item.error or item.output.get('status', '')}")
        if stats:
            print(f"success {stats['success']}, business exceptions {stats['business_exception']}, failed {stats['failed']}")
        print(f"report: {os.path.join(report.run_dir or str(robot.run_path), 'report.html')}")
        if mock:
            print("invoices stored by the demo ERP:", [inv["invoice_no"] for inv in desktop.backend.invoices])
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
