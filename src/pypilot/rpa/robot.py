"""Code-first RPA robots: tasks, steps, retries, evidence and reports.

::

    from pypilot.rpa import Robot

    robot = Robot(name="invoices", run_dir="runs")

    @robot.task(retries=2, retry_delay=5)
    def enter_invoices():
        d = robot.desktop
        with robot.step("open app"):
            d.launch("erp.exe", wait_window="ERP")
        with robot.step("fill form"):
            d.find('edit "Customer"').set_text(robot.vars["customer"])
            d.click('button "Save"')
        robot.log("saved", customer=robot.vars["customer"])

    report = robot.run()            # -> RunReport; runs/<run-id>/report.{json,html}
    assert report.ok, report.summary()

Features:

* **Tasks** (``@robot.task``) with retries + exponential backoff, a
  cooperative ``timeout`` and ``continue_on_error``. A task function may take
  no argument or one (the robot).
* **Steps** as context managers (``with robot.step("name"):``) that nest and
  are timed individually. A step with retries is written as a loop of
  attempts, or wraps a callable::

      for attempt in robot.step("login", retries=3, retry_delay=1):
          with attempt:
              d.find('button "Login"').click()

      total = robot.step("read total", retries=2).run(read_total, "Q3")

* **Evidence**: on failure a screenshot is saved to ``run_dir/<run-id>/`` and
  linked from the step result (and embedded in the HTML report).
* **Structured logging**: :meth:`Robot.log` writes JSON-able records to the
  report and to the standard :mod:`logging` logger ``pypilot.robot.<name>``
  (record attribute ``pypilot`` carries the fields).
* :class:`~pypilot.rpa.queue.BusinessException` and
  :class:`~pypilot.errors.FailSafeTriggered` are never retried; a fail-safe
  aborts the whole run.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import logging
import os
import pathlib
import re
import time
import traceback as _tb
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Union

from ..errors import ActionTimeout, FailSafeTriggered
from .queue import BusinessException
from .report import FAILED, PASSED, SKIPPED, RunReport, StepResult

if TYPE_CHECKING:  # pragma: no cover
    from ..desktop import Desktop

__all__ = ["Robot", "Step", "Attempt", "Task", "save_error_screenshot"]

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHOT_ATTR = "_pypilot_screenshot"


def _slug(text: str, limit: int = 48) -> str:
    s = _SLUG_RE.sub("-", str(text)).strip("-.")
    return (s[:limit].rstrip("-.") or "step")


def _new_run_id(name: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{_slug(name, 32)}-{uuid.uuid4().hex[:6]}"


def _exc_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _format_tb(exc: BaseException) -> str:
    return "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))


def save_error_screenshot(desktop: Any, directory: Union[str, "os.PathLike[str]"], name: str) -> str:
    """Capture the screen with ``desktop`` and save it as ``directory/name.png``. Returns the path.

    Raises whatever the capture raises; callers decide how to handle a failed capture.
    """
    d = pathlib.Path(directory).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_slug(name, 80)}.png"
    n = 1
    while path.exists():
        n += 1
        path = d / f"{_slug(name, 80)}-{n}.png"
    img = desktop.screenshot()
    img.save(str(path), format="PNG")
    return str(path)


@dataclass
class Task:
    """A registered robot task (see :meth:`Robot.task`)."""

    name: str
    fn: Callable[..., Any]
    retries: int = 0
    retry_delay: float = 1.0
    timeout: Optional[float] = None
    continue_on_error: bool = False
    backoff: float = 2.0
    pass_robot: bool = False
    description: str = ""


@dataclass
class _Frame:
    result: StepResult
    deadline: Optional[float] = None
    timeout: Optional[float] = None
    label: str = ""
    children_start: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


class Attempt:
    """One try of a retried step (``for attempt in robot.step(..., retries=n): with attempt: ...``).

    The ``with`` block's exception is captured (and suppressed) so the loop can
    decide whether to retry; the step re-raises it after the last attempt.
    """

    def __init__(self, step: "Step", number: int):
        self.step = step
        self.number = number
        self.error: Optional[BaseException] = None
        self.entered = False

    def __enter__(self) -> "Attempt":
        self.entered = True
        self.step.robot.check_timeout()
        return self

    def __exit__(self, exc_type: Any, exc: Optional[BaseException], tb: Any) -> bool:
        if exc is None:
            return False
        if not isinstance(exc, Exception):  # KeyboardInterrupt, SystemExit, GeneratorExit
            return False
        self.error = exc
        return True

    def __repr__(self) -> str:
        return f"<Attempt {self.number} of step {self.step.name!r}>"


class Step:
    """A timed, optionally retried unit of work (create with :meth:`Robot.step`).

    Use it as a context manager (one attempt), iterate it to get retryable
    :class:`Attempt` blocks, call :meth:`run` with a callable, or use it as a
    decorator.
    """

    def __init__(
        self,
        robot: "Robot",
        name: str,
        retries: int = 0,
        *,
        retry_delay: float = 0.0,
        backoff: float = 2.0,
        continue_on_error: bool = False,
        screenshot: Optional[bool] = None,
    ):
        if int(retries) < 0:
            raise ValueError("retries must be >= 0")
        if retry_delay < 0:
            raise ValueError("retry_delay must be >= 0")
        self.robot = robot
        self.name = str(name)
        self.retries = int(retries)
        self.retry_delay = float(retry_delay)
        self.backoff = float(backoff)
        self.continue_on_error = bool(continue_on_error)
        self.screenshot = screenshot
        self._frame: Optional[_Frame] = None
        self.result: Optional[StepResult] = None

    # -- single attempt ----------------------------------------------------------
    def __enter__(self) -> "Step":
        if self._frame is not None:
            raise RuntimeError(f"Step {self.name!r} is already running")
        if self.retries:
            # A 'with' body cannot be re-executed; silently running once would drop the retries.
            raise TypeError(
                f"step {self.name!r} has retries={self.retries}, but a plain 'with' block runs only once; "
                "write 'for attempt in robot.step(name, retries=n):' + 'with attempt:' or use "
                "robot.step(name, retries=n).run(fn, ...) to retry"
            )
        self._frame = self.robot._open(self.name, "step")
        self.result = self._frame.result
        try:
            self.robot.check_timeout()
        except BaseException as exc:
            self._finish(exc)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Optional[BaseException], tb: Any) -> bool:
        if exc is None:
            try:
                self.robot.check_timeout()
            except ActionTimeout as timeout_exc:
                suppressed = self._finish(timeout_exc)
                if suppressed:
                    return False
                raise
        return self._finish(exc)

    def _finish(self, exc: Optional[BaseException]) -> bool:
        frame, self._frame = self._frame, None
        assert frame is not None
        return self.robot._close(frame, exc, continue_on_error=self.continue_on_error, screenshot=self.screenshot)

    # -- retried forms -------------------------------------------------------------
    def __iter__(self) -> Iterator[Attempt]:
        if self._frame is not None:
            raise RuntimeError(f"Step {self.name!r} is already running")
        frame = self._frame = self.robot._open(self.name, "step")
        self.result = frame.result
        robot = self.robot
        delay = self.retry_delay
        attempts = self.retries + 1
        last: Optional[BaseException] = None
        for n in range(1, attempts + 1):
            frame.result.attempts = n
            mark = len(robot._report().steps)
            attempt = Attempt(self, n)
            try:
                yield attempt
            except GeneratorExit:  # the caller broke out of the loop
                self._frame = None
                robot._close(frame, attempt.error, continue_on_error=True, screenshot=self.screenshot)
                raise
            last = attempt.error
            if last is None:
                self._frame = None
                robot._close(frame, None)
                return
            if not robot._retryable(last) or n >= attempts:
                break
            robot._handle_retry(frame, mark, n, attempts, last, delay)
            delay *= self.backoff
        self._frame = None
        assert last is not None
        if not robot._close(frame, last, continue_on_error=self.continue_on_error, screenshot=self.screenshot):
            raise last

    def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn(*args, **kwargs)`` as this step, with retries. Returns its result
        (``None`` when the failure was swallowed by ``continue_on_error``)."""
        result: Any = None
        for attempt in self:
            with attempt:
                result = fn(*args, **kwargs)
        return result

    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator: every call of ``fn`` runs as this step."""
        import functools

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return Step(
                self.robot, self.name, self.retries, retry_delay=self.retry_delay, backoff=self.backoff,
                continue_on_error=self.continue_on_error, screenshot=self.screenshot,
            ).run(fn, *args, **kwargs)

        return wrapper

    def __repr__(self) -> str:
        return f"<Step {self.name!r} retries={self.retries}>"


class Robot:
    """An RPA robot (see module docstring).

    Args:
        desktop: The :class:`~pypilot.desktop.Desktop` to drive (created lazily
            with the default backend on first use when ``None``).
        name: Robot name (report title, logger name, run-id component).
        run_dir: Parent directory of the per-run evidence folders.
        screenshot_on_error: Save a screenshot when a task/step fails.
        logger: A :class:`logging.Logger` (default ``pypilot.robot.<name>``).
        save_reports: Write ``report.json`` and ``report.html`` into the run
            directory at the end of :meth:`run` / :meth:`finish`.
        sleep: Sleep function for retry delays (default: the desktop's sleep, which honours the abort flag).
        clock: Monotonic clock used for durations and timeouts.

    Attributes:
        vars: Free-form dict shared by all tasks (survives across runs).
        report: The current/last :class:`RunReport`.
        tasks: Registered tasks by name, in registration order.
    """

    def __init__(
        self,
        desktop: Optional["Desktop"] = None,
        name: str = "robot",
        run_dir: Union[str, "os.PathLike[str]"] = "pypilot_runs",
        screenshot_on_error: bool = True,
        logger: Optional[logging.Logger] = None,
        *,
        save_reports: bool = True,
        sleep: Optional[Callable[[float], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._desktop = desktop
        self.name = str(name)
        self.run_dir = pathlib.Path(run_dir).expanduser()
        self.screenshot_on_error = bool(screenshot_on_error)
        self.logger = logger or logging.getLogger(f"pypilot.robot.{_slug(self.name)}")
        self.save_reports = bool(save_reports)
        self._sleep = sleep
        self._clock = clock
        self.vars: Dict[str, Any] = {}
        self.tasks: Dict[str, Task] = {}
        self._setup: List[Callable[..., Any]] = []
        self._teardown: List[Callable[..., Any]] = []
        self._stack: List[_Frame] = []
        self.report: Optional[RunReport] = None
        self._active = False
        self._shot_seq = 0

    # ----------------------------------------------------------------- plumbing
    @property
    def desktop(self) -> "Desktop":
        """The desktop this robot drives (created on first access when not given)."""
        if self._desktop is None:
            from ..desktop import Desktop

            self._desktop = Desktop()
        return self._desktop

    @desktop.setter
    def desktop(self, value: "Desktop") -> None:
        self._desktop = value

    def sleep(self, seconds: float) -> None:
        """Sleep (via the injected sleep / the desktop's sleep), then check the current task timeout."""
        seconds = float(seconds)
        if seconds > 0:
            if self._sleep is not None:
                self._sleep(seconds)
            elif self._desktop is not None:
                self._desktop.sleep(seconds)
            else:
                time.sleep(seconds)
        self.check_timeout()

    @property
    def run_id(self) -> Optional[str]:
        return self.report.run_id if self.report is not None else None

    @property
    def run_path(self) -> pathlib.Path:
        """``run_dir/<run-id>`` for the current run (not created until something is written)."""
        return self.run_dir / self._report().run_id

    @property
    def current_path(self) -> str:
        """Path of the innermost running task/step (``""`` outside any)."""
        return self._stack[-1].result.path if self._stack else ""

    def _report(self) -> RunReport:
        if self.report is None or not self._active:
            self.start()
        assert self.report is not None
        return self.report

    # -------------------------------------------------------------- registration
    def task(
        self,
        name: Union[str, Callable[..., Any], None] = None,
        retries: int = 0,
        retry_delay: float = 1.0,
        timeout: Optional[float] = None,
        continue_on_error: bool = False,
        *,
        backoff: float = 2.0,
    ) -> Any:
        """Register a task. Usable as ``@robot.task`` or ``@robot.task(name=..., retries=...)``.

        Args:
            name: Task name (default: the function name).
            retries: Extra attempts after a failure (system errors only).
            retry_delay: Seconds before the first retry; multiplied by ``backoff`` each retry.
            timeout: Seconds the task may take. Enforced cooperatively: checked
                when steps start/end, on :meth:`log`/:meth:`sleep` and when the
                task returns (an over-running task fails with
                :class:`~pypilot.errors.ActionTimeout`).
            continue_on_error: Keep running the following tasks when this one fails
                (the failure still counts in the report).
        """
        if callable(name):
            return self.task()(name)
        if int(retries) < 0:
            raise ValueError("retries must be >= 0")
        if retry_delay < 0:
            raise ValueError("retry_delay must be >= 0")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be > 0")

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            if not callable(fn):
                raise TypeError(f"@task needs a callable, got {fn!r}")
            task_name = str(name or getattr(fn, "__name__", "task"))
            if task_name in self.tasks:
                raise ValueError(f"A task named {task_name!r} is already registered")
            self.tasks[task_name] = Task(
                name=task_name,
                fn=fn,
                retries=int(retries),
                retry_delay=float(retry_delay),
                timeout=None if timeout is None else float(timeout),
                continue_on_error=bool(continue_on_error),
                backoff=float(backoff),
                pass_robot=_wants_argument(fn),
                description=inspect.getdoc(fn) or "",
            )
            return fn

        return register

    def setup(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator: run ``fn`` before the tasks of every :meth:`run` (a failure skips all tasks)."""
        self._setup.append(fn)
        return fn

    def teardown(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator: run ``fn`` after the tasks of every :meth:`run`, even when they failed."""
        self._teardown.append(fn)
        return fn

    def step(
        self,
        name: str,
        retries: int = 0,
        *,
        retry_delay: float = 0.0,
        backoff: float = 2.0,
        continue_on_error: bool = False,
        screenshot: Optional[bool] = None,
    ) -> Step:
        """Create a :class:`Step` (see module docstring for the ``with`` / loop / ``run`` forms).

        ``continue_on_error`` swallows the failure (recorded as *handled*);
        ``screenshot`` overrides the robot's ``screenshot_on_error`` for this step.
        """
        return Step(self, name, retries, retry_delay=retry_delay, backoff=backoff,
                    continue_on_error=continue_on_error, screenshot=screenshot)

    # ------------------------------------------------------------------ logging
    def log(self, message: str, level: str = "info", **fields: Any) -> Dict[str, Any]:
        """Record a structured log entry (in the report and the Python logger). Returns the record.

        ``fields`` are arbitrary JSON-able key/values (``robot.log("saved", invoice=42)``).
        """
        return self._emit(str(message), level, self.current_path, fields)

    def _emit(self, message: str, level: str, path: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        lvl = _LEVELS.get(str(level).lower())
        if lvl is None:
            raise ValueError(f"Unknown log level {level!r}; expected one of {', '.join(sorted(_LEVELS))}")
        record: Dict[str, Any] = {
            "time": time.time(),
            "level": logging.getLevelName(lvl).lower(),
            "message": message,
            "robot": self.name,
            "path": path,
        }
        for k, v in fields.items():
            if k in record:
                k = f"field_{k}"
            record[k] = v
        if self.report is not None and self._active and lvl >= logging.INFO:
            self.report.logs.append(record)
        self.logger.log(lvl, "[%s] %s", path or self.name, message, extra={"pypilot": record})
        return record

    # ---------------------------------------------------------------- evidence
    def screenshot(self, name: str = "screenshot") -> Optional[str]:
        """Save a screenshot into the run directory. Returns its path, or ``None`` if capture failed."""
        self._shot_seq += 1
        try:
            path = save_error_screenshot(self.desktop, self.run_path, f"{self._shot_seq:03d}-{name}")
        except Exception as exc:
            self.log(f"screenshot failed: {type(exc).__name__}: {exc}", level="warning")
            return None
        return path

    # ------------------------------------------------------------------ timeouts
    def check_timeout(self) -> None:
        """Raise :class:`ActionTimeout` if the innermost running task with a timeout has run out of time."""
        now = self._clock()
        for frame in reversed(self._stack):
            if frame.deadline is not None and now > frame.deadline:
                raise ActionTimeout(
                    f"{frame.result.kind} {frame.result.name!r} exceeded its timeout of {frame.timeout:g}s"
                )

    # ------------------------------------------------------------- step engine
    def _open(self, name: str, kind: str, *, timeout: Optional[float] = None) -> _Frame:
        report = self._report()
        parent = self._stack[-1] if self._stack else None
        path = f"{parent.result.path} > {name}" if parent is not None else name
        result = StepResult(name=name, kind=kind, path=path, depth=len(self._stack), started_at=time.time())
        report.add(result)
        frame = _Frame(result=result, timeout=timeout, children_start=len(report.steps))
        frame.extra["t0"] = self._clock()
        if timeout is not None:
            frame.deadline = frame.extra["t0"] + timeout
        self._stack.append(frame)
        self.logger.debug("[%s] started", path)
        return frame

    def _close(
        self,
        frame: _Frame,
        exc: Optional[BaseException],
        *,
        continue_on_error: bool = False,
        screenshot: Optional[bool] = None,
    ) -> bool:
        """Finalize ``frame``; returns True when ``exc`` must be suppressed."""
        if frame in self._stack:
            # Pop the frame and anything left above it (a step leaked by an exception).
            idx = self._stack.index(frame)
            del self._stack[idx:]
        r = frame.result
        r.finished_at = time.time()
        r.duration = max(0.0, self._clock() - frame.extra.get("t0", self._clock()))
        if exc is None:
            r.status = PASSED
            r.error = r.error_type = r.traceback = None
            self.logger.debug("[%s] passed in %.3fs", r.path, r.duration)
            return False
        r.status = FAILED
        r.error = _exc_text(exc)
        r.error_type = type(exc).__name__
        r.traceback = _format_tb(exc)
        want_shot = self.screenshot_on_error if screenshot is None else bool(screenshot)
        if want_shot and isinstance(exc, Exception):
            existing = getattr(exc, _SHOT_ATTR, None)
            if existing:
                r.screenshot = existing
            else:
                r.screenshot = self.screenshot(f"error-{r.name}")
                if r.screenshot:
                    try:
                        setattr(exc, _SHOT_ATTR, r.screenshot)
                    except Exception:  # exceptions with __slots__
                        pass
        fatal = isinstance(exc, FailSafeTriggered) or not isinstance(exc, Exception)
        suppress = bool(continue_on_error) and not fatal
        r.handled = suppress
        self._log_at(
            r.path,
            f"{r.kind} {r.name!r} failed: {r.error_type}: {r.error}" + (" (continuing)" if suppress else ""),
            "warning" if suppress else "error",
            error_type=r.error_type,
            attempts=r.attempts,
            screenshot=r.screenshot,
        )
        return suppress

    def _log_at(self, path: str, message: str, level: str, **fields: Any) -> None:
        # log() uses the current stack path; failures are logged after popping, so pass it explicitly.
        self._emit(message, level, path, fields)

    @staticmethod
    def _retryable(exc: BaseException) -> bool:
        return isinstance(exc, Exception) and not isinstance(exc, (BusinessException, FailSafeTriggered))

    def _handle_retry(self, frame: _Frame, mark: int, attempt: int, attempts: int, exc: BaseException, delay: float) -> None:
        report = self._report()
        for s in report.steps[mark:]:
            if s.status == FAILED and not s.handled:
                s.handled = True
                s.data.setdefault("note", f"failed during attempt {attempt} of {frame.result.path!r}, which was retried")
        frame.result.data.setdefault("retry_errors", []).append(f"{type(exc).__name__}: {_exc_text(exc)}")
        self._log_at(
            frame.result.path,
            f"{frame.result.kind} {frame.result.name!r} attempt {attempt}/{attempts} failed ({type(exc).__name__}: {_exc_text(exc)}); "
            f"retrying in {delay:g}s",
            "warning",
            attempt=attempt,
        )
        if delay > 0:
            if self._sleep is not None:
                self._sleep(delay)
            elif self._desktop is not None:
                self._desktop.sleep(delay)
            else:
                time.sleep(delay)

    def _run_callable(self, name: str, kind: str, fn: Callable[[], Any], *, retries: int = 0, retry_delay: float = 0.0,
                      backoff: float = 2.0, timeout: Optional[float] = None) -> StepResult:
        """Run ``fn`` as a ``kind`` frame with retries; never raises for ``Exception`` (fatal errors propagate)."""
        frame = self._open(name, kind, timeout=timeout)
        attempts = int(retries) + 1
        delay = float(retry_delay)
        for n in range(1, attempts + 1):
            frame.result.attempts = n
            mark = len(self._report().steps)
            frame.extra["t_attempt"] = self._clock()
            if timeout is not None:
                frame.deadline = frame.extra["t_attempt"] + timeout
            if frame not in self._stack:
                self._stack.append(frame)
            try:
                value = fn()
                elapsed = self._clock() - frame.extra["t_attempt"]
                if timeout is not None and elapsed > timeout:
                    raise ActionTimeout(f"{kind} {name!r} exceeded its timeout of {timeout:g}s (took {elapsed:.2f}s)")
            except (FailSafeTriggered, KeyboardInterrupt, SystemExit) as exc:
                self._close(frame, exc)
                raise
            except Exception as exc:
                if self._retryable(exc) and n < attempts:
                    # Drop frames leaked above ours, then retry.
                    if frame in self._stack:
                        del self._stack[self._stack.index(frame) + 1:]
                    self._handle_retry(frame, mark, n, attempts, exc, delay)
                    delay *= float(backoff)
                    continue
                self._close(frame, exc)
                return frame.result
            if value is not None:
                frame.result.data["result"] = value
            self._close(frame, None)
            return frame.result
        raise AssertionError("unreachable")  # pragma: no cover

    # ---------------------------------------------------------------- lifecycle
    def start(self, *, metadata: Optional[Dict[str, Any]] = None) -> RunReport:
        """Begin a new run (done automatically by :meth:`run` and by steps used outside a run)."""
        if self._active and self.report is not None:
            raise RuntimeError(f"Robot {self.name!r} already has an active run ({self.report.run_id})")
        self.report = RunReport(name=self.name, run_id=_new_run_id(self.name), started_at=time.time(),
                                run_dir=None, metadata=dict(metadata or {}))
        self.report.metadata.setdefault("robot", self.name)
        self._active = True
        self._stack = []
        self._shot_seq = 0
        return self.report

    def finish(self, *, save: Optional[bool] = None) -> RunReport:
        """End the current run: stamp the finish time and (optionally) save JSON + HTML reports."""
        report = self._report()
        report.finished_at = time.time()
        self._active = False
        self._stack = []
        if self.save_reports if save is None else save:
            self.save_report(report)
        self.logger.log(logging.INFO if report.ok else logging.ERROR, "%s", report.summary())
        return report

    def save_report(self, report: Optional[RunReport] = None) -> Dict[str, str]:
        """Write ``report.json`` and ``report.html`` into the run directory. Returns their paths."""
        report = report or self.report
        if report is None:
            raise RuntimeError("No report to save")
        directory = self.run_dir / report.run_id
        report.run_dir = str(directory)
        return {
            "json": report.save_json(directory / "report.json"),
            "html": report.save_html(directory / "report.html"),
        }

    def __enter__(self) -> "Robot":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Optional[BaseException], tb: Any) -> None:
        if self.report is not None and self._active:
            if exc is not None:
                self.report.error = f"{type(exc).__name__}: {_exc_text(exc)}"
                if isinstance(exc, (FailSafeTriggered, KeyboardInterrupt)):
                    self.report.aborted = True
            self.finish()

    def run(self, *task_names: str, save: Optional[bool] = None) -> RunReport:
        """Run the named tasks (default: all, in registration order) and return the :class:`RunReport`.

        A failed task stops the run (remaining tasks are recorded as skipped)
        unless it has ``continue_on_error``. Teardown hooks always run. A
        fail-safe abort marks the report ``aborted``. ``save`` overrides
        ``save_reports``.
        """
        names = list(task_names) or list(self.tasks)
        unknown = [n for n in names if n not in self.tasks]
        if unknown:
            raise KeyError(f"Unknown task(s) {', '.join(map(repr, unknown))}; registered: {', '.join(map(repr, self.tasks)) or 'none'}")
        if self._active:
            raise RuntimeError(f"Robot {self.name!r} is already running")
        report = self.start(metadata={"tasks": names})
        self.log(f"run {report.run_id} started", tasks=names)
        stop = False
        try:
            for hook in self._setup:
                hook_name = f"setup:{getattr(hook, '__name__', 'setup')}"
                res = self._run_callable(hook_name, "setup", self._caller(hook))
                if res.status == FAILED:
                    stop = True
                    break
            for name in names:
                task = self.tasks[name]
                if stop:
                    report.add(StepResult(name=name, kind="task", status=SKIPPED, started_at=time.time(),
                                          finished_at=time.time(), data={"reason": "an earlier task failed"}))
                    continue
                self.log(f"task {name!r} started")
                res = self._run_callable(name, "task", self._caller(task.fn, task.pass_robot), retries=task.retries,
                                         retry_delay=task.retry_delay, backoff=task.backoff, timeout=task.timeout)
                if res.status == FAILED and not task.continue_on_error:
                    stop = True
                elif res.status == PASSED:
                    self.log(f"task {name!r} passed in {res.duration:.2f}s", attempts=res.attempts)
        except (FailSafeTriggered, KeyboardInterrupt) as exc:
            report.aborted = True
            report.error = f"{type(exc).__name__}: {_exc_text(exc)}"
            done = {s.name for s in report.steps if s.kind == "task"}
            for name in names:
                if name not in done:
                    report.add(StepResult(name=name, kind="task", status=SKIPPED, started_at=time.time(),
                                          finished_at=time.time(), data={"reason": "run aborted"}))
            self._run_teardown()
            self.finish(save=save)
            if isinstance(exc, KeyboardInterrupt):
                raise
            return report
        self._run_teardown()
        return self.finish(save=save)

    def _run_teardown(self) -> None:
        for hook in self._teardown:
            hook_name = f"teardown:{getattr(hook, '__name__', 'teardown')}"
            try:
                self._run_callable(hook_name, "teardown", self._caller(hook))
            except (FailSafeTriggered, KeyboardInterrupt):  # still run the other hooks
                continue

    def _caller(self, fn: Callable[..., Any], pass_robot: Optional[bool] = None) -> Callable[[], Any]:
        want = _wants_argument(fn) if pass_robot is None else pass_robot
        return (lambda: fn(self)) if want else fn

    def __repr__(self) -> str:
        return f"<Robot {self.name!r} tasks={list(self.tasks)} run_dir={str(self.run_dir)!r}>"


def _wants_argument(fn: Callable[..., Any]) -> bool:
    """Whether ``fn`` has a required positional parameter (it then receives the robot)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    required = [
        p for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
    ]
    if len(required) > 1:
        raise TypeError(f"Task {getattr(fn, '__name__', fn)!r} must take no argument or one (the robot), not {len(required)}")
    return len(required) == 1
