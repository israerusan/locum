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


def test_selftest_opens_textedit_on_an_empty_file_of_its_own(monkeypatch):
    import os

    monkeypatch.setattr(selftest.sys, "platform", "darwin")
    cmd, title = selftest._editor_for_platform("macos")
    path = cmd[-1]
    try:
        assert cmd[:4] == ["open", "-n", "-a", "TextEdit"] and os.path.getsize(path) == 0
        assert title.search(os.path.basename(path))  # no Open panel: the window is named after the file
    finally:
        os.remove(path)
        os.rmdir(os.path.dirname(path))


def test_selftest_uses_the_platform_shortcut_key():
    import inspect

    src = inspect.getsource(selftest.SelfTest)
    for key in "anc":  # Ctrl+A moves to the line start on macOS instead of selecting all
        assert f'hotkey("ctrl+{key}")' not in src and f'hotkey("mod+{key}")' in src


def test_selftest_restores_a_non_text_clipboard_in_full(tmp_path):
    """The self-test used to save/restore clipboard *text* only, so a copied image or
    file list was wiped by every run (on every platform)."""
    d = selftest._mock_desktop()
    be = d.backend
    snapshot = {"formats": [("image/png", b"\x89PNG...")]}  # opaque, no text at all
    restored = []
    be.save_clipboard = lambda: snapshot
    be.restore_clipboard = lambda saved: restored.append(saved)
    selftest.SelfTest(d, str(tmp_path / "st"), log=lambda m: None).run()
    assert restored == [snapshot]


def test_selftest_does_not_overwrite_a_clipboard_it_could_not_save(tmp_path):
    d = selftest._mock_desktop()
    be = d.backend
    d.clipboard_set("precious")

    def refuse():
        raise RuntimeError("clipboard owned by another process")

    be.save_clipboard = refuse
    st = selftest.SelfTest(d, str(tmp_path / "st"), log=lambda m: None)
    st.run()
    status = {c.name: c.status for c in st.checks}
    assert status["hotkeys + clipboard"] == "SKIP"
    assert d.clipboard_get() == "precious"


def test_selftest_retries_a_flaky_check_and_reports_the_earlier_failure(tmp_path):
    d = selftest._mock_desktop()
    st = selftest.SelfTest(d, str(tmp_path / "st"), log=lambda m: None, retries=2)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("clipboard busy")
        return "ok"

    assert st.run_check("hotkeys + clipboard", flaky) is True
    assert len(calls) == 2
    assert [c.status for c in st.checks] == ["PASS"]
    assert "passed on attempt 2" in st.checks[0].detail and "clipboard busy" in st.checks[0].detail


def test_selftest_never_retries_side_effect_checks(tmp_path):
    d = selftest._mock_desktop()
    st = selftest.SelfTest(d, str(tmp_path / "st"), log=lambda m: None, retries=3)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("no")

    assert st.run_check("launch editor", boom) is False
    assert len(calls) == 1 and st.checks[-1].status == "FAIL"


def test_selftest_strict_turns_a_skip_into_a_failed_run(tmp_path, monkeypatch):
    def skip_vision(self):
        raise selftest._Skip("no screen capture permission")

    monkeypatch.setattr(selftest.SelfTest, "c_vision", skip_vision)
    out = tmp_path / "a"
    assert selftest.main(["--backend", "mock", "--yes", "--out", str(out)]) == 0
    assert selftest.main(["--backend", "mock", "--yes", "--strict", "--out", str(tmp_path / "b")]) == 2
