"""Read-back verification modes for financial fields (Element.set_text / Element.type).

The review that prompted these: "1,000" compared equal to "1.00" (and to "1": a
1000x error), a field that could not be read back counted as verified, and
there was no exact mode for document numbers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from _stub_desktop import StubDesktop
from locum.backends.mock import MockBackend
from locum.element import Element, VerifyResult, _matches, _parse_number, _same_text
from locum.errors import ActionTimeout, VerificationUnavailable


# ------------------------------------------------------------ the comparison rules
@pytest.mark.parametrize("actual,expected", [
    ("1,000", "1.00"),   # 1000 vs 1: only "equal" if 1,000 is read as German 1.000
    ("1,000", "1"),
    ("1.000", "1"),
    ("12,345", "12.345"),
])
def test_normalized_never_equates_locale_ambiguous_numbers(actual, expected):
    assert not _same_text(actual, expected)


@pytest.mark.parametrize("actual,expected", [
    ("1,250.00", "1250"),    # unambiguous reformatting is still accepted
    ("1.250,00", "1250"),
    ("1.250,00-", "-1250"),  # SAP negative
    ("(1,250.00)", "-1250"),
    ("ACME corp", "acme  CORP"),
    ("0001234", "1234"),     # SAP alpha conversion: fine for normalized, not for exact
    ("12,345", "12,345"),    # identical text needs no number parsing
])
def test_normalized_still_accepts_unambiguous_reformatting(actual, expected):
    assert _same_text(actual, expected)


def test_known_decimal_resolves_the_ambiguity():
    assert _same_text("1,000", "1", decimal=",")       # German: 1,000 == 1
    assert not _same_text("1,000", "1", decimal=".")   # English: 1,000 == 1000
    assert _same_text("12,345", "12345", decimal=".")


def test_parse_number_with_decimal_has_one_reading():
    assert _parse_number("1,000") == {Decimal("1000"), Decimal("1.000")}
    assert _parse_number("1,000", ".") == {Decimal("1000")}
    assert _parse_number("1,000", ",") == {Decimal("1.000")}
    assert _parse_number("1.250,00-", ",") == {Decimal("-1250.00")}
    assert _parse_number("1.250,00", ".") == set()  # not a valid English number
    with pytest.raises(ValueError):
        _parse_number("1", decimal=";")


@pytest.mark.parametrize("actual,expected,ok", [
    ("0001234", "0001234", True),
    ("0001234", "1234", False),
    ("5100012345", "5100012345 ", False),
    ("abc", "ABC", False),
])
def test_exact_mode(actual, expected, ok):
    assert _matches(actual, expected, "exact", None) is ok


@pytest.mark.parametrize("actual,expected,dec,ok", [
    ("1,250.00", "1250", ".", True),
    ("1.250,00", "1250", ",", True),
    ("1.250,00-", "-1250", ",", True),
    ("1,000", "1", ".", False),
    ("1,000", "1", ",", True),
    ("1,000", "1.00", ".", False),
    ("n/a", "0", ".", False),
])
def test_amount_mode(actual, expected, dec, ok):
    assert _matches(actual, expected, "amount", dec) is ok


# ------------------------------------------------------------ set_text end to end
def _form(on_change=None, value=""):
    be = MockBackend(width=1280, height=800)
    d = StubDesktop(be)
    win = be.add_window("Invoice", (0, 0, 800, 600), app="sap.exe")
    field = win.add_edit("Amount", (10, 10, 200, 24), value=value, on_change=on_change)
    return d, be, field


def _german_quantity(widget):
    # The app shows the typed integer with three decimals, German style: "1" -> "1,000"
    v = widget.value
    if v and v.isdigit():
        widget.value = f"{v},000"


def test_set_text_refuses_an_ambiguous_read_back_by_default():
    d, be, _ = _form(on_change=_german_quantity)
    el = d.find('edit "Amount"')
    with pytest.raises(ActionTimeout) as exc:
        el.set_text("1", native=True)
    assert "decimal=" in str(exc.value)  # the message says how to resolve it


def test_set_text_accepts_it_with_the_decimal_separator():
    d, be, _ = _form(on_change=_german_quantity)
    el = d.find('edit "Amount"')
    el.set_text("1", native=True, decimal=",")
    assert el.last_verification == VerifyResult("verified", "normalized", "1", "1,000")
    el.set_text("1", native=True, verify="amount", decimal=",")
    assert el.last_verification.verified


def test_desktop_decimal_separator_is_the_default():
    d, be, _ = _form(on_change=_german_quantity)
    d.decimal_separator = ","
    el = d.find('edit "Amount"')
    el.set_text("1", native=True, verify="amount")
    assert el.last_verification.status == "verified"


def test_amount_mode_needs_a_decimal_separator():
    d, be, _ = _form()
    with pytest.raises(ValueError, match="decimal"):
        d.find('edit "Amount"').set_text("1", verify="amount")


def test_exact_mode_rejects_alpha_conversion():
    def strip_zeros(w):
        w.value = w.value.lstrip("0")

    d, be, _ = _form(on_change=strip_zeros)
    el = d.find('edit "Amount"')
    el.set_text("0001234", native=True)  # normalized: numerically the same document number
    with pytest.raises(ActionTimeout):
        el.set_text("0001234", native=True, verify="exact")


def test_exact_mode_on_the_typing_path():
    d, be, field = _form()
    el = d.find('edit "Amount"')
    el.set_text("5100012345", native=False, verify="exact")
    assert field.value == "5100012345"
    assert el.last_verification == VerifyResult("verified", "exact", "5100012345", "5100012345")


def test_verify_false_is_recorded_as_skipped():
    d, be, _ = _form()
    el = d.find('edit "Amount"')
    el.set_text("7", verify=False)
    assert el.last_verification.status == "skipped"
    el.set_text("8", native=False, verify=False)
    assert el.last_verification.status == "skipped"


def test_bad_verify_value():
    d, be, _ = _form()
    with pytest.raises(ValueError, match="verify"):
        d.find('edit "Amount"').set_text("7", verify="strict")


# ------------------------------------------------------------ unreadable fields
@pytest.fixture
def unreadable(monkeypatch):
    monkeypatch.setattr(Element, "value", property(lambda self: None))


@pytest.mark.parametrize("native", [True, False])
def test_unreadable_field_is_unverified_not_verified(unreadable, native):
    d, be, _ = _form()
    el = d.find('edit "Amount"')
    el.set_text("1250", native=native)
    assert el.last_verification.status == "unverified"
    assert not el.last_verification.verified


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("mode,dec", [("exact", None), ("amount", ".")])
def test_strict_modes_raise_on_an_unreadable_field(unreadable, native, mode, dec):
    d, be, _ = _form()
    el = d.find('edit "Amount"')
    with pytest.raises(VerificationUnavailable):
        el.set_text("1250", native=native, verify=mode, decimal=dec)
    assert el.last_verification.status == "unverified"


def test_password_field_is_unverified(monkeypatch):
    import locum._nodeinfo as ni

    monkeypatch.setattr(ni, "is_password", lambda node: True)
    d, be, _ = _form()
    el = d.find('edit "Amount"')
    el.set_text("s3cret", native=False)
    assert el.last_verification.status == "unverified"
    assert "password" in el.last_verification.reason
    with pytest.raises(VerificationUnavailable):
        el.set_text("s3cret", native=False, verify="exact")


def test_unverified_is_reported_to_listeners(unreadable):
    from locum.element import add_action_listener, remove_action_listener

    d, be, _ = _form()
    seen = []
    listener = lambda kind, el, data: seen.append((kind, data.get("verification")))  # noqa: E731
    add_action_listener(be, listener)
    try:
        d.find('edit "Amount"').set_text("1250", native=False)
    finally:
        remove_action_listener(be, listener)
    assert ("verify", VerifyResult("unverified", "normalized", "1250", None,
                                   "the field exposes no readable value")) in seen


# ------------------------------------------------------------ Element.type
def test_type_exact_and_amount():
    d, be, field = _form()
    el = d.find('edit "Amount"')
    el.type("4711", verify="exact")
    assert el.last_verification.status == "verified"
    field.value = ""
    el.type("1.250,00", verify="amount", decimal=",")
    assert el.last_verification == VerifyResult("verified", "amount", "1.250,00", "1.250,00")


def test_type_amount_needs_an_empty_field():
    d, be, field = _form(value="99")
    with pytest.raises(ValueError, match="empty field"):
        d.find('edit "Amount"').type("1", verify="amount", decimal=".")
    assert field.value == "99"  # nothing was typed


def test_type_strict_refuses_before_typing_into_an_unreadable_field(unreadable):
    d, be, field = _form()
    with pytest.raises(VerificationUnavailable):
        d.find('edit "Amount"').type("4711", verify="exact")
    assert field.value == ""  # not a single key was sent


def test_type_normalized_unreadable_is_unverified(unreadable):
    d, be, field = _form()
    el = d.find('edit "Amount"')
    el.type("4711")
    assert field.value == "4711"
    assert el.last_verification.status == "unverified"
