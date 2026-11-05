"""Guardrails for LLM-driven automation: policies, human confirmation, dry-runs and audit logs.

Every tool call an agent makes (custom desktop tools, computer-use actions,
MCP tool calls) passes through :meth:`Policy.check` *before* anything touches
the desktop. A policy can:

* allow/deny tools by name (globs: ``"click"``, ``"*clipboard*"``);
* restrict which applications may be driven (``allowed_apps`` / ``blocked_apps``);
* block actions whose target or arguments match ``deny_text_patterns``;
* require a **human confirmation** (``confirm`` callback) when the target of an
  action looks consequential -- by default finance/destructive verbs such as
  *pay, transfer, wire, approve, post, submit, delete, send, purchase*;
* confine pointer actions to a screen ``region``;
* cap the total number of actions and the actions per minute;
* run in ``dry_run`` mode, where tools report what they *would* have done.

:class:`AuditLog` records every decision and result as JSON lines.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Pattern, Union

from ..errors import PolicyViolation
from ..types import Point, Rect, coerce_point

#: Verbs whose targets usually commit something irreversible (money, messages, data).
DEFAULT_CONFIRM: tuple = (
    r"\bdelete\b", r"\bremove\b", r"\bpay(ment)?\b", r"\btransfer\b", r"\bwire\b", r"\bsubmit\b",
    r"\bsend\b", r"\bpurchase\b", r"\bapprove\b", r"\bpost(ing)?\b", r"\bbuy\b", r"\bcheckout\b",
    r"\bplace order\b", r"\brefund\b", r"\bauthori[sz]e\b", r"\bexecute payment\b",
)

#: Extra confirmation triggers used by :meth:`Policy.strict`.
STRICT_CONFIRM: tuple = DEFAULT_CONFIRM + (
    r"\bsave\b", r"\bclose\b", r"\bquit\b", r"\bexit\b", r"\binstall\b", r"\buninstall\b", r"\bformat\b",
    r"\blog ?out\b", r"\bsign ?out\b", r"\border\b", r"\bconfirm\b", r"\baccept\b", r"\bpublish\b",
    r"\bshutdown\b", r"\brestart\b", r"\breset\b", r"\boverwrite\b", r"\bempty\b",
    r"\bsign\b(?!\s*(?:in|up)\b)",
)

#: Tools that only read state. They skip app/region/confirmation checks and
#: rate limits, and still run in dry-run mode (but obey allow/deny lists).
READ_ONLY_TOOLS: frozenset = frozenset(
    {
        "observe", "screenshot", "find_elements", "list_windows", "read_clipboard", "read_text", "wait",
        "wait_for", "done", "finish", "zoom", "cursor_position",
    }
)

#: Applications :meth:`Policy.strict` refuses to drive (shells, registry, admin consoles).
STRICT_BLOCKED_APPS: tuple = (
    "cmd", "powershell", "pwsh", "windowsterminal", "terminal", "iterm2", "xterm", "gnome-terminal*", "konsole",
    "bash", "zsh", "sh", "regedit", "mmc", "taskmgr", "keychain*",
)

ConfirmCallback = Callable[[str, Dict[str, Any]], bool]


def _norm_app(app: Any) -> str:
    a = str(app or "").strip().lower()
    a = os.path.basename(a.replace("\\", "/"))
    return a[:-4] if a.endswith(".exe") else a


def _app_matches(app: str, patterns: Iterable[str]) -> bool:
    a = _norm_app(app)
    if not a:
        return False
    for p in patterns:
        pn = _norm_app(p)
        if not pn:
            continue
        if any(ch in pn for ch in "*?[") and fnmatch.fnmatchcase(a, pn):
            return True
        if a == pn:
            return True
    return False


def _compile(patterns: Iterable[Union[str, Pattern]]) -> List[Pattern]:
    out = []
    for p in patterns:
        out.append(p if isinstance(p, re.Pattern) else re.compile(p, re.IGNORECASE))
    return out


def _strings(value: Any, depth: int = 0) -> Iterable[str]:
    """Every string inside nested args (dict values, list items)."""
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, depth + 1)


class Policy:
    """Decides whether an agent's tool call may run (see module docstring).

    Args:
        allow_tools: Tool-name globs that may run (``None`` = all tools).
        deny_tools: Tool-name globs that may never run (wins over ``allow_tools``).
        allowed_apps: If set, actions are only allowed while the active window (or
            the target) belongs to one of these apps (globs, ``.exe`` optional).
            Pointer actions are judged by the application under the pointer
            (``target_app(s)``); when it cannot be determined they are refused.
        blocked_apps: Apps that may never be driven (also checked for ``launch_app``).
        deny_text_patterns: Regexes (case-insensitive); an action whose arguments,
            target or window title match is blocked (e.g. card numbers, ``rm -rf``).
        confirm_patterns: Regexes; an action whose **target** (the clicked element's
            name, selector or text, the key combo, the launched command...) matches
            needs ``confirm`` to return True. Typed text and key names are not
            matched (typing is not a commit); the click that submits it is.
        confirm: ``callback(description, details) -> bool`` asked for confirmations.
            Without a callback, actions that need confirmation are **blocked**.
        dry_run: Tools describe what they would do instead of acting.
        max_actions: Maximum number of (non read-only) actions in total.
        max_actions_per_minute: Sliding-window rate limit for actions.
        region: Pointer actions must land inside this screen rectangle.
        read_only_tools: Names treated as read-only (default :data:`READ_ONLY_TOOLS`).
        clock: Monotonic clock (injectable for tests).
    """

    def __init__(
        self,
        allow_tools: Optional[Iterable[str]] = None,
        deny_tools: Iterable[str] = (),
        allowed_apps: Optional[Iterable[str]] = None,
        blocked_apps: Iterable[str] = (),
        deny_text_patterns: Iterable[Union[str, Pattern]] = (),
        confirm_patterns: Iterable[Union[str, Pattern]] = DEFAULT_CONFIRM,
        confirm: Optional[ConfirmCallback] = None,
        dry_run: bool = False,
        max_actions: Optional[int] = None,
        max_actions_per_minute: Optional[int] = None,
        region: Optional[Any] = None,
        *,
        read_only_tools: Iterable[str] = READ_ONLY_TOOLS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.allow_tools = None if allow_tools is None else tuple(allow_tools)
        self.deny_tools = tuple(deny_tools)
        self.allowed_apps = None if allowed_apps is None else tuple(allowed_apps)
        self.blocked_apps = tuple(blocked_apps)
        self.deny_text_patterns = _compile(deny_text_patterns)
        self.confirm_patterns = _compile(confirm_patterns)
        self.confirm = confirm
        self.dry_run = bool(dry_run)
        if max_actions is not None and int(max_actions) < 0:
            raise ValueError("max_actions must be >= 0")
        if max_actions_per_minute is not None and int(max_actions_per_minute) <= 0:
            raise ValueError("max_actions_per_minute must be > 0")
        self.max_actions = None if max_actions is None else int(max_actions)
        self.max_actions_per_minute = None if max_actions_per_minute is None else int(max_actions_per_minute)
        self.region = None if region is None else Rect.coerce(region)
        self.read_only_tools = frozenset(read_only_tools)
        self._clock = clock
        self._lock = threading.Lock()
        self.action_count = 0
        self._recent: Deque[float] = deque()
        #: Human-readable log of confirmations asked: ``(description, approved)``.
        self.confirmations: List[tuple] = []

    # ------------------------------------------------------------------ presets
    @classmethod
    def permissive(cls, **kw: Any) -> "Policy":
        """Everything allowed, no confirmations (trusted scripts, sandboxes, evals)."""
        kw.setdefault("confirm_patterns", ())
        return cls(**kw)

    @classmethod
    def strict(cls, **kw: Any) -> "Policy":
        """Conservative defaults for unattended agents on a real desktop.

        No app launching or clipboard access, shells/registry blocked, a broad set
        of confirmation triggers, at most 200 actions and 30 per minute.
        """
        kw.setdefault("deny_tools", ("launch_app", "write_clipboard", "read_clipboard"))
        kw.setdefault("blocked_apps", STRICT_BLOCKED_APPS)
        kw.setdefault("confirm_patterns", STRICT_CONFIRM)
        kw.setdefault("deny_text_patterns", (r"\brm\s+-rf\b", r"\bformat\s+[a-z]:", r"\b(?:\d[ -]?){13,19}\b"))
        kw.setdefault("max_actions", 200)
        kw.setdefault("max_actions_per_minute", 30)
        return cls(**kw)

    # ------------------------------------------------------------------ helpers
    def is_read_only(self, tool_name: str) -> bool:
        return tool_name in self.read_only_tools

    def tool_allowed(self, tool_name: str) -> bool:
        """Whether ``tool_name`` passes the allow/deny lists."""
        if any(fnmatch.fnmatchcase(tool_name, p) for p in self.deny_tools):
            return False
        if self.allow_tools is not None:
            return any(fnmatch.fnmatchcase(tool_name, p) for p in self.allow_tools)
        return True

    @staticmethod
    def _points(args: Dict[str, Any], context: Dict[str, Any]) -> List[Point]:
        pts: List[Point] = []
        for key in ("point", "start_point", "end_point"):
            if context.get(key) is not None:
                try:
                    pts.append(coerce_point(context[key]))
                except (TypeError, ValueError, KeyError):
                    pass
        for p in context.get("points") or ():
            try:
                pts.append(coerce_point(p))
            except (TypeError, ValueError, KeyError):
                pass
        return pts

    def confirmation_reason(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Optional[str]:
        """The pattern match that makes this action need confirmation, or ``None``."""
        if self.is_read_only(tool_name) or not self.confirm_patterns:
            return None
        # Key names are deliberately not matched: the Delete key is editing, not a "delete" commit.
        haystack = [str(context.get(k)) for k in ("target", "target_text", "selector", "command") if context.get(k)]
        for key in ("selector", "command", "title"):
            if key in args:
                haystack.extend(_strings(args[key]))
        if tool_name in ("launch_app",):
            haystack.extend(_strings(args))
        for text in haystack:
            for rx in self.confirm_patterns:
                m = rx.search(text)
                if m:
                    return f"{m.group(0)!r} in {text[:120]!r}"
        return None

    # -------------------------------------------------------------------- check
    def check(self, tool_name: str, args: Optional[Dict[str, Any]] = None, *, context: Optional[Dict[str, Any]] = None) -> None:
        """Raise :class:`PolicyViolation` if the call may not run; record it as an action otherwise.

        ``context`` keys understood (all optional): ``app`` and ``window_title``
        (of the active window), ``target_app`` (application of the target
        element), ``target_apps`` (every application the action touches, e.g.
        both ends of a drag; each must pass ``allowed_apps``/``blocked_apps``),
        ``target`` (description of the target, e.g. the element name), ``point`` / ``start_point`` / ``end_point`` /
        ``points`` (screen coordinates), ``command`` (for launches),
        ``description`` (what the action will do, shown in confirmations).
        In dry-run mode confirmations are skipped (nothing will happen) and
        ``context["needs_confirmation"]`` is set instead.
        """
        args = dict(args or {})
        ctx = context if context is not None else {}
        if not self.tool_allowed(tool_name):
            raise PolicyViolation(f"Tool {tool_name!r} is not allowed by the policy")
        if self.is_read_only(tool_name):
            return

        # Applications. ``target_apps`` lists every app an action touches (e.g. both ends of a drag).
        target_apps = [str(a) for a in (ctx.get("target_apps") or ()) if a]
        if ctx.get("target_app") and str(ctx["target_app"]) not in target_apps:
            target_apps.insert(0, str(ctx["target_app"]))
        apps = [a for a in [ctx.get("app")] + target_apps if a]
        if tool_name == "launch_app":
            cmd = ctx.get("command") or args.get("command") or ""
            first = str(cmd).strip().split()[0] if str(cmd).strip() else ""
            if first:
                apps.append(first.strip("\"'"))
        for app in apps:
            if self.blocked_apps and _app_matches(app, self.blocked_apps):
                raise PolicyViolation(f"Application {app!r} is blocked by the policy")
        if self.allowed_apps is not None and tool_name != "launch_app":
            if not target_apps and self._points(args, ctx):
                # A pointer action lands in whatever is under the pointer, not in the
                # active window: if that application is unknown, refuse rather than
                # judging the action by the active app.
                raise PolicyViolation(
                    "Cannot determine the application under the pointer; "
                    f"allowed apps: {', '.join(self.allowed_apps)}"
                )
            for app in target_apps or [ctx.get("app")]:
                if not app or not _app_matches(app, self.allowed_apps):
                    raise PolicyViolation(
                        f"Action in application {app or 'unknown'!r} is not allowed; allowed apps: {', '.join(self.allowed_apps)}"
                    )

        # Forbidden text anywhere in the arguments or target.
        if self.deny_text_patterns:
            hay = list(_strings(args)) + [str(ctx[k]) for k in ("target", "window_title", "command") if ctx.get(k)]
            for text in hay:
                for rx in self.deny_text_patterns:
                    if rx.search(text):
                        raise PolicyViolation(f"Action blocked: {rx.pattern!r} matched {text[:80]!r}")

        # Region confinement.
        if self.region is not None:
            for p in self._points(args, ctx):
                if not self.region.contains(p):
                    raise PolicyViolation(
                        f"Point ({p.x}, {p.y}) is outside the allowed region "
                        f"({self.region.left},{self.region.top} {self.region.width}x{self.region.height})"
                    )

        # Rate limits.
        with self._lock:
            now = self._clock()
            if self.max_actions is not None and self.action_count >= self.max_actions:
                raise PolicyViolation(f"Action budget exhausted ({self.max_actions} actions)")
            if self.max_actions_per_minute is not None:
                while self._recent and now - self._recent[0] >= 60.0:
                    self._recent.popleft()
                if len(self._recent) >= self.max_actions_per_minute:
                    raise PolicyViolation(f"Rate limit exceeded ({self.max_actions_per_minute} actions per minute)")

        # Human confirmation.
        reason = self.confirmation_reason(tool_name, args, ctx)
        if reason is not None:
            if self.dry_run:
                ctx["needs_confirmation"] = reason
            else:
                description = ctx.get("description") or f"{tool_name} {json.dumps(args, default=str)[:200]}"
                if self.confirm is None:
                    raise PolicyViolation(
                        f"Action {description!r} needs human confirmation ({reason}) but no confirm callback is configured"
                    )
                details = {"tool": tool_name, "args": args, "reason": reason}
                details.update({k: v for k, v in ctx.items() if k not in ("desktop",)})
                try:
                    approved = bool(self.confirm(description, details))
                except Exception as exc:
                    raise PolicyViolation(f"Confirmation failed for {description!r}: {exc}") from exc
                self.confirmations.append((description, approved))
                if not approved:
                    raise PolicyViolation(f"Action {description!r} was declined by the user")

        with self._lock:
            self.action_count += 1
            if self.max_actions_per_minute is not None:
                self._recent.append(self._clock())

    def reset(self) -> None:
        """Reset action counters (e.g. between agent runs)."""
        with self._lock:
            self.action_count = 0
            self._recent.clear()

    def __repr__(self) -> str:
        return (
            f"<Policy dry_run={self.dry_run} allow={self.allow_tools} deny={self.deny_tools} "
            f"actions={self.action_count}/{self.max_actions}>"
        )


#: Keys that activate the focused control (press a button, submit a form).
ACTIVATING_KEYS = frozenset({"enter", "num_enter", "space"})


def keys_activate(keys: Iterable[Any]) -> bool:
    """Whether any key/combination in ``keys`` can activate the focused control.

    Items are hotkey strings (``"ctrl+enter"``) or sequences of key names; text
    containing a newline also counts. Unparseable items are treated
    conservatively by plain substring checks.
    """
    from ..keys import parse_hotkey

    for item in keys:
        if isinstance(item, str) and ("\n" in item or "\r" in item):
            return True
        try:
            parts = parse_hotkey(item)
        except Exception:
            text = str(item).lower()
            if any(k in text for k in ("enter", "return", "space")):
                return True
            continue
        if any(p in ACTIVATING_KEYS for p in parts):
            return True
    return False


def console_confirm(description: str, details: Dict[str, Any]) -> bool:
    """A ``confirm`` callback that asks on the terminal (``y``/``n``)."""
    try:
        answer = input(f"[pypilot] Allow: {description}? (reason: {details.get('reason')}) [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


_MAX_STR = 2000


def _scrub(value: Any, depth: int = 0) -> Any:
    """JSON-safe copy with long strings (base64 images) truncated."""
    if depth > 8:
        return "..."
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:200] + f"...<{len(value)} chars>"
    if isinstance(value, dict):
        return {str(k): _scrub(v, depth + 1) for k, v in value.items() if k != "desktop"}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _scrub(to_dict(), depth + 1)
        except Exception:
            pass
    return str(value)


class AuditLog:
    """Append-only JSON-lines audit trail.

    Every event gets an ISO-8601 UTC ``ts``; long strings (screenshots) are
    truncated. Events are kept in memory (:attr:`events`) and, when ``path`` is
    given, appended to that file (one JSON object per line, flushed per event).
    """

    def __init__(self, path: Optional[Union[str, "os.PathLike[str]"]] = None, *, keep: Optional[int] = 10000):
        self.path = None if path is None else os.fspath(path)
        self.events: List[Dict[str, Any]] = []
        self._keep = keep
        self._lock = threading.Lock()
        if self.path:
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)

    def record(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Record ``event`` (a dict); returns the stored, JSON-safe copy."""
        entry = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")}
        entry.update(_scrub(dict(event)))
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            self.events.append(entry)
            if self._keep is not None and len(self.events) > self._keep:
                del self.events[: len(self.events) - self._keep]
            if self.path:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        return entry

    def read(self) -> List[Dict[str, Any]]:
        """All events stored in the file (or in memory when there is no file)."""
        if not self.path or not os.path.exists(self.path):
            return list(self.events)
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def __len__(self) -> int:
        return len(self.events)

    def __repr__(self) -> str:
        return f"<AuditLog path={self.path!r} events={len(self.events)}>"


__all__ = [
    "ACTIVATING_KEYS",
    "keys_activate",
    "AuditLog",
    "DEFAULT_CONFIRM",
    "Policy",
    "READ_ONLY_TOOLS",
    "STRICT_CONFIRM",
    "console_confirm",
]
