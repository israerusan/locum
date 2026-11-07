"""Run reports for robots, flows and queues.

A :class:`RunReport` is the durable record of one automation run: every task /
step / flow step as a :class:`StepResult` (status, timing, attempts, error,
traceback, error screenshot), plus structured log records and free-form
metadata. Reports serialize to JSON (:meth:`RunReport.save_json`, round-trips
through :meth:`RunReport.load_json`) and to a **self-contained HTML** page
(:meth:`RunReport.save_html`): inline CSS, error screenshots embedded as base64
thumbnails, every piece of user content HTML-escaped -- so the file can be
mailed or attached to a ticket as-is.
"""

from __future__ import annotations

import base64
import datetime as _dt
import html
import io
import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Union

__all__ = ["StepResult", "RunReport", "PASSED", "FAILED", "SKIPPED", "STATUSES"]

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
#: Every valid :attr:`StepResult.status`.
STATUSES = (PASSED, FAILED, SKIPPED)

PathLike = Union[str, "os.PathLike[str]"]

#: Embedded base64 images in the HTML report (left out of the secret scrub).
_DATA_URL_RE = re.compile(r"(data:image/[a-z+]+;base64,[A-Za-z0-9+/=]+)")


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    # Convert from UTC: Windows rejects fromtimestamp() for local times before 1970
    # (e.g. ts near 0 west of UTC), which raised OSError [Errno 22].
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of ``value`` into JSON-serializable data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict())
        except Exception:
            pass
    if isinstance(value, pathlib.PurePath):
        return str(value)
    return repr(value)


@dataclass
class StepResult:
    """Outcome of one task, step or flow step.

    Attributes:
        name: Human readable name (``"Login"``, ``"click button \\"OK\\""``).
        status: ``"passed"``, ``"failed"`` or ``"skipped"``.
        kind: ``"task"``, ``"step"``, ``"flow"`` (a declarative flow step) or ``"item"`` (queue item).
        path: Unique location of the step in the run, e.g. ``"Invoice > Login"``
            or ``"steps[3].for_each.steps[1]"``.
        depth: Nesting level (0 = top level), used for indentation in reports.
        started_at / finished_at: Wall-clock epoch seconds.
        duration: Seconds spent, including retries.
        attempts: Number of attempts made (1 = no retry).
        error / error_type / traceback: Failure details (``None`` when passed).
        screenshot: Path of the screenshot taken when the step failed.
        handled: ``True`` when the failure was handled (``continue_on_error``,
            a flow ``try``/``catch``...) and therefore does not fail the run.
        data: Extra JSON-able details (step arguments, outputs...).
    """

    name: str
    status: str = PASSED
    kind: str = "step"
    path: str = ""
    depth: int = 0
    started_at: float = 0.0
    finished_at: Optional[float] = None
    duration: float = 0.0
    attempts: int = 1
    error: Optional[str] = None
    error_type: Optional[str] = None
    traceback: Optional[str] = None
    screenshot: Optional[str] = None
    handled: bool = False
    data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}, got {self.status!r}")
        if not self.path:
            self.path = self.name

    @property
    def ok(self) -> bool:
        """``True`` unless the step failed without being handled."""
        return self.status != FAILED or self.handled

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    def to_dict(self) -> Dict[str, Any]:
        from .._secret import scrub_data

        return scrub_data({
            "name": self.name,
            "status": self.status,
            "kind": self.kind,
            "path": self.path,
            "depth": self.depth,
            "started_at": self.started_at,
            "started": _iso(self.started_at) if self.started_at else None,
            "finished_at": self.finished_at,
            "duration": round(float(self.duration), 6),
            "attempts": self.attempts,
            "error": self.error,
            "error_type": self.error_type,
            "traceback": self.traceback,
            "screenshot": self.screenshot,
            "handled": self.handled,
            "ok": self.ok,
            "data": _jsonable(self.data),
        })

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StepResult":
        return cls(
            name=str(d.get("name", "")),
            status=str(d.get("status", PASSED)),
            kind=str(d.get("kind", "step")),
            path=str(d.get("path", "") or d.get("name", "")),
            depth=int(d.get("depth", 0) or 0),
            started_at=float(d.get("started_at") or 0.0),
            finished_at=d.get("finished_at"),
            duration=float(d.get("duration") or 0.0),
            attempts=int(d.get("attempts", 1) or 1),
            error=d.get("error"),
            error_type=d.get("error_type"),
            traceback=d.get("traceback"),
            screenshot=d.get("screenshot"),
            handled=bool(d.get("handled", False)),
            data=dict(d.get("data") or {}),
        )


@dataclass
class RunReport:
    """Everything that happened during one run.

    Attributes:
        name: Robot / flow name.
        run_id: Unique id of the run (also the name of its run directory).
        started_at / finished_at: Wall-clock epoch seconds.
        steps: Every :class:`StepResult` in execution order (tasks, steps, flow steps).
        logs: Structured log records (``{"time", "level", "message", "path", ...}``).
        run_dir: Directory holding screenshots and saved reports, if any.
        aborted: The run was stopped early (fail-safe, abort hotkey, fatal error).
        error: The error that ended the run, if any (e.g. a flow failure).
        metadata: Free-form JSON-able details (host, flow file, variables summary...).
        variables: Final variable values of a flow run. Kept in memory only (never
            serialized, since variables often hold credentials).
    """

    name: str = "run"
    run_id: str = ""
    started_at: float = 0.0
    finished_at: Optional[float] = None
    steps: List[StepResult] = field(default_factory=list)
    logs: List[Dict[str, Any]] = field(default_factory=list)
    run_dir: Optional[str] = None
    aborted: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    variables: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ----------------------------------------------------------------- queries
    @property
    def ok(self) -> bool:
        """``True`` when no step failed unhandled, the run was not aborted and recorded no fatal error."""
        return not self.aborted and self.error is None and all(s.ok for s in self.steps)

    @property
    def duration(self) -> float:
        if self.finished_at is None or not self.started_at:
            return 0.0
        return max(0.0, self.finished_at - self.started_at)

    @property
    def failures(self) -> List[StepResult]:
        """Steps that failed and were not handled."""
        return [s for s in self.steps if not s.ok]

    def by_kind(self, kind: str) -> List[StepResult]:
        return [s for s in self.steps if s.kind == kind]

    @property
    def tasks(self) -> List[StepResult]:
        return self.by_kind("task")

    def find(self, name_or_path: str) -> Optional[StepResult]:
        """The last step whose path or name equals ``name_or_path``."""
        for s in reversed(self.steps):
            if s.path == name_or_path:
                return s
        for s in reversed(self.steps):
            if s.name == name_or_path:
                return s
        return None

    def counts(self) -> Dict[str, int]:
        c = {PASSED: 0, FAILED: 0, SKIPPED: 0, "handled": 0}
        for s in self.steps:
            c[s.status] = c.get(s.status, 0) + 1
            if s.status == FAILED and s.handled:
                c["handled"] += 1
        c["total"] = len(self.steps)
        return c

    def add(self, step: StepResult) -> StepResult:
        self.steps.append(step)
        return step

    def __iter__(self) -> Iterator[StepResult]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def summary(self) -> str:
        """One-line human summary: ``"robot: PASSED 5 passed, 0 failed, 1 skipped in 2.31s"``."""
        c = self.counts()
        status = "PASSED" if self.ok else ("ABORTED" if self.aborted else "FAILED")
        text = f"{self.name}: {status} {c[PASSED]} passed, {c[FAILED]} failed, {c[SKIPPED]} skipped in {self.duration:.2f}s"
        if c["handled"]:
            text += f" ({c['handled']} failure(s) handled)"
        if self.error:
            text += f" - {self.error}"
        return text

    # ----------------------------------------------------------- serialization
    def to_dict(self) -> Dict[str, Any]:
        """The report as JSON-able data. Credential values (:class:`~locum.rpa.credentials.Secret`)
        are replaced by ``***`` wherever they appear (errors, logs, results)."""
        from .._secret import scrub_data

        return scrub_data(self._to_dict())

    def _to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "run_id": self.run_id,
            "ok": self.ok,
            "aborted": self.aborted,
            "error": self.error,
            "started_at": self.started_at,
            "started": _iso(self.started_at) if self.started_at else None,
            "finished_at": self.finished_at,
            "finished": _iso(self.finished_at),
            "duration": round(self.duration, 6),
            "counts": self.counts(),
            "run_dir": self.run_dir,
            "metadata": _jsonable(self.metadata),
            "steps": [s.to_dict() for s in self.steps],
            "logs": [_jsonable(r) for r in self.logs],
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunReport":
        return cls(
            name=str(d.get("name", "run")),
            run_id=str(d.get("run_id", "")),
            started_at=float(d.get("started_at") or 0.0),
            finished_at=d.get("finished_at"),
            steps=[StepResult.from_dict(s) for s in d.get("steps", [])],
            logs=list(d.get("logs", [])),
            run_dir=d.get("run_dir"),
            aborted=bool(d.get("aborted", False)),
            error=d.get("error"),
            metadata=dict(d.get("metadata") or {}),
        )

    @classmethod
    def load_json(cls, path: PathLike) -> "RunReport":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def save_json(self, path: PathLike) -> str:
        """Write the report as pretty JSON (parent directories are created). Returns the path."""
        p = pathlib.Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, self.to_json())
        return str(p)

    def to_html(self, *, thumbnail_size: int = 480) -> str:
        """Render the report as a self-contained HTML document (see module docstring)."""
        from .._secret import scrub

        # Scrub the text, not the embedded screenshots: a short secret can occur in base64 by chance.
        parts = _DATA_URL_RE.split(_render_html(self, thumbnail_size=thumbnail_size))
        return "".join(p if i % 2 else (scrub(p) or "") for i, p in enumerate(parts))

    def save_html(self, path: PathLike, *, thumbnail_size: int = 480) -> str:
        """Write :meth:`to_html` to ``path`` (parent directories are created). Returns the path."""
        p = pathlib.Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, self.to_html(thumbnail_size=thumbnail_size))
        return str(p)

    def __repr__(self) -> str:
        return f"<RunReport {self.summary()}>"


def _atomic_write(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ------------------------------------------------------------------- HTML

_CSS = """
:root{--bg:#f6f7f9;--fg:#1d2330;--muted:#667085;--card:#fff;--line:#e4e7ec;
--ok:#12805c;--okbg:#e3f6ee;--bad:#c4320a;--badbg:#fdece6;--skip:#7a5af8;--skipbg:#efeafe;--warn:#b54708;--warnbg:#fef3e2}
@media (prefers-color-scheme:dark){:root{--bg:#11141a;--fg:#e6e9ef;--muted:#98a2b3;--card:#1a1f29;--line:#2b3240;
--okbg:#0f2e23;--badbg:#3a1a10;--skipbg:#251d45;--warnbg:#3a2a12}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.meta{color:var(--muted);font-size:13px}.meta code{color:var(--fg)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px;letter-spacing:.02em;text-transform:uppercase}
.passed{color:var(--ok);background:var(--okbg)}.failed{color:var(--bad);background:var(--badbg)}
.skipped{color:var(--skip);background:var(--skipbg)}.handled{color:var(--warn);background:var(--warnbg)}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:120px}
.card b{display:block;font-size:22px}.card span{color:var(--muted);font-size:12px}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:0}td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.path{color:var(--muted);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.err{color:var(--bad);margin-top:4px;white-space:pre-wrap;word-break:break-word}
details{margin-top:6px}summary{cursor:pointer;color:var(--muted);font-size:12px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;font-size:12px;margin:6px 0 0}
img.shot{display:block;max-width:100%;height:auto;margin-top:8px;border:1px solid var(--line);border-radius:6px}
.lvl-error,.lvl-critical{color:var(--bad)}.lvl-warning{color:var(--warn)}.lvl-debug{color:var(--muted)}
footer{margin-top:32px;color:var(--muted);font-size:12px}
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _thumbnail_data_url(path: Optional[str], max_size: int) -> Optional[str]:
    """Load ``path`` and return a PNG data URL of a thumbnail, or ``None`` if unreadable."""
    if not path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image

        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((max_size, max_size))
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 120:
        return f"{seconds:.2f} s"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _render_html(report: RunReport, *, thumbnail_size: int = 480) -> str:
    c = report.counts()
    overall = "passed" if report.ok else "failed"
    overall_label = "PASSED" if report.ok else ("ABORTED" if report.aborted else "FAILED")
    rows: List[str] = []
    for s in report.steps:
        status_cls = "handled" if (s.status == FAILED and s.handled) else s.status
        status_label = "failed (handled)" if status_cls == "handled" else s.status
        indent = min(max(int(s.depth), 0), 12) * 18
        cell = [f'<div style="padding-left:{indent}px"><strong>{_esc(s.name)}</strong>']
        if s.path and s.path != s.name:
            cell.append(f'<div class="path">{_esc(s.path)}</div>')
        if s.error:
            prefix = f"{s.error_type}: " if s.error_type and not str(s.error).startswith(str(s.error_type)) else ""
            cell.append(f'<div class="err">{_esc(prefix + str(s.error))}</div>')
        if s.traceback:
            cell.append(f"<details><summary>Traceback</summary><pre>{_esc(s.traceback)}</pre></details>")
        if s.data:
            try:
                data_text = json.dumps(_jsonable(s.data), indent=2, ensure_ascii=False)
            except Exception:
                data_text = repr(s.data)
            cell.append(f"<details><summary>Details</summary><pre>{_esc(data_text)}</pre></details>")
        if s.screenshot:
            thumb = _thumbnail_data_url(s.screenshot, thumbnail_size)
            if thumb:
                cell.append(f'<img class="shot" alt="{_esc("Screenshot of " + s.name)}" src="{thumb}">')
            cell.append(f'<div class="path">screenshot: {_esc(s.screenshot)}</div>')
        cell.append("</div>")
        rows.append(
            "<tr>"
            f'<td><span class="badge {status_cls}">{_esc(status_label)}</span></td>'
            f"<td>{_esc(s.kind)}</td>"
            f"<td>{''.join(cell)}</td>"
            f'<td class="num">{_esc(s.attempts)}</td>'
            f'<td class="num">{_esc(_fmt_duration(s.duration))}</td>'
            "</tr>"
        )
    log_rows = []
    for rec in report.logs:
        level = str(rec.get("level", "info")).lower()
        extra = {k: v for k, v in rec.items() if k not in ("time", "level", "message", "path", "robot")}
        extra_text = json.dumps(_jsonable(extra), ensure_ascii=False) if extra else ""
        when = rec.get("time")
        when_text = _dt.datetime.fromtimestamp(when, tz=_dt.timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3] if isinstance(when, (int, float)) else ""
        log_rows.append(
            "<tr>"
            f'<td class="num">{_esc(when_text)}</td>'
            f'<td class="lvl-{_esc(level)}">{_esc(level.upper())}</td>'
            f'<td class="path">{_esc(rec.get("path", ""))}</td>'
            f"<td>{_esc(rec.get('message', ''))}"
            + (f'<div class="path">{_esc(extra_text)}</div>' if extra_text else "")
            + "</td></tr>"
        )
    meta_items = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v if isinstance(v, str) else json.dumps(_jsonable(v), ensure_ascii=False))}</td></tr>"
        for k, v in report.metadata.items()
    )
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(report.name)} - run report</title>",
        f"<style>{_CSS}</style></head><body><main>",
        f'<h1>{_esc(report.name)} <span class="badge {overall}">{_esc(overall_label)}</span></h1>',
        f'<div class="meta">Run <code>{_esc(report.run_id)}</code> &middot; started {_esc(_iso(report.started_at) or "-")}'
        f" &middot; duration {_esc(_fmt_duration(report.duration))}"
        + (f" &middot; dir <code>{_esc(report.run_dir)}</code>" if report.run_dir else "")
        + "</div>",
    ]
    if report.error:
        parts.append(f'<div class="err"><strong>Error:</strong> {_esc(report.error)}</div>')
    parts.append(
        '<div class="cards">'
        f'<div class="card"><b>{c["total"]}</b><span>steps</span></div>'
        f'<div class="card"><b>{c[PASSED]}</b><span>passed</span></div>'
        f'<div class="card"><b>{c[FAILED]}</b><span>failed ({c["handled"]} handled)</span></div>'
        f'<div class="card"><b>{c[SKIPPED]}</b><span>skipped</span></div>'
        "</div>"
    )
    parts.append("<h2>Steps</h2>")
    if rows:
        parts.append(
            '<div class="tablewrap"><table><thead><tr><th>Status</th><th>Kind</th><th>Step</th>'
            '<th class="num">Attempts</th><th class="num">Duration</th></tr></thead><tbody>'
            + "".join(rows)
            + "</tbody></table></div>"
        )
    else:
        parts.append('<p class="meta">No steps were executed.</p>')
    if log_rows:
        parts.append(
            "<h2>Log</h2>"
            '<div class="tablewrap"><table><thead><tr><th class="num">Time</th><th>Level</th><th>Where</th><th>Message</th></tr></thead><tbody>'
            + "".join(log_rows)
            + "</tbody></table></div>"
        )
    if meta_items:
        parts.append(f'<h2>Metadata</h2><div class="tablewrap"><table><tbody>{meta_items}</tbody></table></div>')
    parts.append('<footer>Generated by locum</footer></main></body></html>')
    return "\n".join(parts)
