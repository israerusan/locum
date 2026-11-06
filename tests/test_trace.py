"""locum.trace: step recording, redaction, restore, HTML/JSON output (mock backend only)."""

import base64
import io
import json
import logging

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend, MockNode
from locum.desktop import Desktop as DesktopClass
from locum.errors import ElementNotFound
from locum.trace import MASK, Tracer, trace


@pytest.fixture
def be():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("Login", (100, 100, 500, 400), app="erp")
    w.add_button("OK", (20, 300, 80, 30), automation_id="okBtn")
    w.add_edit("User", (20, 50, 200, 28))
    w.add_edit("Password", (20, 100, 200, 28), attrs={"password": True})
    return be


@pytest.fixture
def d(be):
    return Desktop(be, failsafe=False, default_timeout=0.05, sleep=lambda s: None)


@pytest.fixture
def password_states(monkeypatch):
    orig = MockNode.states
    monkeypatch.setattr(MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))


def test_records_actions_targets_and_results(d):
    with Tracer(d) as t:
        d.click('button "OK"')
        d.find('edit "User"').type("ada")
        d.window("Login").activate()
    methods = [s.method for s in t.steps]
    # Internal calls (click -> find, type -> focused) are not separate steps.
    assert methods == ["click", "find", "element.type", "window", "window.activate"]
    click = t.steps[0]
    assert click.status == "passed" and click.error is None
    assert click.target["selector"] == 'button "OK"'
    assert click.target["element"].startswith('button "OK"')
    assert click.target["bounds"] == [120, 400, 80, 30]
    assert click.target["point"] == [160, 415]
    assert click.result == "(160, 415)"
    assert click.duration >= 0 and click.started_at > 0
    assert t.steps[2].args == {"text": "'ada'"}
    assert t.steps[3].target["bounds"] == [100, 100, 500, 400]
    assert [s.index for s in t.steps] == list(range(5))
    assert not t.failed


def test_failed_step_records_error_and_reraises(d):
    with Tracer(d) as t:
        with pytest.raises(ElementNotFound):
            d.click('button "Nope"')
    (step,) = t.steps
    assert step.status == "failed" and t.failed
    assert step.error["type"] == "ElementNotFound"
    assert "Nope" in step.error["message"] and "Traceback" in step.error["traceback"]
    assert step.target == {"selector": 'button "Nope"'}


def test_stop_restores_the_original_methods_exactly(d):
    before = dict(vars(d))
    t = Tracer(d).start()
    assert "click" in vars(d) and d.click.__wrapped__.__func__ is DesktopClass.click
    el = d.find('button "OK"')
    assert "click" in vars(el)
    t.stop()
    assert vars(d) == before
    assert d.click.__func__ is DesktopClass.click
    assert "click" not in vars(el)
    d.click('button "OK"')  # untraced now
    assert len(t.steps) == 1
    t.stop()  # idempotent


def test_restores_a_preexisting_instance_attribute(d):
    def custom(*a, **k):
        return "custom"

    d.press = custom
    with Tracer(d):
        assert d.press("a") == "custom"
    assert d.press is custom


def test_only_one_active_tracer_per_desktop(d):
    with Tracer(d):
        with pytest.raises(RuntimeError):
            Tracer(d).start()
    with Tracer(d) as t2:  # fine once the first stopped
        d.press("a")
    assert len(t2.steps) == 1


def test_screenshot_modes(d):
    with Tracer(d, max_width=320) as t:
        d.press("a")
    shot = t.steps[0].screenshot
    assert shot.startswith("data:image/jpeg;base64,")
    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(shot.split(",", 1)[1])))
    assert img.size == (320, 200)  # downscaled from 1280x800
    assert t.steps[0].screen == [0, 0, 1280, 800]

    with Tracer(d, screenshots="errors") as t:
        d.press("a")
        with pytest.raises(ElementNotFound):
            d.find('button "Nope"')
    assert t.steps[0].screenshot is None and t.steps[1].screenshot

    with Tracer(d, screenshots=False, before=True) as t:
        d.press("a")
    assert t.steps[0].screenshot is None and t.steps[0].screenshot_before is None

    with Tracer(d, before=True) as t:
        d.press("a")
    assert t.steps[0].screenshot_before and t.steps[0].screenshot

    with pytest.raises(ValueError):
        Tracer(d, screenshots="sometimes")


def test_password_text_is_masked_and_scrubbed(d, password_states):
    with Tracer(d) as t:
        pw = d.find('edit "Password"')
        pw.type("hunter22")
        d.click('edit "Password"')
        d.type("hunter22")
        logging.getLogger("locum.heal").warning("could not verify 'hunter22'")
        d.clipboard_set("clip-secret")
    data = t.to_json(screenshots=False)
    assert "hunter22" not in data and "clip-secret" not in data
    assert t.steps[1].args["text"] == MASK
    assert t.steps[3].args["text"] == MASK
    assert t.steps[4].args["text"] == MASK
    assert "hunter22" not in t.to_html()


def test_non_password_typing_is_kept_and_redact_masks_strings(d):
    with Tracer(d, redact=["s3cr3t"]) as t:
        d.click('edit "User"')
        d.type("ada s3cr3t")
    assert t.steps[1].args["text"] == "'ada ***'"


def test_log_records_become_annotations(d, be):
    heal = logging.getLogger("locum.heal")
    # Emitted while the click step runs (as a self-healing lookup would).
    be.windows[0].add_button("Heal", (300, 300, 80, 30),
                             on_click=lambda w: heal.info("healed %r -> %r", "button 'Okay'", 'button "Heal"'))
    with Tracer(d) as t:
        d.click('button "Heal"')
        heal.info("outside any step")
        logging.getLogger("locum.heal").debug("below the capture level")
    assert len(t.steps[0].annotations) == 1
    note = t.steps[0].annotations[0]
    assert note["logger"] == "locum.heal" and note["level"] == "INFO" and "healed" in note["message"]
    assert t.logs == [dict(t.logs[0], message="outside any step", after_step=0)]
    page = t.to_html()
    assert "healed" in page and 'class="chip note">healed' in page
    # The locum logger level is restored.
    assert logging.getLogger("locum").level == logging.NOTSET


def test_html_is_self_contained_and_escaped(d, be):
    be.windows[0].add_button("<script>alert(1)</script>", (300, 300, 80, 30))
    with Tracer(d, title="T <b>") as t:
        d.click('button "<script>alert(1)</script>"')
        with pytest.raises(ElementNotFound):
            d.find('button "<img src=x onerror=alert(1)>"')
    page = t.to_html()
    assert page.startswith("<!DOCTYPE html>")
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "T &lt;b&gt;" in page
    assert "http://" not in page and "https://" not in page  # no external assets
    assert page.count('<section class="detail"') == 2
    assert 'class="failed"' in page and 'class="box"' in page and 'class="dot"' in page
    assert "data:image/jpeg;base64," in page


def test_save_json_html_and_trace_context_manager(d, tmp_path):
    with Tracer(d) as t:
        d.press("a")
    data = json.loads(open(t.save_json(tmp_path / "t.json"), encoding="utf-8").read())
    assert data["counts"] == {"total": 1, "passed": 1, "failed": 0}
    assert data["steps"][0]["method"] == "press" and data["steps"][0]["screenshot"]
    assert data["backend"] == "mock"
    assert "screenshot" not in t.to_dict(screenshots=False)["steps"][0]

    out = tmp_path / "sub" / "run.html"
    with pytest.raises(ElementNotFound):
        with trace(d, out):
            d.find('button "Nope"')
    assert out.exists() and "ElementNotFound" in out.read_text(encoding="utf-8")
    assert "find" not in vars(d)

    with trace(d, tmp_path / "ok.json", screenshots=False) as t:
        d.press("b")
    assert json.loads((tmp_path / "ok.json").read_text(encoding="utf-8"))["steps"][0]["args"]["keys"] == "['b']"


def test_find_all_elements_and_window_lookups_are_instrumented(d):
    with Tracer(d, screenshots=False) as t:
        edits = d.find_all("edit")
        edits[0].click()
        w = d.window("Login")
        w.find('button "OK"').click()
    assert [s.method for s in t.steps] == ["find_all", "element.click", "window", "window.find", "element.click"]
    assert t.steps[0].result.startswith("[edit")
    assert t.steps[4].target["element"].startswith('button "OK"')
