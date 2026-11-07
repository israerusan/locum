"""P0: a confirmation callback must never receive a password or a registered secret.

Before the fix ``Policy._confirm_step`` scrubbed registered secrets from ``args`` only, then
merged the raw context (window title, target, description, the target node) into
``details``; text typed into a password field is not a registered secret, so the callback
got it verbatim in ``details["args"]``.
"""

import json

import pytest

from locum import _secret
from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import PolicyViolation
from locum.llm import AuditLog
from locum.llm.safety import GuardedDesktop, Policy
from locum.llm.tools import DesktopToolkit
from locum.rpa.credentials import Secret

from test_browser_cdp import FakeBrowser

PASSWORD = "S3cr3t-Pa55!"


def _dump(asked):
    """Everything the callback saw, including the target node's text attributes."""
    out = []
    for description, details in asked:
        node = details.get("target_node")
        shown = {k: v for k, v in details.items() if k != "target_node"}
        if node is not None:
            shown["node"] = [repr(node), getattr(node, "value", None), getattr(node, "name", None)]
        out.append([description, json.dumps(shown, default=str)])
    return json.dumps(out)


class Recorder:
    def __init__(self, answer=True):
        self.asked = []
        self.answer = answer

    def __call__(self, description, details):
        self.asked.append((description, details))
        return self.answer


@pytest.fixture(autouse=True)
def _no_leftover_secrets():
    _secret.forget_all()
    yield
    _secret.forget_all()


@pytest.fixture
def login(monkeypatch):
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))
    be = MockBackend()
    w = be.add_window("Bank login", (100, 100, 500, 300), app="chrome")
    user = w.add("edit", "User", (20, 20, 200, 30))
    pwd = w.add("edit", "Password", (20, 60, 200, 30), attrs={"password": True})
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    return be, d, user, pwd


def _confirm_all(cb):
    # Allow-list mode with no rules: every consequential action asks the human.
    return Policy(allow=[], confirm=cb)


# --------------------------------------------------------------- browser password entry
def test_browser_password_never_reaches_the_confirm_callback():
    d = Desktop(MockBackend(), failsafe=False, sleep=lambda s: None, default_timeout=0)
    cb = Recorder()
    kit = DesktopToolkit(d, _confirm_all(cb))
    kit._browser = FakeBrowser()
    r = kit.call("browser_type", {"text": PASSWORD, "element": 2})
    assert not r.is_error, r.text
    assert kit._browser.p.typed == [(PASSWORD, 2, False)]  # the field still gets the real text
    assert cb.asked, "the action should have needed a confirmation"
    assert PASSWORD not in _dump(cb.asked)
    details = cb.asked[0][1]
    assert details["args"]["text"] == "***" and details["sensitive"] is True


def test_browser_password_into_the_focused_field_is_masked_too():
    d = Desktop(MockBackend(), failsafe=False, sleep=lambda s: None, default_timeout=0)
    cb = Recorder()
    kit = DesktopToolkit(d, _confirm_all(cb))
    kit._browser = FakeBrowser()
    kit._browser.p.focused_password = True
    kit.call("browser_type", {"text": PASSWORD})
    assert cb.asked and PASSWORD not in _dump(cb.asked)


def test_browser_plain_text_is_still_shown_to_the_human():
    d = Desktop(MockBackend(), failsafe=False, sleep=lambda s: None, default_timeout=0)
    cb = Recorder()
    kit = DesktopToolkit(d, _confirm_all(cb))
    kit._browser = FakeBrowser()
    kit.call("browser_type", {"text": "Ada Lovelace", "element": 1})
    assert cb.asked and cb.asked[0][1]["args"]["text"] == "Ada Lovelace"
    assert cb.asked[0][1]["sensitive"] is False


# --------------------------------------------------------------- guarded desktop input
@pytest.mark.parametrize("how", ["element.type", "desktop.type", "after_tab"])
def test_guarded_password_input_never_reaches_the_confirm_callback(login, how):
    be, d, user, pwd = login
    cb = Recorder()
    g = GuardedDesktop(d, _confirm_all(cb), audit=AuditLog())
    if how == "element.type":
        g.find('edit "Password"').type(PASSWORD)
    elif how == "desktop.type":
        be._set_focus(pwd)
        g.type(PASSWORD)
    else:
        be._set_focus(user)
        g.type("alice\t" + PASSWORD)
    assert pwd.value == PASSWORD
    assert cb.asked
    assert PASSWORD not in _dump(cb.asked)


def test_toolkit_type_text_into_a_password_field_is_masked(login):
    be, d, user, pwd = login
    be._set_focus(pwd)
    cb = Recorder()
    kit = DesktopToolkit(d, _confirm_all(cb))
    r = kit.call("type_text", {"text": PASSWORD})
    assert not r.is_error, r.text
    assert cb.asked and PASSWORD not in _dump(cb.asked)


def test_password_node_handed_to_the_callback_hides_its_value(login):
    be, d, user, pwd = login
    pwd.value = PASSWORD  # e.g. a field the OS reports with its plaintext value
    cb = Recorder()
    g = GuardedDesktop(d, _confirm_all(cb))
    g.find('edit "Password"').click()
    node = cb.asked[0][1]["target_node"]
    assert node.value == "***" and node.name == "Password" and node.role == "edit"
    assert PASSWORD not in _dump(cb.asked)


# --------------------------------------------------------------- registered secrets in context
def test_registered_secret_in_window_title_and_target_is_scrubbed(login):
    be, d, user, pwd = login
    token = "tok-9f8e7d6c5b4a"
    _secret.register(token)
    be.add_window(f"Session {token}", (0, 0, 300, 200), app="chrome").add_button(f"Revoke {token}", (10, 10, 80, 30))
    cb = Recorder()
    g = GuardedDesktop(d, _confirm_all(cb))
    g.find(f'button "Revoke {token}"').click()
    assert cb.asked
    assert token not in _dump(cb.asked)


def test_secret_object_typed_into_a_plain_field_is_masked(login):
    be, d, user, pwd = login
    cb = Recorder()
    g = GuardedDesktop(d, _confirm_all(cb))
    g.find('edit "User"').type(Secret(PASSWORD))
    assert user.value == PASSWORD
    assert cb.asked and PASSWORD not in _dump(cb.asked)


# --------------------------------------------------------------- the policy on its own
def test_direct_policy_check_masks_text_for_a_password_target(login):
    be, d, user, pwd = login
    cb = Recorder()
    p = _confirm_all(cb)
    node = d.find('edit "Password"').node
    p.check("type_text", {"text": PASSWORD}, context={"target_node": node, "description": "type into 'Password'",
                                                      "target": "Password"})
    assert cb.asked and PASSWORD not in _dump(cb.asked)
    assert cb.asked[0][1]["args"]["text"] == "***"


def test_explicit_sensitive_args_are_masked_everywhere():
    cb = Recorder()
    p = _confirm_all(cb)
    p.check("integration_tool", {"pin": "48213377", "account": "ACME"},
            context={"sensitive_args": ("pin",), "description": "send pin 48213377 to ACME",
                     "target": "pin 48213377", "require_confirmation": True})
    description, details = cb.asked[0]
    assert "48213377" not in _dump(cb.asked)
    assert details["args"] == {"pin": "***", "account": "ACME"}


def test_no_callback_error_does_not_echo_the_password(login):
    be, d, user, pwd = login
    g = GuardedDesktop(d, Policy(allow=[]))
    with pytest.raises(PolicyViolation) as ei:
        g.find('edit "Password"').type(PASSWORD)
    assert PASSWORD not in str(ei.value)


def test_a_failing_callback_does_not_chain_the_raw_payload(login):
    be, d, user, pwd = login

    def boom(description, details):
        raise RuntimeError(f"cannot show {details!r} {PASSWORD}")

    g = GuardedDesktop(d, Policy(allow=[], confirm=boom))
    with pytest.raises(PolicyViolation) as ei:
        g.find('edit "Password"').type(PASSWORD)
    assert PASSWORD not in str(ei.value) and ei.value.__cause__ is None


# --------------------------------------------------------------- computer use and set_value
@pytest.mark.parametrize("text", [PASSWORD, PASSWORD + "\n"])
def test_computer_use_password_never_reaches_the_confirm_callback(login, text):
    from locum.llm.computer_use import ComputerUseAdapter

    be, d, user, pwd = login
    be._set_focus(pwd)
    cb = Recorder()
    cu = ComputerUseAdapter(d, policy=_confirm_all(cb), audit=AuditLog())
    r = cu.execute("type", {"text": text})
    assert not r.is_error, r.text
    assert cb.asked and PASSWORD not in _dump(cb.asked)


def test_toolkit_set_value_on_a_password_field_is_masked(login):
    be, d, user, pwd = login
    cb = Recorder()
    kit = DesktopToolkit(d, _confirm_all(cb))
    r = kit.call("set_value", {"selector": 'edit "Password"', "value": PASSWORD})
    assert not r.is_error, r.text
    assert cb.asked and PASSWORD not in _dump(cb.asked)
