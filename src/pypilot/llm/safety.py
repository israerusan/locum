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
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Pattern, Sequence, Union

from ..errors import PolicyViolation
from ..types import Point, Rect, coerce_point

#: Verbs and nouns whose targets usually commit something irreversible (money, messages,
#: data), with their plural/inflected forms ("Payments", "Transfers", "make_payments") and
#: the main German SAP labels (Buchen, Zahlen, Überweisen, Freigeben, Löschen, Senden).
DEFAULT_CONFIRM: tuple = (
    r"\b(?:delete|deletes|deleting)\b", r"\b(?:remove|removes|removing)\b", r"\bpay(?:s|ing|ments?|outs?)?\b", r"\btransfers?\b",
    r"\bwires?\b", r"\bsubmits?\b", r"\bsend(?:s|ing)?\b", r"\bpurchases?\b", r"\bapproves?\b",
    r"\bpost(?:s|ing)?\b", r"\bbuy\b", r"\bcheck ?out\b", r"\bplace orders?\b", r"\brefunds?\b",
    r"\bauthori[sz]es?\b", r"\bexecute payments?\b", r"\breleases?\b",
    r"\bbuchen\b", r"\bverbuchen\b", r"\bzahl(?:en|ungen|ung)\b", r"\büberweis(?:en|ung|ungen)\b",
    r"\bfreigeben\b", r"\bfreigabe\b", r"\blöschen\b", r"\bsenden\b",
)

#: Keyboard shortcuts that commit whatever is focused, so they always need confirmation
#: (``Policy(confirm_keys=...)``): Ctrl/Cmd+Enter sends (Outlook, Teams, web forms) and
#: Ctrl/Cmd+S saves -- which *posts* the document in SAP transactions such as FB60.
DEFAULT_CONFIRM_KEYS: tuple = ("ctrl+enter", "cmd+enter", "ctrl+s", "cmd+s")

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

#: Values of ``Policy(unknown_target=...)``.
UNKNOWN_TARGET_MODES: tuple = ("confirm", "block", "allow")

#: ``context["target_unknown"]`` reason shown when an activating input's target cannot be identified.
UNKNOWN_TARGET_REASON = "the control this input reaches cannot be identified"

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
        # Compiled regexes and matcher objects (``.search(text)`` + ``.pattern``, e.g. CARD_NUMBER) are kept.
        out.append(re.compile(p, re.IGNORECASE) if isinstance(p, str) else p)
    return out


def launch_program(command: Any) -> str:
    """The program a ``launch_app`` command runs (its first token, quote-aware).

    ``'"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -c whoami'`` -> the full quoted
    path (not ``C:\\Program``); backslashes are kept (Windows paths); a list
    command yields its first item; ``open -a NAME`` (macOS) yields ``NAME``.
    """
    if isinstance(command, (list, tuple)):
        tokens = [str(t) for t in command]
    else:
        tokens = [a or b or c for a, b, c in re.findall(r'"([^"]*)"?|\'([^\']*)\'?|(\S+)', str(command or ""))]
    if not tokens:
        return ""
    if os.path.basename(tokens[0].replace("\\", "/")).lower() == "open":
        for flag, name in zip(tokens[1:], tokens[2:]):
            if flag in ("-a", "-b"):
                return name
    return tokens[0]


class _CardNumber:
    """Deny-pattern matcher for payment card numbers (used by :meth:`Policy.strict`).

    A plain digit regex such as ``\\b(?:\\d[ -]?){13,19}\\b`` also matches IBANs,
    account numbers and references in their usual grouped form. This matcher
    only reports 13-19 digit runs (optionally grouped with spaces/dashes) that
    start like a card (2-6) and pass the Luhn checksum, and ignores IBANs. It is
    not applied to window titles (``match_window_title = False``), so a
    banking window showing an account number stays usable.
    """

    pattern = "payment card number"
    match_window_title = False
    _RUN = re.compile(r"(?<![\w-])\d+(?:[ -]\d+)*(?![\w])")
    _IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b", re.IGNORECASE)

    @staticmethod
    def _luhn(digits: str) -> bool:
        total = 0
        for i, ch in enumerate(reversed(digits)):
            d = int(ch)
            if i % 2:
                d = d * 2 - 9 if d > 4 else d * 2
            total += d
        return total % 10 == 0

    @staticmethod
    def _iban_ok(text: str) -> bool:
        s = text.replace(" ", "")
        if not 15 <= len(s) <= 34:
            return False
        try:
            return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1
        except ValueError:
            return False

    def _is_card(self, digits: str) -> bool:
        return 13 <= len(digits) <= 19 and digits[0] in "23456" and self._luhn(digits)

    def search(self, text: str) -> Optional["re.Match[str]"]:
        text = str(text)
        for m in self._IBAN.finditer(text):
            if self._iban_ok(m.group(0)):
                text = text[: m.start()] + "#" * (m.end() - m.start()) + text[m.end():]
        for m in self._RUN.finditer(text):
            # The whole run, or a card number next to other digit groups ("4111 1111 1111 1111 2027").
            groups = re.split(r"[ -]", m.group(0))
            for i in range(len(groups)):
                digits = ""
                for g in groups[i:]:
                    digits += g
                    if len(digits) > 19:
                        break
                    if self._is_card(digits):
                        return m
        return None

    def __repr__(self) -> str:
        return "CARD_NUMBER"


#: Deny pattern for payment card numbers (Luhn-checked; IBANs and account numbers pass).
CARD_NUMBER = _CardNumber()


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
        allowed_apps: If set, actions are only allowed in these apps (globs, ``.exe``
            optional): the target's application (``target_app(s)``: the element
            acted on, the focused control for keyboard input, the application
            under the pointer) or, when no target is known, the active window's.
            Pointer actions whose application cannot be determined are refused.
            ``launch_app`` may only start programs that match (see
            :func:`launch_program`; an unquoted multi-word command without
            ``/`` is also judged as a whole app name, as macOS opens
            ``"Microsoft Excel"`` with ``open -a``).
        blocked_apps: Apps that may never be driven, judged like ``allowed_apps``
            (the target's app, else the active one -- a blocked terminal in front
            does not stop the agent from focusing another window); also checked
            against the program ``launch_app`` starts.
        deny_text_patterns: Regexes (case-insensitive) or matcher objects such as
            :data:`CARD_NUMBER`; an action whose arguments, target or window title
            match is blocked (e.g. ``rm -rf``). Matchers with
            ``match_window_title = False`` ignore the window title.
        confirm_patterns: Regexes; an action whose **target** (the clicked element's
            name, selector or text, the key combo, the launched command...) matches
            needs ``confirm`` to return True. Typed text and key names are not
            matched (typing is not a commit); the click that submits it is.
        confirm_keys: Key combinations that always need confirmation, whatever has
            focus (default :data:`DEFAULT_CONFIRM_KEYS`; ``mod`` means Ctrl and Cmd).
            Keyboard tools put their combos in ``context["keys"]``. Shortcuts that
            press a labelled control (``alt+p`` for "&Pay now", a menu item's
            ``Ctrl+S`` accelerator) are also judged by that control's label
            (``context["shortcut_targets"]``); other application shortcuts
            (``F8`` = Execute in SAP) are not gated unless listed here.
        confirm: ``callback(description, details) -> bool`` asked for confirmations.
            Without a callback, actions that need confirmation are **blocked**.
        unknown_target: What to do with input that can activate something
            (a click, Enter/Space, typed text) when its target cannot be
            identified -- the focused element is unreadable (no AT-SPI, Java,
            Citrix...) or nothing is found under the pointer, so confirmation
            patterns cannot be matched: ``"confirm"`` (default) asks the human
            like a pattern match, ``"block"`` refuses, ``"allow"`` lets it run.
            Ignored when there are no ``confirm_patterns``
            (:meth:`permissive`). Tools set ``context["target_unknown"]``.
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
        unknown_target: str = "confirm",
        confirm_keys: Iterable[Union[str, Sequence[str]]] = DEFAULT_CONFIRM_KEYS,
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
        self.confirm_keys = tuple(confirm_keys)
        self._confirm_combos = [c for spec in self.confirm_keys for c in _combo_sets(spec)]
        if unknown_target not in UNKNOWN_TARGET_MODES:
            raise ValueError(f"unknown_target must be one of {', '.join(UNKNOWN_TARGET_MODES)}, got {unknown_target!r}")
        self.unknown_target = unknown_target
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
        kw.setdefault("confirm_keys", ())
        return cls(**kw)

    @classmethod
    def strict(cls, **kw: Any) -> "Policy":
        """Conservative defaults for unattended agents on a real desktop.

        No app launching or clipboard access, shells/registry blocked, payment
        card numbers (:data:`CARD_NUMBER`) and ``rm -rf``/``format`` denied, a
        broad set of confirmation triggers, at most 200 actions and 30 per minute.
        """
        kw.setdefault("deny_tools", ("launch_app", "write_clipboard", "read_clipboard"))
        kw.setdefault("blocked_apps", STRICT_BLOCKED_APPS)
        kw.setdefault("confirm_patterns", STRICT_CONFIRM)
        kw.setdefault("deny_text_patterns", (r"\brm\s+-rf\b", r"\bformat\s+[a-z]:", CARD_NUMBER))
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
        if self.is_read_only(tool_name):
            return None
        if self._confirm_combos and context.get("keys"):
            for combo in _key_items(context["keys"]):
                if any(combo == c for c in self._confirm_combos):
                    return f"shortcut {'+'.join(sorted(combo))!r} commits the focused form"
        if not self.confirm_patterns:
            return None
        if context.get("require_confirmation"):
            # The integrator marked this tool as always needing a human (``ToolSpec(confirm=True)``).
            return str(context["require_confirmation"]) if isinstance(context["require_confirmation"], str) \
                else f"{tool_name!r} always requires confirmation"
        # Key names are deliberately not matched: the Delete key is editing, not a "delete" commit.
        haystack = [str(context.get(k)) for k in ("target", "target_text", "selector", "command") if context.get(k)]
        # Controls a keyboard shortcut presses (access key / accelerator: alt+p on "&Pay now").
        haystack.extend(_strings(context.get("shortcut_targets") or ()))
        for key in ("selector", "command", "title"):
            if key in args:
                haystack.extend(_strings(args[key]))
        if tool_name in ("launch_app",):
            haystack.extend(_strings(args))
        # Every string argument of a non-desktop (integration) tool: ``erp_api(operation="pay")``.
        haystack.extend(_strings(context.get("arg_texts") or ()))
        for text in haystack:
            for rx in self.confirm_patterns:
                m = rx.search(text)
                if m:
                    return f"{m.group(0)!r} in {text[:120]!r}"
        if context.get("target_unknown") and self.unknown_target == "confirm":
            return UNKNOWN_TARGET_REASON
        return None

    # -------------------------------------------------------------------- check
    def check(self, tool_name: str, args: Optional[Dict[str, Any]] = None, *, context: Optional[Dict[str, Any]] = None,
              count: bool = True) -> None:
        """Raise :class:`PolicyViolation` if the call may not run; record it as an action otherwise.

        ``context`` keys understood (all optional): ``app`` and ``window_title``
        (of the active window), ``target_app`` (application of the target
        element), ``target_apps`` (every application the action touches, e.g.
        both ends of a drag; each must pass ``allowed_apps``/``blocked_apps``),
        ``target`` (description of the target, e.g. the element name), ``point`` / ``start_point`` / ``end_point`` /
        ``points`` (screen coordinates), ``command`` (for launches),
        ``description`` (what the action will do, shown in confirmations),
        ``target_unknown`` (the input can activate a control that cannot be
        identified; see ``unknown_target``), ``arg_texts`` (argument strings
        matched against ``confirm_patterns``, for integration tools),
        ``require_confirmation`` (always ask, e.g. ``ToolSpec(confirm=True)``).
        In dry-run mode confirmations are skipped (nothing will happen) and
        ``context["needs_confirmation"]`` is set instead.

        ``count=False`` re-checks a step *inside* an action that was already
        checked (e.g. the Enter after a Tab in one key sequence, whose target is
        only known once focus has moved): every rule applies except the action
        budget and rate limit, and nothing is counted.
        """
        args = dict(args or {})
        ctx = context if context is not None else {}
        if not self.tool_allowed(tool_name):
            raise PolicyViolation(f"Tool {tool_name!r} is not allowed by the policy")
        if self.is_read_only(tool_name):
            return

        # Applications. ``target_apps`` lists every app an action touches (e.g. both ends of a drag);
        # input goes to those apps, so they -- not the active window -- are judged when known
        # (a blocked terminal in front must not stop the agent from focusing/clicking SAP).
        target_apps = [str(a) for a in (ctx.get("target_apps") or ()) if a]
        if ctx.get("target_app") and str(ctx["target_app"]) not in target_apps:
            target_apps.insert(0, str(ctx["target_app"]))
        if tool_name == "launch_app":
            command = ctx.get("command") or args.get("command") or ""
            program = launch_program(command)
            # macOS opens an unquoted multi-word name ("Google Chrome") as one app (`open -a`)
            names = [program]
            if isinstance(command, str):
                whole = " ".join(command.split())
                if " " in whole and not any(c in whole for c in "/\\\"'") and whole != program:
                    names.append(whole)
            for name in names:
                if name and self.blocked_apps and _app_matches(name, self.blocked_apps):
                    raise PolicyViolation(f"Application {name!r} is blocked by the policy")
            if self.allowed_apps is not None and not any(_app_matches(n, self.allowed_apps) for n in names):
                raise PolicyViolation(
                    f"Launching {program or 'an unknown program'!r} is not allowed; allowed apps: {', '.join(self.allowed_apps)}"
                )
        else:
            for app in target_apps or [ctx.get("app")]:
                if app and self.blocked_apps and _app_matches(app, self.blocked_apps):
                    raise PolicyViolation(f"Application {app!r} is blocked by the policy")
            if self.allowed_apps is not None:
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
            hay = list(_strings(args)) + [str(ctx[k]) for k in ("target", "command") if ctx.get(k)]
            title = str(ctx.get("window_title") or "")
            for rx in self.deny_text_patterns:
                texts = hay + [title] if title and getattr(rx, "match_window_title", True) else hay
                for text in texts:
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

        # Input that may activate a control nobody can identify (see ``unknown_target``).
        if ctx.get("target_unknown") and self.unknown_target == "block" and self.confirm_patterns:
            raise PolicyViolation(
                f"Action blocked: {UNKNOWN_TARGET_REASON}, so the confirmation rules cannot be applied "
                "(Policy(unknown_target='block'))"
            )

        # Rate limits.
        if count:
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

        if not count:
            return
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


_MOD_ALIASES = ("mod", "primary", "cmdorctrl", "commandorcontrol")


def _combo_sets(spec: Any) -> List[frozenset]:
    """``"mod+enter"`` -> ``[{ctrl, enter}, {cmd, enter}]`` (right-hand modifiers and the
    keypad Enter folded); unparseable -> ``[]``."""
    from ..keys import parse_hotkey

    specs: List[Any] = [spec if isinstance(spec, str) else list(spec)]
    if isinstance(spec, str) and "+" in spec.strip("+"):
        parts = spec.split("+")
        if any(p.strip().lower() in _MOD_ALIASES for p in parts):
            specs = ["+".join(m if p.strip().lower() in _MOD_ALIASES else p for p in parts) for m in ("ctrl", "cmd")]
    out: List[frozenset] = []
    for sp in specs:
        try:
            keys = parse_hotkey(sp)
        except (ValueError, TypeError):
            continue
        folded = (k[:-2] if k.endswith("_r") else "enter" if k == "num_enter" else k.lower() if len(k) == 1 else k
                  for k in keys)
        out.append(frozenset(folded))
    return out


def _key_items(keys: Any) -> List[frozenset]:
    """The combos in a keyboard tool's ``context["keys"]``: ``"tab ctrl+Return"`` / ``["ctrl", "s"]``."""
    items: List[Any] = (keys.split() or [keys]) if isinstance(keys, str) else [keys]
    out: List[frozenset] = []
    for item in items:
        out.extend(_combo_sets(item)[:1])
    return out


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


def typed_text_activates(text: str) -> bool:
    """Whether typing ``text`` can activate the focused control (a newline is Enter, a space presses a button)."""
    return any(c in str(text) for c in "\n\r ")


#: Keys that edit the focused control without moving focus away from it.
_EDITING_KEYS = frozenset({"backspace", "delete"})


def keys_may_move_focus(keys: Iterable[str]) -> bool:
    """Whether pressing the combination ``keys`` (canonical names) may move keyboard focus.

    Conservative: only characters (with or without Shift) and Backspace/Delete
    are known to leave focus where it is. Tab, arrows, Enter, Escape, shortcuts
    such as ``alt+tab`` ... may put the next key into another control, window or
    application, so the policy must look at the focused element again.
    """
    for k in keys:
        if k in ("shift", "shift_r") or k in _EDITING_KEYS or (len(k) == 1 and k not in "\t\n\r"):
            continue
        return True
    return False


def check_activation_combo(keys: Iterable[str]) -> None:
    """Reject a combination that both activates and moves focus (e.g. ``tab+Return``).

    Enter/Space with only modifiers (``ctrl+enter``) acts on the focused control,
    which the policy knows; combined with another key the control it activates is
    unknowable in advance. Raises ``ValueError`` asking for separate key presses.
    """
    from ..keys import MODIFIERS

    keys = list(keys)
    if any(k in ACTIVATING_KEYS for k in keys) and any(k not in ACTIVATING_KEYS and k not in MODIFIERS for k in keys):
        raise ValueError(
            f"Combination {'+'.join(keys)!r} mixes Enter/Space with another key; press the keys one after another instead"
        )


def split_typed_text(text: str) -> List[str]:
    """Split text to type after every Tab/newline (``"1000\\t\\n"`` -> ``["1000\\t", "\\n"]``).

    Each piece after the first may land in another control (Tab moves focus,
    Enter can submit a dialog and open the next one), so callers re-check the
    policy against the element focused before typing it.
    """
    return [p for p in re.split(r"(?<=[\t\n])|(?<=\r)(?!\n)", text) if p]


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
    "CARD_NUMBER",
    "launch_program",
    "check_activation_combo",
    "keys_activate",
    "keys_may_move_focus",
    "split_typed_text",
    "AuditLog",
    "DEFAULT_CONFIRM",
    "DEFAULT_CONFIRM_KEYS",
    "Policy",
    "READ_ONLY_TOOLS",
    "STRICT_CONFIRM",
    "UNKNOWN_TARGET_MODES",
    "typed_text_activates",
    "console_confirm",
]
