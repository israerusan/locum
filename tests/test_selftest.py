"""The real-desktop self-test, exercised on the simulated desktop."""

from __future__ import annotations

from pypilot import selftest


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
    assert status["unicode typing (raw keystrokes)"] == "SKIP"
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
