"""Polish pass 3 (RPA / trace / pytest plugin / secrets): memory, scrub caching, save errors, atomic writes."""

from __future__ import annotations

import gc
import logging
import re
import threading
import time

import pytest

from locum import _secret
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import FailSafeTriggered
from locum.rpa import Recorder, Robot, RunReport, StepResult, WorkQueue, run_flow
from locum.rpa import flow as flow_mod
from locum.rpa import queue as queue_mod
from locum.rpa import report as report_mod
from locum.rpa.flow import FlowStepError
from locum.trace import Tracer

pytest_plugins = ["pytester"]


def _desktop(n: int = 3) -> Desktop:
    be = MockBackend()
    w = be.add_window("Grid", (0, 0, 800, 600))
    for i in range(n):
        w.add("button", f"cell {i}", (i * 30, 10, 25, 15))
    return Desktop(be, failsafe=False, default_timeout=0.05, sleep=lambda s: None)


def _re_cache_text() -> str:
    """Every pattern held in the ``re`` module's caches, as one string."""
    out = []
    for name in ("_cache", "_cache2"):
        cache = getattr(re, name, None)
        if cache:
            out.extend(repr(k) for k in list(cache))
    return "\n".join(out)


# ------------------------------------------------------------- 1: elements held weakly by the tracer
def test_tracer_holds_returned_elements_weakly_and_forgets_collected_ids():
    d = _desktop(5)
    t = Tracer(d, screenshots=False).start()
    els = d.find_all("button")
    ids = {id(e) for e in els}
    assert ids <= t._patched_ids
    assert all(id(e) in t._weak_patched for e in els)
    assert all(obj is d for obj, *_ in t._patched)  # only the desktop is held strongly
    del els
    gc.collect()
    assert not t._weak_patched
    assert t._patched_ids == {id(d)}  # a reused id must not look instrumented already
    # A new element (whatever its id) is instrumented and its calls recorded.
    el = d.find('button "cell 1"')
    el.click()
    assert t.steps[-1].method == "element.click"
    t.stop()
    assert "click" not in vars(el)  # still alive at stop(): restored
    assert "click" not in vars(d)
    assert not t._weak_patched and not t._patched and not t._patched_ids


def test_tracer_memory_does_not_grow_with_repeated_lookups():
    d = _desktop(50)
    t = Tracer(d, screenshots=False).start()
    for _ in range(20):
        d.find_all("button")
    gc.collect()
    assert len(t._weak_patched) == 0
    assert len(t._patched) < 60  # desktop methods only (was one entry per method per element)
    assert len(t.steps) == 20
    t.stop()


def test_element_collected_while_tracing_then_stop_is_clean():
    d = _desktop(2)
    t = Tracer(d, screenshots=False).start()
    import weakref

    ref = weakref.ref(d.find('button "cell 0"'))
    gc.collect()
    assert ref() is None
    t.stop()  # nothing to restore for a collected element; must not raise
    assert not t.active


# ------------------------------------------------------------- 2: tracer scrub pattern cached per secret set
def test_tracer_scrub_compiles_once_per_secret_set(monkeypatch):
    calls = []
    real = _secret._compile

    def counting(secrets, min_len, cached=True):
        calls.append(cached)
        return real(secrets, min_len, cached)

    monkeypatch.setattr(_secret, "_compile", counting)
    d = _desktop()
    t = Tracer(d, screenshots=False, redact=["tracer-only-s3cret"]).start()
    for i in range(5):
        d.move(i, i)
    assert t._scrub("x tracer-only-s3cret y") == "x *** y"
    assert len(calls) == 1 and calls == [False]  # one uncached compile for all steps
    d.clipboard_set("another-s3cret-value")  # a new tracer secret: rebuilt once
    assert t._scrub("another-s3cret-value and tracer-only-s3cret") == "*** and ***"
    assert len(calls) == 2
    t.stop()
    assert "tracer-only-s3cret" not in _re_cache_text()
    assert "another-s3cret-value" not in _re_cache_text()


def test_tracer_scrub_output_matches_the_generic_scrub():
    d = _desktop()
    t = Tracer(d, screenshots=False, redact=["dom\\pw-123", 'quo"te-pw', "ab"]).start()
    _secret.register("registry-only-pw")
    try:
        for text in ["plain", "'dom\\\\pw-123' and quo\\\"te-pw", "quo&quot;te-pw registry-only-pw ab", ""]:
            assert t._scrub(text) == _secret.scrub(text, tuple(t._secrets), min_len=3)
        # The registry changing invalidates the cached pattern too.
        _secret.register("later-registered-pw")
        assert t._scrub("x later-registered-pw") == "x ***"
    finally:
        t.stop()


# ------------------------------------------------------------- 3: scrub(extra) leaves nothing in re's cache
def test_scrub_with_extra_does_not_cache_the_secret_in_re():
    re.purge()
    assert _secret.scrub("user typed per-call-pw-998877 here", ["per-call-pw-998877"]) == "user typed *** here"
    assert _secret.scrub('json "per-call-pw-998877"', ("per-call-pw-998877",)) == 'json "***"'
    assert "per-call-pw-998877" not in _re_cache_text()


# ------------------------------------------------------------- 4: pytest plugin drops the tracer at teardown
def test_plugin_removes_the_tracer_from_the_item_after_teardown(pytester):
    pytester.makeconftest(
        """
        import json

        def pytest_sessionfinish(session):
            left = [item.nodeid for item in session.items if hasattr(item, "_locum_tracer")]
            with open("left.json", "w") as fh:
                json.dump(left, fh)
        """
    )
    pytester.makepyfile(
        """
        def test_ok(locum_desktop, locum_tracer):
            assert locum_tracer is not None and locum_tracer.active

        def test_fails(locum_desktop):
            locum_desktop.find('button "Missing"', timeout=0)
        """
    )
    for mode, saved in (("failed", 1), ("on", 2)):
        result = pytester.runpytest("--locum-backend", "mock", "-p", "no:cacheprovider", "--locum-trace", mode,
                                    "--locum-trace-dir", mode)
        result.assert_outcomes(passed=1, failed=1)
        assert (pytester.path / "left.json").read_text() == "[]"
        assert len(list((pytester.path / mode).iterdir())) == saved  # the save decision is unchanged
    result = pytester.runpytest("--locum-backend", "mock", "-p", "no:cacheprovider")
    result.stdout.fnmatch_lines(["*locum trace*", "*trace saved:*test_fails-*.html*"])


# ------------------------------------------------------------- 5: a failed report save keeps the original error
@pytest.fixture
def blocked_run_dir(tmp_path):
    blocker = tmp_path / "runs"
    blocker.write_text("a file where the run directory should be")
    return blocker


def _robot(run_dir, name="x"):
    return Robot(Desktop(MockBackend(), failsafe=False, sleep=lambda s: None), name=name, run_dir=run_dir,
                 policy=None, screenshot_on_error=False)


def test_robot_with_block_keeps_its_exception_when_the_save_fails(blocked_run_dir, caplog):
    r = _robot(blocked_run_dir)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="the real failure"):
            with r:
                raise ValueError("the real failure")
    assert "could not save the report" in caplog.text
    assert r.report.error.startswith("ValueError")


def test_robot_failsafe_run_save_error_still_raises(blocked_run_dir):
    # A fail-safe abort *returns* the report: nothing else ends the call, so a failed save
    # raises (as in 0.5.0) instead of the evidence going missing behind a log line.
    r = _robot(blocked_run_dir, "y")

    @r.task
    def t():
        raise FailSafeTriggered("abort")

    with pytest.raises(OSError):
        r.run()


def test_robot_interrupt_in_run_still_propagates_when_the_save_fails(blocked_run_dir):
    r = _robot(blocked_run_dir, "z")

    @r.task
    def t():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        r.run()


def test_robot_save_error_without_an_exception_in_flight_still_raises(blocked_run_dir):
    r = _robot(blocked_run_dir)
    with pytest.raises(OSError):
        with r:
            pass
    r2 = _robot(blocked_run_dir)
    r2.start()
    with pytest.raises(OSError):
        r2.finish()


def test_run_flow_keeps_the_step_error_when_the_save_fails(blocked_run_dir, caplog):
    d = Desktop(MockBackend(), failsafe=False, sleep=lambda s: None, default_timeout=0)
    doc = {"steps": [{"click": 'button "Missing"'}]}
    with caplog.at_level(logging.WARNING):
        with pytest.raises(FlowStepError):
            run_flow(doc, d, run_dir=blocked_run_dir, screenshot_on_error=False, raise_on_error=True)
    assert "could not save the run report" in caplog.text
    # raise_on_error=False returns the report: nothing else ends the call, so the save error
    # still raises (as in 0.5.0) instead of the evidence going missing behind a log line.
    with pytest.raises(OSError):
        run_flow(doc, d, run_dir=blocked_run_dir, screenshot_on_error=False, raise_on_error=False)


def test_run_flow_save_error_of_a_passing_run_still_raises(blocked_run_dir):
    d = Desktop(MockBackend(), failsafe=False, sleep=lambda s: None, default_timeout=0)
    with pytest.raises(OSError):
        run_flow({"steps": [{"set_var": {"a": 1}}]}, d, run_dir=blocked_run_dir, screenshot_on_error=False)


# ------------------------------------------------------------- 6 / 10: atomic writes
def test_trace_save_uses_the_queue_atomic_write(tmp_path, monkeypatch):
    seen = []
    real = queue_mod._atomic_write
    monkeypatch.setattr(queue_mod, "_atomic_write", lambda p, text: (seen.append(p), real(p, text)))
    t = Tracer(_desktop(), screenshots=False)
    target = tmp_path / "a" / "b" / "t.json"
    assert t.save_json(target) == str(target)
    assert seen == [target] and target.is_file()
    assert [p.name for p in target.parent.iterdir()] == ["t.json"]  # no temp file left behind


def test_recorder_save_uses_the_queue_atomic_write(tmp_path, monkeypatch):
    seen = []
    real = queue_mod._atomic_write
    monkeypatch.setattr(queue_mod, "_atomic_write", lambda p, text: (seen.append(p), real(p, text)))
    rec = Recorder(Desktop(MockBackend(), failsafe=False, sleep=lambda s: None))
    out = rec.save(tmp_path / "sub" / "flow.json")
    assert seen and str(seen[0]) == out
    assert '"steps"' in (tmp_path / "sub" / "flow.json").read_text(encoding="utf-8")


# ------------------------------------------------------------- 7: heartbeat failures are logged once per streak
def test_lease_heartbeat_warns_once_per_failure_streak(tmp_path, caplog):
    q = WorkQueue("q", tmp_path / "q.json", lease_duration=0.03)
    q.add({"n": 1})
    item = q.next()
    outcomes = [OSError("disk busy"), OSError("disk busy"), None, OSError("again"), OSError("again")]
    calls = []
    done = threading.Event()

    def renew(it, duration=None):
        calls.append(1)
        if len(calls) > len(outcomes):
            done.set()
            return it
        exc = outcomes[len(calls) - 1]
        if exc is not None:
            raise exc
        return it

    q.renew = renew
    with caplog.at_level(logging.WARNING, logger="locum.queue"):
        stop = q._start_heartbeat(item)
        assert done.wait(10)
        stop()
    warnings = [r for r in caplog.records if r.name == "locum.queue"]
    assert len(warnings) == 2
    assert "could not renew the lease" in warnings[0].getMessage()


# ------------------------------------------------------------- 8: continue_on_error accepts every true word
@pytest.mark.parametrize("word", ["y", "Y", " yes ", "on", "1", "true"])
def test_continue_on_error_true_words(word):
    d = Desktop(MockBackend(), failsafe=False, sleep=lambda s: None, default_timeout=0)
    doc = {"steps": [{"click": 'button "Nope"', "continue_on_error": word}, {"set_var": {"after": 1}}]}
    report = run_flow(doc, d)
    assert report.ok, report.error
    assert report.variables.get("after") == 1


def test_truthy_uses_the_shared_true_words():
    assert all(flow_mod._truthy(w) for w in flow_mod._TRUE_WORDS)
    assert not any(flow_mod._truthy(w) for w in flow_mod._FALSE_WORDS)
    assert flow_mod._truthy("maybe") is False


# ------------------------------------------------------------- 9: one thumbnail per screenshot per page
def test_report_encodes_a_shared_screenshot_once(monkeypatch):
    calls = []
    monkeypatch.setattr(report_mod, "_thumbnail_data_url",
                        lambda p, size: (calls.append(p), "data:image/png;base64,QUJD")[1])
    r = RunReport(name="x")
    for i in range(3):
        r.add(StepResult(name=f"s{i}", status="failed", depth=2 - i, screenshot="shot.png", error="e"))
    r.add(StepResult(name="other", status="failed", screenshot="other.png", error="e"))
    page = r.to_html()
    assert calls == ["shot.png", "other.png"]
    assert page.count("data:image/png;base64,QUJD") == 4
    calls.clear()
    r.to_html()  # the cache lives for one render only
    assert calls == ["shot.png", "other.png"]


def test_report_shared_screenshot_html_is_unchanged(tmp_path):
    from PIL import Image

    p = tmp_path / "s.png"
    Image.new("RGB", (40, 30), (200, 10, 10)).save(p)
    r = RunReport(name="x", started_at=0.0, finished_at=1.0)
    for i in range(2):
        r.add(StepResult(name=f"s{i}", status="failed", screenshot=str(p), error="e"))
    thumb = report_mod._thumbnail_data_url(str(p), 480)
    assert thumb and r.to_html().count(thumb) == 2
