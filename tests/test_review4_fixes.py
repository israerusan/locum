"""Regression tests for the gaps the 2026-11-06 review fixes introduced.

1. browser_type(submit=True) whose Enter target cannot be identified (nothing found, the
   focus inside an embedded frame -- Stripe-style card fields --) is treated as unknown.
2. The recorder never downgrades a secret typing burst on a (lagging) focus probe, and
   fails closed while a later Tab/Enter/click is still queued.
-  Shared selector trees forget the windows that changed (UI Automation events) and are
   freed after each tool call.
-  Bare ``::1`` works as a DevTools host; new_page does not wait for a document that is
   never coming; set_text with an unreadable field uses LOCUM_SECRET.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

import locum.selectors as sel_mod
from locum import Desktop
from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.browser import cdp
from locum.browser.cdp import Browser, CDPError, PageInfo
from locum.llm.safety import Policy
from locum.llm.tools import DesktopToolkit
from locum.rpa.recorder import UNKNOWN_FOCUS, Recorder
from locum.selectors import parse, selector_for

SECRET = "hunter2-Secret"


# ============================================================ 1. unknown Enter targets
class _FakePage:
    def __init__(self, enter):
        self.info = type("I", (), {"id": "p1", "title": "Checkout", "url": "https://shop.example/checkout"})()
        self.title, self.url = "Checkout", "https://shop.example/checkout"
        self.enter = enter
        self.typed = []

    def resolve(self, target, scroll=True):
        return {"name": "Card number", "password": False, "tag": "input"}

    def enter_target(self, target=None):
        return self.enter

    def focused_is_password(self):
        return False

    def type(self, text, target=None, clear=False, submit=False):
        self.typed.append((text, target, submit))


class _FakeBrowser:
    alive, managed, port, name, app, pid = True, True, 9333, "edge", "msedge.exe", 4242
    version = {"Browser": "Edg/156"}

    def __init__(self, page):
        self.p = page

    def page(self, match=None):
        return self.p


def _kit(policy=None):
    be = MockBackend()
    be.add_window("Desk", (0, 0, 400, 300))
    return DesktopToolkit(Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0), policy or Policy())


@pytest.mark.parametrize("enter", [
    None,                                                         # nothing focused
    {"kind": "none", "label": "", "form": "", "action": ""},      # nothing found
    {"kind": "frame", "label": "", "form": "", "action": ""},     # focus inside a cross-origin iframe
    {"kind": "form", "label": "", "form": "", "action": ""},      # a form with no button and no action
    {"kind": "nearby", "label": "", "form": "", "action": ""},    # a nameless button
])
def test_submit_with_an_unidentified_enter_target_needs_confirmation(enter):
    kit = _kit()
    kit._browser = _FakeBrowser(_FakePage(enter))
    r = kit.call("browser_type", {"text": "4242424242424242", "submit": True})
    assert r.is_error and "Blocked by policy" in r.text
    assert kit._browser.p.typed == []


def test_submit_into_an_embedded_frame_says_why():
    kit = _kit()
    kit._browser = _FakeBrowser(_FakePage({"kind": "frame", "label": "", "form": "", "action": ""}))
    r = kit.call("browser_type", {"text": "4242", "submit": True})
    assert "embedded frame" in r.text


def test_identified_harmless_targets_still_run():
    kit = _kit(Policy(dry_run=True))
    kit._browser = _FakeBrowser(_FakePage({"kind": "nearby", "label": "Send", "form": "", "action": ""}))
    assert not kit.call("browser_type", {"text": "hi", "submit": True}).is_error


# Real browser: the page script's view of an iframe / shadow root.
def _real_browser(tmp_path_factory):
    from conftest import skip_unless_real_browser_allowed

    skip_unless_real_browser_allowed()
    name = next((n for n in ("edge", "chrome", "chromium") if cdp.find_executable(n)), None)
    if name is None:
        pytest.skip("no Chromium-based browser installed")
    try:
        return Browser.launch(name, headless=True, profile_dir=str(tmp_path_factory.mktemp("cdp-r4") / "profile"),
                              timeout=60, extra_args=["--no-sandbox"] if name != "edge" else [])
    except (CDPError, OSError) as exc:
        pytest.skip(f"{name} could not start headless here: {exc}")


@pytest.fixture(scope="module")
def real_browser(tmp_path_factory):
    b = _real_browser(tmp_path_factory)
    try:
        yield b
    finally:
        b.close(quit=True)


def _open(b, tmp_path, html, name):
    f = tmp_path / name
    f.write_text(html, encoding="utf-8")
    p = b.new_page(f.as_uri())
    p.wait_ready()
    return p


def test_real_focus_inside_a_cross_origin_frame_is_kind_frame(real_browser, tmp_path):
    inner = "<input id=cc aria-label='Card number' autofocus>"
    p = _open(real_browser, tmp_path, "<title>Pay</title><form action='/charge'><button type=submit>Pay now</button></form>"
              f"<iframe id=f src=\"data:text/html,{inner}\"></iframe>", "frame.html")
    p.evaluate("document.getElementById('f').focus(); 0")
    assert p.enter_target()["kind"] == "frame"


def test_real_focus_is_followed_into_a_shadow_root(real_browser, tmp_path):
    p = _open(real_browser, tmp_path, "<title>Shadow</title><div id=host></div><script>"
              "const r = document.getElementById('host').attachShadow({mode: 'open'});"
              "r.innerHTML = \"<form action='/charge'><input id=cc><button type=submit>Pay now</button></form>\";"
              "</script>", "shadow.html")
    p.evaluate("document.getElementById('host').shadowRoot.getElementById('cc').focus(); 0")
    t = p.enter_target()
    assert t["kind"] == "form" and t["label"] == "Pay now"


# ============================================================ 2. recorder bursts
@pytest.fixture
def login(monkeypatch):
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))
    be = MockBackend(width=800, height=600)
    w = be.add_window("Login", (0, 0, 400, 300))
    user = w.add("edit", "User", (20, 20, 200, 30))
    pwd = w.add("edit", "Password", (20, 60, 200, 30), attrs={"password": True})
    nxt = w.add("edit", "Comment", (20, 100, 200, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, user, pwd, nxt


def _outputs(rec):
    return rec.to_python() + "\n" + repr(rec.to_flow()) + "\n" + repr([a.to_dict() for a in rec.actions])


def test_a_secret_burst_is_not_downgraded_by_a_late_focus_probe(login):
    be, d, _user, pwd, nxt = login
    be._set_focus(pwd)
    rec = Recorder(d).start(source="manual")
    rec.feed(("type", SECRET[:3]))
    be._set_focus(nxt)  # the user is already in the next field when the worker gets here
    for ch in SECRET[3:]:
        rec.feed(("type", ch))
    rec.stop()
    assert SECRET[3:] not in _outputs(rec)
    assert [(a.text, a.secret) for a in rec.actions] == [("", True)]


def test_keys_processed_while_a_focus_key_is_queued_fail_closed(login):
    be, d, user, _pwd, nxt = login
    be._set_focus(nxt)  # live focus has moved on: a Tab typed later is still in the queue
    rec = Recorder(d).start(source="manual")
    rec._focus_events_queued = 1
    for ch in SECRET:
        rec.feed(("type", ch))
    rec._focus_events_queued = 0
    rec.stop()
    assert SECRET not in _outputs(rec)
    assert rec.actions[0].secret and rec.actions[0].secret_reason == UNKNOWN_FOCUS


def test_live_capture_counts_focus_keys_when_they_happen(login, monkeypatch):
    """The pynput callbacks count Tab/Enter/clicks as they happen; the worker uncounts them."""
    import queue
    import sys
    import types

    be, d, _user, pwd, nxt = login
    listeners = {}

    class _L:
        def __init__(self, **cbs):
            listeners.update(cbs)

        def start(self):
            pass

        def stop(self):
            pass

    fake = types.ModuleType("pynput")
    fake.keyboard = types.SimpleNamespace(Listener=_L)
    fake.mouse = types.SimpleNamespace(Listener=_L)
    monkeypatch.setitem(sys.modules, "pynput", fake)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", fake.keyboard)
    monkeypatch.setitem(sys.modules, "pynput.mouse", fake.mouse)

    rec = Recorder(d)
    rec._source = "pynput"
    # Hold the worker: queue everything first, as a lagging worker would see it.
    gate = queue.Queue()
    real_get = queue.Queue.get

    def slow_get(self, *a, **k):
        if self is not gate:
            gate.get()
        return real_get(self, *a, **k)

    be._set_focus(pwd)
    monkeypatch.setattr(queue.Queue, "get", slow_get)
    rec._start_pynput()
    key = lambda c: types.SimpleNamespace(char=c, name=None, vk=None)
    tab = types.SimpleNamespace(char=None, name="tab", vk=None)
    for ch in SECRET:
        listeners["on_press"](key(ch))
        listeners["on_release"](key(ch))
    listeners["on_press"](tab)
    listeners["on_release"](tab)
    assert rec._focus_events_queued == 1
    be._set_focus(nxt)  # the app processed the Tab long ago
    for _ in range(2 * len(SECRET) + 3):
        gate.put(None)
    rec._stop_worker()
    monkeypatch.setattr(queue.Queue, "get", real_get)
    assert rec._focus_events_queued == 0
    assert SECRET not in _outputs(rec)
    assert rec.actions[0].kind == "type" and rec.actions[0].secret


def test_set_text_on_an_unreadable_field_uses_the_secret_variable(login, monkeypatch):
    be, d, user, _pwd, _nxt = login
    rec = Recorder(d, use_selectors=False).start(source="manual")
    node = d.find('edit "User"').node

    class _Broken:
        def __init__(self, n):
            self._n = n

        def __getattr__(self, k):
            return getattr(self._n, k)

        @property
        def states(self):
            raise OSError("element not available")

    a = rec.record_native("set_text", _Broken(node), SECRET)
    rec.stop()
    assert a.secret and a.secret_reason == UNKNOWN_FOCUS
    out = _outputs(rec)
    assert SECRET not in out and "LOCUM_SECRET" in out and "LOCUM_PASSWORD" not in out


# ============================================================ shared selector trees
class _Source:
    def __init__(self):
        self.alive = True
        self.gen = 0
        self.log = []

    def stamp(self):
        return self.gen

    def bump(self, pid):
        self.gen += 1
        self.log.append((self.gen, pid))

    def dirty_since(self, stamp):
        return {pid for g, pid in self.log if g > stamp}

    def pids_of(self, node):
        return frozenset({node.pid})


@pytest.fixture
def source():
    src = _Source()
    sel_mod.register_change_source(src)
    try:
        yield src
    finally:
        sel_mod._SOURCES.discard(src)
        sel_mod.release_trees()


def _two_apps():
    be = MockBackend()
    a = be.add_window("App", (0, 0, 300, 200), app="a", pid=101)
    ok = a.add_button("OK", (10, 10, 60, 24))
    b = be.add_window("Other", (300, 0, 300, 200), app="b", pid=202)
    b.add_button("Cancel", (10, 10, 60, 24))
    return be, a, ok


def _node(be, widget):
    return next(n for n in parse("button").find_all(be.accessibility_roots()) if n.widget is widget)


def test_a_duplicate_added_inside_the_ttl_is_noticed(source):
    be, a, ok = _two_apps()
    cache = []
    roots = be.accessibility_roots()
    s1 = selector_for(_node(be, ok), roots=roots, prefix_cache=cache)
    assert len(parse(s1).find_all(roots)) == 1
    a.add_button("OK", (80, 10, 60, 24))  # within the 2 s the tree is reused
    source.bump(101)
    roots = be.accessibility_roots()
    s2 = selector_for(_node(be, ok), roots=roots, prefix_cache=cache)
    found = parse(s2).find_all(roots)
    assert len(found) == 1 and found[0].same_as(_node(be, ok)), s2


def test_events_from_other_apps_keep_the_tree(source, monkeypatch):
    be, a, ok = _two_apps()
    cache = []
    roots = be.accessibility_roots()
    selector_for(_node(be, ok), roots=roots, prefix_cache=cache)
    tree = sel_mod._tree_cache(cache)
    j = next(i for i, n in enumerate(tree.nodes) if n.pid == 101)
    app_entry = tree.entries[j]
    source.bump(202)
    again = sel_mod._tree_cache(cache)
    assert again is tree and again.entries[j] is app_entry
    source.bump(101)
    assert sel_mod._tree_cache(cache).entries[j] is not app_entry


def test_unknown_history_reads_the_tree_again(source):
    be, a, ok = _two_apps()
    cache = []
    selector_for(_node(be, ok), roots=be.accessibility_roots(), prefix_cache=cache)
    tree = sel_mod._tree_cache(cache)
    source.dirty_since = lambda stamp: None
    source.bump(None)
    assert sel_mod._tree_cache(cache) is not tree


def test_tool_calls_free_the_shared_trees():
    be, a, ok = _two_apps()
    kit = DesktopToolkit(Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0), Policy())
    selector_for(_node(be, ok), roots=be.accessibility_roots(), prefix_cache=[])
    assert sel_mod._TREES.trees
    kit.call("find_elements", {"text": "OK"})
    assert not getattr(sel_mod._TREES, "trees", None)


# ============================================================ small browser items
def test_bare_ipv6_loopback_is_bracketed(monkeypatch):
    seen = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"Browser": "Chrome/150", "Protocol-Version": "1.3", "webSocketDebuggerUrl": "ws://[::1]:9/devtools/browser/x"}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (seen.append(req.full_url), _Resp())[1])
    b = Browser("::1", 9)
    assert seen[0] == "http://[::1]:9/json/version" and b.host == "::1"


class _Session:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def call(self, method, params=None, timeout=None):
        self.calls.append(method)
        return self.result


def _browser_with_page(monkeypatch, navigate_result):
    b = object.__new__(Browser)
    b.host, b.port, b._pages = "127.0.0.1", 9, {}
    monkeypatch.setattr(cdp, "_http_json", lambda *a, **k: {"id": "t1", "url": "about:blank",
                                                             "webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/page/t1"})
    sess = _Session(navigate_result)
    evals = []

    class _P:
        def __init__(self, info, browser):
            self.info, self.session = info, sess

        def evaluate(self, expr, timeout=None):
            evals.append(expr)
            return "about:blank"

    monkeypatch.setattr(cdp, "Page", _P)
    return b, evals


def test_new_page_does_not_wait_for_a_document_that_is_not_coming(monkeypatch):
    import time

    b, evals = _browser_with_page(monkeypatch, {"frameId": "f"})  # no loaderId: a download
    t0 = time.monotonic()
    b.new_page("https://example.test/file.zip")
    assert time.monotonic() - t0 < 1.0 and evals == []


def test_new_page_reports_a_failed_navigation(monkeypatch):
    b, _ = _browser_with_page(monkeypatch, {"frameId": "f", "errorText": "net::ERR_NAME_NOT_RESOLVED"})
    with pytest.raises(CDPError, match="ERR_NAME_NOT_RESOLVED"):
        b.new_page("https://nope.invalid/")
