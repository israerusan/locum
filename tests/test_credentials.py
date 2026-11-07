"""``locum.rpa.credentials`` and the Secret wrapper (``locum._secret``).

Nothing here touches a real credential store: every test runs against an
in-memory store, a fake ``advapi32`` (the real ctypes marshalling of
``WindowsCredentialStore`` against Python stand-ins for CredReadW & co.) or a
fake ``keyring`` module. ``_default_store`` is patched to fail, so a test that
forgot to install a fake cannot reach Windows Credential Manager or a keychain.
"""

from __future__ import annotations

import copy
import ctypes
import io
import json
import pickle

import pytest

from locum import _secret, cli
from locum._secret import MASK, Secret, contains_secret, scrub, scrub_data
from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import ActionTimeout
from locum.llm.safety import AuditLog, GuardedDesktop, Policy
from locum.rpa import credentials as creds
from locum.rpa.credentials import (
    CREDENTIALW,
    Credential,
    CredentialNotFound,
    CredentialStoreUnavailable,
    KeyringCredentialStore,
    WindowsCredentialStore,
)
from locum.rpa.flow import interpolate, run_flow
from locum.trace import Tracer

PW = "Tr0ub4dor&3-vault"


class MemoryStore:
    name = "memory"

    def __init__(self):
        self.items = {}

    def get(self, name):
        return self.items.get(name)

    def set(self, name, username, password):
        self.items[name] = (username, password)

    def delete(self, name):
        return self.items.pop(name, None) is not None

    def names(self):
        return sorted(self.items)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No test may reach the real OS store; the env fallback starts disabled."""

    def forbidden():
        raise AssertionError("a test tried to open the real credential store")

    monkeypatch.setattr(creds, "_default_store", forbidden)
    monkeypatch.delenv(creds.ENV_FALLBACK_VAR, raising=False)
    store = MemoryStore()
    old = creds.set_store(store)
    creds.enable_env_fallback(None)
    # Values registered here ("other", "plain"...) must not be scrubbed out of later tests' text.
    known, known_set = list(_secret._known), set(_secret._known_set)
    yield store
    creds.set_store(old)
    creds.enable_env_fallback(None)
    with _secret._lock:
        _secret._known[:] = known
        _secret._known_set.clear()
        _secret._known_set.update(known_set)


# ====================================================================== Secret

def test_secret_never_shows_its_value():
    s = Secret(PW)
    assert str(s) == MASK and repr(s) == "Secret('***')"
    assert f"{s}" == MASK and "{}".format(s) == MASK and "%s" % s == MASK
    assert s.reveal() == PW and s.get_secret_value() == PW
    assert PW not in repr([s, {"pw": s}])
    assert s == Secret(PW) and s != Secret("other") and bool(s)
    assert copy.deepcopy(s) is s
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with pytest.raises(AttributeError):
        s._value = "x"
    with pytest.raises(TypeError):
        Secret(123)


def test_secret_values_are_scrubbed_from_text():
    Secret(PW)
    assert contains_secret(f"login failed for {PW}")
    assert scrub(f"value {PW!r} and {json.dumps(PW)}") == "value '***' and \"***\""
    assert scrub_data({"a": [f"x{PW}x", Secret("zz")], "n": 1}) == {"a": ["x***x", MASK], "n": 1}
    Secret("ab")  # too short to scrub out of free text
    assert scrub("abc ab") == "abc ab"


def test_credential_repr_masks_the_password(isolated):
    creds.set_credential("erp", "robot01", PW)
    c = creds.get_credential("erp")
    assert c.username == "robot01" and c.password.reveal() == PW and c.source == "memory"
    assert PW not in repr(c) and PW not in str(c)
    assert isinstance(Credential("x", "u", "plain").password, Secret)


# ====================================================================== API

def test_set_get_delete_list(isolated):
    creds.set_credential("erp", "robot01", Secret(PW))
    creds.set_credential("mail", "bot", "pw-two")
    assert creds.list_credentials() == ["erp", "mail"]
    assert creds.delete_credential("mail") is True
    assert creds.list_credentials() == ["erp"]
    with pytest.raises(CredentialNotFound):
        creds.delete_credential("mail")
    assert creds.delete_credential("mail", missing_ok=True) is False
    with pytest.raises(CredentialNotFound, match="locum credential set nope"):
        creds.get_credential("nope")


@pytest.mark.parametrize("bad", ["", "   ", "a*b", "a\nb", "x" * 201])
def test_names_are_validated(bad):
    with pytest.raises(ValueError):
        creds.get_credential(bad)


def test_env_fallback_only_when_enabled(isolated, monkeypatch):
    monkeypatch.setenv("LOCUM_CREDENTIAL_CI_DB_PASSWORD", "env-pass-123")
    monkeypatch.setenv("LOCUM_CREDENTIAL_CI_DB_USERNAME", "ci")
    with pytest.raises(CredentialNotFound):
        creds.get_credential("ci-db")
    c = creds.get_credential("ci-db", env=True)
    assert (c.username, c.password.reveal(), c.source) == ("ci", "env-pass-123", "env")
    monkeypatch.setenv(creds.ENV_FALLBACK_VAR, "1")
    assert creds.get_credential("ci-db").source == "env"
    creds.enable_env_fallback(False)
    with pytest.raises(CredentialNotFound):
        creds.get_credential("ci-db")
    # The store wins over the environment.
    creds.enable_env_fallback(True)
    creds.set_credential("ci-db", "stored", "from-store")
    assert creds.get_credential("ci-db").source == "memory"


def test_no_store_without_fallback_is_unavailable(monkeypatch):
    def unavailable():
        raise CredentialStoreUnavailable("no store")

    monkeypatch.setattr(creds, "_default_store", unavailable)
    creds.set_store(None)
    with pytest.raises(CredentialStoreUnavailable):
        creds.get_credential("x")
    monkeypatch.setenv("LOCUM_CREDENTIAL_X_PASSWORD", "only-env")
    assert creds.get_credential("x", env=True).password.reveal() == "only-env"


# ====================================================================== Windows (fake advapi32)

def _write_ptr(byref_arg, address):
    """Store ``address`` into the pointer variable a ``ctypes.byref(...)`` argument refers to."""
    target = byref_arg._obj
    ctypes.memmove(ctypes.addressof(target), ctypes.byref(ctypes.c_void_p(address)), ctypes.sizeof(ctypes.c_void_p))


class FakeAdvapi32:
    """CredReadW / CredWriteW / CredDeleteW / CredEnumerateW / CredFree over a dict.

    Like real Windows (verified against Credential Manager): CredReadW/CredDeleteW match
    target names case-insensitively and act on the oldest matching entry, while a
    CredWriteW whose case differs from an existing entry stacks a second one."""

    def _find(self, target):
        return next((t for t in self.items if t.casefold() == target.casefold()), None)

    def __init__(self):
        self.items = {}  # target -> (type, username, blob, persist)
        self.error = 0
        self.alive = []
        self.freed = 0

    def last_error(self):
        return self.error

    def CredWriteW(self, pcred, flags):
        c = pcred._obj
        blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        self.items[c.TargetName] = (c.Type, c.UserName, blob, c.Persist)
        return 1

    def _make(self, target):
        typ, user, blob, persist = self.items[target]
        buf = ctypes.create_string_buffer(blob, len(blob) or 1)
        cred = CREDENTIALW()
        cred.Type, cred.TargetName, cred.UserName, cred.Persist = typ, target, user, persist
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        self.alive += [buf, cred]
        return cred

    def CredReadW(self, target, typ, flags, out):
        found = self._find(target)
        if found is None or self.items[found][0] != typ:
            self.error = 1168
            return 0
        _write_ptr(out, ctypes.addressof(self._make(found)))
        return 1

    def CredDeleteW(self, target, typ, flags):
        found = self._find(target)
        if found is None:
            self.error = 1168
            return 0
        del self.items[found]
        return 1

    def CredEnumerateW(self, filt, flags, pcount, out):
        prefix = filt.rstrip("*")
        found = [t for t in self.items if t.startswith(prefix)]
        if not found:
            self.error = 1168
            return 0
        ptrs = (ctypes.POINTER(CREDENTIALW) * len(found))(*[ctypes.pointer(self._make(t)) for t in found])
        self.alive.append(ptrs)
        pcount._obj.value = len(found)
        _write_ptr(out, ctypes.addressof(ptrs))
        return 1

    def CredFree(self, p):
        self.freed += 1


@pytest.fixture
def wincred(isolated):
    dll = FakeAdvapi32()
    store = WindowsCredentialStore(dll=dll, last_error=dll.last_error)
    creds.set_store(store)
    return dll, store


def test_windows_store_roundtrip(wincred):
    dll, store = wincred
    creds.set_credential("erp", "robot01", "pässwörd-€")
    typ, user, blob, persist = dll.items["locum:erp"]
    assert (typ, user, persist) == (1, "robot01", 2)
    assert blob == "pässwörd-€".encode("utf-16-le")
    c = creds.get_credential("erp")
    assert (c.username, c.password.reveal(), c.source) == ("robot01", "pässwörd-€", "wincred")
    assert dll.freed >= 1


def test_windows_store_list_and_delete(wincred):
    dll, _store = wincred
    creds.set_credential("b", "u", "pw-b")
    creds.set_credential("a", "u", "pw-a")
    dll.items["other:thing"] = (1, "x", b"", 2)       # not ours: not listed
    assert creds.list_credentials() == ["a", "b"]
    assert creds.delete_credential("a") is True
    assert "locum:a" not in dll.items
    assert creds.delete_credential("a", missing_ok=True) is False
    assert creds.list_credentials() == ["b"]
    creds.delete_credential("b")
    assert creds.list_credentials() == []


def test_windows_store_not_found_and_errors(wincred):
    dll, store = wincred
    with pytest.raises(CredentialNotFound):
        creds.get_credential("missing")

    def denied(*a):
        dll.error = 5
        return 0

    dll.CredReadW = denied
    with pytest.raises(OSError, match="Windows error 5"):
        store.get("x")
    with pytest.raises(ValueError, match="too long"):
        store.set("big", "u", "x" * 2000)


# ====================================================================== keyring (fake module)

class FakeKeyring:
    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self):
        self.items = {}

    def get_password(self, service, user):
        return self.items.get((service, user))

    def set_password(self, service, user, password):
        self.items[(service, user)] = password

    def delete_password(self, service, user):
        if (service, user) not in self.items:
            raise self.errors.PasswordDeleteError(service)
        del self.items[(service, user)]


def test_keyring_store(isolated):
    kr = FakeKeyring()
    creds.set_store(KeyringCredentialStore(kr))
    creds.set_credential("erp", "robot01", PW)
    assert ("locum:erp", "locum") in kr.items
    c = creds.get_credential("erp")
    assert (c.username, c.password.reveal(), c.source) == ("robot01", PW, "keyring")
    creds.set_credential("mail", "bot", "x-pass")
    assert creds.list_credentials() == ["erp", "mail"]
    assert creds.delete_credential("erp") is True
    assert creds.list_credentials() == ["mail"]
    assert creds.delete_credential("erp", missing_ok=True) is False


def test_default_store_without_keyring_names_the_extra(monkeypatch):
    import builtins
    import sys

    monkeypatch.undo()  # the real _default_store, but never on Windows here
    monkeypatch.setattr(sys, "platform", "linux")
    real_import = builtins.__import__

    def no_keyring(name, *a, **k):
        if name == "keyring":
            raise ImportError("no keyring")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_keyring)
    with pytest.raises(CredentialStoreUnavailable, match=r"locum\[credentials\]"):
        creds._default_store()


# ====================================================================== typing a Secret

@pytest.fixture
def login(monkeypatch):
    be = MockBackend(width=800, height=600)
    w = be.add_window("Login", (0, 0, 400, 300))
    user = w.add_edit("User", (20, 20, 200, 30))
    pwd = w.add_edit("Password", (20, 60, 200, 30))   # NOT flagged as a password field
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, user, pwd


def test_typing_apis_accept_a_secret(login):
    be, d, user, pwd = login
    s = Secret(PW)
    d.find('edit "Password"').type(s)
    assert pwd.value == PW
    d.find('edit "User"').set_text(s)
    assert user.value == PW
    be._set_focus(pwd)
    pwd.value = ""
    d.type(s)
    assert pwd.value == PW
    d.clipboard_set(s)
    assert be.clipboard == PW


def test_trace_never_records_a_secret(login):
    be, d, _user, pwd = login
    s = Secret(PW)
    with Tracer(d, screenshots=False) as t:
        d.find('edit "Password"').type(s)
        be._set_focus(pwd)
        d.type(s)
        d.find('edit "User"').set_text(s)
    dumped = t.to_json(screenshots=False) + t.to_html()
    assert PW not in dumped and "***" in dumped


def test_verification_error_does_not_name_the_secret(login, monkeypatch):
    be, d, _user, pwd = login
    s = Secret(PW)
    el = d.find('edit "Password"')

    def garble(text):  # the app garbles keystrokes and refuses the repair
        pwd.value = "garbled"

    monkeypatch.setattr(mock.MockNode, "set_value", lambda self, text: False)
    orig = be.type_text
    monkeypatch.setattr(be, "type_text", lambda text, *a, **k: garble(text))
    with pytest.raises(ActionTimeout) as info:
        el.set_text(s, native=False, verify="exact")
    assert PW not in str(info.value) and MASK in str(info.value), str(info.value)


def test_guarded_desktop_audit_and_confirm_never_see_the_secret(login):
    be, d, _user, pwd = login
    seen = []

    def confirm(description, details):
        seen.append(repr((description, details)))
        return True

    log = AuditLog()
    g = GuardedDesktop(d, Policy(confirm_patterns=[r".*"]), confirm=confirm, audit=log)
    g.find('edit "Password"').type(Secret(PW))
    assert pwd.value == PW
    be._set_focus(pwd)
    pwd.value = ""
    g.type(Secret(PW))
    assert pwd.value == PW
    g.find('edit "User"').set_text(Secret(PW))
    dumped = json.dumps(log.events, default=str) + "".join(seen)
    assert PW not in dumped
    assert seen, "confirmation was asked"


def test_short_secret_is_masked_in_the_audit_log(login):
    be, d, _user, pwd = login
    log = AuditLog()
    g = GuardedDesktop(d, Policy(), audit=log)
    be._set_focus(pwd)
    g.type(Secret("q1"))  # too short to scrub out of free text, but still never logged
    g.find('edit "User"').type(Secret("q2"))
    texts = [e["args"].get("text") for e in log.events if e.get("tool") == "type_text"]
    assert texts and set(texts) == {"***"}


# ====================================================================== flows

def test_flow_credential_interpolation(isolated):
    creds.set_credential("erp", "robot01", PW)
    v: dict = {}
    assert interpolate("${credential:erp.username}", v) == "robot01"
    pw = interpolate("${credential:erp.password}", v)
    assert isinstance(pw, Secret) and pw.reveal() == PW
    composed = interpolate("${credential:erp.password}\n", v)
    assert isinstance(composed, Secret) and composed.reveal() == PW + "\n"
    assert interpolate("user=${credential:erp.username}", v) == "user=robot01"
    from locum.errors import WorkflowError

    with pytest.raises(WorkflowError, match="no credential named 'nope'"):
        interpolate("${credential:nope.password}", v)
    with pytest.raises(WorkflowError, match="credential:NAME.password"):
        interpolate("${credential:erp.token}", v)
    assert interpolate("${credential:nope.password:-fallback}", v) == "fallback"
    dotted = interpolate("${credential:a.b.username}", {"_credentials": lambda n: Credential(n, n.upper(), "x-pass")})
    assert dotted == "A.B"


def test_flow_types_the_password_but_never_reports_it(isolated, login, tmp_path):
    creds.set_credential("erp", "robot01", PW)
    be, d, user, pwd = login
    flow = {
        "name": "login",
        "steps": [
            {"set_text": {"target": 'edit "User"', "text": "${credential:erp.username}"}},
            {"type": {"text": "${credential:erp.password}", "into": 'edit "Password"'}},
            {"log": "typed ${credential:erp.password} for ${credential:erp.username}"},
        ],
    }
    report = run_flow(flow, d, run_dir=tmp_path)
    assert report.ok, report.to_json()
    assert user.value == "robot01" and pwd.value == PW
    files = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("report.*"))
    dumped = report.to_json() + report.to_html() + files
    assert PW not in dumped
    # A message built around a password is a Secret as a whole: it is logged as ***.
    assert any(r["message"] == "***" for r in report.to_dict()["logs"])


def test_flow_failure_message_is_scrubbed(isolated, login, tmp_path):
    creds.set_credential("erp", "robot01", PW)
    _be, d, _user, _pwd = login
    flow = {"steps": [{"fail": f"bad password {PW}"}]}  # a value that leaked into a message
    report = run_flow(flow, d, run_dir=tmp_path)
    assert not report.ok
    assert PW not in report.to_json() and PW not in report.to_html()


def test_flow_through_a_policy_audit_log(isolated, login):
    creds.set_credential("erp", "robot01", PW)
    _be, d, _user, pwd = login
    log = AuditLog()
    report = run_flow({"steps": [{"type": {"text": "${credential:erp.password}", "into": 'edit "Password"'}}]},
                      d, policy=Policy(), audit=log)
    assert report.ok and pwd.value == PW
    assert PW not in json.dumps(log.events, default=str)


def test_pyautogui_log_screenshot_names_never_hold_a_credential(isolated, login, monkeypatch, tmp_path):
    import locum.compat.pyautogui as pg

    _be, d, _u, pwd = login
    pwd_el = d.find('edit "Password"')
    pwd_el.focus()
    names = []
    monkeypatch.setattr(pg, "LOG_SCREENSHOTS", True)
    monkeypatch.setattr(pg, "PAUSE", 0.0)
    monkeypatch.setattr(pg, "screenshot", lambda path=None, **kw: names.append(path))
    monkeypatch.setattr(pg, "G_LOG_SCREENSHOTS_FILENAMES", [])
    old = pg.set_desktop(d)
    try:
        creds.set_credential("erp", "robot01", PW)
        pg.write(creds.get_credential("erp").password.reveal())
    finally:
        pg.set_desktop(old)
    assert pwd.value == PW
    assert names and PW not in names[0] and names[0].endswith("_write____.png")  # *** -> file-safe ___


# ====================================================================== CLI

def run_cli(*argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_set_list_delete(isolated, monkeypatch):
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="", stream=None: asked.append(prompt) or PW)
    code, out, err = run_cli("credential", "set", "erp", "--username", "robot01")
    assert code == cli.EXIT_OK, err
    assert isolated.items["erp"] == ("robot01", PW)
    assert asked == ["Password for erp: "]
    assert PW not in out + err
    code, out, _ = run_cli("credential", "list")
    assert code == cli.EXIT_OK and out.split() == ["erp"]
    code, out, _ = run_cli("--json", "credential", "list")
    assert json.loads(out) == {"credentials": ["erp"]} and PW not in out
    code, out, _ = run_cli("credential", "delete", "erp")
    assert code == cli.EXIT_OK and "erp" not in isolated.items
    code, _out, err = run_cli("credential", "delete", "erp")
    assert code == cli.EXIT_FAILURE and "no credential" in err


def test_cli_set_prompts_for_username_and_reads_password_stdin(isolated, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="", stream=None: "pw-from-getpass")
    code, _out, err = run_cli("credential", "set", "mail", stdin="bot\n")
    assert code == cli.EXIT_OK, err
    assert isolated.items["mail"] == ("bot", "pw-from-getpass")
    code, _out, err = run_cli("credential", "set", "db", "-u", "admin", "--password-stdin", stdin="from-stdin\n")
    assert code == cli.EXIT_OK, err
    assert isolated.items["db"] == ("admin", "from-stdin")


def test_cli_set_rejects_empty_password_and_needs_an_action(isolated, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="", stream=None: "")
    assert run_cli("credential", "set", "x", "-u", "u")[0] == cli.EXIT_USAGE
    assert "x" not in isolated.items
    assert run_cli("credential")[0] == cli.EXIT_USAGE
    assert run_cli("credential", "set", "x", "--password-stdin", stdin="pw\n")[0] == cli.EXIT_USAGE


def test_cli_store_unavailable_is_exit_3(monkeypatch):
    def unavailable():
        raise CredentialStoreUnavailable("no store here")

    monkeypatch.setattr(creds, "_default_store", unavailable)
    creds.set_store(None)
    code, _out, err = run_cli("credential", "list")
    assert code == cli.EXIT_UNAVAILABLE and "no store here" in err


def test_cli_error_messages_are_scrubbed(isolated, login, tmp_path, monkeypatch):
    creds.set_credential("erp", "robot01", PW)
    flow = tmp_path / "f.json"
    flow.write_text(json.dumps({"steps": [{"fail": f"oops {PW}"}]}), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    _be, d, _u, _p = login
    cli.main(["run", str(flow)], desktop=d, stdout=out, stderr=err)
    assert PW not in out.getvalue() + err.getvalue()
