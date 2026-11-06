"""Real-application acceptance scenarios (Excel dialogs, an SAP GUI export).

They drive real applications on the real desktop, so they never run by default:

* ``LOCUM_ACCEPTANCE=1`` enables the Excel scenarios (Windows + Excel + pywin32);
* ``LOCUM_ACCEPTANCE_SAP=1`` additionally enables the SAP GUI scenario (a logged-in
  SAP GUI with scripting enabled and at least one idle session on its home screen).

Safety rules every scenario follows (see docs/TESTING.md):

* Excel is a **private instance** (``DispatchEx``) driven from a child process; the
  dialogs are looked up only among windows of *that* Excel's process id, so a
  workbook you have open in your own Excel is never touched, saved or closed.
  If anything hangs, only that child and that Excel process id are terminated.
* Every file lives in a local temp directory (never a synced folder).
* Dialog buttons are pressed with accessibility-native invoke and text is entered
  with native set-value: no mouse, no keyboard, no foreground changes, so the
  scenarios run while you keep working.
* SAP: an idle session already on the home screen is borrowed, a tiny read-only
  table (T001) is exported to a local file, and the session is sent back to the
  home screen. Sessions are never opened, closed or killed.
"""

from __future__ import annotations

import os
import sys

import pytest

ACCEPTANCE = os.environ.get("LOCUM_ACCEPTANCE") == "1"
SAP = ACCEPTANCE and os.environ.get("LOCUM_ACCEPTANCE_SAP") == "1"


def pytest_configure(config):
    config.addinivalue_line("markers", "acceptance: drives a real application on the real desktop (opt-in)")
    config.addinivalue_line("markers", "sap: needs a logged-in SAP GUI with an idle home-screen session (opt-in)")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "acceptance" not in item.keywords:
            continue
        if sys.platform != "win32":
            item.add_marker(pytest.mark.skip(reason="Windows-only acceptance scenario"))
        elif not ACCEPTANCE:
            item.add_marker(pytest.mark.skip(reason="set LOCUM_ACCEPTANCE=1 to drive real applications"))
        elif "sap" in item.keywords and not SAP:
            item.add_marker(pytest.mark.skip(reason="set LOCUM_ACCEPTANCE_SAP=1 to run the SAP GUI scenario"))


@pytest.fixture
def desktop():
    from locum.desktop import Desktop

    d = Desktop(failsafe=False, default_timeout=20)
    try:
        yield d
    finally:
        d.close()
