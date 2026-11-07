"""Failure diagnostics and honest reporting (2026-11-07 critique of a real Obsidian/Windows run).

No new capability here: these lock in what a failing or partly-successful command says.

* Window titles drift with app state (Welcome -> Review queue -> a note's name): a fixed-title
  command fails with the closest windows, the active window and a recovery route.
* Electron-style apps expose only their top-level window to accessibility: a selector fails
  with an actionable error, while coordinates and the keyboard keep working.
* Unverified typing is reported as unverified, with the reason.
* A good window capture never reads like a failure; a fallback or a clipped image says so.
* ``--version`` shows which installation runs (PATH shadowing).
* Every failure with ``--json`` writes one JSON error document, usage errors included.
"""

import io
import json
import shutil
import sys

import pytest

from locum.backends.mock import MockBackend
from locum.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, main, provenance
from locum.desktop import Desktop
from locum.errors import BackendError


def run(argv, desktop=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), desktop=desktop, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def retitle(window, title):
    window.title = title
    window.root.name = title


# ------------------------------------------------------------------ title drift
@pytest.fixture
def obsidian():
    """An Electron app: only the top-level window is in the accessibility tree."""
    be = MockBackend(width=1600, height=900)
    notes = be.add_window("Untitled - Notepad", (900, 0, 500, 400), app="notepad.exe", pid=7)
    notes.add_edit("Text editor", (920, 40, 400, 300))
    app = be.add_window("Welcome - Vault - Obsidian", (0, 0, 800, 600), app="Obsidian.exe", pid=4242)
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, app


def test_fixed_title_fails_after_drift_with_closest_windows_active_window_and_recovery(obsidian):
    be, d, app = obsidian
    assert run(["hotkey", "--window", "Welcome - Vault - Obsidian", "ctrl+p"], d)[0] == EXIT_OK
    retitle(app, "Review queue - Vault - Obsidian")
    code, out, err = run(["hotkey", "--window", "Welcome - Vault - Obsidian", "ctrl+p"], d)
    assert code == EXIT_FAILURE
    assert "title='Welcome - Vault - Obsidian'" in err                       # what was asked for
    assert "Closest current windows: 'Review queue - Vault - Obsidian'" in err  # what is there now
    assert "app=Obsidian.exe, pid=4242, handle=" in err
    assert "Active window: 'Review queue - Vault - Obsidian'" in err
    assert "'Obsidian' matches 'Review queue - Vault - Obsidian'" in err     # the one-command recovery
    assert err.count("locum:") == 1  # the hint is in the message: not printed twice


def test_title_drift_json_error_carries_the_candidates(obsidian):
    be, d, app = obsidian
    retitle(app, "Daily note 2026-11-07 - Vault - Obsidian")
    code, out, _ = run(["--json", "hotkey", "--window", "Welcome - Vault - Obsidian", "ctrl+p"], d)
    assert code == EXIT_FAILURE
    error = json.loads(out)["error"]
    assert error["type"] == "WindowNotFound" and error["exit_code"] == EXIT_FAILURE
    details = error["details"]
    assert details["requested"] == {"title": "Welcome - Vault - Obsidian"}
    top = details["closest"][0]
    assert top["title"] == "Daily note 2026-11-07 - Vault - Obsidian" and top["app"] == "Obsidian.exe"
    assert top["pid"] == 4242 and isinstance(top["handle"], int)
    assert details["active"]["title"] == top["title"]
    assert "locum windows" in error["hint"]


def test_a_stable_part_of_the_title_survives_the_drift(obsidian):
    be, d, app = obsidian
    for title in ("Welcome - Vault - Obsidian", "Review queue - Vault - Obsidian", "Inbox - Vault - Obsidian"):
        retitle(app, title)
        code, out, err = run(["hotkey", "--window", "Obsidian", "ctrl+p"], d)
        assert code == EXIT_OK, err
        assert title in out


# ------------------------------------------------------------------ sparse accessibility (Electron)
def test_sparse_tree_selector_fails_with_an_actionable_error(obsidian):
    be, d, app = obsidian
    code, out, err = run(["--json", "click", "--window", "Obsidian", 'button "Graph view"'], d)
    assert code == EXIT_FAILURE
    error = json.loads(out)["error"]
    assert error["type"] == "ElementNotFound"
    assert "exposes only 0 unnamed accessibility element(s)" in error["message"]
    assert "click coordinates inside the window" in error["message"]
    assert "locum tree --window 'Obsidian'" in error["hint"]
    assert not [e for e in be.events if e[0] in ("down", "up")]  # nothing was clicked


def test_sparse_tree_coordinates_keyboard_and_tree_still_work(obsidian):
    be, d, app = obsidian
    code, out, err = run(["click", "--window", "Obsidian", "400,300"], d)
    assert code == EXIT_OK, err
    assert ("down", "left", 400, 300) in be.events
    code, out, err = run(["hotkey", "--window", "Obsidian", "ctrl+p"], d)
    assert code == EXIT_OK and ("key_down", "ctrl") in be.events
    code, out, err = run(["tree", "--window", "Obsidian"], d)
    assert code == EXIT_OK and "Obsidian" in out


def test_unnamed_chromium_panes_still_count_as_sparse(obsidian):
    be, d, app = obsidian
    pane = app.add("pane", "", (0, 0, 800, 600))  # "Chrome Legacy Window"-style unnamed containers
    pane.add("document", "", (0, 0, 800, 600))
    code, out, _ = run(["--json", "click", "--window", "Obsidian", 'button "Graph view"'], d)
    assert "exposes only 2 unnamed accessibility element(s)" in json.loads(out)["error"]["message"]


def test_a_small_dialog_is_not_called_sparse():
    be = MockBackend()
    dlg = be.add_window("Confirm", (100, 100, 300, 120), app="app")
    dlg.add_button("OK", (110, 180, 60, 25))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    code, out, _ = run(["--json", "click", "--window", "Confirm", 'button "Cancel"'], d)
    assert code == EXIT_FAILURE and "accessibility element(s)" not in json.loads(out)["error"]["message"]


def test_a_normal_tree_gets_no_sparse_hint(obsidian):
    be, d, app = obsidian
    code, out, _ = run(["--json", "click", "--window", "Notepad", 'button "Nope"'], d)
    assert code == EXIT_FAILURE
    assert "accessibility element(s)" not in json.loads(out)["error"]["message"]


# ------------------------------------------------------------------ typing is reported honestly
@pytest.fixture
def editor():
    be = MockBackend(width=1200, height=800)
    w = be.add_window("Untitled - Notepad", (0, 0, 500, 400), app="notepad.exe")
    field = w.add_edit("Text editor", (20, 40, 400, 300))
    button = w.add_button("Save", (20, 350, 80, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, field, button


def test_typing_into_a_readable_focused_field_is_verified(editor):
    be, d, field, _ = editor
    be._set_focus(field)
    code, out, err = run(["type", "hello", "--window", "Notepad"], d)
    assert code == EXIT_OK and "(read back and verified)" in out and "warning" not in err
    assert field.value == "hello"


def test_typing_that_cannot_be_read_back_says_so_and_why(editor):
    be, d, _, button = editor
    be._set_focus(button)
    code, out, err = run(["type", "hello", "--window", "Notepad"], d)
    assert code == EXIT_OK
    assert "(NOT verified)" in out
    assert "not read back" in err and "'button', not a text field" in err and "--into SELECTOR" in err
    code, out, err = run(["--json", "type", "a\tb", "-e", "--window", "Notepad"], d)
    data = json.loads(out)
    assert data["verified"] is False and "Tab/Enter key presses" in data["unverified_reason"]


def test_typing_into_an_element_reports_its_own_verification(editor):
    be, d, field, _ = editor
    code, out, _ = run(["--json", "type", "abc", "--into", 'edit "Text editor"', "--window", "Notepad"], d)
    data = json.loads(out)
    assert data["verified"] is True and "unverified_reason" not in data


def test_desktop_type_records_why_it_could_not_verify(editor):
    be, d, field, button = editor
    be._set_focus(field)
    d.type("x")
    assert d._last_type_result.verified
    d.type("y", verify=False)
    assert d._last_type_result.status == "skipped" and d._last_type_result.reason == "verify=False"
    be._set_focus(button)
    d.type("z")
    assert d._last_type_result.status == "unverified" and "button" in d._last_type_result.reason


# ------------------------------------------------------------------ capture wording
@pytest.fixture
def covered():
    be = MockBackend(width=1000, height=700)
    target = be.add_window("Target", (100, 100, 300, 200), app="target")
    be.add_window("Cover", (150, 150, 300, 200), app="cover")
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, target


def test_a_failed_window_capture_that_fell_back_says_so(covered, tmp_path, monkeypatch):
    be, d, _ = covered

    def broken(*a, **k):
        raise BackendError("PrintWindow returned 0")

    monkeypatch.setattr(be, "capture_window", broken)
    code, out, err = run(["screenshot", "--window", "Target", "-o", str(tmp_path / "a.png")], d)
    assert code == EXIT_OK
    assert "capturing the window's own content failed (PrintWindow returned 0)" in err
    assert "anything covering it is in the image" in err
    code, out, _ = run(["--json", "screenshot", "--window", "Target", "-o", str(tmp_path / "b.png")], d)
    data = json.loads(out)
    assert data["capture"] == "screen" and data["capture_reason"] == "fallback"
    assert data["fallback_error"] == "PrintWindow returned 0"


def test_a_partly_off_screen_window_says_whether_the_image_is_complete(tmp_path):
    be = MockBackend(width=1000, height=700)
    be.add_window("Edge", (850, 100, 300, 200), app="edge")
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    code, out, err = run(["--json", "screenshot", "--window", "Edge", "-o", str(tmp_path / "a.png")], d)
    data = json.loads(out)
    assert data["capture"] == "window" and data["capture_reason"] == "off_screen" and data["clipped"] is False
    code, out, err = run(["screenshot", "--window", "Edge", "-o", str(tmp_path / "b.png")], d)
    assert "part of it is off-screen: captured the window's own content, so the image is complete" in err
    be.window_capture = False
    code, out, err = run(["screenshot", "--window", "Edge", "-o", str(tmp_path / "c.png")], d)
    assert code == EXIT_OK and "holds only the on-screen part" in err


def test_a_window_in_plain_view_needs_no_note(covered, tmp_path):
    be, d, _ = covered
    code, out, err = run(["screenshot", "--window", "Cover", "-o", str(tmp_path / "a.png")], d)
    assert code == EXIT_OK and err == ""


# ------------------------------------------------------------------ install provenance
def test_version_shows_where_locum_is_installed():
    code, out, _ = run(["--version"])
    assert code == EXIT_OK and out.startswith("locum ")
    assert "executable:" in out and "package:" in out and "on PATH:" in out
    assert sys.executable in out


def test_version_warns_when_another_installation_shadows_this_one(monkeypatch, tmp_path):
    new = str(tmp_path / "new" / "locum.exe")
    old = str(tmp_path / "old" / "locum.exe")
    monkeypatch.setattr(sys, "argv", [new])
    monkeypatch.setattr(shutil, "which", lambda name: old)
    info = provenance()
    assert info["path_mismatch"] is True and info["on_path"] == old
    code, out, _ = run(["--version"])
    assert "another installation shadows it" in out
    monkeypatch.setattr(shutil, "which", lambda name: new)
    assert provenance()["path_mismatch"] is False


def test_info_json_carries_the_provenance():
    d = Desktop(MockBackend(), failsafe=False)
    code, out, _ = run(["--json", "info"], d)
    data = json.loads(out)
    for key in ("executable", "package", "python_executable", "on_path", "path_mismatch"):
        assert key in data


# ------------------------------------------------------------------ failure contract
@pytest.mark.parametrize("argv", [["--json", "clik", "x"], ["--json", "click"], ["--json", "--timeout", "-1", "info"]])
def test_usage_errors_with_json_write_one_error_document(argv):
    code, out, err = run(argv)
    assert code == EXIT_USAGE
    doc = json.loads(out)
    assert doc["ok"] is False and doc["error"]["type"] == "UsageError" and doc["error"]["exit_code"] == EXIT_USAGE
    assert doc["error"]["message"] and err  # and the human diagnostic stays on stderr


def test_usage_errors_without_json_keep_stdout_clean():
    code, out, err = run(["clik", "x"])
    assert code == EXIT_USAGE and out == "" and "invalid choice" in err


def test_a_runtime_failure_says_what_was_tried_found_and_how_to_recover(obsidian):
    be, d, app = obsidian
    code, out, err = run(["find", 'button "Nope"'], d)
    assert code == EXIT_FAILURE
    first, second = err.strip().splitlines()[:2]
    assert first.startswith("locum: ElementNotFound: No element matching")
    assert second.startswith("locum: hint: ") and "locum tree" in second
