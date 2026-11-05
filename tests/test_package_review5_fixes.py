"""Regression tests: agent end_turn success, strict CSV queue loads, image targets in
waits/conditions, off-screen points and hit-tested move/drag/scroll on elements."""

from __future__ import annotations

import pytest

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import ActionTimeout, ElementNotFound
from locum.llm.agent import Agent, ScriptedClient, scripted_response, tool_use_block
from locum.llm.safety import Policy
from locum.rpa import WorkQueue
from locum.rpa.flow import run_flow
from locum.types import Monitor, Point, Rect


def _noop(_s):
    return None


# ------------------------------------------------- agent: end_turn is not "done"


@pytest.fixture
def pay_desk():
    be = MockBackend()
    w = be.add_window("Bank", app="bank")
    pay = w.add_button("Pay", (10, 10, 80, 30))
    ok = w.add_button("OK", (10, 60, 80, 30))
    return Desktop(be, sleep=_noop, default_timeout=0, failsafe=False), pay, ok


def _declining():
    return Policy(confirm=lambda desc, ctx: False)


def test_end_turn_after_blocked_payment_is_not_success(pay_desk):
    d, pay, _ = pay_desk
    c = ScriptedClient([
        scripted_response(tool_use_block("click", {"selector": 'button "Pay"'})),
        scripted_response("The click on Pay was blocked by the policy, so the payment was NOT made."),
    ])
    r = Agent(d, client=c, policy=_declining(), initial_observation=False).run("Pay invoice 4711")
    assert pay.clicks == 0
    assert r.steps[0].tool_results[0].is_error
    assert r.success is False and r.stop_reason == "end_turn"


def test_end_turn_after_block_then_harmless_call_is_not_success(pay_desk):
    d, pay, ok = pay_desk
    c = ScriptedClient([
        scripted_response(tool_use_block("click", {"selector": 'button "Pay"'})),
        scripted_response(tool_use_block("click", {"selector": 'button "OK"'})),
        scripted_response("Could not pay; closed the dialog."),
    ])
    r = Agent(d, client=c, policy=_declining(), initial_observation=False).run("Pay invoice 4711")
    assert pay.clicks == 0 and ok.clicks == 1
    assert r.success is False and r.stop_reason == "end_turn"


def test_end_turn_after_failed_last_call_is_not_success(pay_desk):
    d, _, _ = pay_desk
    c = ScriptedClient([
        scripted_response(tool_use_block("click", {"element_id": 999})),
        scripted_response("I could not find the button."),
    ])
    r = Agent(d, client=c, policy=Policy.permissive(), initial_observation=False).run("Click Missing")
    assert r.success is False and r.stop_reason == "end_turn"


def test_end_turn_after_clean_last_turn_is_success(pay_desk):
    d, _, ok = pay_desk
    c = ScriptedClient([
        scripted_response(tool_use_block("click", {"selector": 'button "OK"'})),
        scripted_response("Clicked OK."),
    ])
    r = Agent(d, client=c, policy=Policy.permissive(), initial_observation=False).run("Click OK")
    assert ok.clicks == 1 and r.success is True and r.stop_reason == "end_turn"


# --------------------------------------------------- queue: strict CSV loading


def test_load_csv_rejects_rows_with_wrong_field_count(tmp_path):
    p = tmp_path / "pay.csv"
    p.write_text("invoice,vendor,amount\n4710,Initech,99.00\n4711,ACME,1,250.00\n", encoding="utf-8")
    q = WorkQueue("pay")
    with pytest.raises(ValueError, match=r"pay\.csv:3: expected 3 field\(s\).*got 4.*1,234\.56"):
        q.load_csv(p)
    assert len(q) == 0  # nothing queued, not even the good row before it
    p.write_text("invoice,vendor,amount\n4712,Globex\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r":2: expected 3 field\(s\).*got 2"):
        q.load(p)
    assert len(q) == 0


def test_load_csv_strict_false_and_blank_rows(tmp_path):
    p = tmp_path / "pay.csv"
    p.write_text("invoice,vendor,amount\n4711,ACME,1,250.00\n,,\n\n4712,Globex\n", encoding="utf-8")
    items = WorkQueue("pay").load_csv(p, strict=False)
    assert [i.payload for i in items] == [
        {"invoice": "4711", "vendor": "ACME", "amount": "1"},
        {"invoice": "4712", "vendor": "Globex", "amount": ""},
    ]
    p.write_text('invoice,vendor,amount\n4711,ACME,"1,250.00"\n,,\n', encoding="utf-8")
    items = WorkQueue("pay2").load_csv(p)
    assert [i.payload["amount"] for i in items] == ["1,250.00"]


# ---------------------------------------- image targets in flows and Desktop


@pytest.fixture
def warn(tmp_path):
    be = MockBackend()
    w = be.add_window("Bank", app="bank")
    w.add_button("OK", (10, 10, 80, 30))
    d = Desktop(be, sleep=_noop, default_timeout=0, failsafe=False)
    img = tmp_path / "warn.png"
    d.screenshot().crop((100, 120, 400, 220)).save(str(img))
    assert d.locate(str(img)) is not None
    return d, tmp_path, img


def _run(d, base, steps):
    r = run_flow({"steps": steps}, desktop=d, base_dir=str(base))
    return r, [entry["message"] for entry in r.logs]


def test_flow_image_target_in_conditions_and_waits(warn):
    d, base, _ = warn
    r, logs = _run(d, base, [{"if_exists": {"target": "warn.png", "then": [{"log": "THEN"}], "else": [{"log": "ELSE"}]}}])
    assert r.ok and "THEN" in logs and "ELSE" not in logs
    r, logs = _run(d, base, [{"wait_gone": {"target": "warn.png", "timeout": 0}}, {"log": "continued"}])
    assert not r.ok and "continued" not in logs  # still on screen: must not pass
    assert "still on screen" in r.steps[0].error
    r, _ = _run(d, base, [{"assert_exists": "warn.png"}])
    assert r.ok
    r, _ = _run(d, base, [{"wait": "warn.png"}])
    assert r.ok


def test_flow_missing_image_target_is_an_error_not_a_selector(warn):
    d, base, _ = warn
    for step in ({"if_exists": {"target": "missing.png", "then": [{"log": "THEN"}], "else": [{"log": "ELSE"}]}},
                 {"wait_gone": "missing.png"}, {"assert_exists": "missing.png"}, {"click": "missing.png"}):
        r, logs = _run(d, base, [step])
        assert not r.ok and "ELSE" not in logs
        assert "not found" in r.steps[0].error and "missing.png" in r.steps[0].error


def test_desktop_exists_wait_find_with_images(warn, tmp_path, monkeypatch):
    d, _, img = warn
    assert d.exists(str(img)) is True and d.exists(img) is True
    assert d.wait(str(img)).confidence > 0.9
    with pytest.raises(ActionTimeout):
        d.wait(str(img), state="gone", timeout=0)
    with pytest.raises(TypeError, match="is an image"):
        d.find(str(img))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        d.exists("spinner.png")
    with pytest.raises(FileNotFoundError):
        d.wait("spinner.png", state="gone")
    # Once the window is gone, so is the image.
    d.backend.remove_window(d.backend.windows[0])
    assert d.exists(str(img)) is False
    assert d.wait(str(img), state="gone", timeout=0) is None


# ----------------------------------------------------- off-screen coordinates


def test_points_off_every_monitor_are_refused():
    be = MockBackend(width=1280, height=800)
    d = Desktop(be, sleep=_noop, failsafe=False)
    d.move((640, 400))
    for action in (lambda: d.click((2500, 400)), lambda: d.click(2500, 400), lambda: d.move((-300, 400)),
                   lambda: d.drag((10, 10), (9999, 10)), lambda: d.scroll(-3, at=(5000, 5)),
                   lambda: d.click((1270, 400), offset=(20, 0))):
        with pytest.raises(ValueError, match="off-screen"):
            action()
    assert d.position() == Point(640, 400)
    assert not [e for e in be.events if e[0] in ("down", "up", "scroll")]
    assert d.click((1279, 799)) == Point(1279, 799)


def test_offscreen_opt_out_and_second_monitor():
    be = MockBackend(width=1280, height=800)
    be._monitors.append(Monitor(1, Rect(-1920, 0, 1920, 1080), 1.0, False, "MOCK1"))
    d = Desktop(be, sleep=_noop, failsafe=False)
    assert d.click((-300, 400)) == Point(-300, 400)
    d.move((10, 10))
    d.move((5000, 10), allow_offscreen=True)  # explicitly allowed: the OS clamps it


# ------------------------------------ move/drag/scroll on covered elements


@pytest.fixture
def covered():
    be = MockBackend()
    ledger = be.add_window("Ledger", rect=(100, 100, 640, 480), app="excel")
    ledger.add_button("Row 42", (10, 10, 120, 30))
    bank = be.add_window("Bank portal", rect=(50, 50, 400, 300), app="chrome")  # in front, covers Row 42
    bank.add_button("Send wire", (60, 60, 200, 40))
    d = Desktop(be, sleep=_noop, default_timeout=0, failsafe=False)
    el = d.find('button "Row 42"')
    assert be.widget_at(*el.center).name == "Send wire"
    return be, d, el


def _front(be):
    return be.list_windows()[0].title


def test_drag_from_covered_element_activates_its_window_first(covered):
    be, d, el = covered
    be.clear_events()
    d.drag(el, (700, 500))
    assert _front(be) == "Ledger"
    downs = [e for e in be.events if e[0] == "down"]
    assert downs and be.widget_at(downs[0][2], downs[0][3]).name == "Row 42"


def test_move_and_scroll_on_covered_element_hit_the_element(covered):
    be, d, el = covered
    d.move(el)
    assert _front(be) == "Ledger" and be.widget_at(*d.position()).name == "Row 42"
    be.activate_window([w for w in be.list_windows() if w.title == "Bank portal"][0].handle)
    be.clear_events()
    d.scroll(-3, at='button "Row 42"')
    assert _front(be) == "Ledger"
    scrolls = [e for e in be.events if e[0] == "scroll"]
    assert scrolls and be.widget_at(scrolls[0][3], scrolls[0][4]).name == "Row 42"


def test_element_that_stays_covered_is_refused(covered, monkeypatch):
    be, d, el = covered
    monkeypatch.setattr(type(el), "_activate_window", lambda self: False)
    be.clear_events()
    for action in (lambda: d.move(el), lambda: d.drag(el, (700, 500)), lambda: d.scroll(-3, at=el)):
        with pytest.raises(ElementNotFound, match="covered"):
            action()
    assert not [e for e in be.events if e[0] in ("down", "up", "scroll", "move")]
