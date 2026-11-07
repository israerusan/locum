"""Review 8: self-healing never picks the opposite action, compat fail-safe, CSV injection,
trace file names, dry-run verification steps, LOCUM_BACKEND for selftest / the pytest plugin."""

import csv
import os

import pytest

import locum.compat.pyautogui as pg
from locum import selftest
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import ElementNotFound
from locum.heal import name_similarity
from locum.llm.safety import Policy
from locum.pytest_plugin import _backend_name, trace_path_for
from locum.rpa.flow import run_flow
from locum.rpa.queue import WorkQueue


def _desk(be, **kw):
    kw.setdefault("default_timeout", 0)
    return Desktop(be, sleep=lambda s: None, **kw)


# ---------------------------------------------------------------- 1. heal: opposite actions

@pytest.mark.parametrize("names, selector", [
    (["Don't Save", "Cancel"], 'button "Save"'),
    (["Do not submit", "Back"], 'button "Submit"'),
    (["Cancel order", "Help"], 'button "Order"'),
    (["Save", "Don't Save", "Cancel"], 'button "Save changes"'),
    (["Nicht speichern", "Abbrechen"], 'button "Speichern"'),
    (["No guardar", "Ayuda"], 'button "Guardar"'),
    (["Ne pas enregistrer", "Aide"], 'button "Enregistrer"'),
    (["Unlock", "Help"], 'button "Lock"'),
    (["Save", "Help"], 'button "Don\'t Save"'),
])
def test_heal_refuses_negated_or_cancelling_candidates(names, selector):
    be = MockBackend(800, 600)
    w = be.add_window("Editor", (0, 0, 600, 400), app="ed")
    for i, n in enumerate(names):
        w.add_button(n, (10 + i * 120, 300, 100, 30))
    with pytest.raises(ElementNotFound):
        _desk(be).find(selector, heal=True)


def test_heal_refuses_negated_candidate_even_with_the_same_automation_id():
    be = MockBackend(800, 600)
    w = be.add_window("Editor", (0, 0, 600, 400))
    w.add_button("Don't Save", (10, 300, 100, 30), automation_id="save")
    with pytest.raises(ElementNotFound):
        _desk(be).find('button "Save"', heal=True, fingerprint={"role": "button", "name": "Save", "automation_id": "save"})


def test_flow_does_not_pass_by_clicking_dont_save():
    be = MockBackend(800, 600)
    w = be.add_window("Notepad", (0, 0, 600, 400), app="notepad")
    hit = []
    w.add_button("Don't Save", (10, 300, 100, 30), on_click=lambda *a: hit.append("dont"))
    w.add_button("Cancel", (200, 300, 100, 30), on_click=lambda *a: hit.append("cancel"))
    rep = run_flow({"settings": {"heal": True, "timeout": 0}, "steps": [{"click": 'window "Notepad" button "Save"'}]},
                   desktop=_desk(be))
    assert not rep.ok and hit == []


def test_name_similarity_gives_no_bonus_for_added_words():
    assert name_similarity("Save", "Don't Save") == 0.0
    assert name_similarity("Order", "Cancel order", partial=True) == 0.0
    assert name_similarity("Save changes", "Save") < 0.7
    assert name_similarity("Save", "Save as") >= 0.7  # a plain rename still heals


# ---------------------------------------------------------------- 2. compat fail-safe

@pytest.fixture
def compat_settings():
    saved = {k: getattr(pg, k) for k in ("PAUSE", "FAILSAFE", "FAILSAFE_POINTS", "_SLEEP")}
    pg._SLEEP = lambda s: None
    pg.PAUSE = 0.0
    old = pg.set_desktop(None)
    try:
        yield
    finally:
        pg.set_desktop(old)
        for k, v in saved.items():
            setattr(pg, k, v)


def test_compat_never_weakens_a_user_desktop_failsafe(compat_settings):
    be = MockBackend(width=800, height=600)
    d = Desktop(be, sleep=lambda s: None, pause=0.0)
    corners = sorted(tuple(c) for c in d.failsafe.corners)
    assert len(corners) == 4
    pg.set_desktop(d)
    pg.FAILSAFE = True
    pg.FAILSAFE_POINTS = [(0, 0)]
    pg.position()
    assert set(corners) <= {tuple(c) for c in d.failsafe.corners}
    be.mouse_move(799, 599)
    with pytest.raises(pg.FailSafeException):
        pg.click()
    pg.FAILSAFE = False
    pg.position()
    assert d.failsafe.enabled  # FAILSAFE = False only drops the compat points
    with pytest.raises(pg.FailSafeException):
        pg.click()
    pg.FAILSAFE = True
    pg.FAILSAFE_POINTS = [(5, 5)]
    pg.position()
    pg.set_desktop(None)  # handing it back restores what its owner configured
    assert d.failsafe.enabled
    assert sorted(tuple(c) for c in d.failsafe.corners) == corners
    assert d.failsafe._explicit is None


def test_compat_settings_still_apply_on_its_own_desktop(compat_settings, monkeypatch):
    from locum import backends

    monkeypatch.setenv("LOCUM_BACKEND", "mock")
    old_backend = backends._default
    backends.set_backend(None)
    try:
        d = pg.get_desktop()
        pg.FAILSAFE = False
        pg.position()
        assert d.failsafe.enabled is False
        pg.FAILSAFE = True
        pg.FAILSAFE_POINTS = [(3, 3)]
        pg.position()
        assert d.failsafe.enabled and [tuple(c) for c in d.failsafe.corners] == [(3, 3)]
    finally:
        backends.set_backend(old_backend)


# ---------------------------------------------------------------- 3. CSV formula injection

def test_export_csv_neutralizes_formulas(tmp_path):
    q = WorkQueue("x")
    q.add({"vendor": '=HYPERLINK("http://evil",1)', "amt": "+1+cmd|' /C calc'!A0", "n": -5,
           "@at": "@SUM(A1)", "tab": "\tx", "cr": "\rx", "dash": "-2+A1", "num": "-2", "ok": "plain"}, reference="=ref")
    out = q.export_csv(tmp_path / "out.csv")
    with open(out, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    r = rows[0]
    assert r["vendor"] == '\'=HYPERLINK("http://evil",1)'
    assert r["amt"].startswith("'+")
    assert r["'@at"] == "'@SUM(A1)"
    assert r["tab"] == "'\tx" and r["cr"] == "'\rx" and r["dash"] == "'-2+A1"
    assert r["n"] == "-5" and r["num"] == "-2" and r["ok"] == "plain"  # a plain number is no formula
    assert r["reference"] == "'=ref"


# ---------------------------------------------------------------- 4. trace file names

def test_trace_paths_never_collide(tmp_path):
    ids = ["t.py::test[a/b]", "t.py::test[a b]", "t.py::test[a:b]", "t.py::test_A", "t.py::test_a"]
    names = [os.path.basename(trace_path_for(i, str(tmp_path))).casefold() for i in ids]
    assert len(set(names)) == len(ids)
    assert trace_path_for(ids[0], "d") == trace_path_for(ids[0], "d")


# ---------------------------------------------------------------- 5. dry-run verification steps

def test_dry_run_skips_verification_of_simulated_input():
    be = MockBackend(800, 600)
    w = be.add_window("App", (0, 0, 600, 400), app="app")
    edit = w.add_edit("", (10, 10, 100, 20), automation_id="vendor")
    w.add_button("Save", (10, 300, 100, 30), automation_id="save")
    doc = {"name": "dry", "steps": [
        {"assert_exists": "#vendor"},
        {"type": {"text": "abc", "into": "#vendor"}},
        {"assert_exists": {"target": "#vendor", "equals": "abc"}},
        {"click": "#save"},
        {"wait": {"target": "text#status", "timeout": 0}},
    ]}
    rep = run_flow(doc, desktop=_desk(be), policy=Policy.default(dry_run=True))
    assert rep.ok, rep.summary()
    assert [s.status for s in rep.steps] == ["passed", "passed", "skipped", "passed", "skipped"]
    assert "dry-run" in rep.steps[2].data["skipped"]
    assert edit.value == ""


def test_dry_run_still_fails_a_precondition_and_real_runs_still_fail():
    be = MockBackend(800, 600)
    w = be.add_window("App", (0, 0, 600, 400), app="app")
    w.add_edit("", (10, 10, 100, 20), automation_id="vendor")
    pre = {"steps": [{"assert_exists": "#missing"}, {"type": {"text": "abc", "into": "#vendor"}}]}
    assert not run_flow(pre, desktop=_desk(be), policy=Policy.default(dry_run=True)).ok
    real = {"steps": [{"type": {"text": "abc", "into": "#vendor"}},
                      {"assert_exists": {"target": "#vendor", "equals": "zzz"}}]}
    assert not run_flow(real, desktop=_desk(be), policy=Policy.default()).ok


# ---------------------------------------------------------------- 6. selftest LOCUM_BACKEND=mock

def test_selftest_env_mock_backend_uses_the_simulated_editor(monkeypatch, tmp_path):
    class Picked(Exception):
        pass

    def fake_mock_desktop():
        raise Picked()

    monkeypatch.setenv("LOCUM_BACKEND", "mock")
    monkeypatch.setattr(selftest, "_mock_desktop", fake_mock_desktop)
    with pytest.raises(Picked):
        selftest.main(["--yes", "--out", str(tmp_path / "st")])


# ---------------------------------------------------------------- 7. pytest plugin "demo"

def test_pytest_plugin_accepts_demo_backend(monkeypatch):
    class Config:
        def getoption(self, name):
            return "demo"

        def getini(self, name):
            return ""

    assert _backend_name(Config()) == "mock"
