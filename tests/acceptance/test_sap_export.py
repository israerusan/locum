"""An SAP GUI export end to end, with locum answering the Windows-level "SAP GUI Security" prompt.

Read-only: SE16N on T001 (company codes) exported as a local unconverted file into
a temp folder, from an idle session borrowed on its home screen and returned there.
"""

from __future__ import annotations

import threading
import time

import pytest

from .test_excel_dialogs import Driven, _norm

pytestmark = [pytest.mark.acceptance, pytest.mark.sap]


def _answer_security_prompts(d, pid: int, stop: threading.Event, answered: list) -> None:
    """While the export runs, press "Allow" on SAP GUI Security prompts of SAP GUI's process."""
    while not stop.is_set():
        for w in d.windows():
            if w.pid != pid or "security" not in w.title.lower():
                continue
            try:
                for b in w.find_all("button"):
                    if _norm(b.name) == "allow":
                        b.invoke()
                        answered.append(w.title)
                        break
            except Exception:
                pass
        stop.wait(0.3)


def test_se16n_export_to_local_file(desktop, tmp_path):
    run = Driven("sap_export", str(tmp_path))
    stop, answered = threading.Event(), []
    try:
        deadline = time.monotonic() + 30
        ev = None
        while time.monotonic() < deadline:
            try:
                ev = run.events.get(timeout=0.5)
            except Exception:
                continue
            if ev.get("event") in ("ready", "skip", "error"):
                break
        if ev is None or ev.get("event") == "skip":
            pytest.skip((ev or {}).get("reason", "SAP driver did not start"))
        if ev["event"] == "error":
            pytest.fail(ev["error"])
        run.pid = None  # SAP GUI's pid: cleanup must never wait on or touch it
        watcher = threading.Thread(target=_answer_security_prompts, args=(desktop, ev["pid"], stop, answered),
                                   daemon=True)
        watcher.start()
        done = run.expect("done", timeout=180)
        assert done["exists"] and done["size"] > 0, done
        assert done["grid_rows"] > 0
        lines = [ln for ln in open(done["path"], encoding="utf-8", errors="replace") if ln.strip()]
        assert len(lines) >= 2  # header + at least one company code
    finally:
        stop.set()
        run.cleanup()
