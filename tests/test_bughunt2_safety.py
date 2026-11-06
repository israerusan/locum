"""Regression tests for bug hunt round 2: locum.llm.safety (mock backend only, no real desktop)."""

import json

import pytest

from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import PolicyViolation
from locum.llm import AuditLog
from locum.llm.safety import CARD_NUMBER, AllowRule, GuardedDesktop, Policy


# ------------------------------------------- unquoted launch paths with spaces

@pytest.fixture
def pwsh_dir(tmp_path):
    sub = tmp_path / "Power Shell"
    sub.mkdir()
    exe = sub / "pwsh.exe"
    exe.write_text("")  # never run: only os.path.isfile looks at it
    return exe


def _launch(policy, command):
    policy.check("launch_app", {"command": command}, context={"command": command})


def test_unquoted_path_with_spaces_is_checked_against_blocked_apps(pwsh_dir):
    cmd = f"{pwsh_dir} -c whoami"
    with pytest.raises(PolicyViolation, match="blocked"):
        _launch(Policy(blocked_apps=["pwsh"]), cmd)
    with pytest.raises(PolicyViolation):
        _launch(Policy.strict(deny_tools=(), confirm=lambda *a: True), cmd)


def test_unquoted_nonexistent_path_checks_every_prefix():
    with pytest.raises(PolicyViolation, match="blocked"):
        _launch(Policy(blocked_apps=["pwsh"]), r"C:\No Such Dir\x y\pwsh.exe -c whoami")


def test_unquoted_path_with_spaces_allowed_by_resolved_program(tmp_path):
    sub = tmp_path / "Microsoft Office"
    sub.mkdir()
    exe = sub / "EXCEL.EXE"
    exe.write_text("")
    cmd = f"{exe} /x"
    _launch(Policy(allowed_apps=["excel"]), cmd)
    p = Policy(allow=[AllowRule(command="excel")], confirm=lambda *a: True)
    _launch(p, cmd)


def test_allowed_apps_judges_the_existing_prefix_not_a_later_one(tmp_path):
    # "<dir>\excel.exe" exists, so CreateProcess runs it: a later prefix ("...excel x\pwsh") is not run,
    # and conversely an allowed name in a later prefix does not authorize the program that does run.
    (tmp_path / "tool.exe").write_text("")
    cmd = f"{tmp_path / 'tool'} x\\excel.exe"
    with pytest.raises(PolicyViolation, match="not allowed"):
        _launch(Policy(allowed_apps=["excel"]), cmd)


def test_plain_and_quoted_launches_unchanged(pwsh_dir):
    _launch(Policy(blocked_apps=["pwsh"]), "notepad.exe C:\\notes\\pwsh.txt")
    with pytest.raises(PolicyViolation):
        _launch(Policy(blocked_apps=["pwsh"]), f'"{pwsh_dir}" -c whoami')
    with pytest.raises(PolicyViolation, match="not allowed"):
        _launch(Policy(allowed_apps=["excel"]), "notepad x.txt")


# ------------------------------------------- password typed after a Tab (guarded paths)

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
    be._set_focus(user)
    return be, d, user, pwd


@pytest.mark.parametrize("how", ["desktop.type", "keyboard.type", "element.type"])
def test_guarded_type_does_not_log_password_after_tab(login, how):
    be, d, user, pwd = login
    audit = AuditLog()
    g = GuardedDesktop(d, Policy(), audit=audit)
    text = "alice\tS3cr3t-Pa55!"
    if how == "desktop.type":
        g.type(text)
    elif how == "keyboard.type":
        g.keyboard.type(text)
    else:
        g.find('edit "User"').type(text)
    assert pwd.value == "S3cr3t-Pa55!"
    dumped = json.dumps(audit.events)
    assert "S3cr3t" not in dumped
    assert "alice" in dumped  # the non-secret piece is still recorded


def test_guarded_type_single_piece_logged_in_full(login):
    be, d, user, pwd = login
    audit = AuditLog()
    GuardedDesktop(d, Policy(), audit=audit).type("alice")
    assert audit.events[-1]["args"] == {"text": "alice"}


def test_deny_patterns_still_judge_the_whole_text(login):
    be, d, user, pwd = login
    g = GuardedDesktop(d, Policy(deny_text_patterns=[r"alice\tS3cr3t"]), audit=AuditLog())
    with pytest.raises(PolicyViolation):
        g.type("alice\tS3cr3t")
    assert not pwd.value and not user.value


# ------------------------------------------- IBAN followed by a word

IBAN = "DE44 5001 0517 5407 3249 31"


@pytest.mark.parametrize("text", [
    f"{IBAN} EUR",
    f"IBAN {IBAN} to Bob",
    f"{IBAN.replace(' ', '')} EUR 120.00",
    "de44500105175407324931 thanks",
    f"{IBAN} GB82 WEST 1234 5698 7654 32 ok",
])
def test_iban_followed_by_word_is_not_a_card(text):
    assert CARD_NUMBER.search(text) is None


def test_card_numbers_still_detected_near_ibans():
    assert CARD_NUMBER.search("4111 1111 1111 1111") is not None
    assert CARD_NUMBER.search(f"{IBAN} EUR card 4111 1111 1111 1111") is not None

def test_strict_policy_allows_iban_with_currency():
    Policy.strict(confirm=lambda *a: True).check("type_text", {"text": f"{IBAN} EUR"}, context={"app": "chrome"})


# ------------------------------------------- rate limit window starts when the action runs

def test_rate_limit_window_excludes_confirmation_wait():
    t = [0.0]

    def confirm(desc, details):
        t[0] += 59.0
        return True

    p = Policy(confirm=confirm, max_actions_per_minute=1, clock=lambda: t[0])
    p.check("click", {}, context={"target": "Pay now"})  # runs at t=59
    t[0] += 1.0
    with pytest.raises(PolicyViolation, match="Rate limit"):
        p.check("click", {}, context={"target": "Next"})
    t[0] = 119.0
    p.check("click", {}, context={"target": "Next"})  # 60 s after the first ran


def test_rate_limit_declined_confirmation_frees_slot():
    t = [0.0]
    p = Policy(confirm=lambda *a: False, max_actions_per_minute=1, clock=lambda: t[0])
    with pytest.raises(PolicyViolation):
        p.check("click", {}, context={"target": "Pay now"})
    p.check("click", {}, context={"target": "Next"})
