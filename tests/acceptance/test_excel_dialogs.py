"""Excel dialogs answered with locum: native invoke / set-value only, private Excel instance only."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import zipfile

import pytest

pytestmark = pytest.mark.acceptance

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "_app_driver.py")


def _norm(name: str) -> str:
    return (name or "").replace("’", "'").replace("&", "").strip().casefold()


class Driven:
    """The child process running one scenario, and the application pid it reports."""

    def __init__(self, scenario: str, workdir: str) -> None:
        pytest.importorskip("win32com.client")
        self.proc = subprocess.Popen([sys.executable, DRIVER, scenario, workdir], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.output: list = []
        threading.Thread(target=self._read, daemon=True).start()
        self.pid = None

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.output.append(line)
            try:
                self.events.put(json.loads(line))
            except ValueError:
                pass

    def expect(self, event: str, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ev = self.events.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None and self.events.empty():
                    break
                continue
            if ev.get("event") == "error":
                pytest.fail(f"driver failed: {ev['error']}")
            if ev.get("event") == event:
                if event == "ready":
                    self.pid = ev["pid"]
                return ev
        pytest.fail(f"no {event!r} from the driver within {timeout} s; output:\n{''.join(self.output)}")

    def cleanup(self) -> None:
        """Only ever this child and the Excel process it started."""
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.pid:
            try:
                import psutil  # optional

                p = psutil.Process(self.pid)
                if p.name().lower() == "excel.exe":
                    p.wait(15)
            except Exception:
                pass


@pytest.fixture
def no_input(desktop, monkeypatch):
    """Fail the test if anything would move the mouse or press a key."""
    def refuse(*a, **k):
        raise AssertionError("acceptance scenarios must not send mouse/keyboard input")

    for name in ("mouse_down", "mouse_up", "key_down", "key_up", "type_text", "scroll", "move_mouse"):
        if hasattr(desktop.backend, name):
            monkeypatch.setattr(desktop.backend, name, refuse)
    return desktop


def _find_button(d, pid: int, wanted: str, also: tuple = (), title: str = "", timeout: float = 30.0):
    """The button named ``wanted`` in a window of ``pid`` that also shows the buttons ``also``."""
    want, others = _norm(wanted), {_norm(x) for x in also}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in d.windows():
            if w.pid != pid or (title and w.title != title):
                continue
            try:
                buttons = w.find_all("button")
            except Exception:
                continue
            names = {_norm(b.name) for b in buttons}
            if want in names and others <= names:
                return w, next(b for b in buttons if _norm(b.name) == want)
        time.sleep(0.25)
    raise AssertionError(f"no dialog with button {wanted!r} (and {also}) in Excel pid {pid} within {timeout} s")


def _a1_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        strings = z.read("xl/sharedStrings.xml").decode("utf-8") if "xl/sharedStrings.xml" in names else ""
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    return strings + sheet


def test_save_changes_prompt_dont_save(no_input, tmp_path):
    d = no_input
    run = Driven("save_prompt", str(tmp_path))
    try:
        ready = run.expect("ready")
        path = tmp_path / "save_prompt.xlsx"
        before = path.stat().st_mtime_ns
        _, button = _find_button(d, ready["pid"], "Don't Save", also=("Save", "Cancel"))
        button.invoke()
        done = run.expect("done")
        assert done["open_workbooks"] == 0
        assert path.stat().st_mtime_ns == before  # nothing was saved
        assert "<v>1</v>" in _a1_text(str(path))  # still the saved value, not the unsaved 2
    finally:
        run.cleanup()


def test_overwrite_prompt_yes(no_input, tmp_path):
    d = no_input
    run = Driven("overwrite_prompt", str(tmp_path))
    try:
        ready = run.expect("ready")
        _, button = _find_button(d, ready["pid"], "Yes", also=("No",))
        button.invoke()
        done = run.expect("done")
        assert done["replaced"] is True
        assert "new" in _a1_text(done["path"])
    finally:
        run.cleanup()


def test_save_as_dialog_exact_file_name(no_input, tmp_path):
    d = no_input
    target = str(tmp_path / "Invoice 5100012345.xlsx")
    run = Driven("save_as_dialog", str(tmp_path))
    try:
        ready = run.expect("ready")
        win, save = _find_button(d, ready["pid"], "Save", also=("Cancel",), title="Save As")
        field = next(e for e in win.find_all("edit") if _norm(e.name).startswith("file name"))
        field.set_text(target, native=True, verify="exact")
        assert field.last_verification.verified, field.last_verification
        save.invoke()
        done = run.expect("done")
        assert done["filename"] == target
    finally:
        run.cleanup()
