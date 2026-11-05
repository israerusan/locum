"""The real-desktop self-test, exercised on the simulated desktop."""

from __future__ import annotations

from locum import selftest


def test_selftest_passes_on_simulated_desktop(tmp_path):
    rc = selftest.main(["--backend", "mock", "--yes", "--out", str(tmp_path / "st")])
    assert rc == 0
    report = (tmp_path / "st" / "report.txt").read_text(encoding="utf-8")
    assert "FAIL 0" in report and "[PASS] unicode typing" in report
    assert (tmp_path / "st" / "report.json").exists()


def test_selftest_refuses_to_type_into_a_non_empty_editor(tmp_path):
    """Windows 11 Notepad restores the user's tabs: the test must never type into them."""
    d = selftest._mock_desktop()
    be = d.backend
    orig = be.launch

    def launch(command, **kw):
        orig(command, **kw)
        be.windows[0].find("Text editor").value = "my private notes"

    be.launch = launch
    st = selftest.SelfTest(d, str(tmp_path / "st"), log=lambda m: None)
    st.run()
    status = {c.name: c.status for c in st.checks}
    assert status["accessibility tree + selector"] == "FAIL"
    assert status["raw keystrokes (unverified, informational)"] == "SKIP"
    assert status["unicode typing (Desktop.type)"] == "SKIP"
    assert status["unicode typing (Element.type, verified)"] == "SKIP"
    assert status["set_text (native)"] == "SKIP"
    assert not any(e[0] == "type" for e in be.events)
    assert st.editor is None


def test_selftest_restores_clipboard_and_mouse(tmp_path):
    d = selftest._mock_desktop()
    d.clipboard_set("user clipboard")
    d.move(123, 456)
    selftest.SelfTest(d, str(tmp_path / "st"), log=lambda m: None).run()
    assert d.clipboard_get() == "user clipboard"
    assert tuple(d.position()) == (123, 456)


def test_selftest_close_when_ctrl_w_closed_the_only_tab(tmp_path):
    """On a clean machine the test tab is Notepad's only tab: Ctrl+W closes the
    window itself (seen on GitHub's Windows Server runner), which is a pass."""
    d = selftest._mock_desktop()
    be = d.backend
    st = selftest.SelfTest(d, str(tmp_path / "st"), log=lambda m: None)
    orig = st._discard_test_text

    def discard_and_close_window():
        orig()
        if st.editor_window is not None:
            be.close_window(st.editor_window.handle)

    st._discard_test_text = discard_and_close_window
    st.run()
    status = {c.name: (c.status, c.detail) for c in st.checks}
    assert status["close editor without saving"] == ("PASS", "closed with its only tab")
    assert not any(c.status == "FAIL" for c in st.checks)


def test_selftest_never_closes_a_notepad_window_that_was_already_open(tmp_path):
    """Windows 11 Notepad opens the test tab inside the user's open window (seen
    live: a window holding the user's own documents). Only the tab may close."""
    d = selftest._mock_desktop()
    be = d.backend
    be.launch("notepad.exe")                 # the user's Notepad, open before the test
    be.launch = lambda command, **kw: None   # launching again only adds a tab to it
    users = [w for w in d.windows() if "Notepad" in w.title][0]
    st = selftest.SelfTest(d, str(tmp_path / "st"), log=lambda m: None)
    st.run()
    status = {c.name: (c.status, c.detail) for c in st.checks}
    assert st.editor_preexisting is True
    assert status["close editor without saving"] == (
        "PASS", "closed the test tab; left your existing Notepad window open")
    assert users.exists
