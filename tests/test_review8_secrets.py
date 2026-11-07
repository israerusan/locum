"""Regression tests for review 8: secrets and the credential vault.

Nothing touches a real credential store, keyboard or screen: the Windows store runs
against the fake ``advapi32`` of ``test_credentials`` (case-insensitive like real
Credential Manager), keyring against a fake module, the desktop is a MockBackend.
"""

from __future__ import annotations

import json
import logging
import traceback

import pytest

from locum import _secret
from locum._secret import MASK, Secret, scrubbing_errors
from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import ActionTimeout, WorkflowError
from locum.llm.observe import observe
from locum.llm.tools import ToolResult
from locum.rpa import Robot
from locum.rpa import credentials as creds
from locum.rpa.credentials import (
    CredentialNotFound,
    CredentialStoreUnavailable,
    KeyringCredentialStore,
    WindowsCredentialStore,
)
from locum.rpa.flow import interpolate
from locum.rpa.report import RunReport, StepResult, _thumbnail_data_url
from locum.trace import Tracer

from test_credentials import FakeAdvapi32, FakeKeyring, MemoryStore

PW = "Tr0ubdor-Vault-77"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No real OS store; env fallback off; the process-wide scrub list restored afterwards."""

    def forbidden():
        raise AssertionError("a test tried to open the real credential store")

    monkeypatch.setattr(creds, "_default_store", forbidden)
    monkeypatch.delenv(creds.ENV_FALLBACK_VAR, raising=False)
    old = creds.set_store(MemoryStore())
    creds.enable_env_fallback(None)
    known, known_set = list(_secret._known), set(_secret._known_set)
    yield
    creds.set_store(old)
    creds.enable_env_fallback(None)
    with _secret._lock:
        _secret._known[:] = known
        _secret._known_set.clear()
        _secret._known_set.update(known_set)


@pytest.fixture
def login():
    be = MockBackend(width=800, height=600)
    w = be.add_window("Login", (0, 0, 400, 300))
    user = w.add_edit("User", (20, 20, 200, 30))
    pwd = w.add_edit("Password", (20, 60, 200, 30))  # NOT flagged as a password field
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, user, pwd


# ============================================================ 1: Windows names differing in case

@pytest.fixture
def wincred():
    dll = FakeAdvapi32()
    store = WindowsCredentialStore(dll=dll, last_error=dll.last_error)
    creds.set_store(store)
    return dll, store


def test_windows_names_differing_in_case_are_one_credential(wincred):
    dll, _store = wincred
    creds.set_credential("Erp", "u1", "first-pass")
    creds.set_credential("ERP", "u2", "second-pass")
    c = creds.get_credential("erp")
    assert (c.username, c.password.reveal()) == ("u2", "second-pass")
    assert creds.list_credentials() == ["erp"]
    assert list(dll.items) == ["locum:erp"]
    assert creds.delete_credential("eRp") is True
    assert dll.items == {}
    with pytest.raises(CredentialNotFound):
        creds.get_credential("erp")


def test_windows_stale_case_variant_is_replaced_and_deleted(wincred):
    dll, store = wincred
    # Entries written with their original case before names were normalised: Windows
    # stacks them, and a read returns the oldest one.
    dll.items["locum:ERP"] = (1, "old", "stale-pass".encode("utf-16-le"), 2)
    dll.items["locum:Erp"] = (1, "old2", "stale-pass-2".encode("utf-16-le"), 2)
    creds.set_credential("erp", "new", "fresh-pass")
    assert store.get("erp") == ("new", "fresh-pass")  # verified after the write
    assert list(dll.items) == ["locum:erp"]
    dll.items["locum:ERP"] = (1, "old", "stale-pass".encode("utf-16-le"), 2)
    assert store.delete("erp") is True
    assert dll.items == {}  # every variant gone, not just one


def test_windows_write_that_does_not_read_back_raises(wincred):
    dll, store = wincred
    real_read = dll.CredReadW

    def stale(target, typ, flags, out):  # an entry locum cannot replace
        dll.items.setdefault("locum:stuck", (1, "x", "other-pass".encode("utf-16-le"), 2))
        return real_read("locum:stuck", typ, flags, out)

    dll.CredReadW = stale
    with pytest.raises(Exception, match="does not return credential"):
        store.set("erp", "u", "mine-pass")


def test_windows_overlong_username_is_a_clear_error(wincred):
    _dll, store = wincred
    with pytest.raises(ValueError, match="username is too long"):
        store.set("erp", "u" * 513, "x-pass")
    store.set("erp", "u" * 512, "x-pass")


# ============================================================ 2 + 4: verification errors / VerifyResult

def test_near_copy_of_a_secret_never_in_a_verification_error(login, monkeypatch):
    be, d, _user, pwd = login
    el = d.find('edit "Password"')
    near = PW[:5] + PW[6:]  # the app dropped one keystroke
    monkeypatch.setattr(mock.MockNode, "set_value", lambda self, text: False)
    monkeypatch.setattr(be, "type_text", lambda text, *a, **k: setattr(pwd, "value", near))
    with pytest.raises(ActionTimeout) as info:
        el.set_text(Secret(PW), native=False, verify="exact")
    msg = str(info.value)
    assert near not in msg and PW not in msg and PW[:5] + PW[6:10] not in msg, msg
    assert "position 5" in msg and MASK in msg


def test_type_secret_near_copy_never_in_error(login, monkeypatch):
    be, d, _user, pwd = login
    el = d.find('edit "Password"')
    near = PW[:-1]
    monkeypatch.setattr(mock.MockNode, "set_value", lambda self, text: False)
    monkeypatch.setattr(be, "type_text", lambda text, *a, **k: setattr(pwd, "value", near))
    real_has = be.has
    monkeypatch.setattr(be, "has", lambda cap: cap != "clipboard" and real_has(cap))  # the repair retypes too
    with pytest.raises(ActionTimeout) as info:
        el.type(Secret(PW), verify="exact")
    assert near not in str(info.value), str(info.value)


def test_last_verification_masks_the_secret(login):
    _be, d, _user, _pwd = login
    el = d.find('edit "Password"')
    el.set_text(Secret(PW))
    v = el.last_verification
    assert v.verified
    assert PW not in repr(v) and PW not in json.dumps(v.to_dict())
    assert v.to_dict()["expected"] == MASK and v.to_dict()["actual"] == MASK
    el.set_text("ordinary 42")
    assert el.last_verification.to_dict()["expected"] == "ordinary 42"


# ============================================================ 3: observe / repr / tool output

def test_observe_and_repr_never_show_a_typed_secret(login):
    _be, d, _user, _pwd = login
    el = d.find('edit "Password"')
    el.type(Secret(PW))
    obs = observe(d, screenshot=False)
    assert PW not in obs.text()
    assert PW not in json.dumps(obs.to_dict())
    assert PW not in repr(el)
    assert PW not in ToolResult(f"read {PW}").text


def test_repr_scrubs_a_long_secret_before_shortening(login):
    _be, d, _user, pwd = login
    long_pw = "Q7-long-secret-" + "x9" * 40
    Secret(long_pw)
    pwd.value = long_pw
    assert long_pw[:30] not in repr(d.find('edit "Password"'))


# ============================================================ 5: robot logs

def test_robot_logs_are_scrubbed(login, tmp_path, caplog):
    _be, d, _user, _pwd = login
    Secret(PW)
    robot = Robot(d, name="r", run_dir=tmp_path / "runs", sleep=lambda s: None)

    @robot.task
    def t():
        robot.log(f"logging in with {PW}", pw=PW)
        for attempt in robot.step("login", retries=1, continue_on_error=True):
            with attempt:
                raise ValueError(f"wrong password {PW}")

    with caplog.at_level(logging.DEBUG, logger="locum.robot"):
        report = robot.run(save=False)
    dumped = json.dumps(report.logs, default=str) + "".join(r.getMessage() for r in caplog.records)
    dumped += json.dumps([getattr(r, "locum", None) for r in caplog.records], default=str)
    assert "wrong password" in dumped and PW not in dumped


# ============================================================ 6: scrubbing_errors

def test_scrubbing_errors_handles_chains_and_custom_str():
    Secret(PW)

    class Custom(Exception):
        def __str__(self):
            return f"custom says {PW}"

    with pytest.raises(Custom) as info:
        with scrubbing_errors():
            raise Custom()
    # The type is never swapped (review 9): a custom __str__ is scrubbed by the renderers.
    assert type(info.value) is Custom
    assert PW not in _secret.scrub(str(info.value)) and "custom says" in _secret.scrub(str(info.value))

    with pytest.raises(RuntimeError) as info:
        with scrubbing_errors():
            try:
                raise ValueError(f"inner {PW}")
            except ValueError as e:
                try:
                    raise KeyError(f"context {PW}")
                except KeyError:
                    raise RuntimeError("outer") from e
    exc = info.value
    assert PW not in str(exc.__cause__) and PW not in str(exc.__context__)
    assert PW not in "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# ============================================================ 7: keyring errors -> env fallback

class FailKeyring(FakeKeyring):
    class errors:
        class KeyringError(Exception):
            pass

        class NoKeyringError(KeyringError, RuntimeError):
            pass

        class PasswordDeleteError(KeyringError):
            pass

    def get_password(self, service, user):
        raise self.errors.NoKeyringError("No recommended backend was available")


def test_keyring_fail_backend_falls_back_to_env(monkeypatch):
    creds.set_store(KeyringCredentialStore(FailKeyring()))
    monkeypatch.setenv("LOCUM_CREDENTIAL_CI_PASSWORD", "env-only-pass")
    assert creds.get_credential("ci", env=True).password.reveal() == "env-only-pass"
    with pytest.raises(CredentialStoreUnavailable):
        creds.get_credential("ci")


# ============================================================ 8: report screenshots

def test_report_html_keeps_screenshots_intact(tmp_path):
    from PIL import Image

    shot = tmp_path / "shot.png"
    Image.new("RGB", (40, 30), (200, 30, 90)).save(shot)
    url = _thumbnail_data_url(str(shot), 480)
    b64 = url.split(",", 1)[1]
    Secret(b64[20:26])  # a short secret that happens to occur in the base64
    report = RunReport(name="r")
    report.steps.append(StepResult(name="s", kind="step", status="failed", error=f"bad {b64[20:26]}",
                                   screenshot=str(shot)))
    html = report.to_html()
    assert url in html
    assert f"bad {MASK}" in html


# ============================================================ 9: trace scrubs before shortening

def test_trace_scrubs_before_truncating(login):
    _be, d, _user, _pwd = login
    Secret(PW)
    sel = 'edit "' + "a" * 280 + PW + '"'  # PW straddles the 300-character cut
    with Tracer(d, screenshots=False) as t:
        with pytest.raises(Exception):
            d.find(sel, timeout=0)
    dumped = t.to_json(screenshots=False)
    assert PW[:6] not in dumped


# ============================================================ 10: flow credential defaults

def test_flow_credential_default_only_for_not_found():
    def missing(name):
        raise CredentialNotFound(f"no credential named {name!r}")

    def broken(name):
        raise OSError(5, "access denied")

    assert interpolate("${credential:x.password:-dflt}", {"_credentials": missing}) == "dflt"
    with pytest.raises(WorkflowError, match="access denied"):
        interpolate("${credential:x.password:-dflt}", {"_credentials": broken})


# ============================================================ 11: keyring index across processes

def test_keyring_index_survives_a_concurrent_writer():
    kr = FakeKeyring()
    index_key = ("locum", "credential-index")
    real_set = kr.set_password
    raced = []

    def set_password(service, user, password):
        real_set(service, user, password)
        if (service, user) == index_key and not raced:
            raced.append(1)  # another process saves its own stale read-modify-write afterwards
            real_set(service, user, json.dumps(["other"]))

    kr.set_password = set_password
    store = KeyringCredentialStore(kr)
    creds.set_store(store)
    creds.set_credential("mine", "u", "pw-mine")
    assert creds.list_credentials() == ["mine", "other"]


def test_keyring_names_are_case_insensitive():
    kr = FakeKeyring()
    creds.set_store(KeyringCredentialStore(kr))
    creds.set_credential("ERP", "u", "pw-erp")
    assert creds.get_credential("erp").password.reveal() == "pw-erp"
    # an entry written in its original case by an older version
    kr.items[("locum:Mail", "locum")] = json.dumps({"username": "m", "password": "pw-mail"})
    kr.items[("locum", "credential-index")] = json.dumps(["Mail", "erp"])
    assert creds.get_credential("MAIL").password.reveal() == "pw-mail"
    assert creds.delete_credential("mail") is True
    assert ("locum:Mail", "locum") not in kr.items and creds.list_credentials() == ["erp"]
