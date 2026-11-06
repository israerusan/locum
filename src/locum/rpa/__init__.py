"""RPA layer: robots, transaction queues, declarative flows, recorder and reports.

* :class:`Robot` -- code-first tasks/steps with retries, evidence screenshots and reports.
* :class:`WorkQueue` -- Orchestrator-style transaction queue with resume (:class:`BusinessException`).
* :func:`run_flow` -- YAML/JSON declarative workflows.
* :class:`Recorder` -- record input as selectors and emit flows / Python.
* :class:`RunReport` / :class:`StepResult` -- JSON + self-contained HTML reports.

Importing this package only needs the core dependencies; PyYAML, openpyxl and
pynput are imported lazily when a feature needs them.

Safety: robots and flows are checked by the safety policy **by default**.
``Robot()`` and ``run_flow()`` drive a :class:`~locum.llm.safety.GuardedDesktop`
under :meth:`Policy.default() <locum.llm.safety.Policy.default>` (every action
checked first; refusals raise :class:`~locum.errors.PolicyViolation` and are
never retried). The default is a keyword/shortcut tripwire, and because robots
run unattended the actions it flags are blocked unless a ``confirm`` callback
is given; ``Policy.default(exempt=[...])`` lets named targets through and
``policy=Policy.off()`` (or ``None``) opts out explicitly. Use
``Policy.strict(allow=[...])`` for authorization by explicit rules.
:class:`FlowRunner` (the embedding API) gates only when given ``policy=``.
"""

from __future__ import annotations

from .flow import FlowAssertionError, FlowRunner, FlowStepError, interpolate, load_flow, run_flow, validate_flow
from .queue import (
    BUSINESS_EXCEPTION,
    FAILED,
    IN_PROGRESS,
    NEW,
    SUCCESS,
    BusinessException,
    QueueItem,
    WorkQueue,
    default_queue_dir,
    synced_location,
)
from .recorder import RecordedAction, Recorder
from .report import RunReport, StepResult
from .robot import Robot, Step, Task

__all__ = [
    "Robot",
    "Step",
    "Task",
    "RunReport",
    "StepResult",
    "WorkQueue",
    "default_queue_dir",
    "synced_location",
    "QueueItem",
    "BusinessException",
    "NEW",
    "IN_PROGRESS",
    "SUCCESS",
    "FAILED",
    "BUSINESS_EXCEPTION",
    "run_flow",
    "load_flow",
    "validate_flow",
    "interpolate",
    "FlowRunner",
    "FlowStepError",
    "FlowAssertionError",
    "Recorder",
    "RecordedAction",
]
