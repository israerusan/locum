"""Regression tests for review 9: the secret scrubbing added in review 8 must not corrupt
what the model sees, must not break exceptions, and must stay cheap.

MockBackend only: no real input, screen or credential store.
"""

from __future__ import annotations

import importlib
import json
import pickle

import pytest

from locum import _secret
from locum._secret import MASK, Secret, scrub_exception, scrubbing_errors
from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import ActionTimeout
from locum.llm.observe import observe
from locum.llm.tools import ToolResult
from locum.rpa import Robot

observe_mod = importlib.import_module("locum.llm.observe")  # locum.llm.observe is also the function

PW = "Tr0ubdor-Vault-77"


@pytest.fixture(autouse=True)
def isolated():
    known, known_set = list(_secret._known), set(_secret._known_set)
    with _secret._lock:
        _secret._known.clear()
        _secret._known_set.clear()
    yield
    with _secret._lock:
        _secret._known[:] = known
        _secret._known_set.clear()
        _secret._known_set.update(known_set)


def _desktop(be):
    return Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)


@pytest.fixture
def orders():
    be = MockBackend(width=1920, height=1080)
    w = be.add_window("Orders 1234", (0, 0, 1900, 1000))
    w.add_button("Invoice 51234", (1200, 300, 100, 30))
    amt = w.add_edit("Amount", (20, 20, 200, 30))
    amt.value = "1234.50"
    return be, _desktop(be), amt


# ============================================================ 1: short secrets vs. the model's view

def test_short_secret_never_garbles_an_observation(orders):
    _be, d, _amt = orders
    Secret("1234")  # a PIN
    obs = observe(d, screenshot=False)
    text = obs.text()
    assert '"Orders 1234"' in text and '"Invoice 51234"' in text and 'value="1234.50"' in text
    data = json.loads(json.dumps(obs.to_dict()))
    names = {e["name"]: e for e in data["elements"]}
    assert names["Amount"]["value"] == "1234.50"
    assert names["Invoice 51234"]["window"] == "Orders 1234"
    Secret("080")
    assert "1920x1080" in observe(d, screenshot=False).text()


def test_selectors_from_an_observation_still_find_their_element():
    be = MockBackend(width=1920, height=1080)
    w = be.add_window("Orders", (0, 0, 1900, 1000))
    w.add_button("Delete 5999", (100, 100, 100, 30))
    w.add_button("Delete 51234", (100, 300, 100, 30))
    w.add_button(f"Reset {PW}", (100, 500, 100, 30))
    d = _desktop(be)
    Secret("1234")
    Secret(PW)
    obs = observe(d, screenshot=False)
    for e in obs.to_dict()["elements"]:
        assert MASK not in (e["selector"] or ""), e
        assert PW not in json.dumps(e)
        if e["selector"]:
            assert d.find(e["selector"], timeout=0).name == {"Delete 5999": "Delete 5999",
                                                              "Delete 51234": "Delete 51234"}[e["name"]]
        else:  # the name holds a long secret: no selector rather than a broken one
            assert e["name"] == f"Reset {MASK}"
    assert PW not in obs.text()


def test_field_value_equal_to_a_short_secret_is_masked(orders):
    _be, d, amt = orders
    Secret("4321")
    amt.value = "4321"
    obs = observe(d, screenshot=False)
    assert 'value="***"' in obs.text()
    assert "4321" not in json.dumps(obs.to_dict())


def test_tool_result_keeps_numbers_and_json_valid():
    Secret("1234")
    assert ToolResult("Clicked at (1234, 250); order 1234").text == "Clicked at (1234, 250); order 1234"
    Secret(PW)
    payload = json.dumps({"value": f"pw={PW}", "pin": "1234", "nested": [PW, 1234]})
    out = ToolResult(payload).text
    data = json.loads(out)  # still JSON
    # A JSON string leaf is a value: masked whole when it is a secret, even a short one.
    assert data == {"value": f"pw={MASK}", "pin": MASK, "nested": [MASK, 1234]}
    weird = Secret('a", "b": "cc')  # a long secret that spans JSON structure when quoted
    text = json.dumps({"a": weird.reveal()})
    assert json.loads(ToolResult(text).text) == {"a": MASK}
    assert ToolResult("1234").text == MASK  # a result that *is* a secret
    assert ToolResult(f"read {PW}").text == f"read {MASK}"


def test_typed_text_that_merely_contains_a_pin_is_not_a_secret(monkeypatch):
    be = MockBackend(width=800, height=600)
    w = be.add_window("Form", (0, 0, 400, 300))
    field = w.add_edit("Order", (20, 20, 200, 30))
    d = _desktop(be)
    Secret("1234")
    monkeypatch.setattr(mock.MockNode, "set_value", lambda self, text: False)
    monkeypatch.setattr(be, "type_text", lambda text, *a, **k: setattr(field, "value", "Order 1235"))
    with pytest.raises(ActionTimeout) as info:
        d.find('edit "Order"').set_text("Order 12345", native=False, verify="exact")
    msg = str(info.value)
    assert "secret" not in msg and "'Order 12345'" in msg and "'Order 1235'" in msg, msg
    field.value = ""
    monkeypatch.setattr(be, "type_text", lambda text, *a, **k: setattr(field, "value", text))
    el = d.find('edit "Order"')
    el.set_text("Order 12345", native=False)
    assert el.last_verification.to_dict()["expected"] == "Order 12345"


def test_text_built_from_a_secret_is_still_masked(monkeypatch):
    be = MockBackend(width=800, height=600)
    w = be.add_window("Form", (0, 0, 400, 300))
    field = w.add_edit("Pin", (20, 20, 200, 30))
    field.value = "x"
    d = _desktop(be)
    monkeypatch.setattr(mock.MockNode, "set_value", lambda self, text: False)
    monkeypatch.setattr(be, "type_text", lambda text, *a, **k: setattr(field, "value", "x12"))
    with pytest.raises(ActionTimeout) as info:
        d.find('edit "Pin"').set_text(Secret("123"), clear=False, native=False, verify="exact")
    msg = str(info.value)
    assert "x12" not in msg and "'x123'" not in msg and "secret" in msg, msg


# ============================================================ 4: scrub_exception

class AppError(Exception):
    def __init__(self, field):
        super().__init__("bad")
        self.field = field

    def __str__(self):
        return f"field {self.field} rejected"


def test_scrub_exception_keeps_the_type_and_pickles():
    Secret(PW)
    e = AppError(PW)
    scrub_exception(e)
    assert type(e) is AppError
    assert type(pickle.loads(pickle.dumps(e))) is AppError


def test_scrub_exception_scrubs_oserror_attributes_and_the_chain():
    Secret(PW)
    e = FileNotFoundError(2, f"No such file {PW}", f"C:/vault/{PW}.txt", None, f"D:/{PW}")
    scrub_exception(e)
    assert PW not in str(e) and PW not in repr(e.args) and type(e) is FileNotFoundError
    with pytest.raises(RuntimeError) as info:
        with scrubbing_errors():
            try:
                open(f"Z:/no/such/{PW}.txt")
            except OSError as inner:
                raise RuntimeError("outer") from inner
    assert PW not in str(info.value.__cause__)


def test_robot_scrubs_a_custom_str_error(tmp_path):
    Secret(PW)
    be = MockBackend(width=200, height=200)
    robot = Robot(_desktop(be), name="r", run_dir=tmp_path / "runs", sleep=lambda s: None)

    @robot.task
    def t():
        with robot.step("login", continue_on_error=True):
            raise AppError(PW)

    report = robot.run(save=False)
    step = next(s for s in report.steps if s.name == "login")
    assert step.error and "rejected" in step.error and PW not in step.error
    assert PW not in (step.traceback or "")


# ============================================================ 6: cost

def test_scrub_pattern_is_built_once_and_observations_scrubbed_once(orders, monkeypatch):
    _be, d, _amt = orders
    for i in range(10):
        Secret(f"Sup3r-secret-{i}-xyz")
    obs = observe(d, screenshot=False)
    built, calls = [], []
    real_compile, real_scrub_data = _secret._compile, observe_mod.scrub_data
    monkeypatch.setattr(_secret, "_compile", lambda *a: built.append(1) or real_compile(*a))
    monkeypatch.setattr(observe_mod, "scrub_data", lambda *a, **k: calls.append(1) or real_scrub_data(*a, **k))
    for _ in range(3):
        obs.to_dict()
        obs.text()
    assert len(built) <= 2  # one per strength, then cached
    assert len(calls) == 3  # once per Observation.to_dict, not once per element too


def test_read_text_and_reformatted_or_truncated_secrets_are_masked(orders):
    from locum._secret import mask_value

    Secret("4821")
    Secret("4111111111111111")
    Secret("Tr0ubdor-Vault-77")
    assert mask_value("4111 1111 1111 1111") == MASK  # the field groups the digits
    # A prefix of a secret is ordinary text (review 11: "Invoice" vs "Invoice-2024-001").
    Secret("Invoice-2024-001")
    assert mask_value("Invoice") == "Invoice" and mask_value("Invoice-2024") == "Invoice-2024"
    assert mask_value("411111") == "411111" and mask_value("Tr0ubdor") == "Tr0ubdor"
    assert mask_value("4821") == MASK
    assert mask_value("1920x1080") == "1920x1080" and mask_value("Total 4821.50") == "Total 4821.50"
