"""Guardrails for desktop automation: policies, human confirmation, dry-runs and audit logs.

Every tool call an agent makes (custom desktop tools, computer-use actions,
MCP tool calls) passes through :meth:`Policy.check` *before* anything touches
the desktop. RPA code (:class:`~locum.rpa.Robot`, YAML/JSON flows) is
checked the same way **only when it is given a policy**: ``Robot(policy=...)``
/ ``run_flow(..., policy=...)`` drive a :class:`GuardedDesktop`; without one
they act exactly as before, ungated. A policy can:

* allow/deny tools by name (globs: ``"click"``, ``"*clipboard*"``);
* restrict which applications may be driven (``allowed_apps`` / ``blocked_apps``);
* block actions whose target or arguments match ``deny_text_patterns``;
* require a **human confirmation** (``confirm`` callback) for consequential
  actions -- see the two modes below;
* confine pointer actions to a screen ``region``;
* cap the total number of actions and the actions per minute;
* run in ``dry_run`` mode, where tools report what they *would* have done.

Two ways to decide what needs a human:

* **Heuristic tripwire (default, ``Policy()``).** Confirmation is required
  when the target's label, selector, command or arguments match
  ``confirm_patterns`` (English/German finance and destructive verbs such as
  *pay, transfer, submit, delete, send, Buchen*), when a ``confirm_keys``
  shortcut is pressed (Ctrl/Cmd+Enter, Ctrl/Cmd+S), or when an activating
  input (click, Enter/Space, typing that clicks to focus) reaches a control
  with **no usable identity** -- nothing under the pointer, an unreadable
  focus, or a control with no name, automation id, text value or named
  descendant at the click point (``unknown_target``). This is keyword
  inference: it misses icon-only buttons that report a name it does not
  know, labels in other languages, "Continue"/"Apply"-style commits and
  application shortcuts it was not told about. Treat it as a tripwire that
  catches common mistakes, **not** as authorization for money or data actions.
* **Allow-list authorization (``Policy(allow=[...])`` / ``Policy.strict(allow=[...])``).**
  Every consequential action -- clicks and other activations, text entry,
  native invoke/toggle/select, ``launch_app``, ``write_clipboard``,
  integration tools, and any key press other than pure navigation
  (Tab/arrows/Home/End/PageUp/PageDown) -- needs confirmation unless an
  explicit :class:`AllowRule` you configured matches its target (a selector
  the target element matches, a window title or application glob, a launch
  command, a key combination, or a tool name). Read-only tools (observe,
  screenshot, find, list windows, read text/clipboard, wait) are never gated;
  pointer movement, scrolling and window focus are not consequential. The
  keyword tripwire still applies on top, but it is never the only gate.
  Rules judge what really receives the input: a click by the control the
  mouse presses (an actionable child at a container's centre, not the
  container), Enter in a single-line field by the default button. A rule
  without ``keys`` authorizes only keys that act on the control it matched
  (navigation, bare Enter/Space, typing and editing keys in a text field);
  application shortcuts (Escape, F-keys, ``alt+f4``, ``alt+<access key>``,
  ``ctrl+s``) need a rule that lists them.

:class:`AuditLog` records every decision and result as JSON lines.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import functools
import itertools
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Pattern, Sequence, Tuple, Union

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

#: Tools that never commit anything: moving the pointer, scrolling, focusing a
#: window or element, expanding/collapsing a tree node. In allow-list mode
#: (``Policy(allow=...)``) they need no allow rule (app, region and budget rules still apply).
NON_COMMITTING_TOOLS: frozenset = frozenset(
    {"scroll", "move_mouse", "mouse_move", "focus_window", "focus", "expand", "collapse", "scroll_into_view"}
)

#: Keys that only move the caret or the focus. A key press made of these (with
#: Shift/Ctrl at most: ``shift+tab``, ``ctrl+right``) is not consequential in
#: allow-list mode; every other key or shortcut (Enter, Space, Delete, Escape,
#: ``ctrl+s``, ``alt+f4``, F-keys, characters) is.
NAVIGATION_KEYS: frozenset = frozenset({"tab", "up", "down", "left", "right", "home", "end", "pageup", "pagedown"})
_NAVIGATION_MODIFIERS = frozenset({"shift", "ctrl"})

#: Tools that start programs / fill the clipboard: an :class:`AllowRule` only covers
#: them when it names them (``launch=...`` for ``launch_app``, ``tool=write_clipboard``).
_UNTARGETED_TOOLS = frozenset({"launch_app", "write_clipboard"})

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


#: ``launch`` options that never change *which* program runs: guarded launches accept them.
SAFE_LAUNCH_OPTIONS = frozenset({
    "wait_window", "timeout",                                   # Desktop.launch's own arguments
    "stdin", "stdout", "stderr", "close_fds", "bufsize",
    "text", "universal_newlines", "encoding", "errors",
    "creationflags", "startupinfo", "start_new_session",
})
#: Options that let a launch run something other than the program the policy checked:
#: ``shell=True`` makes ``"notepad.exe & powershell ..."`` run PowerShell through
#: ``cmd.exe``; ``executable`` replaces the binary; ``preexec_fn`` runs code in the child.
#: A guarded launch never accepts them (``shell=False`` is fine).
FORBIDDEN_LAUNCH_OPTIONS = frozenset({"shell", "executable", "preexec_fn"})


def check_launch_options(options: Mapping[str, Any], policy: Optional["Policy"] = None) -> None:
    """Refuse launch options that would run something other than the program the policy approved.

    Authorization looks at the command (:func:`launch_program`); it is only
    meaningful if the process started is that program. Options in
    :data:`SAFE_LAUNCH_OPTIONS` are always accepted; :data:`FORBIDDEN_LAUNCH_OPTIONS`
    never are (unless falsy, e.g. ``shell=False``); anything else -- ``env`` (its
    ``PATH`` decides which binary a bare name resolves to), ``cwd`` (relative
    paths), ``pass_fds`` ... -- needs an explicit ``Policy(launch_options=[...])``.
    Raises :class:`PolicyViolation`.
    """
    extra = getattr(policy, "launch_options", frozenset()) if policy is not None else frozenset()
    bad = []
    for key, value in options.items():
        if key in FORBIDDEN_LAUNCH_OPTIONS:
            if value:
                bad.append(f"{key}={value!r} (never allowed: it can run a program the policy did not check)")
        elif key not in SAFE_LAUNCH_OPTIONS and key not in extra:
            bad.append(f"{key}=... (not allowed unless the policy opts in: Policy(launch_options=[{key!r}]))")
    if bad:
        raise PolicyViolation("launch refused: " + "; ".join(bad))


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


# ============================================================================ target identity


def _safe_get(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


#: Roles of single-control text inputs: typing (without Enter) into one only edits it.
TEXT_INPUT_ROLES = frozenset({"edit", "combobox", "spinbutton", "passwordtext", "entry", "searchbox", "textbox",
                              "document"})


def is_text_input(node: Any) -> bool:
    """Whether ``node`` is a text field (typed characters and spaces stay in it)."""
    if node is None:
        return False
    role = str(_safe_get(lambda: node.role, "") or "").lower()
    states = _safe_get(lambda: node.states, None) or ()
    return role in TEXT_INPUT_ROLES or "editable" in states


def _children_lazy(node: Any, budget: int) -> List[Any]:
    """At most ``budget`` children of ``node``, fetched lazily (a UIA grid can have thousands)."""
    if budget <= 0:
        return []
    try:
        return list(itertools.islice(iter(node.children()), budget))
    except Exception:
        return []


def _named_descendant_at(node: Any, point: Optional[Point], limit: int = 64, backend: Any = None) -> str:
    """The name of a descendant of ``node`` under ``point``, or ``""``.

    With ``backend``, one hit test (``backend.element_at``) finds the node under
    the point and its ancestors are walked up to ``node`` (innermost name wins),
    so a nameless container with thousands of children (a UIA grid or list)
    costs a handful of accessibility calls. Without one, descendants whose
    bounds contain the point are searched breadth first, fetching at most
    ``limit`` nodes in total (children are read lazily where the backend allows).
    """
    if node is None or point is None:
        return ""
    from .._nodeinfo import _label_of, ancestors  # noqa: PLC0415 - avoid an import cycle at module load

    if backend is not None and callable(getattr(backend, "element_at", None)):
        hit = _safe_get(lambda: backend.element_at(point.x, point.y))
        if hit is None:
            return ""
        below: List[Any] = []
        for cur in [hit] + ancestors(hit):
            if cur is node or _safe_get(lambda: cur.same_as(node), False):
                for n in below:
                    label = _label_of(n).strip()
                    if label:
                        return label
                return ""
            below.append(cur)
        return ""  # the hit is not inside ``node`` (something covers it)

    queue: Deque[Any] = deque(_children_lazy(node, limit))
    fetched = len(queue)
    while queue:
        cur = queue.popleft()
        b = _safe_get(lambda: cur.bounds)
        if b is None or not _safe_get(lambda: b.contains(point), False):
            continue
        label = _label_of(cur).strip()
        if label:
            return label
        more = _children_lazy(cur, limit - fetched)
        fetched += len(more)
        queue.extend(more)
    return ""


def target_identity(node: Any, point: Optional[Point] = None, backend: Any = None) -> str:
    """Text that identifies what activating ``node`` acts on, or ``""`` if it has no usable identity.

    In order: its policy label (its name or text value, or its enclosing
    actionable control's -- :func:`~locum._nodeinfo.target_label`), its
    automation id (``#id``), and the name of a descendant under ``point`` (the
    click point; found by hit-testing when ``backend`` is given). An icon-only button that reports none of these cannot be
    judged by any rule that reads labels, so the policy treats an activating
    input on it like one on an unknown target.
    """
    if node is None:
        return ""
    from .._nodeinfo import target_label  # noqa: PLC0415

    label = (_safe_get(lambda: target_label(node), "") or "").strip()
    if label:
        return label
    aid = str(_safe_get(lambda: node.automation_id, "") or "").strip()
    if aid:
        return f"#{aid}"
    return _named_descendant_at(node, point, backend=backend)


def keys_navigate_only(keys: Any) -> bool:
    """Whether every combination in ``keys`` (a keyboard tool's ``context["keys"]``) only navigates.

    Navigation = :data:`NAVIGATION_KEYS`, optionally with Shift/Ctrl. An empty or
    unparseable value is not navigation-only (conservative).
    """
    items = _key_items(keys) if keys else []
    if not items:
        return False
    from ..keys import MODIFIERS  # noqa: PLC0415

    for combo in items:
        mods = {k for k in combo if k in MODIFIERS}
        rest = combo - mods
        if not mods <= _NAVIGATION_MODIFIERS or not rest <= NAVIGATION_KEYS:
            return False
    return True


#: Editing shortcuts that act only on the focused text field (select all, copy, cut, paste, undo, redo).
_FIELD_EDIT_COMBOS = frozenset(frozenset({m, k}) for m in ("ctrl", "cmd") for k in "acvxyz")


def combo_acts_on_target(combo: frozenset, node: Any) -> bool:
    """Whether the key combination ``combo`` only acts on the control it is judged against.

    Allow-list mode authorizes such keys with a rule that matches the target
    (the focused control, or for Enter the default button it presses) without
    naming the keys: navigation (:data:`NAVIGATION_KEYS`, with Shift/Ctrl), a
    bare Enter or Space, and -- when ``node`` is a text field -- characters
    (with or without Shift), Backspace/Delete and the editing shortcuts
    Ctrl/Cmd+A/C/V/X/Y/Z. Anything else (Escape, F-keys, ``alt+f4``,
    ``alt+<access key>``, ``ctrl+s``, ``ctrl+enter``...) is an application
    shortcut that may act on another control or the whole window, so it needs
    a rule that lists it (``keys=...``).
    """
    from ..keys import MODIFIERS  # noqa: PLC0415

    combo = frozenset(combo)
    mods = {k for k in combo if k in MODIFIERS}
    rest = combo - mods
    if not rest:
        return False
    if mods <= _NAVIGATION_MODIFIERS and rest <= NAVIGATION_KEYS:
        return True
    if not mods and rest in (frozenset({"enter"}), frozenset({"space"})):
        return True
    if not is_text_input(node):
        return False
    if mods <= {"shift"} and len(rest) == 1 and len(next(iter(rest))) == 1:
        return True
    if not mods and rest <= {"backspace", "delete"}:
        return True
    return combo in _FIELD_EDIT_COMBOS


def _pressed_combos(ctx: Dict[str, Any]) -> Optional[List[frozenset]]:
    """The key combinations a keyboard action presses (``None``: not a keyboard action; ``[]``: unparseable)."""
    keys = ctx.get("step_keys") or ctx.get("keys")
    if not keys:
        return None
    items = (keys.split() or [keys]) if isinstance(keys, str) else [keys]
    pressed = _key_items(keys)
    return pressed if len(pressed) == len(items) else []


def pressed_control(hit: Any, container: Any = None) -> Any:
    """The control that a mouse press at the hit-tested node ``hit`` really activates.

    Walks up from ``hit`` to the nearest actionable node (button, link, menu
    item... -- see :func:`~locum._nodeinfo.target_label`). With
    ``container`` (the element that was asked for, whose centre is clicked),
    the walk stops there: a click on a decorative child (an icon or text run in
    a button) presses the container itself, while a click on an actionable
    child (a "Pay now" button in a toolbar) presses that child. Returns
    ``hit`` when nothing on the way is actionable (or ``container`` when
    ``hit`` is inside it).
    """
    if hit is None:
        return container
    from .._nodeinfo import _TOP_ROLES, _is_actionable, ancestors  # noqa: PLC0415

    def same(a: Any, b: Any) -> bool:
        return a is b or bool(_safe_get(lambda: a.same_as(b), False))

    for cur in [hit] + ancestors(hit):
        if container is not None and same(cur, container):
            return container
        role = str(_safe_get(lambda: cur.role, "") or "").lower()
        if container is None and role in _TOP_ROLES:
            return hit
        if _is_actionable(cur):
            return cur
    return container if container is not None else hit


# ============================================================================ allow rules


@functools.lru_cache(maxsize=256)
def _compiled_selector(text: str) -> Any:
    from ..selectors import parse  # noqa: PLC0415

    return parse(text)


def node_matches_selector(node: Any, selector: str) -> bool:
    """Whether ``node`` is one of the elements ``selector`` finds (ancestry steps included).

    The selector is evaluated from the node's top-most ancestor, so
    ``window[name^="Invoice"] button "Save"`` only matches a Save button inside an
    Invoice window, exactly as :meth:`~locum.desktop.Desktop.find` would.
    """
    if node is None:
        return False
    try:
        sel = _compiled_selector(selector)
    except Exception:
        return False
    try:
        if not sel.matches(node):  # cheap reject on the last compound
            return False
    except Exception:
        return False
    from .._nodeinfo import ancestors  # noqa: PLC0415
    from ..selectors import _same  # noqa: PLC0415

    chain = [node] + ancestors(node)
    root = chain[-1]
    try:
        found = sel.find_all(root)
    except Exception:
        return False
    return any(_same(n, node) for n in found)


def _glob_ci(text: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(str(text).lower(), str(pattern).lower())


@dataclass(frozen=True)
class AllowRule:
    """One explicit authorization for :class:`Policy` allow-list mode (``Policy(allow=[...])``).

    Every field that is set must match (they are AND-ed); a rule list matches
    when any rule does. Fields:

    * ``selector`` -- the action's target element must be one the selector
      finds (``'window[name^="Invoice"] button "Save"'``, ``"edit#amount"``).
    * ``window`` -- glob (case-insensitive) on the title of the window the
      action lands in (the target element's window, the window under the
      pointer, or for keyboard input the focused control's / active window).
    * ``app`` -- application glob as in ``allowed_apps`` (``"excel"``,
      ``"notepad*"``); every application the action touches must match.
    * ``command`` -- glob on the command line of ``launch_app`` (or the program
      it runs: ``"notepad"`` matches ``"notepad.exe C:\\\\a.txt"``).
    * ``keys`` -- key combinations a keyboard action may press
      (``("ctrl+s",)``); every combination pressed must be listed. A rule
      without ``keys`` authorizes only keys that act on the control it
      matched (see :func:`combo_acts_on_target`: navigation, a bare
      Enter/Space, and typing/editing keys in a text field); an application
      shortcut such as Escape, F8, ``alt+f4`` or ``alt+<access key>`` needs
      a rule that lists it, e.g. ``{"selector": "edit#amount", "keys": "f8"}``.
    * ``tools`` -- tool-name globs the rule covers (``("write_clipboard",)``,
      ``("erp_*",)`` for integration tools). Without ``tools`` a rule covers
      desktop actions that have a target; ``launch_app``, ``write_clipboard``
      and integration tools are only covered when named here (``command``
      implies ``launch_app``).

    String shorthands (handy in YAML): ``"window=Invoice*"``, ``"app=excel"``,
    ``"launch=notepad.exe"``, ``"keys=ctrl+s ctrl+p"``, ``"tool=write_clipboard"``,
    ``"selector=<sel>"``; any other string is a selector. Dicts take the field
    names (``{"selector": 'button "Save"', "window": "Invoice*"}``).
    """

    selector: Optional[str] = None
    window: Optional[str] = None
    app: Optional[str] = None
    command: Optional[str] = None
    keys: Tuple[str, ...] = ()
    tools: Optional[Tuple[str, ...]] = None

    _PREFIXES = ("window=", "app=", "launch=", "keys=", "tool=", "tools=", "selector=")

    def __post_init__(self) -> None:
        if isinstance(self.keys, str):
            object.__setattr__(self, "keys", tuple(self.keys.split()))
        else:
            object.__setattr__(self, "keys", tuple(self.keys or ()))
        if isinstance(self.tools, str):
            object.__setattr__(self, "tools", tuple(t.strip() for t in self.tools.split(",") if t.strip()))
        elif self.tools is not None:
            object.__setattr__(self, "tools", tuple(self.tools))
        if not any((self.selector, self.window, self.app, self.command, self.keys, self.tools)):
            raise ValueError("An AllowRule needs at least one of selector, window, app, command, keys or tools")
        if self.selector is not None:
            _compiled_selector(self.selector)  # a typo fails at configuration time, not mid-run
        object.__setattr__(self, "_combos", frozenset(c for k in self.keys for c in _combo_sets(k)))

    @classmethod
    def coerce(cls, rule: Any) -> "AllowRule":
        """An :class:`AllowRule` from a rule, a shorthand string or a dict."""
        if isinstance(rule, AllowRule):
            return rule
        if isinstance(rule, dict):
            known = {"selector", "window", "app", "command", "keys", "tools"}
            data = dict(rule)
            if "launch" in data and "command" not in data:
                data["command"] = data.pop("launch")
            if "tool" in data and "tools" not in data:
                data["tools"] = data.pop("tool")
            unknown = set(data) - known
            if unknown:
                raise ValueError(f"Unknown allow-rule field(s) {', '.join(sorted(unknown))}; expected {', '.join(sorted(known))}")
            return cls(**data)
        if isinstance(rule, str):
            text = rule.strip()
            for prefix in cls._PREFIXES:
                if text.lower().startswith(prefix):
                    value = text[len(prefix):].strip()
                    field = {"launch=": "command", "tool=": "tools", "tools=": "tools"}.get(prefix, prefix[:-1])
                    return cls(**{field: value})
            return cls(selector=text)
        raise TypeError(f"An allow rule must be a selector string, a dict or an AllowRule, got {type(rule).__name__}")

    def covers_tool(self, tool_name: str) -> bool:
        """Whether this rule names ``tool_name`` (explicitly, via ``tools`` or ``command``)."""
        if self.tools is not None and any(fnmatch.fnmatchcase(tool_name, p) for p in self.tools):
            return True
        return tool_name == "launch_app" and self.command is not None

    def matches(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
        """Whether this rule authorizes ``tool_name`` with ``args`` / ``ctx`` (see :meth:`Policy.check`)."""
        if self.tools is not None:
            if not any(fnmatch.fnmatchcase(tool_name, p) for p in self.tools):
                return False
        elif tool_name in _UNTARGETED_TOOLS or ctx.get("integration_tool"):
            if not (tool_name == "launch_app" and self.command is not None):
                return False
        if self.command is not None:
            if tool_name != "launch_app":
                return False
            command = ctx.get("command") or args.get("command") or ""
            text = " ".join(command) if isinstance(command, (list, tuple)) else str(command)
            program = launch_program(command)
            if not (_glob_ci(" ".join(text.split()), self.command) or (program and _app_matches(program, [self.command]))):
                return False
        pressed = _pressed_combos(ctx)
        if self.keys:
            if not pressed or any(c not in self._combos for c in pressed):  # type: ignore[attr-defined]
                return False
        elif pressed is not None:
            # A rule that does not list keys authorizes only keys that act on the control it
            # matched (Enter/Space on it, editing a field it names, navigation); application
            # shortcuts (alt+f4, F8, Escape, alt+<access key>) need a ``keys=`` rule.
            node = ctx.get("target_node")
            if not pressed or not all(combo_acts_on_target(c, node) for c in pressed):
                return False
        has_points = bool(Policy._points(args, ctx))
        if self.window is not None:
            title = ctx.get("target_window")
            if not title and not has_points:
                title = ctx.get("window_title")  # keyboard input goes to the active window
            if not title or not _glob_ci(title, self.window):
                return False
        if self.app is not None:
            apps = [str(a) for a in (ctx.get("target_apps") or ()) if a]
            if ctx.get("target_app") and str(ctx["target_app"]) not in apps:
                apps.insert(0, str(ctx["target_app"]))
            if not apps and not has_points and ctx.get("app"):
                apps = [str(ctx["app"])]
            if not apps or not all(_app_matches(a, [self.app]) for a in apps):
                return False
        if self.selector is not None:
            if not node_matches_selector(ctx.get("target_node"), self.selector):
                return False
        return True

    def __str__(self) -> str:
        parts = [f"{k}={v!r}" for k, v in (("selector", self.selector), ("window", self.window), ("app", self.app),
                                            ("command", self.command), ("keys", " ".join(self.keys) or None),
                                            ("tools", ",".join(self.tools) if self.tools else None)) if v]
        return "AllowRule(" + ", ".join(parts) + ")"


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
            Citrix...), nothing is found under the pointer, or the control
            found has no usable identity (no name, automation id, text value
            or named descendant at the click point: an icon-only button), so
            confirmation patterns cannot be matched: ``"confirm"`` (default)
            asks the human like a pattern match, ``"block"`` refuses,
            ``"allow"`` lets it run. Ignored when there are no
            ``confirm_patterns`` and no ``allow`` list (:meth:`permissive`).
            Tools set ``context["target_unknown"]``.
        allow: Switch to **allow-list (default-deny) mode**: every
            consequential action (see the module docstring) needs ``confirm``
            unless one of these :class:`AllowRule` s (rules, selector strings,
            ``"window=..."``-style shorthands or dicts) matches it. ``None``
            (default) keeps the heuristic mode; ``[]`` confirms everything
            consequential. ``confirm_patterns``/``confirm_keys`` still apply on
            top as tripwires (pass ``confirm_patterns=()`` to make the allow
            rules final).
        dry_run: Tools describe what they would do instead of acting.
        max_actions: Maximum number of (non read-only) actions in total.
        max_actions_per_minute: Sliding-window rate limit for actions.
        region: Pointer actions must land inside this screen rectangle.
        read_only_tools: Names treated as read-only (default :data:`READ_ONLY_TOOLS`).
        launch_options: Extra ``subprocess.Popen`` options that guarded launches
            (``launch_app``, ``GuardedDesktop.launch``) may pass, beyond
            :data:`SAFE_LAUNCH_OPTIONS` -- e.g. ``["cwd", "env"]``. ``shell``,
            ``executable`` and ``preexec_fn`` are never allowed (see
            :func:`check_launch_options`).
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
        allow: Optional[Iterable[Any]] = None,
        launch_options: Iterable[str] = (),
    ):
        launch_options = frozenset(launch_options)
        forbidden = launch_options & FORBIDDEN_LAUNCH_OPTIONS
        if forbidden:
            raise ValueError(f"launch_options may not include {', '.join(sorted(forbidden))}: "
                             "they can run a program the policy did not check")
        #: Extra ``launch`` options (beyond :data:`SAFE_LAUNCH_OPTIONS`) guarded launches accept.
        self.launch_options = launch_options
        if isinstance(allow, (str, dict, AllowRule)):
            allow = [allow]
        #: Allow rules (allow-list mode), or ``None`` in the default heuristic mode.
        self.allow: Optional[Tuple[AllowRule, ...]] = (
            None if allow is None else tuple(AllowRule.coerce(r) for r in allow))
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

        ``Policy.strict(allow=[...])`` adds allow-list authorization (see
        ``allow``): only actions matching your rules run without a human. When
        a rule names ``launch_app`` (``"launch=notepad.exe"``) or
        ``write_clipboard`` (``"tool=write_clipboard"``), that tool is no longer
        in the default ``deny_tools`` (an explicit ``deny_tools`` always wins).
        """
        if "deny_tools" not in kw:
            deny = ["launch_app", "write_clipboard", "read_clipboard"]
            allow = kw.get("allow")
            if allow is not None:
                if isinstance(allow, (str, dict, AllowRule)):
                    allow = [allow]
                rules = [AllowRule.coerce(r) for r in allow]
                kw["allow"] = rules
                for tool in ("launch_app", "write_clipboard"):
                    if any(r.covers_tool(tool) for r in rules):
                        deny.remove(tool)
            kw["deny_tools"] = tuple(deny)
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

    @property
    def allow_list_mode(self) -> bool:
        """Whether the policy authorizes by explicit allow rules (``allow`` given) rather than by heuristics."""
        return self.allow is not None

    def is_consequential(self, tool_name: str, context: Optional[Dict[str, Any]] = None) -> bool:
        """Whether ``tool_name`` (with ``context``) can commit something, i.e. is gated in allow-list mode.

        Read-only tools and :data:`NON_COMMITTING_TOOLS` are not; neither is a
        key press made only of :data:`NAVIGATION_KEYS`. Everything else is.
        """
        if self.is_read_only(tool_name) or tool_name in NON_COMMITTING_TOOLS:
            return False
        ctx = context or {}
        keys = ctx.get("step_keys") or ctx.get("keys")
        if keys and keys_navigate_only(keys):
            return False
        return True

    def allow_rule_for(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Optional[AllowRule]:
        """The first :class:`AllowRule` that authorizes this action, or ``None``."""
        for rule in self.allow or ():
            try:
                if rule.matches(tool_name, args, context):
                    return rule
            except Exception:  # a rule that cannot be evaluated authorizes nothing
                continue
        return None

    def authorization_reason(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Optional[str]:
        """Allow-list mode: why this action needs confirmation (no rule authorizes it), or ``None``."""
        if self.allow is None or not self.is_consequential(tool_name, context):
            return None
        rule = self.allow_rule_for(tool_name, args, context)
        if rule is not None:
            context["allowed_by"] = str(rule)
            return None
        what = context.get("target") or context.get("command") or context.get("target_window")
        if context.get("target_unknown") and not context.get("target"):
            what = "a control that cannot be identified"
        return f"no allow rule authorizes {tool_name!r}" + (f" on {str(what)[:120]!r}" if what else "")

    def confirmation_reason(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Optional[str]:
        """Why this action needs confirmation (a tripwire match or, in allow-list mode, no allow rule), or ``None``."""
        if self.is_read_only(tool_name):
            return None
        reason = self._tripwire_reason(tool_name, args, context)
        if reason is not None:
            return reason
        return self.authorization_reason(tool_name, args, context)

    def _tripwire_reason(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Optional[str]:
        """The heuristic part: confirm keys, ``require_confirmation``, confirm patterns, unknown targets."""
        if self._confirm_combos and context.get("keys"):
            for combo in _key_items(context["keys"]):
                if any(combo == c for c in self._confirm_combos):
                    return f"shortcut {'+'.join(sorted(combo))!r} commits the focused form"
        if self.allow is not None and context.get("require_confirmation"):
            return str(context["require_confirmation"]) if isinstance(context["require_confirmation"], str) \
                else f"{tool_name!r} always requires confirmation"
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
        # In allow-list mode an unidentifiable target is judged by the allow rules instead
        # (no selector rule can match it; a window/app rule the user wrote may).
        if context.get("target_unknown") and self.unknown_target == "confirm" and self.allow is None:
            return UNKNOWN_TARGET_REASON
        return None

    # -------------------------------------------------------------------- check
    def check(self, tool_name: str, args: Optional[Dict[str, Any]] = None, *, context: Optional[Dict[str, Any]] = None,
              count: bool = True, confirm: Optional[ConfirmCallback] = None) -> None:
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
        For allow rules: ``target_node`` (the accessibility node acted on,
        matched against rule selectors), ``target_window`` (title of the window
        the action lands in), ``keys`` / ``step_keys`` (key combinations
        pressed), ``integration_tool`` (a non-desktop tool). When a rule
        authorizes the action, ``context["allowed_by"]`` names it.
        In dry-run mode confirmations are skipped (nothing will happen) and
        ``context["needs_confirmation"]`` is set instead.

        ``confirm`` overrides the policy's own ``confirm`` callback for this
        call (used by :class:`GuardedDesktop`).

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
        if ctx.get("target_unknown") and self.unknown_target == "block" and (self.confirm_patterns or self.allow is not None):
            raise PolicyViolation(
                f"Action blocked: {UNKNOWN_TARGET_REASON}, so the confirmation rules cannot be applied "
                "(Policy(unknown_target='block'))"
            )

        # Rate limits.
        # The slot is reserved atomically with the limit test (and given back below if the
        # action is not allowed after all), so concurrent checks cannot overrun the budget.
        stamp: Optional[float] = None
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
                self.action_count += 1
                if self.max_actions_per_minute is not None:
                    stamp = now
                    self._recent.append(now)
        try:
            self._confirm_step(tool_name, args, ctx, confirm)
        except BaseException:
            if count:
                with self._lock:
                    self.action_count = max(0, self.action_count - 1)
                    if stamp is not None:
                        try:
                            self._recent.remove(stamp)
                        except ValueError:
                            pass
            raise

    def _confirm_step(self, tool_name: str, args: Dict[str, Any], ctx: Dict[str, Any], confirm: Any) -> None:
        """The human-confirmation part of :meth:`check` (raises if declined)."""
        # Human confirmation.
        reason = self.confirmation_reason(tool_name, args, ctx)
        if reason is not None:
            if self.dry_run:
                ctx["needs_confirmation"] = reason
            else:
                description = ctx.get("description") or f"{tool_name} {json.dumps(args, default=str)[:200]}"
                confirm_cb = confirm if confirm is not None else self.confirm
                if confirm_cb is None:
                    raise PolicyViolation(
                        f"Action {description!r} needs human confirmation ({reason}) but no confirm callback is configured"
                    )
                details = {"tool": tool_name, "args": args, "reason": reason}
                details.update({k: v for k, v in ctx.items() if k not in ("desktop",)})
                try:
                    approved = bool(confirm_cb(description, details))
                except Exception as exc:
                    raise PolicyViolation(f"Confirmation failed for {description!r}: {exc}") from exc
                self.confirmations.append((description, approved))
                if not approved:
                    raise PolicyViolation(f"Action {description!r} was declined by the user")

    def reset(self) -> None:
        """Reset action counters (e.g. between agent runs)."""
        with self._lock:
            self.action_count = 0
            self._recent.clear()

    def __repr__(self) -> str:
        mode = "heuristic" if self.allow is None else f"allow-list({len(self.allow)} rules)"
        return (
            f"<Policy mode={mode} dry_run={self.dry_run} allow_tools={self.allow_tools} deny={self.deny_tools} "
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
        answer = input(f"[locum] Allow: {description}? (reason: {details.get('reason')}) [y/N] ")
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


# ============================================================================ guarded desktop (RPA)


def _unwrap(value: Any) -> Any:
    """The real object behind a guarded proxy (lists/tuples of them too; named tuples untouched)."""
    if isinstance(value, _GuardedProxy):
        return object.__getattribute__(value, "_g_target")
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    if isinstance(value, tuple) and not hasattr(value, "_fields"):
        return tuple(_unwrap(v) for v in value)
    return value


def _click_tool(button: str, clicks: int) -> str:
    button = str(getattr(button, "value", button) or "left").lower()
    if button == "right" and int(clicks) == 1:
        return "right_click"
    if button == "left" and int(clicks) == 2:
        return "double_click"
    return "click"


class _GuardedProxy:
    """Forwards everything to the wrapped object, wrapping returned elements/windows in guards.

    Arguments are unwrapped before they reach the real object, so a guarded
    element can be passed anywhere an :class:`~locum.element.Element` is expected.
    """

    def __init__(self, target: Any, guard: "GuardedDesktop") -> None:
        object.__setattr__(self, "_g_target", target)
        object.__setattr__(self, "_g_guard", guard)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_g_target")
        guard = object.__getattribute__(self, "_g_guard")
        value = getattr(target, name)
        if callable(value) and not isinstance(value, type):
            @functools.wraps(value)
            def forward(*args: Any, **kwargs: Any) -> Any:
                return guard._wrap(value(*_unwrap(args), **{k: _unwrap(v) for k, v in kwargs.items()}))

            return forward
        return guard._wrap(value)

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(type(self), name):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_g_target"), name, _unwrap(value))

    def __eq__(self, other: object) -> bool:
        return bool(object.__getattribute__(self, "_g_target") == _unwrap(other))

    def __hash__(self) -> int:
        return hash(object.__getattribute__(self, "_g_target"))

    def __repr__(self) -> str:
        return f"<guarded {object.__getattribute__(self, '_g_target')!r}>"

    @property
    def unwrapped(self) -> Any:
        """The real (ungated) object."""
        return object.__getattribute__(self, "_g_target")


class GuardedDesktop(_GuardedProxy):
    """A :class:`~locum.desktop.Desktop` facade that checks a :class:`Policy` before every action.

    Used by :class:`~locum.rpa.Robot` (``Robot(policy=...)``) and flows
    (``run_flow(..., policy=...)``); usable on its own::

        d = GuardedDesktop(Desktop(), Policy.strict(allow=['window[name^="Invoice"] button "Save"']),
                           confirm=console_confirm, audit=AuditLog("audit.jsonl"))
        d.find('button "Save"').click()      # allowed by the rule
        d.click('button "Post"')             # needs a human; PolicyViolation if declined

    Intercepted (judged with the same context the LLM toolkit builds -- target
    label, element, window, application, point, keys): ``click`` and its
    variants, ``mouse_down``/``mouse_up``, ``move``/``move_rel``, ``drag``,
    ``scroll``, ``type``, ``press``, ``hotkey``, ``key_down``, ``hold``,
    ``click_image``, ``click_text``, ``launch``, ``clipboard_set`` /
    ``clipboard_get``, the ``mouse`` and ``keyboard`` objects, and every
    element/window reached through ``find()``, ``find_all()``, ``wait()``,
    ``window()``, ``windows()``, ``focused()``... (their ``click``,
    ``double_click``, ``right_click``, ``hover``, ``invoke``, ``toggle``,
    ``check``, ``uncheck``, ``select``, ``expand``, ``collapse``, ``focus``,
    ``set_text``, ``clear``, ``type``, ``press``, ``scroll``; windows'
    ``activate`` and ``close``). Read-only calls pass straight through.
    Violations raise :class:`~locum.errors.PolicyViolation` before anything
    is sent; in ``dry_run`` mode actions are skipped (and audited) instead.

    The guard is a seat belt for data-driven automation (flow variables, CSV
    rows, recorded selectors), not a sandbox: code that reaches
    ``desktop.backend`` or keeps the unwrapped desktop (:attr:`unwrapped`) is
    not checked.

    Args:
        desktop: The desktop to drive.
        policy: The :class:`Policy` to enforce.
        confirm: Confirmation callback for this guard (default: the policy's ``confirm``).
        audit: :class:`AuditLog` receiving one ``rpa_action`` event per checked
            action (default: a new in-memory log).
    """

    def __init__(self, desktop: Any, policy: Policy, *, confirm: Optional[ConfirmCallback] = None,
                 audit: Optional[AuditLog] = None) -> None:
        if isinstance(desktop, GuardedDesktop):
            desktop = desktop.unwrapped
        if not isinstance(policy, Policy):
            raise TypeError(f"policy must be a Policy, got {type(policy).__name__}")
        super().__init__(desktop, self)
        object.__setattr__(self, "_g_policy", policy)
        object.__setattr__(self, "_g_confirm", confirm)
        object.__setattr__(self, "_g_audit", audit if audit is not None else AuditLog())

    # ------------------------------------------------------------------ plumbing
    @property
    def policy(self) -> Policy:
        return object.__getattribute__(self, "_g_policy")

    @property
    def audit(self) -> AuditLog:
        return object.__getattribute__(self, "_g_audit")

    @property
    def confirm(self) -> Optional[ConfirmCallback]:
        return object.__getattribute__(self, "_g_confirm")

    @property
    def _raw(self) -> Any:
        return object.__getattribute__(self, "_g_target")

    def _wrap(self, value: Any) -> Any:
        from ..element import Element  # noqa: PLC0415
        from ..window import Window  # noqa: PLC0415

        if isinstance(value, _GuardedProxy):
            return value
        if isinstance(value, Element):
            return GuardedElement(value, self)
        if isinstance(value, Window):
            return GuardedWindow(value, self)
        if isinstance(value, list) and any(isinstance(v, (Element, Window)) for v in value):
            return [self._wrap(v) for v in value]
        return value

    def _record(self, tool: str, args: Dict[str, Any], status: str, description: str, ctx: Dict[str, Any],
                error: Optional[str] = None) -> None:
        event: Dict[str, Any] = {"event": "rpa_action", "tool": tool, "args": args, "status": status,
                                 "description": description}
        for key in ("allowed_by", "needs_confirmation"):
            if ctx.get(key):
                event[key] = ctx[key]
        if error is not None:
            event["error"] = error
        try:
            self.audit.record(event)
        except Exception:  # noqa: BLE001 - an audit failure must not turn into an action failure
            pass

    def _gate(self, tool: str, args: Dict[str, Any], ctx: Dict[str, Any], description: str, *,
              count: bool = True, secret: bool = False) -> bool:
        """Check the policy; ``True`` when the action may run (``False``: dry-run). Raises PolicyViolation."""
        ctx.setdefault("description", description)
        clean = {k: v for k, v in args.items() if v is not None}
        logged = {k: ("***" if secret and isinstance(v, str) else v) for k, v in clean.items()}
        policy = self.policy
        try:
            policy.check(tool, clean, context=ctx, count=count, confirm=self.confirm)
        except PolicyViolation as exc:
            msg = str(exc) if count else f"{exc} (the input before it in this call was already sent)"
            self._record(tool, logged, "blocked", description, ctx, error=msg)
            if count:
                raise
            raise PolicyViolation(msg) from None
        if policy.dry_run and not policy.is_read_only(tool):
            self._record(tool, logged, "dry_run", description, ctx)
            return False
        self._record(tool, logged, "allowed", description, ctx)
        return True

    # ------------------------------------------------------------------ context
    def _base_ctx(self) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {}
        aw = _safe_get(lambda: self._raw.backend.active_window())
        if aw is not None:
            ctx["app"] = aw.app
            ctx["window_title"] = aw.title
        return ctx

    def _point_ctx(self, point: Point, *, activates: bool) -> Dict[str, Any]:
        from .._nodeinfo import node_app, target_label, window_at  # noqa: PLC0415

        backend = self._raw.backend
        ctx = self._base_ctx()
        ctx["point"] = point
        node = _safe_get(lambda: backend.element_at(point.x, point.y))
        apps: List[str] = []
        if node is not None:
            ctx["target_node"] = node
            label = _safe_get(lambda: target_label(node), "") or ""
            if label:
                ctx["target"] = label
            app = _safe_get(lambda: node_app(backend, node), "") or ""
            if app:
                apps.append(app)
        w = window_at(backend, point)
        if w is not None:
            if w.app and w.app not in apps:
                apps.append(w.app)
            if w.title:
                ctx["target_window"] = w.title
        if apps:
            ctx["target_app"] = apps[0]
            ctx["target_apps"] = apps
        if activates and (node is None or not target_identity(node, point, backend)):
            ctx["target_unknown"] = True
        return ctx

    def _element_ctx(self, el: Any, *, activates: bool, offset: Any = (0, 0)) -> Dict[str, Any]:
        from .._nodeinfo import ancestors, node_app, owning_window, target_label  # noqa: PLC0415

        backend = self._raw.backend
        node = el.node
        ctx = self._base_ctx()
        ctx["target_node"] = node
        label = _safe_get(lambda: target_label(node), "") or ""
        selector = getattr(el, "selector", None)
        if isinstance(selector, str):
            ctx["selector"] = selector
        app = _safe_get(lambda: node_app(backend, node), "") or ""
        if app:
            ctx["target_app"] = app
        w = _safe_get(lambda: owning_window(backend, node))
        if w is not None and w.title:
            ctx["target_window"] = w.title
        # Judge the point the mouse really presses: the centre, or centre+offset.
        off = (int(offset[0]), int(offset[1])) if offset is not None else (0, 0)
        point = _safe_get(lambda: el._click_point(off, require=False))
        if point is not None:
            ctx["point"] = point
        identified = bool(target_identity(node, point, backend))
        if activates and point is not None:
            # The mouse lands at the centre (or offset point), which may be a child control
            # ("Pay now" in a toolbar).
            hit = _safe_get(lambda: backend.element_at(point.x, point.y))
            if hit is not None and not _safe_get(lambda: hit.same_as(node), False) and \
                    any(_safe_get(lambda: a.same_as(node), False) for a in ancestors(hit)):
                pressed = pressed_control(hit, node)
                if pressed is not node:
                    # An actionable child receives the press: allow rules and the identity
                    # rule judge that control, not the container that was asked for.
                    ctx["target_node"] = pressed
                    identified = bool(target_identity(pressed, point, backend))
                child = _safe_get(lambda: target_label(hit), "") or ""
                if child and child not in label:
                    label = f"{label} > {child}" if label else child
                    if pressed is node:
                        identified = True
        if label:
            ctx["target"] = label
        if activates and not identified:
            ctx["target_unknown"] = True
        return ctx

    def _focus_ctx(self, *, activating: bool, enter: bool = False, **extra: Any) -> Tuple[Dict[str, Any], bool]:
        """Context for keyboard input (the focused control is the target) and whether it is a password field."""
        from .._nodeinfo import is_password, node_app, owning_window, target_label  # noqa: PLC0415

        backend = self._raw.backend
        ctx = self._base_ctx()
        node = _safe_get(lambda: backend.focused_element())
        secret = False
        if node is None:
            if activating:
                ctx["target_unknown"] = True
        else:
            secret = is_password(node)
            ctx["target_node"] = node
            app = _safe_get(lambda: node_app(backend, node), "") or ""
            if app:
                ctx["target_app"] = app
            w = _safe_get(lambda: owning_window(backend, node))
            if w is not None and w.title:
                ctx["target_window"] = w.title
            name = _safe_get(lambda: target_label(node), "") or ""
            identified = bool(target_identity(node))
            if enter:
                from .tools import _default_button  # noqa: PLC0415

                button = _default_button(node)
                label = _safe_get(lambda: target_label(button), "") if button is not None else ""
                if label:
                    name = f"{name} -> {label}" if name else label
                    identified = True
                    ctx["target_node"] = button  # allow rules judge the button Enter activates
            if activating and name:
                ctx["target"] = name
            if activating and not identified and not (is_text_input(node) and not enter):
                ctx["target_unknown"] = True
        ctx.update({k: v for k, v in extra.items() if v is not None})
        return ctx, secret

    def _resolve(self, target: Any, y: Optional[float] = None, offset: Any = (0, 0)) -> Tuple[Any, Optional[Point]]:
        """``(element, None)`` for element/selector targets, else ``(None, screen point)``."""
        raw = self._raw
        t = _unwrap(target)
        if y is None and t is not None:
            el = raw._resolve_element(t)
            if el is not None:
                return el, None
        dx, dy = (int(offset[0]), int(offset[1])) if offset is not None else (0, 0)
        return None, raw.resolve_point(t, y).offset(dx, dy)

    # ------------------------------------------------------------------ mouse
    def click(self, target: Any = None, y: Optional[float] = None, *, button: str = "left", clicks: int = 1,
              **kw: Any) -> Any:
        """:meth:`Desktop.click <locum.desktop.Desktop.click>` behind the policy."""
        kw = {k: _unwrap(v) for k, v in kw.items()}
        tool = _click_tool(button, clicks)
        offset = kw.get("offset", (0, 0)) or (0, 0)
        current = target is None and y is None and tuple(offset) == (0, 0)
        el, point = self._resolve(target, y, offset)
        btn = str(getattr(button, "value", button)).lower()
        if el is not None:
            ctx = self._element_ctx(el, activates=btn == "left", offset=offset)
            what = ctx.get("target") or getattr(el, "selector", None) or el.role
        else:
            assert point is not None
            ctx = self._point_ctx(point, activates=btn == "left")
            what = f"({point.x}, {point.y})" + (f" on {ctx['target']!r}" if ctx.get("target") else "")
        verb = {1: "click", 2: "double-click", 3: "triple-click"}.get(int(clicks), f"{clicks}x click")
        if btn != "left":
            verb = f"{btn}-{verb}"
        args = {"target": None if el is None else getattr(el, "selector", None), "x": None if point is None else point.x,
                "y": None if point is None else point.y, "button": btn, "clicks": int(clicks)}
        if not self._gate(tool, args, ctx, f"{verb} {what}"):
            return point
        if el is not None:
            return self._raw.click(el, button=button, clicks=clicks, **kw)
        kw.pop("offset", None)
        return self._raw.click(None if current else point, button=button, clicks=clicks, **kw)

    def double_click(self, target: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        kw["clicks"] = 2
        return self.click(target, y, **kw)

    def triple_click(self, target: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        kw["clicks"] = 3
        return self.click(target, y, **kw)

    def right_click(self, target: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        kw["button"] = "right"
        return self.click(target, y, **kw)

    def middle_click(self, target: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        kw["button"] = "middle"
        return self.click(target, y, **kw)

    def mouse_down(self, button: str = "left") -> None:
        point = self._raw.position()
        ctx = self._point_ctx(point, activates=str(button).lower() == "left")
        if self._gate("mouse_down", {"button": str(button)}, ctx, f"press the {button} mouse button"):
            self._raw.mouse_down(button)

    def mouse_up(self, button: str = "left") -> None:
        point = self._raw.position()
        ctx = self._point_ctx(point, activates=False)
        if self._gate("mouse_up", {"button": str(button)}, ctx, f"release the {button} mouse button"):
            self._raw.mouse_up(button)

    def move(self, target: Any, y: Optional[float] = None, **kw: Any) -> Any:
        kw = {k: _unwrap(v) for k, v in kw.items()}
        el, point = self._resolve(target, y, kw.get("offset", (0, 0)))
        ctx = self._element_ctx(el, activates=False, offset=kw.get("offset", (0, 0)) or (0, 0)) if el is not None             else self._point_ctx(point, activates=False)  # type: ignore[arg-type]
        if not self._gate("move_mouse", {}, ctx, f"move the mouse to {ctx.get('target') or point}"):
            return point
        if el is not None:
            return self._raw.move(el, **kw)
        kw.pop("offset", None)
        return self._raw.move(point, **kw)

    def move_rel(self, dx: float, dy: float, **kw: Any) -> Any:
        cur = self._raw.position()
        return self.move(Point(int(round(cur.x + dx)), int(round(cur.y + dy))), **kw)

    def drag(self, start: Any, end: Any, **kw: Any) -> Any:
        kw = {k: _unwrap(v) for k, v in kw.items()}
        s_el, s_pt = (None, self._raw.position()) if start is None else self._resolve(start)
        e_el, e_pt = self._resolve(end)
        s_ctx = self._element_ctx(s_el, activates=True) if s_el is not None else self._point_ctx(s_pt, activates=True)  # type: ignore[arg-type]
        e_ctx = self._element_ctx(e_el, activates=False) if e_el is not None else self._point_ctx(e_pt, activates=False)  # type: ignore[arg-type]
        ctx = dict(s_ctx)
        apps: List[str] = []
        for c in (s_ctx, e_ctx):
            for a in [c.get("target_app"), *(c.get("target_apps") or ())]:
                if a and a not in apps:
                    apps.append(a)
        if apps:
            ctx["target_apps"] = apps
        pts = [p for p in (s_ctx.get("point"), e_ctx.get("point")) if p is not None]
        ctx["points"] = pts
        if e_ctx.get("target"):
            ctx["target"] = f"{s_ctx.get('target', '')} -> {e_ctx['target']}".strip()
        if e_ctx.get("target_window") and e_ctx.get("target_window") != s_ctx.get("target_window"):
            ctx.pop("target_window", None)  # spans two windows: a window rule must not cover it
        if not self._gate("drag", {}, ctx, f"drag from {s_ctx.get('target') or s_pt} to {e_ctx.get('target') or e_pt}"):
            return e_pt
        return self._raw.drag(None if start is None else (s_el if s_el is not None else s_pt),
                              e_el if e_el is not None else e_pt, **kw)

    def scroll(self, dy: int = 0, dx: int = 0, *, at: Any = None, **kw: Any) -> None:
        if at is None:
            el, point = None, self._raw.position()
        else:
            el, point = self._resolve(at)
        ctx = self._element_ctx(el, activates=False) if el is not None else self._point_ctx(point, activates=False)  # type: ignore[arg-type]
        if self._gate("scroll", {"dy": dy, "dx": dx}, ctx, f"scroll dy={dy} dx={dx}"):
            self._raw.scroll(dy, dx, at=None if at is None else (el if el is not None else point), **kw)

    def click_image(self, image: Any, *, timeout: Optional[float] = None, button: str = "left", clicks: int = 1,
                    offset: Tuple[int, int] = (0, 0), **kw: Any) -> Any:
        m = self._raw.wait_image(_unwrap(image), timeout=timeout, **{k: _unwrap(v) for k, v in kw.items()})
        point = m.center.offset(int(offset[0]), int(offset[1]))
        ctx = self._point_ctx(point, activates=str(button).lower() == "left")
        if self._gate(_click_tool(button, clicks), {"image": str(image)[:200], "button": str(button), "clicks": clicks},
                      ctx, f"click image at ({point.x}, {point.y})"):
            self._raw.mouse.click(point, button=button, clicks=clicks)
        return m

    def click_text(self, text: str, *, timeout: Optional[float] = None, button: str = "left", clicks: int = 1,
                   offset: Tuple[int, int] = (0, 0), **kw: Any) -> Any:
        from ..errors import ImageNotFound  # noqa: PLC0415

        raw = self._raw
        t = raw._timeout(timeout)
        m = raw.find_text(text, timeout=t, **{k: _unwrap(v) for k, v in kw.items()})
        if m is None:
            raise ImageNotFound(f"Text {text!r} not found on screen within {t:g}s")
        point = m.center.offset(int(offset[0]), int(offset[1]))
        ctx = self._point_ctx(point, activates=str(button).lower() == "left")
        # The OCR text is what the human sees: judge the click by it too.
        ctx["target"] = f"{text} < {ctx['target']}" if ctx.get("target") and ctx["target"] != text else text
        if ctx.get("target_node") is not None:
            ctx.pop("target_unknown", None)
        if self._gate(_click_tool(button, clicks), {"text": text, "button": str(button), "clicks": clicks}, ctx,
                      f"click on-screen text {text!r}"):
            raw.mouse.click(point, button=button, clicks=clicks)
        return m

    # ------------------------------------------------------------------ keyboard
    def type(self, text: str, *, interval: float = 0.0, mode: Optional[str] = None) -> None:
        text = str(text)
        pieces = split_typed_text(text) or [""]
        first = pieces[0]
        ctx, secret = self._focus_ctx(activating=typed_text_activates(first), enter="\n" in first or "\r" in first)
        shown = "***" if secret else (text if len(text) <= 60 else text[:57] + "...")
        if not self._gate("type_text", {"text": text}, ctx, f"type {shown!r}", secret=secret):
            return None
        self._type_pieces(pieces, shown, interval=interval, mode=mode, first_done=False)
        return None

    def _type_pieces(self, pieces: List[str], shown: str, *, interval: float, mode: Optional[str],
                     first_done: bool) -> None:
        raw = self._raw
        for i, piece in enumerate(pieces):
            if i == 0 and first_done:
                continue
            if i:  # a Tab/newline was typed: the rest may land in another control
                ctx, secret = self._focus_ctx(activating=typed_text_activates(piece), enter="\n" in piece or "\r" in piece)
                self._gate("type_text", {"text": piece}, ctx, f"type {'***' if secret else piece!r} (part of {shown!r})",
                           count=False, secret=secret)
            raw.type(piece, interval=interval, mode=mode)

    def press(self, *keys: Any, presses: int = 1, interval: float = 0.0) -> None:
        from ..keys import press_actions  # noqa: PLC0415
        from .tools import _shortcut_targets  # noqa: PLC0415

        parsed = [parts for k in keys for parts in press_actions(k)]
        if not parsed:
            return None
        combos = " ".join("+".join(p) for p in parsed)
        activates = keys_activate(parsed)
        pressed = _shortcut_targets(self._raw, parsed)
        ctx, _ = self._focus_ctx(activating=activates, enter=any(p in ("enter", "num_enter") for ps in parsed for p in ps),
                                 keys=combos, shortcut_targets=pressed or None)
        desc = f"press {combos}" + (f" x{presses}" if presses > 1 else "") + (
            f" (presses {', '.join(map(repr, pressed))})" if pressed else "")
        if not self._gate("press_keys", {"keys": [str(k) for k in keys], "presses": presses}, ctx, desc):
            return None
        raw = self._raw
        moved = False
        first = True
        for _ in range(max(1, int(presses))):
            for parts in parsed:
                k = "+".join(parts)
                if moved:  # an earlier key may have moved the focus: judge what this one acts on
                    c, _ = self._focus_ctx(activating=keys_activate([parts]), enter=any(p in ("enter", "num_enter") for p in parts),
                                           step_keys=k)
                    self._gate("press_keys", {"keys": [k]}, c, f"press {k} (part of {combos!r})", count=False)
                if not first and interval:
                    raw.sleep(interval)
                raw.press(parts)
                first = False
                moved = moved or keys_may_move_focus(parts)
        return None

    def hotkey(self, *keys: Any, interval: float = 0.0) -> None:
        from ..keys import parse_hotkey  # noqa: PLC0415
        from .tools import _shortcut_targets  # noqa: PLC0415

        parts = parse_hotkey(keys[0] if len(keys) == 1 else [str(k) for k in keys])
        combo = "+".join(parts)
        pressed = _shortcut_targets(self._raw, [parts])
        ctx, _ = self._focus_ctx(activating=keys_activate([parts]), enter=any(p in ("enter", "num_enter") for p in parts),
                                 keys=combo, shortcut_targets=pressed or None)
        desc = f"press {combo}" + (f" (presses {', '.join(map(repr, pressed))})" if pressed else "")
        if self._gate("hotkey", {"combo": combo}, ctx, desc):
            self._raw.hotkey(*keys, interval=interval)

    def key_down(self, key: str) -> None:
        ctx, _ = self._focus_ctx(activating=keys_activate([key]), enter=str(key).lower() in ("enter", "return"), keys=str(key))
        if self._gate("key_down", {"key": str(key)}, ctx, f"hold down {key}"):
            self._raw.key_down(key)

    def hold(self, *keys: Any) -> Any:
        combo = "+".join(str(k) for k in keys)
        ctx, _ = self._focus_ctx(activating=keys_activate([combo]), keys=combo)
        if not self._gate("hold_key", {"keys": combo}, ctx, f"hold {combo}"):
            import contextlib  # noqa: PLC0415

            return contextlib.nullcontext([])
        return self._raw.hold(*keys)

    # ------------------------------------------------------------------ system
    def launch(self, command: Any, **kw: Any) -> Any:
        check_launch_options(kw, self.policy)  # before the gate: never ask a human about a refused launch
        ctx = self._base_ctx()
        ctx.update(command=command, target=" ".join(map(str, command)) if isinstance(command, (list, tuple)) else str(command))
        if not self._gate("launch_app", {"command": command}, ctx, f"launch {command!r}"):
            return None
        return self._wrap(self._raw.launch(command, **{k: _unwrap(v) for k, v in kw.items()}))

    def clipboard_set(self, text: str) -> None:
        shown = text if len(str(text)) <= 60 else str(text)[:57] + "..."
        if self._gate("write_clipboard", {"text": text}, self._base_ctx(), f"copy {shown!r} to the clipboard"):
            self._raw.clipboard_set(text)

    def clipboard_get(self) -> str:
        self.policy.check("read_clipboard", {}, context={})
        return self._raw.clipboard_get()

    @property
    def mouse(self) -> "_GuardedMouse":
        return _GuardedMouse(self._raw.mouse, self)

    @property
    def keyboard(self) -> "_GuardedKeyboard":
        return _GuardedKeyboard(self._raw.keyboard, self)

    def __repr__(self) -> str:
        return f"<GuardedDesktop {self._raw!r} policy={self.policy!r}>"


class _GuardedMouse(_GuardedProxy):
    """``desktop.mouse`` behind the guard."""

    def _point(self, x: Any, y: Any) -> Point:
        guard = object.__getattribute__(self, "_g_guard")
        if x is None:
            return guard._raw.position() if y is None else Point(guard._raw.position().x, int(round(y)))
        if y is not None:
            return Point(int(round(float(x))), int(round(float(y))))
        return coerce_point(_unwrap(x))

    def click(self, x: Any = None, y: Optional[float] = None, *, button: Any = "left", clicks: int = 1, **kw: Any) -> Any:
        guard = object.__getattribute__(self, "_g_guard")
        point = self._point(x, y)
        btn = str(getattr(button, "value", button)).lower()
        ctx = guard._point_ctx(point, activates=btn == "left")
        if not guard._gate(_click_tool(btn, clicks), {"x": point.x, "y": point.y, "button": btn, "clicks": clicks}, ctx,
                           f"click ({point.x}, {point.y})"):
            return point
        return self.unwrapped.click(_unwrap(x), y, button=button, clicks=clicks, **kw)

    def double_click(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        kw["clicks"] = 2
        return self.click(x, y, **kw)

    def triple_click(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        kw["clicks"] = 3
        return self.click(x, y, **kw)

    def right_click(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        kw["button"] = "right"
        return self.click(x, y, **kw)

    def middle_click(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        kw["button"] = "middle"
        return self.click(x, y, **kw)

    def down(self, button: Any = "left") -> None:
        guard = object.__getattribute__(self, "_g_guard")
        ctx = guard._point_ctx(guard._raw.position(), activates=str(getattr(button, "value", button)).lower() == "left")
        if guard._gate("mouse_down", {"button": str(button)}, ctx, f"press the {button} mouse button"):
            self.unwrapped.down(button)

    def up(self, button: Any = "left") -> None:
        guard = object.__getattribute__(self, "_g_guard")
        ctx = guard._point_ctx(guard._raw.position(), activates=False)
        if guard._gate("mouse_up", {"button": str(button)}, ctx, f"release the {button} mouse button"):
            self.unwrapped.up(button)

    def move(self, x: Any = None, y: Optional[float] = None, **kw: Any) -> Any:
        guard = object.__getattribute__(self, "_g_guard")
        point = self._point(x, y)
        if guard._gate("move_mouse", {}, guard._point_ctx(point, activates=False), f"move the mouse to ({point.x}, {point.y})"):
            return self.unwrapped.move(_unwrap(x), y, **kw)
        return point

    def drag(self, start: Any, end: Any, **kw: Any) -> Any:
        guard = object.__getattribute__(self, "_g_guard")
        return guard.drag(start, end, **{k: v for k, v in kw.items() if k in ("button", "duration", "humanize", "hold")})

    def scroll(self, dy: int = 0, dx: int = 0, *, at: Any = None) -> None:
        guard = object.__getattribute__(self, "_g_guard")
        point = guard._raw.position() if at is None else self._point(at, None)
        if guard._gate("scroll", {"dy": dy, "dx": dx}, guard._point_ctx(point, activates=False), f"scroll dy={dy} dx={dx}"):
            self.unwrapped.scroll(dy, dx, at=_unwrap(at))


class _GuardedKeyboard(_GuardedProxy):
    """``desktop.keyboard`` behind the guard."""

    def type(self, text: str, *, interval: float = 0.0, mode: str = "auto") -> None:
        object.__getattribute__(self, "_g_guard").type(text, interval=interval, mode=mode)

    def press(self, *keys: Any, presses: int = 1, interval: float = 0.0) -> None:
        object.__getattribute__(self, "_g_guard").press(*keys, presses=presses, interval=interval)

    def hotkey(self, *keys: Any, interval: float = 0.0) -> None:
        object.__getattribute__(self, "_g_guard").hotkey(*keys, interval=interval)

    def down(self, key: str) -> None:
        object.__getattribute__(self, "_g_guard").key_down(key)

    def hold(self, *keys: Any) -> Any:
        return object.__getattribute__(self, "_g_guard").hold(*keys)


class GuardedElement(_GuardedProxy):
    """An :class:`~locum.element.Element` whose actions are checked by its :class:`GuardedDesktop`'s policy."""

    def _act(self, tool: str, args: Dict[str, Any], description: str, *, activates: bool,
             extra: Optional[Dict[str, Any]] = None, offset: Any = (0, 0)) -> bool:
        guard: GuardedDesktop = object.__getattribute__(self, "_g_guard")
        el = self.unwrapped
        ctx = guard._element_ctx(el, activates=activates, offset=_unwrap(offset) or (0, 0))
        if extra:
            ctx.update(extra)
        from .._nodeinfo import is_password  # noqa: PLC0415

        what = ctx.get("target") or getattr(el, "selector", None) or el.role or "element"
        return guard._gate(tool, args, ctx, f"{description} {what!r}", secret=is_password(el.node))

    def click(self, button: str = "left", clicks: int = 1, **kw: Any) -> "GuardedElement":
        btn = str(getattr(button, "value", button)).lower()
        if self._act(_click_tool(btn, clicks), {"button": btn, "clicks": clicks}, "click", activates=btn == "left",
                     offset=kw.get("offset", (0, 0))):
            self.unwrapped.click(button, clicks, **kw)
        return self

    def double_click(self, button: str = "left", **kw: Any) -> "GuardedElement":
        if self._act("double_click", {"button": str(button)}, "double-click", activates=str(button).lower() == "left",
                     offset=kw.get("offset", (0, 0))):
            self.unwrapped.double_click(button, **kw)
        return self

    def right_click(self, **kw: Any) -> "GuardedElement":
        if self._act("right_click", {}, "right-click", activates=False, offset=kw.get("offset", (0, 0))):
            self.unwrapped.right_click(**kw)
        return self

    def hover(self, **kw: Any) -> "GuardedElement":
        if self._act("move_mouse", {}, "hover over", activates=False, offset=kw.get("offset", (0, 0))):
            self.unwrapped.hover(**kw)
        return self

    def _native(self, name: str, verb: str, *, activates: bool = True) -> "GuardedElement":
        if self._act(name, {}, verb, activates=activates):
            getattr(self.unwrapped, name)()
        return self

    def invoke(self) -> "GuardedElement":
        return self._native("invoke", "invoke")

    def toggle(self) -> "GuardedElement":
        return self._native("toggle", "toggle")

    def check(self) -> "GuardedElement":
        return self._native("check", "check")

    def uncheck(self) -> "GuardedElement":
        return self._native("uncheck", "uncheck")

    def select(self) -> "GuardedElement":
        return self._native("select", "select")

    def expand(self) -> "GuardedElement":
        return self._native("expand", "expand", activates=False)

    def collapse(self) -> "GuardedElement":
        return self._native("collapse", "collapse", activates=False)

    def focus(self) -> "GuardedElement":
        return self._native("focus", "focus", activates=False)

    def scroll_into_view(self) -> "GuardedElement":
        return self._native("scroll_into_view", "scroll into view", activates=False)

    def scroll(self, dy: int, dx: int = 0) -> "GuardedElement":
        if self._act("scroll", {"dy": dy, "dx": dx}, "scroll", activates=False):
            self.unwrapped.scroll(dy, dx)
        return self

    def set_text(self, text: str, **kw: Any) -> "GuardedElement":
        # May click the field to focus it: judged like a click on it (identity rule included).
        if self._act("set_value", {"value": text}, "set the text of", activates=True):
            self.unwrapped.set_text(text, **kw)
        return self

    def clear(self) -> "GuardedElement":
        if self._act("set_value", {"value": ""}, "clear", activates=True):
            self.unwrapped.clear()
        return self

    def type(self, text: str, **kw: Any) -> "GuardedElement":
        text = str(text)
        pieces = split_typed_text(text) or [""]
        enter = "\n" in pieces[0] or "\r" in pieces[0]
        extra = None
        if enter:
            from .tools import _default_button  # noqa: PLC0415
            from .._nodeinfo import target_label  # noqa: PLC0415

            button = _default_button(self.unwrapped.node)
            label = _safe_get(lambda: target_label(button), "") if button is not None else ""
            if label:
                extra = {"default_button": label, "target_node": button}
        if extra:
            # Enter in a single-line field presses the default button ("Pay now").
            guard: GuardedDesktop = object.__getattribute__(self, "_g_guard")
            ctx = guard._element_ctx(self.unwrapped, activates=True)
            ctx["target"] = f"{ctx.get('target', '')} -> {extra['default_button']}".strip(" ->")
            ctx["target_node"] = extra["target_node"]
            ctx.pop("target_unknown", None)
            from .._nodeinfo import is_password  # noqa: PLC0415

            ok = guard._gate("type_text", {"text": text}, ctx, f"type into {ctx['target']!r}",
                             secret=is_password(self.unwrapped.node))
        else:
            ok = self._act("type_text", {"text": text}, "type into", activates=True)
        if not ok:
            return self
        self.unwrapped.type(pieces[0], **kw)
        if len(pieces) > 1:
            guard = object.__getattribute__(self, "_g_guard")
            guard._type_pieces(pieces, text if len(text) <= 60 else text[:57] + "...",
                               interval=float(kw.get("interval", 0.0) or 0.0), mode=kw.get("mode"), first_done=True)
        return self

    def press(self, *keys: Any, **kw: Any) -> "GuardedElement":
        el = self.unwrapped
        focus = getattr(el, "_ensure_focus", None)
        if callable(focus):
            focus()
        else:
            el.focus()
        object.__getattribute__(self, "_g_guard").press(*keys, **kw)
        return self

    def __repr__(self) -> str:
        return f"<guarded {self.unwrapped!r}>"


class GuardedWindow(_GuardedProxy):
    """A :class:`~locum.window.Window` whose ``activate``/``close`` are checked by the policy."""

    def _ctx(self) -> Dict[str, Any]:
        guard: GuardedDesktop = object.__getattribute__(self, "_g_guard")
        win = self.unwrapped
        ctx = guard._base_ctx()
        ctx.update(target=win.title, target_app=win.app, target_window=win.title)
        return ctx

    def activate(self) -> "GuardedWindow":
        guard: GuardedDesktop = object.__getattribute__(self, "_g_guard")
        if guard._gate("focus_window", {"title": self.unwrapped.title}, self._ctx(), f"focus window {self.unwrapped.title!r}"):
            self.unwrapped.activate()
        return self

    def close(self) -> "GuardedWindow":
        guard: GuardedDesktop = object.__getattribute__(self, "_g_guard")
        if guard._gate("close_window", {"title": self.unwrapped.title}, self._ctx(), f"close window {self.unwrapped.title!r}"):
            self.unwrapped.close()
        return self


__all__ = [
    "ACTIVATING_KEYS",
    "AllowRule",
    "CARD_NUMBER",
    "GuardedDesktop",
    "GuardedElement",
    "GuardedWindow",
    "NAVIGATION_KEYS",
    "NON_COMMITTING_TOOLS",
    "is_text_input",
    "keys_navigate_only",
    "node_matches_selector",
    "target_identity",
    "launch_program",
    "check_launch_options",
    "SAFE_LAUNCH_OPTIONS",
    "FORBIDDEN_LAUNCH_OPTIONS",
    "check_activation_combo",
    "combo_acts_on_target",
    "pressed_control",
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
