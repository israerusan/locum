"""Code-first RPA robots: tasks, steps, retries, evidence and reports.

::

    from locum.rpa import Robot

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
  no argument or one (the robot). A task that overruns its ``timeout`` fails
  and is **not** retried (its actions may already have happened), and a
  step's ``continue_on_error`` / retries never swallow that deadline.
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
  report and to the standard :mod:`logging` logger ``locum.robot.<name>``
  (record attribute ``locum`` carries the fields).
* :class:`~locum.rpa.queue.BusinessException`,
  :class:`~locum.errors.FailSafeTriggered` and a task's own timeout are
  never retried; a fail-safe aborts the whole run.
* Steps (or :meth:`Robot.screenshot`) used outside :meth:`Robot.run` start an
  implicit run; the next :meth:`Robot.run` (or :meth:`Robot.start`) adopts it,
  and :meth:`Robot.finish` ends it.
* **Safety policy (on by default).** :attr:`Robot.desktop` is a
  :class:`~locum.llm.safety.GuardedDesktop`: every click, keystroke, text
  entry, launch and clipboard write -- including actions on elements and
  windows reached through ``find()``/``window()`` -- is checked against the
  policy first and raises :class:`~locum.errors.PolicyViolation` (never
  retried) when it is refused or declined. Without a ``policy=`` argument
  the robot uses :meth:`Policy.default() <locum.llm.safety.Policy.default>`,
  the keyword/shortcut tripwire: a click on "Delete", "Pay now", "Supprimer",
  "Überweisen"..., Ctrl+S or Ctrl+Enter, or a click on a control nobody can
  identify needs a confirmation -- and since a robot runs unattended, without
  a ``confirm`` callback it is **blocked**. To let such a step run, exempt its
  target (``Robot(policy=Policy.default(exempt=['button "Delete row"']))``),
  pass ``confirm=``, or opt out explicitly with ``policy=Policy.off()`` (or
  ``policy=None``). For money or data actions use an allow-list instead:
  ``Robot(policy=Policy.strict(allow=[...]))``. The policy applied is recorded
  in ``report.metadata["policy"]``.
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

from .. import failsafe as _failsafe
from .._policy import DEFAULT_POLICY, policy_label, resolve_policy
from .._secret import scrub, scrub_data
from ..errors import ActionTimeout, FailSafeTriggered, PolicyViolation
from .queue import TASK_DEADLINE_ATTR as _DEADLINE_ATTR
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
_SHOT_ATTR = "_locum_screenshot"


def _policy_metadata(policy: Any) -> Dict[str, Any]:
    """``RunReport.metadata`` entries naming the policy a run was gated by."""
    if policy is None:
        return {"policy": "off"}
    return {"policy": policy_label(policy),
            "policy_mode": "allow-list" if getattr(policy, "allow", None) is not None else "heuristic"}


def _deadline_error(message: str) -> ActionTimeout:
    """An :class:`ActionTimeout` for a task's own deadline (never retried, never swallowed by a step)."""
    exc = ActionTimeout(message)
    setattr(exc, _DEADLINE_ATTR, True)
    return exc


def _is_deadline(exc: Optional[BaseException]) -> bool:
    return exc is not None and bool(getattr(exc, _DEADLINE_ATTR, False))


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


def _wraps_refusal(exc: Optional[BaseException]) -> bool:
    """Whether ``exc`` wraps a :class:`PolicyViolation` (a flow's ``FlowStepError.original``, or ``raise ... from``)."""
    seen = 0
    while exc is not None and seen < 64:
        if isinstance(exc, PolicyViolation):
            return True
        nxt = getattr(exc, "original", None)
        exc = nxt if isinstance(nxt, BaseException) else exc.__cause__
        seen += 1
    return False


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
    closed: bool = False
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
        self.exited = False

    def __enter__(self) -> "Attempt":
        self.entered = True
        try:
            self.step.robot.check_timeout()
        except BaseException as exc:  # the task's deadline passed: the step fails here, not later
            self.exited = True
            self.error = exc
            self.step._abort(exc)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Optional[BaseException], tb: Any) -> bool:
        self.exited = True
        if exc is None:
            return False
        self.error = exc
        if not isinstance(exc, Exception):  # KeyboardInterrupt, SystemExit, GeneratorExit
            # It leaves the loop without resuming the step: record the failure now.
            self.step._abort(exc)
            return False
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

    def _abort(self, exc: BaseException) -> None:
        """Close the running step as failed with ``exc``, which propagates past the step."""
        frame, self._frame = self._frame, None
        if frame is not None and not frame.closed:
            self.robot._close(frame, exc, screenshot=self.screenshot)

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
            except GeneratorExit:  # the loop was left: 'break', or an exception escaped it
                self._frame = None
                if frame.closed:  # already recorded (an interrupt in the attempt, a timeout on entering it)
                    raise
                if attempt.error is not None:  # gave up after a failed attempt
                    robot._close(frame, attempt.error, continue_on_error=True, screenshot=self.screenshot)
                elif attempt.exited:  # 'break' after a successful attempt
                    robot._close(frame, None)
                else:  # left before the attempt finished: never report that as passed
                    detail = f" (last error: {type(last).__name__}: {_exc_text(last)})" if last is not None else ""
                    robot._close(frame, RuntimeError(
                        f"step {self.name!r} was left before attempt {n} completed{detail}"), screenshot=False)
                raise
            if frame.closed:
                self._frame = None
                return
            last = attempt.error
            if last is None:
                self._frame = None
                robot._close(frame, None)
                return
            if not robot._retryable(last) or n >= attempts:
                break
            try:
                robot._handle_retry(frame, mark, n, attempts, last, delay)
            except BaseException as exc:  # abort / Ctrl+C during the retry wait
                self._frame = None
                robot._close(frame, exc, screenshot=self.screenshot)
                raise
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
        desktop: The :class:`~locum.desktop.Desktop` to drive (created lazily
            with the default backend on first use when ``None``).
        name: Robot name (report title, logger name, run-id component).
        run_dir: Parent directory of the per-run evidence folders.
        screenshot_on_error: Save a screenshot when a task/step fails.
        logger: A :class:`logging.Logger` (default ``locum.robot.<name>``).
        save_reports: Write ``report.json`` and ``report.html`` into the run
            directory at the end of :meth:`run` / :meth:`finish`.
        sleep: Sleep function for retry delays (default: the desktop's sleep, which honours the abort flag).
        clock: Monotonic clock used for durations and timeouts.
        policy: The :class:`~locum.llm.safety.Policy` every desktop action of
            the robot must pass (see the module docstring). Default:
            :meth:`Policy.default() <locum.llm.safety.Policy.default>`. A preset
            name (``"default"``, ``"strict"``, ``"permissive"``) is accepted;
            ``Policy.off()`` / ``None`` / ``False`` turn gating off explicitly.
        confirm: Confirmation callback for actions the policy sends to a human
            (default: the policy's own ``confirm``; none = such actions are blocked).
        audit: :class:`~locum.llm.safety.AuditLog` for the policy decisions
            (default: an in-memory log, :attr:`audit`).

    Attributes:
        vars: Free-form dict shared by all tasks (survives across runs).
        report: The current/last :class:`RunReport`.
        tasks: Registered tasks by name, in registration order.
    """

    def __init__(
        self,
        desktop: Optional["Desktop"] = None,
        name: str = "robot",
        run_dir: Union[str, "os.PathLike[str]"] = "locum_runs",
        screenshot_on_error: bool = True,
        logger: Optional[logging.Logger] = None,
        *,
        save_reports: bool = True,
        sleep: Optional[Callable[[float], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        policy: Any = DEFAULT_POLICY,
        confirm: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        audit: Any = None,
    ):
        self._desktop = desktop
        policy = resolve_policy(policy)
        #: The resolved :class:`~locum.llm.safety.Policy`, or ``None`` when gating was turned off.
        self.policy = policy
        self.confirm = confirm
        if policy is not None and audit is None:
            from ..llm.safety import AuditLog

            audit = AuditLog()
        #: Audit log of the policy decisions (``None`` without a policy).
        self.audit = audit
        self._guarded: Any = None
        self.name = str(name)
        self.run_dir = pathlib.Path(run_dir).expanduser()
        self.screenshot_on_error = bool(screenshot_on_error)
        self.logger = logger or logging.getLogger(f"locum.robot.{_slug(self.name)}")
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
        self._implicit = False
        self._shot_seq = 0

    # ----------------------------------------------------------------- plumbing
    @property
    def desktop(self) -> "Desktop":
        """The desktop this robot drives (created on first access when not given).

        With a ``policy`` this is a :class:`~locum.llm.safety.GuardedDesktop`
        over it, so every action is checked first.
        """
        if self._desktop is None:
            from ..desktop import Desktop

            self._desktop = Desktop()
        if not self.policy:  # None, False or Policy.off(): not gated
            return self._desktop
        if self._guarded is None or self._guarded.unwrapped is not _unguarded(self._desktop) \
                or self._guarded.policy is not self.policy:
            from ..llm.safety import GuardedDesktop

            self._guarded = GuardedDesktop(self._desktop, self.policy, confirm=self.confirm, audit=self.audit)
        return self._guarded

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
                self._desktop.sleep(seconds)  # ends at once on abort() (kill hotkey)
            else:
                _failsafe.abortable_sleep(seconds)
            _failsafe._raise_if_aborted()
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
            self._implicit = True  # adopted by the next run()/start(), ended by finish()
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
                when steps start/end, when step attempts start, on :meth:`sleep` and
                when the task returns. An over-running task fails with
                :class:`~locum.errors.ActionTimeout` and is **not** retried
                (its actions may already have taken effect); a step's
                ``continue_on_error`` or retries do not swallow it.
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
        _wants_argument(fn, "Setup hook")  # a bad signature fails here, not in the middle of run()
        self._setup.append(fn)
        return fn

    def teardown(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator: run ``fn`` after the tasks of every :meth:`run`, even when they failed."""
        _wants_argument(fn, "Teardown hook")
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
        # Every log line (failures and retries quote error messages) goes out with secrets masked.
        message = scrub(message) or message
        path = scrub(path) or path
        fields = scrub_data(dict(fields))
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
        self.logger.log(lvl, "[%s] %s", path or self.name, message, extra={"locum": record})
        return record

    # ---------------------------------------------------------------- evidence
    def screenshot(self, name: str = "screenshot") -> Optional[str]:
        """Save a screenshot into the run directory. Returns its path, or ``None`` if capture failed."""
        self._shot_seq += 1
        try:
            path = save_error_screenshot(self.desktop, self.run_path, f"{self._shot_seq:03d}-{name}")
        except Exception as exc:
            self._log_at(self.current_path, f"screenshot failed: {type(exc).__name__}: {exc}", "warning")
            return None
        return path

    # ------------------------------------------------------------------ timeouts
    def check_timeout(self) -> None:
        """Raise :class:`ActionTimeout` if the innermost running task with a timeout has run out of time."""
        now = self._clock()
        for frame in reversed(self._stack):
            if frame.deadline is not None and now > frame.deadline:
                raise _deadline_error(
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
        self.logger.debug("[%s] started", scrub(path))
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
        if frame.closed:
            return False
        if frame in self._stack:
            # Pop the frame and anything left above it (a step leaked by an exception).
            self._drop_above(frame, exc)
            self._stack.remove(frame)
        frame.closed = True
        r = frame.result
        r.finished_at = time.time()
        r.duration = max(0.0, self._clock() - frame.extra.get("t0", self._clock()))
        if exc is None:
            r.status = PASSED
            r.error = r.error_type = r.traceback = None
            self.logger.debug("[%s] passed in %.3fs", scrub(r.path), r.duration)
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
        # A task's deadline belongs to the enclosing task: a step must not swallow it.
        fatal = isinstance(exc, FailSafeTriggered) or not isinstance(exc, Exception) or _is_deadline(exc)
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

    def _drop_above(self, frame: Optional[_Frame], exc: Optional[BaseException]) -> None:
        """Pop the frames above ``frame`` (all frames when ``None``), recording them as failed, never passed."""
        idx = self._stack.index(frame) + 1 if frame is not None else 0
        leaked, self._stack[idx:] = self._stack[idx:], []
        for f in reversed(leaked):
            if f.closed:
                continue
            f.closed = True
            r = f.result
            r.finished_at = time.time()
            r.duration = max(0.0, self._clock() - f.extra.get("t0", self._clock()))
            r.status = FAILED
            r.handled = False
            if exc is not None:
                r.error = f"did not finish: {type(exc).__name__}: {_exc_text(exc)}"
                r.error_type = type(exc).__name__
            else:
                r.error = f"{r.kind} {r.name!r} did not finish"
                r.error_type = "Unfinished"

    def _log_at(self, path: str, message: str, level: str, **fields: Any) -> None:
        # log() uses the current stack path; failures are logged after popping, so pass it explicitly.
        self._emit(message, level, path, fields)

    @staticmethod
    def _retryable(exc: BaseException) -> bool:
        # A refused/declined action is a decision, not a glitch: retrying would ask the human again.
        return (isinstance(exc, Exception) and not isinstance(exc, (BusinessException, FailSafeTriggered, PolicyViolation))
                and not _wraps_refusal(exc) and not _is_deadline(exc))

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
                self._desktop.sleep(delay)  # ends at once on abort() (kill hotkey)
            else:
                _failsafe.abortable_sleep(delay)
        _failsafe._raise_if_aborted()  # abort() / the kill hotkey during the wait: never retry

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
                    # The body finished: its actions happened. Fail, but never run it again.
                    frame.result.data["note"] = "the body completed, but after the timeout; not retried"
                    if value is not None:
                        frame.result.data["result"] = value
                    raise _deadline_error(f"{kind} {name!r} exceeded its timeout of {timeout:g}s (took {elapsed:.2f}s)")
            except Exception as exc:
                elapsed = self._clock() - frame.extra["t_attempt"]
                if timeout is not None and elapsed > timeout and self._retryable(exc) and n < attempts:
                    # It failed after overrunning its timeout: its actions may have happened. Never run it again.
                    frame.result.data.setdefault("note", "the body failed after the timeout; not retried")
                    overrun = _deadline_error(f"{kind} {name!r} exceeded its timeout of {timeout:g}s (took {elapsed:.2f}s), "
                                              f"then failed: {type(exc).__name__}: {_exc_text(exc)}")
                    overrun.__cause__ = exc
                    self._close(frame, overrun)
                    return frame.result
                if self._retryable(exc) and n < attempts:
                    # Drop frames leaked above ours, then retry.
                    if frame in self._stack:
                        self._drop_above(frame, exc)
                    try:
                        self._handle_retry(frame, mark, n, attempts, exc, delay)
                    except BaseException as stop:  # abort / Ctrl+C during the retry wait
                        self._close(frame, stop)
                        raise
                    delay *= float(backoff)
                    continue
                self._close(frame, exc)
                if isinstance(exc, FailSafeTriggered):
                    raise
                return frame.result
            except BaseException as exc:  # KeyboardInterrupt, SystemExit...
                self._close(frame, exc)
                raise
            if value is not None:
                frame.result.data["result"] = value
            self._close(frame, None)
            return frame.result
        raise AssertionError("unreachable")  # pragma: no cover

    # ---------------------------------------------------------------- lifecycle
    def start(self, *, metadata: Optional[Dict[str, Any]] = None) -> RunReport:
        """Begin a new run (done automatically by :meth:`run` and by steps used outside a run).

        An implicit run (started by a step or screenshot outside any run) with
        no step still open is adopted: it becomes this run.
        """
        if self._active and self.report is not None:
            if not self._implicit or self._stack:
                raise RuntimeError(f"Robot {self.name!r} already has an active run ({self.report.run_id})")
            self._implicit = False
            self.report.metadata.update(metadata or {})
            return self.report
        self._implicit = False
        self.report = RunReport(name=self.name, run_id=_new_run_id(self.name), started_at=time.time(),
                                run_dir=None, metadata=dict(metadata or {}))
        self.report.metadata.setdefault("robot", self.name)
        self.report.metadata.update(_policy_metadata(self.policy))
        self._active = True
        self._stack = []
        self._shot_seq = 0
        return self.report

    def finish(self, *, save: Optional[bool] = None) -> RunReport:
        """End the current run: stamp the finish time and (optionally) save JSON + HTML reports."""
        report = self._report()
        self._drop_above(None, None)  # a step still open now did not finish: never report it as passed
        report.finished_at = time.time()
        self._active = False
        self._implicit = False
        self._stack = []
        if self.save_reports if save is None else save:
            self.save_report(report)
        self.logger.log(logging.INFO if report.ok else logging.ERROR, "%s", scrub(report.summary()))
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
                self._drop_above(None, exc)
            self.finish()

    def run(self, *task_names: str, save: Optional[bool] = None) -> RunReport:
        """Run the named tasks (default: all, in registration order) and return the :class:`RunReport`.

        A failed task stops the run (remaining tasks are recorded as skipped)
        unless it has ``continue_on_error``. Teardown hooks always run. A
        fail-safe abort marks the report ``aborted`` and returns it; any other
        exception escaping a task (``KeyboardInterrupt``, ``SystemExit``...)
        also marks it ``aborted``, runs teardown, ends the run (saving the
        report) and then propagates. ``save`` overrides ``save_reports``.
        Steps recorded before by an implicit run are adopted into this run.
        """
        names = list(task_names) or list(self.tasks)
        unknown = [n for n in names if n not in self.tasks]
        if unknown:
            raise KeyError(f"Unknown task(s) {', '.join(map(repr, unknown))}; registered: {', '.join(map(repr, self.tasks)) or 'none'}")
        if self._active and (not self._implicit or self._stack):
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
        except BaseException as exc:  # fail-safe, Ctrl+C, SystemExit, a bug in the robot...
            report.aborted = True
            report.error = f"{type(exc).__name__}: {_exc_text(exc)}"
            self._drop_above(None, exc)
            done = {s.name for s in report.steps if s.kind == "task"}
            for name in names:
                if name not in done:
                    report.add(StepResult(name=name, kind="task", status=SKIPPED, started_at=time.time(),
                                          finished_at=time.time(), data={"reason": "run aborted"}))
            self._end_run(save)
            if isinstance(exc, FailSafeTriggered):
                return report
            raise
        return self._end_run(save)

    def _end_run(self, save: Optional[bool]) -> RunReport:
        """Run the teardown hooks, then finish the run -- also when a teardown hook raises."""
        try:
            self._run_teardown()
        except BaseException as exc:
            report = self._report()
            report.aborted = True
            report.error = report.error or f"{type(exc).__name__}: {_exc_text(exc)}"
            self.finish(save=save)
            if isinstance(exc, FailSafeTriggered):  # like a fail-safe in a task: aborted, returned
                return report
            raise
        return self.finish(save=save)

    def _run_teardown(self) -> None:
        pending: Optional[BaseException] = None
        for hook in self._teardown:
            hook_name = f"teardown:{getattr(hook, '__name__', 'teardown')}"
            try:
                self._run_callable(hook_name, "teardown", self._caller(hook))
            except BaseException as exc:  # fail-safe, Ctrl+C, SystemExit: still run the other hooks...
                if pending is None:
                    pending = exc
        if pending is not None:  # ...then let the interrupt / fail-safe abort the run
            raise pending

    def _caller(self, fn: Callable[..., Any], pass_robot: Optional[bool] = None) -> Callable[[], Any]:
        want = _wants_argument(fn) if pass_robot is None else pass_robot
        return (lambda: fn(self)) if want else fn

    def __repr__(self) -> str:
        return f"<Robot {self.name!r} tasks={list(self.tasks)} run_dir={str(self.run_dir)!r}>"


def _unguarded(desktop: Any) -> Any:
    """The real desktop behind a guarded one (or ``desktop`` itself)."""
    inner = getattr(type(desktop), "unwrapped", None)
    return desktop.unwrapped if inner is not None else desktop


def _wants_argument(fn: Callable[..., Any], what: str = "Task") -> bool:
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
        raise TypeError(f"{what} {getattr(fn, '__name__', fn)!r} must take no argument or one (the robot), not {len(required)}")
    return len(required) == 1
