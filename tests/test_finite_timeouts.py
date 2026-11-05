"""Non-finite timeouts are rejected instead of silently waiting forever (None means "forever")."""

from __future__ import annotations

import math

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.wait import Deadline, wait_until

BAD = [math.inf, -math.inf, math.nan]


@pytest.mark.parametrize("value", BAD)
def test_wait_until_rejects_non_finite_timeout(value):
    with pytest.raises(ValueError, match="finite"):
        wait_until(lambda: False, timeout=value, sleep=lambda s: None)


@pytest.mark.parametrize("value", BAD)
def test_deadline_rejects_non_finite_timeout(value):
    with pytest.raises(ValueError, match="finite"):
        Deadline(value)


@pytest.mark.parametrize("value", BAD)
def test_wait_until_rejects_non_finite_interval(value):
    with pytest.raises(ValueError, match="interval"):
        wait_until(lambda: True, timeout=1, interval=value, sleep=lambda s: None)


@pytest.mark.parametrize("value", BAD)
def test_desktop_rejects_non_finite_timeouts(value):
    with pytest.raises(ValueError, match="finite"):
        Desktop(MockBackend(), default_timeout=value)
    d = Desktop(MockBackend(), sleep=lambda s: None, default_timeout=0)
    with pytest.raises(ValueError, match="finite"):
        d.wait_until(lambda: True, timeout=value)


def test_none_still_means_wait_forever_and_finite_values_work():
    assert wait_until(lambda: 42, timeout=None) == 42
    assert wait_until(lambda: "ok", timeout=0) == "ok"
    Deadline(None)
