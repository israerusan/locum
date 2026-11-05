"""Polling, retrying and deadlines.

Everything in pypilot that "waits for something" goes through this module so
behaviour is consistent: predicates are polled with an optional exponential
back-off, transient exceptions are tolerated (and attached to the final
:class:`~pypilot.errors.ActionTimeout` as its ``__cause__``), and both the sleep
function and the clock are injectable so tests never actually sleep.

    >>> from pypilot.wait import wait_until
    >>> wait_until(lambda: 42, timeout=0)
    42
"""

from __future__ import annotations

import functools
import math
import time
from typing import Any, Callable, Optional, Tuple, Type, TypeVar, Union

from .errors import ActionTimeout, FailSafeTriggered, PolicyViolation

T = TypeVar("T")

ExceptionTypes = Union[Type[BaseException], Tuple[Type[BaseException], ...]]

__all__ = ["wait_until", "wait_while", "retry", "retry_call", "Deadline", "NEVER_SWALLOWED"]

#: Stop requests that polling and retrying never treat as transient: an emergency
#: stop (fail-safe corner, :func:`pypilot.failsafe.abort`, kill hotkey) or a
#: safety-policy refusal must end the automation, not be retried until a timeout.
NEVER_SWALLOWED: Tuple[Type[BaseException], ...] = (FailSafeTriggered, PolicyViolation)


def _check_timeout(timeout: Optional[float]) -> Optional[float]:
    if timeout is None:
        return None
    timeout = float(timeout)
    if math.isnan(timeout) or timeout < 0:
        raise ValueError(f"timeout must be >= 0 or None, got {timeout!r}")
    return timeout


def wait_until(
    predicate: Callable[[], T],
    timeout: Optional[float] = 10.0,
    interval: float = 0.1,
    *,
    message: Optional[str] = None,
    backoff: float = 1.0,
    max_interval: float = 1.0,
    ignore_exceptions: Union[bool, ExceptionTypes] = True,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Poll ``predicate`` until it returns a truthy value and return that value.

    Args:
        predicate: Zero-argument callable. Its first truthy return value is returned.
        timeout: Seconds to keep trying. ``0`` performs exactly one check;
            ``None`` waits forever.
        interval: Initial delay between checks (seconds).
        message: Message of the :class:`ActionTimeout` raised on expiry.
        backoff: Multiplier applied to the interval after every failed check (``1`` = constant).
        max_interval: Upper bound for the (backed-off) interval.
        ignore_exceptions: ``True`` swallows any :class:`Exception` raised by the
            predicate (treated as "not yet"); an exception class/tuple swallows
            only those; ``False`` propagates everything. The last swallowed
            exception becomes the ``__cause__`` of the timeout error.
            :data:`NEVER_SWALLOWED` errors (fail-safe / abort, policy
            violations) always propagate.
        sleep: Sleep function (inject a no-op in tests).
        clock: Monotonic clock.

    Raises:
        ActionTimeout: if the deadline passes without a truthy result.
    """
    timeout = _check_timeout(timeout)
    if interval < 0:
        raise ValueError("interval must be >= 0")
    if backoff <= 0:
        raise ValueError("backoff must be > 0")
    if ignore_exceptions is True:
        swallow: Tuple[Type[BaseException], ...] = (Exception,)
    elif ignore_exceptions is False or ignore_exceptions is None:
        swallow = ()
    elif isinstance(ignore_exceptions, tuple):
        swallow = ignore_exceptions
    else:
        swallow = (ignore_exceptions,)  # type: ignore[assignment]

    start = clock()
    deadline = None if timeout is None else start + timeout
    delay = float(interval)
    last_exc: Optional[BaseException] = None
    attempts = 0
    while True:
        attempts += 1
        try:
            result = predicate()
        except NEVER_SWALLOWED:
            raise
        except swallow as exc:  # type: ignore[misc]
            last_exc = exc
        else:
            if result:
                return result
        now = clock()
        if deadline is not None and now >= deadline:
            break
        pause = delay
        if deadline is not None:
            pause = min(pause, max(0.0, deadline - now))
        if pause > 0:
            sleep(pause)
        elif deadline is not None and timeout == 0:
            break
        delay = min(max(delay * backoff, 0.0), max(max_interval, interval))
    msg = message or f"Condition not met within {timeout:g}s ({attempts} checks)"
    if last_exc is not None and message is None:
        msg += f"; last error: {type(last_exc).__name__}: {last_exc}"
    raise ActionTimeout(msg) from last_exc


def wait_while(predicate: Callable[[], Any], timeout: Optional[float] = 10.0, interval: float = 0.1, **kw: Any) -> bool:
    """Poll until ``predicate`` becomes falsy. Returns ``True``; raises :class:`ActionTimeout` otherwise.

    Accepts the same keyword arguments as :func:`wait_until`. Exceptions raised by
    the predicate count as "still true" while ``ignore_exceptions`` allows them.
    """
    return wait_until(lambda: not predicate(), timeout, interval, **kw)


def _compute_delays(attempts: int, delay: float, backoff: float, max_delay: float):
    d = float(delay)
    for _ in range(max(0, attempts - 1)):
        yield min(d, max_delay)
        d *= backoff


def retry_call(
    fn: Callable[..., T],
    *args: Any,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    max_delay: float = 30.0,
    exceptions: ExceptionTypes = (Exception,),
    on_retry: Optional[Callable[[int, BaseException, float], Any]] = None,
    sleep: Callable[[float], Any] = time.sleep,
    **kwargs: Any,
) -> T:
    """Call ``fn(*args, **kwargs)``, retrying on ``exceptions``.

    Args:
        attempts: Total number of tries (>= 1).
        delay: Delay before the first retry; multiplied by ``backoff`` each time, capped at ``max_delay``.
        exceptions: Exception class or tuple that triggers a retry. Anything else propagates
            immediately, as do :data:`NEVER_SWALLOWED` errors (fail-safe / abort, policy violations).
        on_retry: ``on_retry(attempt_number, exception, next_delay)`` called before each retry sleep.

    The exception of the last attempt is re-raised unchanged.
    """
    attempts = int(attempts)
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if delay < 0 or max_delay < 0:
        raise ValueError("delay and max_delay must be >= 0")
    if not isinstance(exceptions, tuple):
        exceptions = (exceptions,)
    delays = _compute_delays(attempts, delay, backoff, max_delay)
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(*args, **kwargs)
        except NEVER_SWALLOWED:
            raise
        except exceptions as exc:  # type: ignore[misc]
            if attempt >= attempts:
                raise
            wait_s = next(delays)
            if on_retry is not None:
                on_retry(attempt, exc, wait_s)
            if wait_s > 0:
                sleep(wait_s)


def retry(
    fn: Optional[Callable[..., T]] = None,
    *,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    max_delay: float = 30.0,
    exceptions: ExceptionTypes = (Exception,),
    on_retry: Optional[Callable[[int, BaseException, float], Any]] = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> Any:
    """Retry decorator usable with or without arguments.

    ::

        @retry
        def flaky(): ...

        @retry(attempts=5, delay=0.1, exceptions=(ElementNotFound,))
        def flaky2(): ...

        safe = retry(some_function, attempts=2)   # wrap an existing callable
        safe(arg1, arg2)

    See :func:`retry_call` for the meaning of the options.
    """
    if int(attempts) < 1:
        raise ValueError("attempts must be >= 1")

    def decorate(func: Callable[..., T]) -> Callable[..., T]:
        if not callable(func):
            raise TypeError(f"retry() needs a callable, got {func!r}")

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return retry_call(
                func,
                *args,
                attempts=attempts,
                delay=delay,
                backoff=backoff,
                max_delay=max_delay,
                exceptions=exceptions,
                on_retry=on_retry,
                sleep=sleep,
                **kwargs,
            )

        return wrapper

    if fn is not None:
        return decorate(fn)
    return decorate


class Deadline:
    """A point in time ``timeout`` seconds from now.

    ``timeout=None`` never expires. Useful to share one time budget across
    several waits::

        dl = Deadline(5)
        win = desktop.window("Login", timeout=dl.remaining)
        win.find("edit", timeout=dl.remaining)
    """

    def __init__(
        self,
        timeout: Optional[float],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = time.sleep,
    ):
        self.timeout = _check_timeout(timeout)
        self._clock = clock
        self._sleep = sleep
        self.start = clock()

    @property
    def end(self) -> Optional[float]:
        return None if self.timeout is None else self.start + self.timeout

    @property
    def elapsed(self) -> float:
        return max(0.0, self._clock() - self.start)

    @property
    def remaining(self) -> Optional[float]:
        """Seconds left (never negative); ``None`` for an infinite deadline."""
        if self.timeout is None:
            return None
        return max(0.0, self.timeout - (self._clock() - self.start))

    @property
    def expired(self) -> bool:
        return self.timeout is not None and self._clock() - self.start >= self.timeout

    def sleep(self, interval: float) -> float:
        """Sleep ``interval`` seconds, but never past the deadline. Returns the time actually slept."""
        interval = max(0.0, float(interval))
        rem = self.remaining
        if rem is not None:
            interval = min(interval, rem)
        if interval > 0:
            self._sleep(interval)
        return interval

    def raise_if_expired(self, message: Optional[str] = None) -> None:
        if self.expired:
            raise ActionTimeout(message or f"Deadline of {self.timeout:g}s expired")

    def __bool__(self) -> bool:  # truthy while time remains
        return not self.expired

    def __repr__(self) -> str:
        rem = self.remaining
        return f"<Deadline timeout={self.timeout} remaining={'inf' if rem is None else f'{rem:.3f}'}>"
