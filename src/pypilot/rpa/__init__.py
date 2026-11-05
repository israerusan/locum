"""RPA layer: robots, transaction queues, declarative flows, recorder and reports.

* :class:`Robot` -- code-first tasks/steps with retries, evidence screenshots and reports.
* :class:`WorkQueue` -- Orchestrator-style transaction queue with resume (:class:`BusinessException`).
* :func:`run_flow` -- YAML/JSON declarative workflows.
* :class:`Recorder` -- record input as selectors and emit flows / Python.
* :class:`RunReport` / :class:`StepResult` -- JSON + self-contained HTML reports.

Importing this package only needs the core dependencies; PyYAML, openpyxl and
pynput are imported lazily when a feature needs them.
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
