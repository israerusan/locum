from __future__ import annotations

import pytest

from locum.errors import ActionTimeout
from locum.wait import Deadline, retry, retry_call, wait_until, wait_while


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def counter(succeed_at, value="ok"):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return value if calls["n"] >= succeed_at else None

    return fn, calls


# ------------------------------------------------------------------ wait_until
def test_wait_until_returns_first_truthy_value():
    clk = FakeClock()
    fn, calls = counter(3, value={"x": 1})
    assert wait_until(fn, 5, 0.1, sleep=clk.sleep, clock=clk) == {"x": 1}
    assert calls["n"] == 3
    assert clk.sleeps == [0.1, 0.1]


def test_wait_until_timeout_zero_is_single_check():
    clk = FakeClock()
    fn, calls = counter(2)
    with pytest.raises(ActionTimeout):
        wait_until(fn, 0, sleep=clk.sleep, clock=clk)
    assert calls["n"] == 1
    assert clk.sleeps == []


def test_wait_until_timeout_message_and_cause():
    clk = FakeClock()

    def boom():
        raise RuntimeError("nope")

    with pytest.raises(ActionTimeout) as ei:
        wait_until(boom, 1, 0.25, sleep=clk.sleep, clock=clk)
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert "nope" in str(ei.value)
    with pytest.raises(ActionTimeout, match="custom"):
        wait_until(lambda: False, 0.5, 0.1, message="custom", sleep=clk.sleep, clock=clk)


def test_wait_until_is_timeout_error_subclass():
    clk = FakeClock()
    with pytest.raises(TimeoutError):
        wait_until(lambda: 0, 0.2, 0.1, sleep=clk.sleep, clock=clk)


def test_wait_until_never_sleeps_past_deadline():
    clk = FakeClock()
    with pytest.raises(ActionTimeout):
        wait_until(lambda: None, 0.25, 0.1, sleep=clk.sleep, clock=clk)
    assert sum(clk.sleeps) == pytest.approx(0.25)
    assert max(clk.sleeps) <= 0.1 + 1e-9


def test_wait_until_backoff_capped():
    clk = FakeClock()
    with pytest.raises(ActionTimeout):
        wait_until(lambda: None, 10, 0.1, backoff=2.0, max_interval=0.5, sleep=clk.sleep, clock=clk)
    assert clk.sleeps[:4] == pytest.approx([0.1, 0.2, 0.4, 0.5])
    assert max(clk.sleeps) == pytest.approx(0.5)


def test_wait_until_exception_handling_modes():
    clk = FakeClock()
    with pytest.raises(KeyError):
        wait_until(lambda: {}["x"], 1, sleep=clk.sleep, clock=clk, ignore_exceptions=False)
    # only listed exceptions are swallowed
    with pytest.raises(ZeroDivisionError):
        wait_until(lambda: 1 / 0, 1, sleep=clk.sleep, clock=clk, ignore_exceptions=(KeyError,))
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise KeyError("x")
        return "done"

    assert wait_until(flaky, 1, 0.01, sleep=clk.sleep, clock=clk, ignore_exceptions=KeyError) == "done"


def test_wait_until_none_timeout_waits_until_success():
    clk = FakeClock()
    fn, calls = counter(50)
    assert wait_until(fn, None, 1.0, sleep=clk.sleep, clock=clk) == "ok"
    assert calls["n"] == 50


def test_wait_until_validates_args():
    with pytest.raises(ValueError):
        wait_until(lambda: 1, -1)
    with pytest.raises(ValueError):
        wait_until(lambda: 1, 1, -0.1)
    with pytest.raises(ValueError):
        wait_until(lambda: 1, 1, backoff=0)


def test_wait_while():
    clk = FakeClock()
    seq = iter([True, True, False])
    assert wait_while(lambda: next(seq), 1, 0.1, sleep=clk.sleep, clock=clk) is True
    with pytest.raises(ActionTimeout):
        wait_while(lambda: True, 0.3, 0.1, sleep=clk.sleep, clock=clk)


# ----------------------------------------------------------------------- retry
def test_retry_call_retries_then_succeeds_with_backoff():
    sleeps = []
    seen = []
    state = {"n": 0}

    def flaky(a, b=0):
        state["n"] += 1
        if state["n"] < 3:
            raise ValueError(state["n"])
        return a + b

    out = retry_call(flaky, 1, b=2, attempts=4, delay=0.5, backoff=2, sleep=sleeps.append,
                     on_retry=lambda i, e, d: seen.append((i, str(e), d)))
    assert out == 3
    assert sleeps == [0.5, 1.0]
    assert seen == [(1, "1", 0.5), (2, "2", 1.0)]


def test_retry_call_reraises_last_and_respects_exception_filter():
    sleeps = []
    with pytest.raises(ValueError, match="3"):
        state = {"n": 0}

        def always():
            state["n"] += 1
            raise ValueError(state["n"])

        retry_call(always, attempts=3, delay=0.1, sleep=sleeps.append)
    assert len(sleeps) == 2
    calls = {"n": 0}

    def wrong():
        calls["n"] += 1
        raise KeyError("x")

    with pytest.raises(KeyError):
        retry_call(wrong, attempts=5, exceptions=ValueError, sleep=sleeps.append)
    assert calls["n"] == 1


def test_retry_call_max_delay_and_validation():
    sleeps = []
    with pytest.raises(RuntimeError):
        retry_call(lambda: (_ for _ in ()).throw(RuntimeError()), attempts=5, delay=1, backoff=10, max_delay=3, sleep=sleeps.append)
    assert sleeps == [1, 3, 3, 3]
    with pytest.raises(ValueError):
        retry_call(lambda: 1, attempts=0)


def test_retry_decorator_forms():
    sleeps = []
    state = {"n": 0}

    @retry
    def bare():
        return "bare"

    assert bare() == "bare"
    assert bare.__name__ == "bare"

    @retry(attempts=3, delay=0.01, sleep=sleeps.append)
    def flaky(x):
        state["n"] += 1
        if state["n"] < 2:
            raise OSError("x")
        return x * 2

    assert flaky(21) == 42
    assert sleeps == [0.01]

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError()
        return "wrapped"

    assert retry(fn, attempts=2, delay=0, sleep=sleeps.append)() == "wrapped"
    with pytest.raises(ValueError):
        retry(attempts=0)
    with pytest.raises(TypeError):
        retry()(42)


# -------------------------------------------------------------------- Deadline
def test_deadline():
    clk = FakeClock()
    d = Deadline(1.0, clock=clk, sleep=clk.sleep)
    assert d.remaining == pytest.approx(1.0) and not d.expired and d
    assert d.sleep(0.4) == pytest.approx(0.4)
    assert d.remaining == pytest.approx(0.6)
    assert d.elapsed == pytest.approx(0.4)
    assert d.sleep(5) == pytest.approx(0.6)  # clipped at the deadline
    assert d.expired and not d and d.remaining == 0.0
    with pytest.raises(ActionTimeout):
        d.raise_if_expired()
    assert d.sleep(1) == 0.0
    assert "Deadline" in repr(d)


def test_deadline_infinite_and_invalid():
    clk = FakeClock()
    d = Deadline(None, clock=clk, sleep=clk.sleep)
    assert d.remaining is None and not d.expired and d.end is None
    assert d.sleep(2) == 2
    d.raise_if_expired()
    with pytest.raises(ValueError):
        Deadline(-1)
    z = Deadline(0, clock=clk)
    assert z.expired
