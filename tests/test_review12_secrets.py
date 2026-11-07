"""Review 12 (secrets and the credential vault): each test fails on HEAD 10615e2."""

from __future__ import annotations

import json
import re
import uuid

import pytest

from locum import _secret
from locum._secret import MASK, Secret, mask_value, scrub
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.llm.tools import ToolResult
from locum.rpa import credentials as creds
from locum.rpa.credentials import WindowsCredentialStore
from locum.trace import Tracer

from test_credentials import FakeAdvapi32


@pytest.fixture(autouse=True)
def isolated():
    known, known_set = list(_secret._known), set(_secret._known_set)
    with _secret._lock:
        _secret._known.clear()
        _secret._known_set.clear()
    old_store = creds.set_store(None)
    yield
    creds.set_store(old_store)
    with _secret._lock:
        _secret._known[:] = known
        _secret._known_set.clear()
        _secret._known_set.update(known_set)


# ============================================== mask_value: a short secret never alters an amount

def test_a_six_digit_pin_does_not_mask_an_amount_that_spells_it_with_a_separator():
    Secret("482150")  # a 6-digit PIN
    assert mask_value("4821.50") == "4821.50"
    assert mask_value("48-21-50") == "48-21-50"
    assert ToolResult("4821.50").text == "4821.50"
    assert mask_value("482150") == MASK  # the value that *is* the PIN still is masked
    Secret("4111111111111111")
    assert mask_value("4111 1111 1111 1111") == MASK  # long secrets shown grouped still are


# ============================================== registry pattern: no copy per registration kept by re

def _re_cache_patterns():
    out = []
    for name in ("_cache", "_cache2"):
        cache = getattr(re, name, None)
        if isinstance(cache, dict):
            for key in list(cache):
                out.extend(k for k in (key if isinstance(key, tuple) else (key,)) if isinstance(k, str))
    return out


def test_scrub_patterns_do_not_pile_up_in_the_re_module_cache():
    re.purge()
    tag = uuid.uuid4().hex[:10]
    for i in range(20):
        Secret(f"{tag}-secret-{i:03d}")
        assert scrub(f"x {tag}-secret-{i:03d} y") == f"x {MASK} y"
    # Each registration changes the alternation; re.compile would keep every version
    # (hundreds of growing patterns over a long run) and each one holds the secrets.
    assert not [p for p in _re_cache_patterns() if tag in p]


# ============================================== Tracer(redact=...): quoted/escaped forms are scrubbed

def test_tracer_redact_scrubs_the_repr_form_of_a_value_with_a_backslash():
    be = MockBackend(width=800, height=600)
    w = be.add_window("Login", (100, 100, 500, 400), app="erp")
    w.add_edit("User", (20, 50, 200, 28))
    d = Desktop(be, failsafe=False, default_timeout=0.05, sleep=lambda s: None)
    secret = "dom\\robot-pw"
    with Tracer(d, redact=[secret]) as t:
        d.click('edit "User"')
        d.type("login " + secret)
    shown = t.steps[1].args["text"]
    assert "robot-pw" not in shown and MASK in shown
    assert "robot-pw" not in t.to_json(screenshots=False) and "robot-pw" not in t.to_html()


# ============================================== tool results: JSON keys and duplicate keys

def test_tool_result_json_never_carries_a_long_secret_in_a_key_or_a_duplicate():
    Secret("Sup3r-secret-key-77")
    as_key = ToolResult(json.dumps({"Sup3r-secret-key-77": 1, "n": 1234}))
    assert "Sup3r-secret-key-77" not in as_key.text
    assert json.loads(as_key.text)["n"] == 1234  # still valid JSON
    dup = ToolResult('{"v": "Sup3r-secret-key-77", "v": "x"}')
    assert "Sup3r-secret-key-77" not in dup.text


# ============================================== Windows store: names Windows itself would match

class UpcaseAdvapi32(FakeAdvapi32):
    """Real Windows matches target names by upper-casing each character (verified with
    Credential Manager: "locum:x-straße" finds "locum:x-Straße", "locum:X-STRASSE" does not)."""

    @staticmethod
    def _up(text):
        return "".join(c.upper() if len(c.upper()) == 1 else c for c in text)

    def _find(self, target):
        return next((t for t in self.items if len(t) == len(target) and self._up(t) == self._up(target)), None)


def test_windows_legacy_entry_with_sharp_s_is_readable_and_deletable():
    dll = UpcaseAdvapi32()
    store = WindowsCredentialStore(dll=dll, last_error=dll.last_error)
    creds.set_store(store)
    store._write("Straße", "u1", "pw-strasse-1".encode("utf-16-le"))  # as 0.4.0 wrote it
    assert creds.list_credentials() == ["straße"]
    c = creds.get_credential("Straße")
    assert (c.username, c.password.reveal()) == ("u1", "pw-strasse-1")
    assert creds.get_credential("STRAßE").password.reveal() == "pw-strasse-1"
    creds.set_credential("Straße", "u2", "pw-strasse-2")
    assert creds.get_credential("straße").username == "u2"
    assert creds.delete_credential("Straße") is True
    assert dll.items == {}
