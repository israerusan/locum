"""The Calculator and Paint demos run end to end on their built-in mock apps (never the real desktop)."""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(f"_t_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _mock_only(monkeypatch):
    monkeypatch.setenv("LOCUM_BACKEND", "mock")  # belt and braces: nothing may reach the real backend


calc = _load("examples/calculator_selectors.py")
paint = _load("examples/paint_drawing.py")


@pytest.mark.parametrize("expr, result", [("7+8", "15"), ("12+30", "42"), ("2+3*4", "20"),
                                          ("10/4", "2.5"), ("5-8", "-3"), ("9/0", "Cannot divide by zero")])
def test_calculator_selectors_compute_on_the_mock(expr, result, capsys):
    assert calc.main(["--backend", "mock", "--expr", expr]) == 0
    assert capsys.readouterr().out.strip() == f"{expr} = {result}"


def test_calculator_mock_matches_the_real_apps_selectors():
    with calc.build_mock_desktop() as d:
        calc.run(d, "7+8")
        assert d.backend.launched == ["calc.exe"]
        win = d.window("Calculator")
        assert win.find("#CalculatorResults").name == "Display is 15"
        for key, (aid, name) in calc.BUTTONS.items():
            el = win.find(calc.button_selector(key))
            assert (el.automation_id, el.name) == (aid, name)
        assert win.find('button "Seven"').automation_id == "num7Button"
        assert not [e for e in d.backend.events if e[0] in ("type", "key_down", "set_clipboard")]


def test_paint_demo_draws_with_drags_on_the_canvas(capsys):
    with paint.build_mock_desktop() as d:
        assert paint.run(d) == 6  # five star edges + one spiral stroke
        be = d.backend
        downs = [e for e in be.events if e[0] == "down"]
        ups = [e for e in be.events if e[0] == "up"]
        assert len(downs) == len(ups) == 6 and not be.pressed_buttons
        canvas = be.canvas.screen_rect
        assert all(canvas.contains((e[2], e[3])) for e in downs + ups)
        assert len(be.segments) > 100
        # The strokes really render: the canvas is no longer blank.
        shot = d.screenshot(canvas)
        assert shot.getextrema() != ((255, 255), (255, 255), (255, 255))
    assert paint.main(["--backend", "mock"]) == 0
    assert capsys.readouterr().out.startswith("performed 6 drags")


def test_paint_canvas_fallback_uses_the_client_area():
    with paint.build_mock_desktop() as d:
        d.launch("mspaint.exe")
        d.backend.canvas.name = "Drawing"  # no selector matches any more
        win = d.window("*Paint*")
        area = paint.find_canvas(win)
        assert win.rect.contains(area.center) and area.top > win.rect.top + 50


@pytest.mark.parametrize("script", ["calculator_selectors.py", "paint_drawing.py"])
def test_demo_scripts_run_as_subprocesses_in_mock_mode(script):
    proc = subprocess.run([sys.executable, str(ROOT / "examples" / script), "--backend", "mock"],
                          capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()


def test_demo_gif_tool_records_the_mock_runs(tmp_path):
    from PIL import Image

    tool = _load("tools/make_demo_gif.py")
    out = tmp_path / "demo.gif"
    assert tool.main([str(out), "--width", "320"]) == 0
    with Image.open(out) as im:
        assert im.n_frames > 10 and im.width == 320
