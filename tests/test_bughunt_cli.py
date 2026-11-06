"""Regression tests for bug-hunt findings in selftest.py, cli.py and compat/pyautogui.py (mock backend only)."""

from __future__ import annotations

import fractions
import json

import numpy as np
import pytest

import locum.compat.pyautogui as pg
from locum import cli, selftest
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import FailSafeTriggered


# ---------------------------------------------------------------- selftest abort
@pytest.mark.parametrize("exc", [FailSafeTriggered("cursor in corner"), KeyboardInterrupt()])
def test_selftest_abort_is_not_reported_as_passed(tmp_path, monkeypatch, capsys, exc):
    def boom(self):
        raise exc

    monkeypatch.setattr(selftest.SelfTest, "c_screenshot", boom)
    out = tmp_path / "st"
    rc = selftest.main(["--backend", "mock", "--yes", "--out", str(out)])
    text = capsys.readouterr().out
    assert rc != 0
    assert "ALL PASSED" not in text and "RESULT: ABORTED" in text
    # the aborted check is in the written report
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    names = {c["name"]: c for c in data["checks"]}
    assert names["screenshot"]["status"] == "FAIL" and "aborted" in names["screenshot"]["detail"]
    assert "mouse movement" not in names
    assert data["summary"]["FAIL"] == 1


def test_selftest_clean_mock_run_still_passes(tmp_path, capsys):
    assert selftest.main(["--backend", "mock", "--yes", "--out", str(tmp_path / "st")]) == 0
    assert "RESULT: ALL PASSED" in capsys.readouterr().out


# ---------------------------------------------------------------- cli _unescape
def test_unescape_lone_backslash_u_is_kept():
    assert cli._unescape(r"C:\users\me\n") == "C:\\users\\me\n"
    assert cli._unescape(r"\update") == "\\update"
    assert cli._unescape(r"\u12") == "\\u12"
    assert cli._unescape("trail\\u") == "trail\\u"
    # full escapes still work
    assert cli._unescape(r"\u00e9\t") == "\u00e9\t"


# ---------------------------------------------------------------- pyautogui numeric x
@pytest.fixture
def pgenv():
    be = MockBackend(width=800, height=600)
    d = Desktop(be, sleep=lambda s: None, pause=0.0, default_timeout=0.0)
    saved_pause = pg.PAUSE
    pg.PAUSE = 0.0
    old = pg.set_desktop(d)
    be.mouse_move(400, 300)
    try:
        yield be
    finally:
        pg.set_desktop(old)
        pg.PAUSE = saved_pause


@pytest.mark.parametrize(
    "x, want", [(np.int64(50), 50), (np.float32(60.4), 60), (fractions.Fraction(70), 70), (np.float64(10.6), 11)]
)
def test_pyautogui_lone_numeric_x_accepts_real_numbers(pgenv, x, want):
    pg.moveTo(x)
    assert tuple(pg.position()) == (want, 300)


def test_pyautogui_lone_non_numeric_x_still_rejected(pgenv):
    with pytest.raises(TypeError):
        pg.moveTo(object())
