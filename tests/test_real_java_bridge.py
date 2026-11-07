"""The Java Access Bridge against a real JVM: a Swing window (helpers/JabProbe.java)
driven through ``locum.bridges.java``.

Needs a JDK (``JAVA_HOME`` or ``javac``/``java`` on PATH), the Access Bridge
enabled (``jabswitch -enable``) and a real Windows desktop, so it only runs with
``LOCUM_TEST_JAVA=1`` (the CI job ``windows-java-bridge`` sets it on a throwaway
runner). The mouse-fallback test sends a real click: never run it on a desktop
someone is using.

The first run of this job found what the fake DLL could not: nodes cached their
state, so a checkbox a click had checked still read as unchecked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("LOCUM_TEST_JAVA") != "1",
    reason="real Java Access Bridge test: set LOCUM_TEST_JAVA=1 on a dedicated Windows desktop with a JDK",
)

SOURCE = Path(__file__).parent / "helpers" / "JabProbe.java"


def _jdk_tool(name: str) -> str:
    home = os.environ.get("JAVA_HOME")
    if home:
        exe = Path(home) / "bin" / f"{name}.exe"
        if exe.exists():
            return str(exe)
    found = shutil.which(name)
    if not found:
        pytest.fail(f"{name} not found (set JAVA_HOME to a JDK)")
    return found


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    build = tmp_path_factory.mktemp("jab")
    subprocess.run([_jdk_tool("javac"), "-d", str(build), str(SOURCE)], check=True)
    title = f"locum JAB probe {os.getpid()}"
    log = build / "events.log"
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen([_jdk_tool("java"), "-cp", str(build), "JabProbe", title], stdout=out)
        try:
            yield title, log
        finally:
            proc.kill()
            proc.wait(10)


@pytest.fixture
def window(probe):
    from locum.bridges.java import JavaBridge

    title, _log = probe
    with JavaBridge() as jab:
        yield jab, jab.window(title, timeout=30.0)


def _until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not check() and time.monotonic() < deadline:
        time.sleep(0.1)
    return check()


def _status(win) -> str:
    # A JLabel has no AccessibleText unless it is HTML: its visible text is its accessible name.
    labels = [e.name for e in win.find_all("text") if e.name.startswith(("Status:", "Saved:"))]
    assert len(labels) == 1, labels
    return labels[0]


def _events(probe) -> list:
    return probe[1].read_text(encoding="utf-8").splitlines()  # println writes \r\n on Windows


def test_tree_has_the_swing_controls(window):
    _jab, win = window
    tree = win.tree()
    assert win.java_role == "frame", tree
    assert win.exists('edit[name="Customer"]'), tree
    assert win.exists('button "Save"'), tree
    assert win.exists('checkbox "Rush order"'), tree
    assert _status(win) == "Status: idle"


def test_set_text_and_click_through_accessible_actions(window, probe):
    _jab, win = window
    win.find('edit[name="Customer"]').set_text("ACME Corp")
    assert win.find('edit[name="Customer"]').text == "ACME Corp"
    win.find('button "Save"').click(native=True)
    assert _until(lambda: _status(win) == "Saved: ACME Corp"), _status(win)
    assert "EVENT save Saved: ACME Corp" in _events(probe)


def test_state_reads_follow_the_ui(window, probe):
    _jab, win = window
    box = win.find('checkbox "Rush order"')
    assert "checked" not in box.states
    box.click(native=True)
    assert _until(lambda: "EVENT rush true" in _events(probe))
    # The same element and a fresh lookup both report the new state.
    assert _until(lambda: "checked" in box.states), sorted(box.states)
    assert "checked" in win.find('checkbox "Rush order"').states


def test_mouse_fallback_clicks_inside_the_java_window(window, probe):
    _jab, win = window
    win.find('edit[name="Customer"]').set_text("Mouse Inc")
    win.find('button "Save"').click(native=False)  # a real click, bound to the window by locum._target
    assert _until(lambda: _status(win).startswith("Saved: Mouse Inc")), _status(win)


def test_table_cells_are_readable(window):
    _jab, win = window
    table = win.find("table")
    cells = table.find_all("*[name]")
    names = [c.name for c in cells]
    assert len(names) >= 60 and "R0C0" in names and "R19C2" in names, names[:10]
    cell = table.find('*[name="R3C1"]')
    assert cell.text == "R3C1"


def test_list_and_tree_items_are_found(window):
    _jab, win = window
    for item in ("Alpha", "Bravo", "Charlie"):
        assert win.exists(f'list *[name="{item}"]'), win.find("list").tree()
    for node in ("Sales", "Billing"):
        assert win.exists(f'tree *[name="{node}"]'), win.find("tree").tree()


def test_repeated_table_list_tree_lookups_keep_references_flat(probe):
    # Swing returns a NEW AccessibleJTableCell / AccessibleJListChild / AccessibleJTreeNode on every
    # getAccessibleChild: matching them with isSameObject never succeeds, and every lookup used to
    # pin one more reference per cell.
    from locum.bridges.java import JavaBridge

    jab = JavaBridge()
    try:
        win = jab.window(probe[0], timeout=30.0)
        table = win.find("table")
        cell = table.find('*[name="R5C2"]')
        win.tree(max_depth=64)
        held = jab.live_references
        for _ in range(5):
            assert len(table.find_all("*[name]")) >= 60
            win.find_all("*")
            win.tree(max_depth=64)
            assert win.exists('list *[name="Bravo"]') and win.exists('tree *[name="Billing"]')
        assert jab.live_references == held, (held, jab.live_references)
        assert cell.name == "R5C2"  # a held cell element survives the re-reads
    finally:
        jab.close()
    assert jab.live_references == 0


def test_references_are_released(probe):
    from locum.bridges.java import JavaBridge

    jab = JavaBridge()
    try:
        win = jab.window(probe[0], timeout=30.0)
        list(win.walk())
        held = jab.live_references
        assert held > 0
        for _ in range(3):
            win.find_all("*")
        assert jab.live_references == held  # lookups re-read the tree without piling up references
    finally:
        jab.close()
    assert jab.live_references == 0
