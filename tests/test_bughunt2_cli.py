"""Regression tests for bug-hunt round 2 findings in locum.cli (demo backend / fakes only)."""
import io

import numpy as np
import pytest
from PIL import Image

from locum import cli
from locum.backends.mock import MockBackend
from locum.desktop import Desktop


def _desk(be):
    return Desktop(be, sleep=lambda s: None, pause=0.0, default_timeout=0.0)


def test_agent_aborted_by_failsafe_exits_4():
    from locum.llm.agent import ScriptedClient, scripted_response, tool_use_block

    be = cli.DemoBackend()
    d = _desk(be)
    client = ScriptedClient([
        scripted_response("Clicking.", tool_use_block("click", {"selector": 'button "Save"'})),
        scripted_response(tool_use_block("done", {"summary": "ok", "success": True})),
    ])
    be.mouse_move(0, 0)  # pointer in the fail-safe corner
    rc = cli.main(["agent", "save it", "--no-confirm", "-q"], desktop=d,
                  stdout=io.StringIO(), stderr=io.StringIO(), client=client)
    assert rc == cli.EXIT_ABORTED


def test_agent_ordinary_failure_still_exits_1():
    from locum.llm.agent import ScriptedClient, scripted_response, tool_use_block

    d = _desk(cli.DemoBackend())
    client = ScriptedClient([scripted_response(tool_use_block("done", {"summary": "nope", "success": False}))])
    rc = cli.main(["agent", "x", "--no-confirm", "-q"], desktop=d,
                  stdout=io.StringIO(), stderr=io.StringIO(), client=client)
    assert rc == cli.EXIT_FAILURE


@pytest.fixture
def needle(tmp_path):
    rng = np.random.default_rng(3)
    img = Image.fromarray(rng.integers(0, 256, size=(20, 30, 3), dtype=np.uint8))
    path = tmp_path / "needle.png"
    img.save(path)
    return img, str(path)


def _late_image_desktop(img, appear_at=2):
    be = MockBackend(width=400, height=300)
    calls = {"n": 0}
    orig = be.screenshot

    def screenshot(region=None):
        calls["n"] += 1
        shot = orig(region)
        if calls["n"] >= appear_at:
            shot = shot.copy()
            off = (0, 0) if region is None else (region.left, region.top)
            shot.paste(img, (100 - off[0], 100 - off[1]))
        return shot

    be.screenshot = screenshot
    return _desk(be)


def test_locate_all_waits_for_timeout(needle):
    img, path = needle
    out = io.StringIO()
    rc = cli.main(["locate", path, "--all", "--timeout", "5"], desktop=_late_image_desktop(img),
                  stdout=out, stderr=io.StringIO())
    assert rc == cli.EXIT_OK
    assert "found (100,100 30x20)" in out.getvalue()


def test_locate_all_without_timeout_does_not_wait(needle):
    img, path = needle
    rc = cli.main(["locate", path, "--all"], desktop=_late_image_desktop(img),
                  stdout=io.StringIO(), stderr=io.StringIO())
    assert rc == cli.EXIT_FAILURE


def test_demo_spreadsheet_enter_follows_arrow_moves():
    be = cli.DemoBackend()
    d = _desk(be)
    d.window("Book1 - Excel").activate()
    d.click('edit "A1"')
    d.press("right")
    d.press("right")
    d.type("total")
    d.press("enter")
    assert be.focused_widget.name == "C2"
    assert be.cell("C1") == "total"
    d.press("right")
    d.type("x")
    d.press("enter")
    assert be.focused_widget.name == "D3"


def test_demo_spreadsheet_tab_still_returns_to_anchor():
    be = cli.DemoBackend()
    d = _desk(be)
    d.window("Book1 - Excel").activate()
    d.click('edit "B1"')
    d.type("a")
    d.press("tab")
    d.type("b")
    d.press("tab")
    d.press("enter")
    assert be.focused_widget.name == "B2"
