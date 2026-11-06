"""The ``locum`` command line.

::

    locum info                                   # backend, monitors, capabilities, optional deps
    locum screenshot -o shot.png --region 0,0,800,600
    locum windows --json
    locum tree --window "Invoice Entry" --depth 6 --all
    locum find 'edit#amount' --json
    locum click 'button "Save"'                  # a selector: bound to its element's window
    locum click 300,200 --window "Invoice"       # "x,y" or an image: only inside that window
    locum type "hello\\n" -e --into 'edit "Vendor"'
    locum hotkey ctrl+s --window "Invoice"       # keys only while "Invoice" has the focus
    locum locate ok_button.png --confidence 0.85
    locum observe --json                         # what an LLM agent sees (set-of-marks)
    locum inspect 120,340                        # selector of the element under a point
    locum click -1200,300 --anywhere             # negative x: a monitor left of the primary
    locum mcp --policy strict                    # MCP server over stdio (Claude Desktop / Code)
    locum agent "Enter invoice INV-7 for ACME, 120.50" --dry-run
    locum run flow.yaml --var customer=ACME --report-dir runs
    locum record -o recording.py
    locum a11y-audit Paint --fail-on warning     # unnamed / unreachable controls, labels

Global options come before the command: ``--backend NAME`` (``mock`` gives an
in-memory demo desktop with an ERP form, a spreadsheet and a text editor, ideal
for trying locum, CI and tests), ``--timeout``, ``--json``, ``--no-failsafe``,
``--humanize``, ``--pause``, ``--kill-hotkey``/``--kill-combo`` and ``--debug``.

Input commands need a target: ``type``/``press``/``hotkey`` take ``--window``
(``type`` also ``--into``) and a click on coordinates or an image takes
``--window``; every key press and click is checked against that target right
before it is sent (exit ``1``, ``TargetLost``, when another window took the focus
or covers the point). ``--focused`` / ``--anywhere`` opt into the focus / pointer.
With ``--json``, stdout holds exactly one JSON document, also on failure.

Exit codes: ``0`` success, ``1`` the action failed (element/image/window not
found, timeout, policy violation, failed flow or agent run, target lost), ``2`` usage error
(bad arguments, invalid selector or flow), ``3`` unavailable (no usable backend,
unsupported capability or missing optional dependency), ``4`` aborted by the
fail-safe, ``130`` interrupted (Ctrl+C).

:func:`main` takes ``argv`` and returns the exit code, so the CLI can be driven
in-process (tests inject ``desktop=``, ``stdout=``, ``stderr=``...).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import importlib.util
import json
import math
import os
import platform
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from . import __version__
from .backends.base import Backend
from .backends.mock import MockBackend, MockWidget, MockWindow
from .errors import (
    BackendUnavailable,
    FailSafeTriggered,
    NotSupported,
    LocumError,
    SelectorSyntaxError,
    WorkflowError,
)
from .types import Point, Rect

__all__ = [
    "DemoBackend",
    "EXIT_ABORTED",
    "EXIT_FAILURE",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "build_parser",
    "main",
]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_ABORTED = 4
EXIT_INTERRUPTED = 130

#: Backend names that the CLI maps to the populated :class:`DemoBackend`.
DEMO_BACKEND_NAMES = ("mock", "demo")

#: Optional dependencies reported by ``locum info`` (import name -> extra / purpose).
OPTIONAL_DEPENDENCIES: Tuple[Tuple[str, str], ...] = (
    ("anthropic", "llm: Agent with Claude"),
    ("yaml", "yaml: YAML flows"),
    ("pynput", "record: live recorder, kill hotkey"),
    ("pytesseract", "ocr: Tesseract OCR"),
    ("rapidocr_onnxruntime", "OCR alternative"),
    ("easyocr", "OCR alternative"),
    ("cv2", "faster template matching"),
    ("openpyxl", "XLSX work queues"),
    ("jeepney", "wayland: native Wayland backend (xdg-desktop-portal)"),
)


class _UsageError(Exception):
    """Invalid command-line input detected after argparse (exit code 2)."""


# =============================================================================== demo desktop


_CELL_COLS = "ABCDE"
_CELL_ROWS = 12


class DemoBackend(MockBackend):
    """A :class:`MockBackend` pre-populated with a small, fully interactive demo desktop.

    Three windows (front to back):

    * **"Invoice Entry - ERP"** (app ``erp``): edits ``#vendor``, ``#invoice_no``,
      ``#amount``, ``#due_date``, checkbox ``#urgent``, buttons ``Save`` (validates
      and stores the invoice in :attr:`invoices`), ``Clear`` and ``Submit Payment``
      (records a payment in :attr:`payments`), a status label ``text#status`` and a
      ``Saved invoices`` list.
    * **"Book1 - Excel"** (app ``excel``): a ``Name Box`` and a 5x12 grid of cells
      named ``A1`` ... ``E12`` with spreadsheet keyboard behaviour (Name Box +
      Enter jumps to a cell, Tab moves right, Enter returns to the row's first
      typed column one row down, arrows move, typing replaces the cell, ``mod+s``
      counts a save in :attr:`saved_workbooks`).
    * **"Untitled - Notepad"** (app ``notepad``): a multi-line ``Text Editor``.

    It is what ``locum --backend mock ...`` drives, and what the examples use
    when ``LOCUM_BACKEND=mock``: a deterministic sandbox for demos, CI, flows
    and LLM-agent evaluations.
    """

    name = "mock"

    def __init__(self, width: int = 1920, height: int = 1080, **kw: Any):
        super().__init__(width, height, **kw)
        #: Invoices stored by the ERP form's Save button.
        self.invoices: List[Dict[str, Any]] = []
        #: Payments submitted with "Submit Payment".
        self.payments: List[Dict[str, Any]] = []
        #: Number of ``mod+s`` presses in the spreadsheet.
        self.saved_workbooks = 0
        self._row_anchor = 0
        self.notepad = self._build_notepad()
        self.excel = self._build_excel()
        self.erp = self._build_erp()  # added last: front-most and active

    # ------------------------------------------------------------------ notepad
    def _build_notepad(self) -> MockWindow:
        win = self.add_window("Untitled - Notepad", (40, 560, 560, 320), app="notepad", pid=5103)
        win.add_edit("Text Editor", (6, 32, 548, 282), automation_id="editor", attrs={"multiline": True})
        return win

    # -------------------------------------------------------------------- excel
    def _build_excel(self) -> MockWindow:
        win = self.add_window("Book1 - Excel", (640, 40, 640, 420), app="excel", pid=5102)
        self._name_box = win.add_edit("Name Box", (10, 34, 80, 24), automation_id="NameBox")
        self._formula = win.add_edit("Formula Bar", (100, 34, 530, 24), automation_id="FormulaBar")
        self._cells: Dict[str, MockWidget] = {}
        for ci, col in enumerate(_CELL_COLS):
            win.add_text(col, (40 + ci * 116, 64, 116, 22))
        for row in range(1, _CELL_ROWS + 1):
            win.add_text(str(row), (6, 86 + (row - 1) * 26, 30, 26))
            for ci, col in enumerate(_CELL_COLS):
                ref = f"{col}{row}"
                cell = win.add_edit(ref, (40 + ci * 116, 86 + (row - 1) * 26, 116, 26),
                                    class_name="Cell", attrs={"cell": (ci, row)}, on_change=self._on_cell_change)
                self._cells[ref] = cell
        return win

    def cell(self, ref: str) -> str:
        """Current text of spreadsheet cell ``ref`` (e.g. ``"B3"``)."""
        try:
            return self._cells[ref.strip().upper()].value or ""
        except KeyError:
            raise KeyError(f"No demo cell {ref!r} (A1..{_CELL_COLS[-1]}{_CELL_ROWS})") from None

    def _on_cell_change(self, cell: MockWidget) -> None:
        if self._focus is cell:
            self._formula.value = cell.value or ""

    def _goto_cell(self, col: int, row: int, *, anchor: bool = False) -> None:
        col = min(max(col, 0), len(_CELL_COLS) - 1)
        row = min(max(row, 1), _CELL_ROWS)
        ref = f"{_CELL_COLS[col]}{row}"
        cell = self._cells[ref]
        self._focus = cell
        cell.attrs["select_all"] = True  # typing replaces the cell, like a spreadsheet
        if anchor:
            self._row_anchor = col
        self._name_box.value = ref
        self._formula.value = cell.value or ""

    def mouse_down(self, button: Any = "left") -> None:
        super().mouse_down(button)
        w = self._focus
        if w is not None and w.window is self.excel:
            if w is self._name_box:
                w.attrs["select_all"] = True
            elif "cell" in w.attrs:
                col, row = w.attrs["cell"]
                self._goto_cell(col, row, anchor=True)

    def _apply_key(self, key: str) -> None:
        w = self._focus
        if w is not None and w.window is self.excel and w.enabled:
            mods = self._mods()
            if mods & {"ctrl", "cmd"}:
                if key.lower() == "s":
                    self.saved_workbooks += 1
                    self._log("workbook_saved", self.saved_workbooks)
                    return
            elif not mods & {"alt"}:
                if w is self._name_box and key == "enter":
                    m = re.fullmatch(r"\s*([A-Za-z])\s*(\d+)\s*", w.value or "")
                    if m and m.group(1).upper() in _CELL_COLS:
                        self._goto_cell(_CELL_COLS.index(m.group(1).upper()), int(m.group(2)), anchor=True)
                    return
                if "cell" in w.attrs:
                    col, row = w.attrs["cell"]
                    moves = {
                        "tab": (col - 1, row) if "shift" in mods else (col + 1, row),
                        "enter": (self._row_anchor, row - 1) if "shift" in mods else (self._row_anchor, row + 1),
                        "up": (col, row - 1),
                        "down": (col, row + 1),
                        "left": (col - 1, row),
                        "right": (col + 1, row),
                    }
                    if key in moves:
                        # Tab keeps the row anchor; every other move (arrows, Enter) re-anchors
                        self._goto_cell(*moves[key], anchor=(key != "tab"))
                        return
        super()._apply_key(key)

    # ---------------------------------------------------------------------- erp
    def _build_erp(self) -> MockWindow:
        win = self.add_window("Invoice Entry - ERP", (40, 40, 560, 480), app="erp", pid=5101)
        fields = (("Vendor", "vendor"), ("Invoice No", "invoice_no"), ("Amount", "amount"), ("Due Date", "due_date"))
        self._fields: Dict[str, MockWidget] = {}
        for i, (label, aid) in enumerate(fields):
            y = 46 + i * 36
            win.add_text(label + ":", (20, y, 110, 26))
            self._fields[aid] = win.add_edit(label, (140, y, 380, 26), automation_id=aid)
        self._urgent = win.add_checkbox("Urgent", (140, 190, 150, 26), automation_id="urgent")
        win.add_button("Save", (140, 228, 100, 32), on_click=self._on_save, automation_id="save")
        win.add_button("Clear", (250, 228, 100, 32), on_click=self._on_clear, automation_id="clear")
        win.add_button("Submit Payment", (360, 228, 160, 32), on_click=self._on_pay, automation_id="pay")
        self._status = win.add_text("Ready", (20, 272, 500, 24), automation_id="status")
        self._list = win.add_list("Saved invoices", (20, 304, 520, 160), automation_id="invoices")
        return win

    def _set_status(self, text: str) -> None:
        self._status.name = text
        self._status.value = text

    def _clear_fields(self) -> None:
        for w in self._fields.values():
            w.value = ""
        self._urgent.checked = False

    def _on_clear(self, _w: MockWidget) -> None:
        self._clear_fields()
        self._set_status("Cleared")

    def _on_save(self, _w: MockWidget) -> None:
        values = {k: (w.value or "").strip() for k, w in self._fields.items()}
        if not values["vendor"] or not values["invoice_no"]:
            self._set_status("Error: vendor and invoice number are required")
            return
        try:
            amount = float(values["amount"].replace(",", ""))
        except ValueError:
            self._set_status(f"Error: amount {values['amount']!r} is not a number")
            return
        if not math.isfinite(amount):
            self._set_status(f"Error: amount {values['amount']!r} is not a number")
            return
        if amount <= 0:
            self._set_status("Error: amount must be positive")
            return
        if any(inv["invoice_no"] == values["invoice_no"] and inv["vendor"] == values["vendor"] for inv in self.invoices):
            self._set_status(f"Error: invoice {values['invoice_no']} already exists")
            return
        record = {
            "vendor": values["vendor"],
            "invoice_no": values["invoice_no"],
            "amount": round(amount, 2),
            "due_date": values["due_date"],
            "urgent": bool(self._urgent.checked),
            "paid": False,
        }
        self.invoices.append(record)
        self._add_list_item(f"{record['invoice_no']} | {record['vendor']} | {record['amount']:.2f}")
        self._clear_fields()
        self._set_status(f"Saved invoice {record['invoice_no']}")
        self._focus = self._fields["vendor"]

    def _add_list_item(self, text: str) -> None:
        items = self._list.children
        visible_rows = max(1, self._list.rect.height // 24)
        top = self._list.rect.top
        self._list.add("listitem", text, (self._list.rect.left, top, self._list.rect.width, 24), fill=None)
        shown = items[-visible_rows:]
        for w in items:
            w.visible = w in shown
        for i, w in enumerate(shown):
            w.rect = Rect(self._list.rect.left, top + i * 24, self._list.rect.width, 24)

    def _on_pay(self, _w: MockWidget) -> None:
        unpaid = [inv for inv in self.invoices if not inv["paid"]]
        if not unpaid:
            self._set_status("Nothing to pay")
            return
        total = round(sum(inv["amount"] for inv in unpaid), 2)
        for inv in unpaid:
            inv["paid"] = True
        self.payments.append({"invoices": [inv["invoice_no"] for inv in unpaid], "total": total})
        self._set_status(f"Payment submitted for {len(unpaid)} invoice(s), total {total:.2f}")


# =============================================================================== helpers


class _Ctx:
    """Per-invocation state: parsed args, streams and the (lazily created) desktop."""

    def __init__(self, args: argparse.Namespace, out: TextIO, err: TextIO, inp: TextIO,
                 desktop: Any = None, client: Any = None):
        self.args = args
        self.out = out
        self.err = err
        self.inp = inp
        self.client = client
        self._desktop = desktop
        self._owned_backend: Optional[Backend] = None
        self.json_emitted = False
        #: The stdout stream as given (``mcp`` speaks UTF-8 JSON-RPC on it itself).
        self.raw_out = getattr(out, "wrapped", out)

    @property
    def json(self) -> bool:
        return bool(getattr(self.args, "json", False))

    @property
    def desktop(self) -> Any:
        if self._desktop is None:
            from .desktop import Desktop

            backend = _create_backend(self.args.backend)
            self._owned_backend = backend
            kw: Dict[str, Any] = {
                "failsafe": not self.args.no_failsafe,
                "pause": self.args.pause,
                "humanize": bool(self.args.humanize),
            }
            if self.args.timeout is not None:
                kw["default_timeout"] = self.args.timeout
            self._desktop = Desktop(backend, **kw)
        return self._desktop

    def close(self) -> None:
        if self._owned_backend is not None:
            try:
                self._desktop.close()
            except Exception:
                pass
            try:
                self._owned_backend.close()
            except Exception:
                pass
            self._owned_backend = None

    def print(self, *parts: Any) -> None:
        print(*parts, file=self.out)

    def info(self, *parts: Any) -> None:
        print(*parts, file=self.err)

    def emit_json(self, data: Any) -> None:
        """Write the command's one JSON document to stdout.

        Non-ASCII text is escaped (``\\u280b``) unless stdout is UTF-8, so the document
        is valid and complete on any console code page (a braille spinner in a window
        title cannot break it)."""
        if self.json_emitted:
            raise RuntimeError("a --json command wrote two documents")  # pragma: no cover - programming error
        text = json.dumps(data, indent=2, ensure_ascii=not _is_utf8(self.out), default=_json_default)
        self.out.write(text + "\n")
        self.json_emitted = True


def _is_utf8(stream: Any) -> bool:
    enc = (getattr(stream, "encoding", None) or "utf-8").lower().replace("-", "").replace("_", "")
    return enc in ("utf8", "utf8sig", "cp65001")


class _SafeText:
    """A text stream that never fails on characters its encoding lacks.

    A Windows console or pipe with a legacy code page (cp1252, cp437) cannot encode
    a braille spinner or an emoji in a window title: such a character is written as
    ``?`` instead of crashing the command (JSON output escapes them instead, see
    :meth:`_Ctx.emit_json`)."""

    def __init__(self, stream: Any) -> None:
        self.wrapped = stream

    @property
    def encoding(self) -> Optional[str]:
        return getattr(self.wrapped, "encoding", None)

    def write(self, text: str) -> int:
        try:
            return self.wrapped.write(text)
        except UnicodeEncodeError:
            enc = self.encoding or "ascii"
            return self.wrapped.write(text.encode(enc, "replace").decode(enc, "replace"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)


def _json_default(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def _create_backend(name: Optional[str]) -> Backend:
    """The backend for ``--backend NAME`` (``mock``/``demo`` = :class:`DemoBackend`)."""
    from .backends import create_backend

    resolved = (name or os.environ.get("LOCUM_BACKEND") or "").strip().lower()
    if resolved in DEMO_BACKEND_NAMES:
        return DemoBackend()
    return create_backend(resolved or None)


_INT_RE = r"-?\d+(?:\.\d+)?"


def _parse_numbers(text: str, count: int, what: str) -> List[int]:
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if len(parts) != count or not all(re.fullmatch(_INT_RE, p) for p in parts):
        raise _UsageError(f"{what} must be {count} comma-separated numbers, got {text!r}")
    return [int(round(float(p))) for p in parts]


def _parse_region(text: Optional[str]) -> Optional[Rect]:
    """``"L,T,W,H"`` -> :class:`Rect` (``None`` passes through)."""
    if text is None:
        return None
    left, top, width, height = _parse_numbers(text, 4, "--region")
    if width <= 0 or height <= 0:
        raise _UsageError(f"--region width and height must be positive, got {text!r}")
    return Rect(left, top, width, height)


def _parse_point(text: str, what: str = "point") -> Point:
    x, y = _parse_numbers(text, 2, what)
    return Point(x, y)


def _is_point(text: str) -> bool:
    return re.fullmatch(rf"\s*{_INT_RE}\s*[, ]\s*{_INT_RE}\s*", text) is not None


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "e": "\x1b", "0": "\0"}


def _unescape(text: str) -> str:
    r"""Interpret ``\n``, ``\t``, ``\r``, ``\\``, ``\e`` and ``\uXXXX`` like ``echo -e``."""

    def sub(m: "re.Match[str]") -> str:
        tok = m.group(1)
        if len(tok) == 5 and tok[0] == "u":  # a full \uXXXX; a lone \u is kept as-is
            return chr(int(tok[1:], 16))
        return _ESCAPES.get(tok, "\\" + tok)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", sub, text)


def _fmt_handle(handle: Any) -> str:
    return hex(handle) if isinstance(handle, int) and not isinstance(handle, bool) else str(handle)


def _fmt_rect(rect: Optional[Rect]) -> str:
    if rect is None:
        return "(no bounds)"
    return f"({rect.left},{rect.top} {rect.width}x{rect.height})"


def _parse_vars(pairs: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in pairs or ():
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise _UsageError(f"--var expects KEY=VALUE, got {pair!r}")
        out[key.strip()] = value
    return out


def _build_policy(name: str, *, dry_run: bool = False, confirm: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
                  allowed_apps: Optional[Sequence[str]] = None, max_actions: Optional[int] = None) -> Any:
    """A :class:`~locum.llm.safety.Policy` from the ``--policy`` preset name and flags."""
    from .llm.safety import Policy

    kw: Dict[str, Any] = {"dry_run": dry_run, "confirm": confirm}
    if allowed_apps:
        kw["allowed_apps"] = list(allowed_apps)
    if max_actions is not None:
        kw["max_actions"] = max_actions
    if name == "strict":
        return Policy.strict(**kw)
    if name == "permissive":
        return Policy.permissive(**kw)
    return Policy(**kw)


def _short_json(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 3] + "..."


# =============================================================================== input targets

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")


def _is_image_target(text: str) -> bool:
    return text.lower().endswith(_IMAGE_EXTS) or (os.path.isfile(os.path.expanduser(text)) and not _is_point(text))


def _target_window(ctx: _Ctx) -> Any:
    """``--window TITLE``: the window, brought to the foreground and verified to be there.

    Input sent while it is bound (:func:`_bound`) reaches this window or nothing: if
    another window takes the focus (or covers a click point) before or during the
    input, the command stops with exit code 1 instead of typing into it."""
    from .errors import TargetLost

    a = ctx.args
    d = ctx.desktop
    w = d.window(a.window, timeout=a.timeout)
    w.activate()
    try:
        fg = d.backend.foreground_handle()
    except Exception:
        fg = None
    if fg is None or not (fg == w.handle or d.backend.window_belongs_to(fg, w.handle)):
        raise TargetLost(f"could not bring {w.title!r} to the foreground (another window keeps the focus); "
                         "nothing was sent")
    return w


def _bound(ctx: _Ctx, window: Any) -> Any:
    from . import _target

    if window is None:
        return contextlib.nullcontext()
    return _target.bind(_target.for_window(window, strict=True))


def _require_scope(ctx: _Ctx, what: str, *, flag: str, allowed: str) -> None:
    a = ctx.args
    if getattr(a, "window", None) or getattr(a, flag, False) or getattr(a, "into", None):
        return
    raise _UsageError(
        f"{what} go to whatever has the focus (or is under the pointer) when they are sent, which may be "
        f"another application than the one you meant (a chat window that just took the focus). "
        f"Name the target with {allowed}, or pass --{flag.replace('_', '-')} to send them there anyway."
    )


def _unscoped_target(ctx: _Ctx) -> Any:
    """``--focused`` / ``--anywhere``: still bound to the window that has the focus (or is under
    the point) when the command starts, so a focus change *during* the input stops it."""
    from . import _target

    try:
        h = ctx.desktop.backend.foreground_handle()
    except Exception:
        h = None
    if h is None:
        return contextlib.nullcontext()
    return _target.bind(_target.InputTarget(ctx.desktop.backend, h, title="the window that had the focus when "
                                            "the command started", strict=False))


# =============================================================================== commands


def cmd_info(ctx: _Ctx) -> int:
    d = ctx.desktop
    be = d.backend

    def safe(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (LocumError, OSError):
            return None

    monitors = safe(d.monitors) or []
    active = safe(d.active_window)
    cursor = safe(d.position)
    deps = {mod: importlib.util.find_spec(mod) is not None for mod, _ in OPTIONAL_DEPENDENCIES}
    data = {
        "version": __version__,
        "python": platform.python_version(),
        "platform": sys.platform,
        "os": platform.platform(),
        "backend": getattr(be, "name", type(be).__name__),
        "backend_class": f"{type(be).__module__}.{type(be).__name__}",
        "capabilities": sorted(be.capabilities),
        "monitors": [m.to_dict() for m in monitors],
        "cursor": cursor.to_dict() if cursor is not None else None,
        "active_window": active.to_dict() if active is not None else None,
        "failsafe": bool(d.failsafe.enabled),
        "optional_dependencies": deps,
    }
    limits = getattr(be, "limitations", None)
    if callable(limits):
        data["limitations"] = [str(x) for x in (safe(limits) or [])]
    if ctx.json:
        ctx.emit_json(data)
        return EXIT_OK
    ctx.print(f"locum {__version__} on Python {data['python']} ({data['os']})")
    ctx.print(f"backend:      {data['backend']} ({data['backend_class']})")
    ctx.print(f"capabilities: {', '.join(data['capabilities']) or 'none'}")
    for m in monitors:
        flag = " primary" if m.is_primary else ""
        ctx.print(f"monitor {m.index}:    {_fmt_rect(m.rect)} scale {m.scale:g}{flag}")
    if cursor is not None:
        ctx.print(f"cursor:       ({cursor.x}, {cursor.y})")
    if active is not None:
        ctx.print(f"active:       {active.title!r} [{active.app}]")
    for note in data.get("limitations", []):
        ctx.print(f"limitation:   {note}")
    ctx.print(f"failsafe:     {'on' if data['failsafe'] else 'off'}")
    ctx.print("optional:     " + ", ".join(f"{mod} {'yes' if ok else 'no'}" for mod, ok in deps.items()))
    return EXIT_OK


def cmd_screenshot(ctx: _Ctx) -> int:
    a = ctx.args
    out = a.output or _dt.datetime.now().strftime("screenshot-%Y%m%d-%H%M%S.png")
    window = getattr(a, "window", None)
    mode = getattr(a, "mode", "auto")
    restore_minimized = bool(getattr(a, "restore_minimized", False))
    how = None
    if window is not None:
        if a.region or a.monitor is not None:
            raise _UsageError("--window cannot be combined with --region or --monitor")
        win = ctx.desktop.window(window, timeout=0)
        img, how = win._capture(mode, restore_minimized=restore_minimized)
        p = os.path.expanduser(out)
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        if os.path.splitext(p)[1]:
            img.save(p)
        else:
            img.save(p, format="PNG")
    else:
        if mode != "auto" or restore_minimized:
            raise _UsageError("--mode and --restore-minimized need --window")
        region = _parse_region(a.region)
        img = ctx.desktop.screenshot(region, monitor=a.monitor, path=out)
    path = os.path.abspath(os.path.expanduser(out))
    if ctx.json:
        data = {"path": path, "width": img.size[0], "height": img.size[1]}
        if how is not None:
            data["capture"] = how
        ctx.emit_json(data)
    else:
        via = " (window captured off-screen)" if how == "window" else ""
        ctx.print(f"saved {img.size[0]}x{img.size[1]} screenshot to {path}{via}")
    return EXIT_OK


def cmd_windows(ctx: _Ctx) -> int:
    a = ctx.args
    wins = ctx.desktop.windows(a.title, app=a.app)
    if ctx.json:
        ctx.emit_json([w.info.to_dict() for w in wins])
        return EXIT_OK
    if not wins:
        ctx.info("no windows" + (" match" if a.title or a.app else ""))
        return EXIT_OK
    for w in wins:
        info = w.info
        flags = []
        if info.is_active:
            flags.append("active")
        if info.is_minimized:
            flags.append("minimized")
        if info.is_maximized:
            flags.append("maximized")
        mark = "*" if info.is_active else " "
        extra = f" [{', '.join(flags)}]" if flags else ""
        ctx.print(f"{mark} {info.title!r} app={info.app or '?'} pid={info.pid} {_fmt_rect(info.rect)} "
                  f"handle={_fmt_handle(info.handle)}{extra}")
    return EXIT_OK


def cmd_tree(ctx: _Ctx) -> int:
    a = ctx.args
    if a.depth < 0:
        raise _UsageError("--depth must be >= 0")
    text = ctx.desktop.tree(window=a.window, max_depth=a.depth, interactive_only=not a.all, max_nodes=a.max_nodes)
    if ctx.json:
        ctx.emit_json({"tree": text.splitlines()})
    else:
        ctx.print(text if text else "(empty accessibility tree)")
    return EXIT_OK


def _robust_selector(el: Any) -> str:
    """``selector_for(el.node)`` checked for uniqueness across *all* windows,
    so the printed selector finds this element (not a same-titled window's) with ``find``."""
    from .selectors import selector_for

    try:
        roots: Optional[List[Any]] = list(el.desktop.roots())
    except Exception:
        roots = None
    return selector_for(el.node, roots=roots)


def _element_line(el: Any) -> str:
    from .selectors import describe_node

    try:
        sel = _robust_selector(el)
    except Exception:
        sel = ""
    return describe_node(el.node) + (f"  ->  {sel}" if sel else "")


def _element_dict(el: Any) -> Dict[str, Any]:
    d = el.to_dict()
    try:
        d["robust_selector"] = _robust_selector(el)
    except Exception:
        d["robust_selector"] = None
    return d


def cmd_find(ctx: _Ctx) -> int:
    a = ctx.args
    d = ctx.desktop
    if a.all:
        found = d.find_all(a.selector)
        if not found and a.timeout:
            # --all with a timeout: wait for the first match, then list them all
            d.find(a.selector, timeout=a.timeout)
            found = d.find_all(a.selector)
    else:
        found = [d.find(a.selector, timeout=a.timeout)]
    if ctx.json:
        ctx.emit_json([_element_dict(e) for e in found] if a.all else _element_dict(found[0]))
        return EXIT_OK if found else EXIT_FAILURE
    if not found:
        ctx.info(f"no element matches {a.selector!r}")
        return EXIT_FAILURE
    for el in found:
        ctx.print(_element_line(el))
    return EXIT_OK


def cmd_click(ctx: _Ctx) -> int:
    a = ctx.args
    if a.clicks < 1:
        raise _UsageError("--clicks must be >= 1")
    offset = tuple(_parse_numbers(a.offset, 2, "--offset")) if a.offset else (0, 0)
    positional = _is_point(a.target) or _is_image_target(a.target)
    if positional:
        # A selector names its element (and is bound to its window); a point or an image
        # clicks whatever window is there.
        _require_scope(ctx, "Clicks at coordinates or on an image", flag="anywhere", allowed="--window TITLE")
    target: Any = _parse_point(a.target, "TARGET") if _is_point(a.target) else a.target
    window = _target_window(ctx) if a.window else None
    if window is not None and not positional:
        target = window.find(a.target, timeout=a.timeout)
    with _bound(ctx, window):
        if target is a.target or isinstance(target, Point):
            point = ctx.desktop.click(target, button=a.button, clicks=a.clicks, offset=offset)
        else:
            target.click(a.button, a.clicks, offset=offset)
            point = getattr(target, "_last_click_point", None) or target.center
    if ctx.json:
        ctx.emit_json({"clicked": point.to_dict(), "button": a.button, "clicks": a.clicks,
                       "window": window.title if window is not None else None})
    else:
        ctx.print(f"clicked ({point.x}, {point.y})" + (f" in {window.title!r}" if window is not None else ""))
    return EXIT_OK


def cmd_move(ctx: _Ctx) -> int:
    a = ctx.args
    target: Any = _parse_point(a.target, "TARGET") if _is_point(a.target) else a.target
    point = ctx.desktop.move(target, duration=a.duration)
    if ctx.json:
        ctx.emit_json({"position": point.to_dict()})
    else:
        ctx.print(f"moved to ({point.x}, {point.y})")
    return EXIT_OK


def cmd_type(ctx: _Ctx) -> int:
    a = ctx.args
    text = _unescape(a.text) if a.escapes else a.text
    d = ctx.desktop
    if a.replace and not a.into:
        raise _UsageError("--replace needs --into SELECTOR")
    _require_scope(ctx, "Typed keystrokes", flag="focused", allowed="--window TITLE or --into SELECTOR")
    window = _target_window(ctx) if a.window else None
    verified: Optional[bool] = None
    if a.into:
        # The element (inside --window when given) keeps the input: every keystroke is
        # checked against its window and focus, and the field is read back afterwards.
        el = window.find(a.into, timeout=a.timeout) if window is not None else d.find(a.into, timeout=a.timeout)
        with _bound(ctx, window):
            if a.replace:
                el.set_text(text)
            else:
                el.type(text, interval=a.interval, mode=a.mode)
        verified = el._value_for_verify() is not None
    else:
        with (_bound(ctx, window) if window is not None else _unscoped_target(ctx)):
            d.type(text, interval=a.interval, mode=a.mode)
    if ctx.json:
        ctx.emit_json({"typed": len(text), "into": a.into, "window": window.title if window is not None else None,
                       "verified": verified})
    else:
        ctx.print(f"typed {len(text)} character(s)" + (f" into {a.into!r}" if a.into else "")
                  + (f" in {window.title!r}" if window is not None else "")
                  + (" (read back and verified)" if verified else ""))
    return EXIT_OK


def cmd_hotkey(ctx: _Ctx) -> int:
    keys = ctx.args.keys
    _require_scope(ctx, "Key combinations", flag="focused", allowed="--window TITLE")
    window = _target_window(ctx) if ctx.args.window else None
    with (_bound(ctx, window) if window is not None else _unscoped_target(ctx)):
        ctx.desktop.hotkey(*keys)
    if ctx.json:
        ctx.emit_json({"hotkey": keys, "window": window.title if window is not None else None})
    else:
        ctx.print("pressed " + "+".join(keys) + (f" in {window.title!r}" if window is not None else ""))
    return EXIT_OK


def cmd_press(ctx: _Ctx) -> int:
    a = ctx.args
    if a.presses < 1:
        raise _UsageError("--presses must be >= 1")
    _require_scope(ctx, "Key presses", flag="focused", allowed="--window TITLE")
    window = _target_window(ctx) if a.window else None
    with (_bound(ctx, window) if window is not None else _unscoped_target(ctx)):
        ctx.desktop.press(*a.keys, presses=a.presses, interval=a.interval)
    if ctx.json:
        ctx.emit_json({"pressed": a.keys, "presses": a.presses, "window": window.title if window is not None else None})
    else:
        ctx.print("pressed " + " ".join(a.keys) + (f" x{a.presses}" if a.presses > 1 else "")
                  + (f" in {window.title!r}" if window is not None else ""))
    return EXIT_OK


def cmd_scroll(ctx: _Ctx) -> int:
    a = ctx.args
    at: Any = None
    if a.at is not None:
        at = _parse_point(a.at, "--at") if _is_point(a.at) else a.at
    ctx.desktop.scroll(a.dy, a.dx, at=at)
    if ctx.json:
        ctx.emit_json({"dy": a.dy, "dx": a.dx})
    else:
        ctx.print(f"scrolled dy={a.dy} dx={a.dx}")
    return EXIT_OK


def cmd_wait(ctx: _Ctx) -> int:
    a = ctx.args
    el = ctx.desktop.wait(a.selector, state=a.state, timeout=a.timeout)
    if el is not None and not hasattr(el, "node"):  # an image path: a vision Match
        if ctx.json:
            ctx.emit_json({"state": a.state, "match": el.to_dict()})
        else:
            ctx.print(f"{a.state}: image at {tuple(el.rect)} (confidence {el.confidence:.3f})")
        return EXIT_OK
    if ctx.json:
        ctx.emit_json({"state": a.state, "element": _element_dict(el) if el is not None else None})
    elif el is None:
        ctx.print(f"{a.selector!r} is gone")
    else:
        ctx.print(f"{a.state}: {_element_line(el)}")
    return EXIT_OK


def cmd_locate(ctx: _Ctx) -> int:
    a = ctx.args
    if not 0.0 < a.confidence <= 1.0:
        raise _UsageError("--confidence must be in (0, 1]")
    d = ctx.desktop
    region = _parse_region(a.region)
    if a.all:
        matches = d.locate_all(a.image, region=region, confidence=a.confidence, grayscale=a.grayscale)
        if not matches and a.timeout:
            # --all with a timeout: wait for the first match, then list them all (like find --all)
            if d.locate(a.image, region=region, confidence=a.confidence, grayscale=a.grayscale,
                        timeout=a.timeout) is not None:
                matches = d.locate_all(a.image, region=region, confidence=a.confidence, grayscale=a.grayscale)
    else:
        m = d.locate(a.image, region=region, confidence=a.confidence, grayscale=a.grayscale, timeout=a.timeout or 0.0)
        matches = [m] if m is not None else []
    if matches and a.click:
        if not a.anywhere:
            raise _UsageError("locate --click clicks whatever window is at the match: pass --anywhere to click it "
                              "anyway, or use `locum click IMAGE --window TITLE`")
        with _unscoped_target(ctx):
            d.mouse.click(matches[0].center)
    if ctx.json:
        ctx.emit_json([m.to_dict() for m in matches])
    else:
        if not matches:
            ctx.info(f"image {a.image!r} not found (confidence >= {a.confidence:g})")
        for m in matches:
            c = m.center
            ctx.print(f"found {_fmt_rect(m.rect)} center ({c.x}, {c.y}) confidence {m.confidence:.3f}"
                      + (f" scale {m.scale:g}" if m.scale != 1.0 else ""))
        if matches and a.click:
            ctx.print(f"clicked ({matches[0].center.x}, {matches[0].center.y})")
    return EXIT_OK if matches else EXIT_FAILURE


def cmd_observe(ctx: _Ctx) -> int:
    from .llm.observe import observe

    a = ctx.args
    obs = observe(
        ctx.desktop,
        screenshot=bool(a.screenshot),
        annotate=True,
        max_long_edge=a.max_long_edge,
        interactive_only=not a.all,
        window=a.window,
        max_elements=a.max_elements,
    )
    saved = None
    if a.screenshot:
        img = obs.annotated if obs.annotated is not None else obs.screenshot
        path = os.path.expanduser(a.screenshot)
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        img.save(path)
        saved = os.path.abspath(path)
    if ctx.json:
        data = obs.to_dict()
        if saved:
            data["screenshot"] = saved
        ctx.emit_json(data)
    else:
        ctx.print(obs.text(max_elements=a.max_elements))
        if saved:
            ctx.print(f"annotated screenshot: {saved}")
    return EXIT_OK


def cmd_inspect(ctx: _Ctx) -> int:
    d = ctx.desktop
    point = _parse_point(ctx.args.point, "POINT") if ctx.args.point else d.position()
    el = d.element_at(point)
    if el is None:
        ctx.info(f"no accessible element at ({point.x}, {point.y})")
        return EXIT_FAILURE
    sel = _robust_selector(el)
    chain = []
    cur = el
    while cur is not None and len(chain) < 64:
        chain.append(cur)
        cur = cur.parent
    if ctx.json:
        data = _element_dict(el)
        data["point"] = point.to_dict()
        data["ancestors"] = [{"role": e.role, "name": e.name} for e in chain[1:]]
        ctx.emit_json(data)
        return EXIT_OK
    from .selectors import describe_node

    ctx.print(f"at ({point.x}, {point.y}): {describe_node(el.node)}")
    ctx.print(f"selector: {sel}")
    for depth, e in enumerate(reversed(chain)):
        ctx.print("  " * (depth + 1) + describe_node(e.node, include_bounds=False))
    return EXIT_OK


def cmd_mcp(ctx: _Ctx) -> int:
    from .llm.mcp_server import MCPServer
    from .llm.safety import AuditLog

    a = ctx.args
    policy = _build_policy(a.policy, dry_run=a.dry_run, allowed_apps=a.allow_app, max_actions=a.max_actions)
    audit = AuditLog(os.path.expanduser(a.audit)) if a.audit else AuditLog()
    t = a.call_timeout
    if t is not None and (t != t or t < 0 or t == float("inf")):
        raise _UsageError("--call-timeout must be a finite number >= 0")
    server = MCPServer(ctx.desktop, policy, audit, call_timeout=t or None)
    be = ctx.desktop.backend
    ctx.info(f"locum {__version__} MCP server on stdio (backend={getattr(be, 'name', '?')}, policy={a.policy}"
             + (", dry-run" if a.dry_run else "") + (f", audit={a.audit}" if a.audit else "") + ")")
    server.serve(ctx.inp, ctx.raw_out)
    return EXIT_OK


def cmd_agent(ctx: _Ctx) -> int:
    from .llm.agent import Agent, AgentStep, ScriptedClient
    from .llm.safety import AuditLog, console_confirm

    a = ctx.args
    if a.max_steps < 1:
        raise _UsageError("--max-steps must be >= 1")
    client = ctx.client
    if a.replay:
        with open(os.path.expanduser(a.replay), encoding="utf-8") as fh:
            script = json.load(fh)
        if isinstance(script, dict):
            script = script.get("responses", [])
        if not isinstance(script, list) or not script:
            raise _UsageError(f"--replay file {a.replay!r} must contain a non-empty list of responses")
        client = ScriptedClient(script)
    interactive = bool(getattr(ctx.inp, "isatty", lambda: False)())
    confirm = console_confirm if (interactive and not a.no_confirm) else None
    policy = _build_policy(a.policy, dry_run=a.dry_run, confirm=confirm, allowed_apps=a.allow_app,
                           max_actions=a.max_actions)
    audit = AuditLog(os.path.expanduser(a.audit)) if a.audit else AuditLog()

    def on_step(step: AgentStep) -> None:
        if a.quiet:
            return
        if step.text:
            ctx.info(f"[{step.index + 1}] {step.text.strip()}")
        for call, res in zip(step.tool_calls, step.tool_results):
            status = "ERROR " if res.is_error else ""
            first = (res.text or "").strip().splitlines()[0] if (res.text or "").strip() else ""
            ctx.info(f"[{step.index + 1}] {call.get('name')}({_short_json(call.get('input') or {}, 120)[1:-1]}) "
                     f"-> {status}{first[:160]}")

    agent = Agent(
        ctx.desktop,
        model=a.model,
        client=client,
        mode=a.mode,
        policy=policy,
        audit=audit,
        max_steps=a.max_steps,
        on_step=on_step,
        effort=a.effort,
    )
    result = agent.run(a.task)
    if ctx.json:
        ctx.emit_json(result.to_dict())
    else:
        ctx.print(result.final_text)
        usage = result.usage
        ctx.info(f"{'success' if result.success else 'FAILED'} after {len(result.steps)} step(s), "
                 f"stop={result.stop_reason}, tokens in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}")
    if result.stop_reason == "aborted":
        return EXIT_ABORTED
    return EXIT_OK if result.success else EXIT_FAILURE


def cmd_run(ctx: _Ctx) -> int:
    from .rpa.flow import load_flow, run_flow, validate_flow

    a = ctx.args
    variables = _parse_vars(a.var)
    if a.validate:
        doc, _path = load_flow(a.flow)
        problems = validate_flow(doc)
        if ctx.json:
            ctx.emit_json({"valid": not problems, "problems": problems})
        elif problems:
            ctx.info(f"{a.flow}: {len(problems)} problem(s):")
            for p in problems:
                ctx.info(f"  - {p}")
        else:
            ctx.print(f"{a.flow}: valid ({len(doc.get('steps') or [])} top-level step(s))")
        return EXIT_OK if not problems else EXIT_USAGE
    report = run_flow(a.flow, ctx.desktop, variables, run_dir=a.report_dir)
    if ctx.json:
        ctx.emit_json(report.to_dict())
    else:
        for failure in report.failures:
            ctx.info(f"FAILED {failure.path or failure.name}: {failure.error}")
        ctx.print(report.summary())
        if report.run_dir:
            ctx.print(f"report: {os.path.join(report.run_dir, 'report.html')}")
    if report.aborted:
        return EXIT_ABORTED
    return EXIT_OK if report.ok else EXIT_FAILURE


def cmd_record(ctx: _Ctx, *, sleep: Callable[[float], Any] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    from .rpa.recorder import Recorder

    a = ctx.args
    ext = os.path.splitext(a.output)[1].lower()
    if ext not in (".py", ".yaml", ".yml", ".json"):
        raise _UsageError(f"-o/--output must end in .py, .yaml, .yml or .json, got {a.output!r}")
    if a.duration is not None and a.duration < 0:
        raise _UsageError("--duration must be >= 0")
    if ext in (".yaml", ".yml"):
        try:  # fail before the session, not after the user has recorded everything
            import yaml  # type: ignore  # noqa: F401
        except ImportError:
            raise ImportError("Saving YAML flows requires PyYAML: pip install locum[yaml] "
                              "(or record to a .json/.py file)") from None
    rec = Recorder(ctx.desktop, use_selectors=not getattr(a, "no_selectors", False))
    source = a.source
    from .backends.mock import MockBackend

    if source == "auto" and isinstance(ctx.desktop.backend, MockBackend):
        # Global OS hooks would record the real display, not the simulated desktop
        # (an explicit --source pynput is honoured).
        source = "backend"
        ctx.info("note: the mock backend has no live input; only actions performed on it are recorded")
    rec.start(source=source)
    ctx.info("recording... " + (f"for {a.duration:g}s" if a.duration is not None else "press Ctrl+C to stop"))
    deadline = None if a.duration is None else clock() + a.duration
    try:
        while deadline is None or clock() < deadline:
            sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        actions = rec.stop()
    try:
        path = rec.save(a.output, name=getattr(a, "name", None))
    except Exception as exc:
        # Never lose a recording session to a save error (unwritable path, missing PyYAML...):
        # keep it as JSON in the current (or temp) directory and still report the failure.
        rescue = _rescue_recording(rec, a.output)
        if rescue is not None:
            ctx.info(f"locum: could not save {a.output!r} ({type(exc).__name__}: {exc}); "
                     f"the recording was saved to {rescue}")
        raise
    if ctx.json:
        ctx.emit_json({"path": os.path.abspath(path), "actions": [x.to_dict() for x in actions]})
    else:
        ctx.print(f"recorded {len(actions)} action(s) to {os.path.abspath(path)}")
    return EXIT_OK


def cmd_a11y_audit(ctx: _Ctx) -> int:
    from .a11y import audit

    a = ctx.args
    if a.max_depth < 0:
        raise _UsageError("--max-depth must be >= 0")
    if a.min_target_size < 0:
        raise _UsageError("--min-target-size must be >= 0")
    if a.time_budget is not None and a.time_budget < 0:
        raise _UsageError("--time-budget must be >= 0")
    report = audit(ctx.desktop, a.window, max_depth=a.max_depth, min_target_size=a.min_target_size,
                   time_budget=a.time_budget or None, selectors=not a.no_selectors)
    failed = report.failed(a.fail_on)
    if ctx.json:
        data = report.to_dict()
        data["fail_on"] = a.fail_on
        data["failed"] = failed
        ctx.emit_json(data)
    else:
        ctx.print(report.format_text())
    return EXIT_FAILURE if failed else EXIT_OK


def _rescue_recording(rec: Any, wanted: str) -> Optional[str]:
    import tempfile

    stem = os.path.splitext(os.path.basename(wanted))[0] or "recording"
    name = f"{stem}-rescued-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    for folder in (os.path.dirname(os.path.abspath(wanted)), os.getcwd(), tempfile.gettempdir()):
        try:
            return os.path.abspath(rec.save(os.path.join(folder, name), name=stem))
        except Exception:
            continue
    return None


COMMANDS: Dict[str, Callable[[_Ctx], int]] = {
    "info": cmd_info,
    "screenshot": cmd_screenshot,
    "windows": cmd_windows,
    "tree": cmd_tree,
    "find": cmd_find,
    "click": cmd_click,
    "move": cmd_move,
    "type": cmd_type,
    "hotkey": cmd_hotkey,
    "press": cmd_press,
    "scroll": cmd_scroll,
    "wait": cmd_wait,
    "locate": cmd_locate,
    "observe": cmd_observe,
    "inspect": cmd_inspect,
    "mcp": cmd_mcp,
    "agent": cmd_agent,
    "run": cmd_run,
    "record": cmd_record,
    "a11y-audit": cmd_a11y_audit,
}


# =============================================================================== parser


class _Parser(argparse.ArgumentParser):
    """argparse treating ``-5,10`` / ``-5,0,800,600`` like a negative number, not an option.

    Monitors left of / above the primary one have negative coordinates
    (``locum click -5,10``, ``--at -1200,300``); stock argparse only accepts a
    bare ``-5`` as a value. Subparsers inherit the class.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._negative_number_matcher = re.compile(rf"^-\d+$|^-\d*\.\d+$|^-\d+(?:\.\d+)?(?:\s*[,\s]\s*{_INT_RE})+$")


def build_parser() -> argparse.ArgumentParser:
    """The ``locum`` argument parser."""
    parser = _Parser(
        prog="locum",
        description="Desktop automation for humans, robots and LLMs.",
        epilog=(
            "Use --backend mock for a sandboxed demo desktop. Exit codes: 0 ok, 1 failed, 2 usage, "
            "3 unavailable, 4 fail-safe abort, 130 interrupted. Docs: README.md, docs/LLM.md."
        ),
    )
    parser.add_argument("--version", action="version", version=f"locum {__version__}")
    parser.add_argument("--backend", metavar="NAME",
                        help="backend: windows, macos, x11, or mock (in-memory demo desktop); "
                             "default: $LOCUM_BACKEND or the platform backend")
    parser.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                        help="default seconds to wait for elements/windows/images (default 10)")
    parser.add_argument("--pause", type=float, default=0.0, metavar="SECONDS",
                        help="sleep after every mouse/keyboard action")
    parser.add_argument("--humanize", action="store_true", help="human-like mouse motion")
    parser.add_argument("--no-failsafe", action="store_true",
                        help="disable the screen-corner fail-safe (not recommended)")
    parser.add_argument("--kill-hotkey", action="store_true",
                        help="install a global abort hotkey (see --kill-combo; needs pynput)")
    parser.add_argument("--kill-combo", default="ctrl+alt+shift+q", metavar="COMBO",
                        help="the abort hotkey for --kill-hotkey (default ctrl+alt+shift+q)")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--debug", action="store_true", help="show full tracebacks on errors")

    common = _Parser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, description=help_text, parents=[common])

    add("info", "show backend, monitors, capabilities and optional dependencies")

    p = add("screenshot", "capture the screen to a PNG file")
    p.add_argument("-o", "--output", metavar="PATH", help="output file (default screenshot-<time>.png)")
    p.add_argument("--region", metavar="L,T,W,H", help="capture only this rectangle")
    p.add_argument("--monitor", type=int, metavar="N", help="capture monitor N (0 = primary)")
    p.add_argument("--window", metavar="TITLE", help="capture this window (even when covered by other windows)")
    p.add_argument("--mode", choices=("auto", "screen", "window"), default="auto",
                   help="with --window: auto (default; off-screen capture when covered/off-screen/minimized), "
                        "screen (grab its screen rectangle) or window (always capture off-screen)")
    p.add_argument("--restore-minimized", action="store_true",
                   help="with --window: briefly show a minimized window without activating it to capture it")

    p = add("windows", "list top-level windows (front-most first)")
    p.add_argument("--title", help="filter: exact, substring (case-insensitive) or glob title")
    p.add_argument("--app", help="filter: application/process name (.exe optional)")

    p = add("tree", "dump the accessibility tree")
    p.add_argument("--window", metavar="TITLE", help="only this window")
    p.add_argument("--depth", type=int, default=8, metavar="N", help="maximum depth (default 8)")
    p.add_argument("--all", action="store_true", help="include non-interactive nodes (labels, groups)")
    p.add_argument("--max-nodes", type=int, default=2000, metavar="N", help="maximum lines (default 2000)")

    p = add("find", "find elements by selector (CSS-like, UiPath XML or JSON)")
    p.add_argument("selector")
    p.add_argument("--all", action="store_true", help="list every match")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS",
                   help="seconds to wait (default: global --timeout)")

    p = add("click", "click a selector, or 'x,y' coordinates / an image file in --window")
    p.add_argument("target")
    p.add_argument("--button", choices=("left", "right", "middle"), default="left")
    p.add_argument("--clicks", type=int, default=1)
    p.add_argument("--offset", metavar="DX,DY", help="offset from the target's center")
    p.add_argument("--window", metavar="TITLE", help="bring this window to the front and click only inside it "
                   "(a selector is searched in it; a point or image is refused if another window covers it)")
    p.add_argument("--anywhere", action="store_true",
                   help="click a point or image without --window: whatever window is there gets the click")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS")

    p = add("move", "move the mouse to a selector, 'x,y' or an image")
    p.add_argument("target")
    p.add_argument("--duration", type=float, default=None, metavar="SECONDS")

    p = add("type", "type text --into an element or into --window (--focused: wherever the focus is)")
    p.add_argument("text")
    p.add_argument("-e", "--escapes", action="store_true", help=r"interpret \n \t \\ \uXXXX escapes")
    p.add_argument("--into", metavar="SELECTOR", help="focus this element and type only while it keeps the focus")
    p.add_argument("--window", metavar="TITLE", help="bring this window to the front and type only while it has the "
                   "focus (with --into: the element is searched in it)")
    p.add_argument("--focused", action="store_true",
                   help="type into whatever control has the focus now (stops if another window takes it)")
    p.add_argument("--replace", action="store_true", help="with --into: replace the element's text (native set-value when possible)")
    p.add_argument("--interval", type=float, default=0.0, metavar="SECONDS", help="delay between characters")
    p.add_argument("--mode", choices=("auto", "unicode", "keys", "clipboard"), default=None)
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS")

    p = add("hotkey", "press a key combination in --window, e.g. ctrl+s or 'ctrl shift esc' (mod = ctrl/cmd)")
    p.add_argument("keys", nargs="+", metavar="COMBO")
    p.add_argument("--window", metavar="TITLE", help="bring this window to the front and press only while it has the focus")
    p.add_argument("--focused", action="store_true", help="press in whatever window has the focus now")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS")

    p = add("press", "tap keys in sequence in --window, e.g. tab tab enter")
    p.add_argument("keys", nargs="+", metavar="KEY")
    p.add_argument("--presses", type=int, default=1)
    p.add_argument("--interval", type=float, default=0.0, metavar="SECONDS")
    p.add_argument("--window", metavar="TITLE", help="bring this window to the front and press only while it has the focus")
    p.add_argument("--focused", action="store_true", help="press in whatever window has the focus now")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS")

    p = add("scroll", "scroll the wheel (positive = up)")
    p.add_argument("dy", type=int)
    p.add_argument("--dx", type=int, default=0, help="horizontal notches (positive = right)")
    p.add_argument("--at", metavar="TARGET", help="selector or 'x,y' to scroll over")

    p = add("wait", "wait for an element to reach a state")
    p.add_argument("selector")
    p.add_argument("--state", default="visible",
                   help="visible, enabled, focused, gone, ... (default visible)")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS")

    p = add("locate", "find a template image on screen (alpha channel = mask)")
    p.add_argument("image")
    p.add_argument("--confidence", type=float, default=0.9, help="minimum match score in (0, 1] (default 0.9)")
    p.add_argument("--grayscale", action="store_true")
    p.add_argument("--region", metavar="L,T,W,H")
    p.add_argument("--all", action="store_true", help="list every occurrence")
    p.add_argument("--click", action="store_true", help="click the best match (needs --anywhere)")
    p.add_argument("--anywhere", action="store_true", help="with --click: click whatever window is at the match")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS")

    p = add("observe", "print what an LLM agent sees: numbered interactive elements")
    p.add_argument("--window", metavar="TITLE", help="only this window")
    p.add_argument("--all", action="store_true", help="include non-interactive named elements")
    p.add_argument("--screenshot", metavar="PATH", help="save the annotated (set-of-marks) screenshot")
    p.add_argument("--max-elements", type=int, default=300)
    p.add_argument("--max-long-edge", type=int, default=1568)

    p = add("inspect", "show the element and a robust selector at a point (default: the cursor)")
    p.add_argument("point", nargs="?", metavar="X,Y")

    for name, help_text in (("mcp", "run an MCP server on stdio (for Claude Desktop, Claude Code, IDEs)"),
                            ("agent", "let an LLM agent perform a task on the desktop")):
        p = add(name, help_text)
        if name == "agent":
            p.add_argument("task")
            p.add_argument("--model", default="claude-opus-5-5")
            p.add_argument("--mode", choices=("tools", "computer_use"), default="tools")
            p.add_argument("--max-steps", type=int, default=50)
            p.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="high")
            p.add_argument("--replay", metavar="FILE",
                           help="replay canned model responses from a JSON file (offline demos/evals)")
            p.add_argument("--no-confirm", action="store_true",
                           help="never ask on the terminal: actions needing confirmation are blocked")
            p.add_argument("-q", "--quiet", action="store_true", help="no per-step progress on stderr")
        else:
            p.description = ("Run an MCP server on stdio. Recommended client command: "
                             "`locum mcp` (or `locum-mcp`, or `python -m locum mcp` / `python -m locum.mcp`). "
                             "Claude Code: claude mcp add locum -- locum mcp --policy default")
            p.add_argument("--call-timeout", type=float, default=60.0, metavar="SEC",
                           help="seconds a tool call may run before the client gets a timeout error and the "
                                "call is stopped (default 60; 0 = no limit; wait_for with a longer timeout gets more)")
        p.add_argument("--policy", choices=("strict", "default", "permissive"), default="default",
                       help="safety preset (default: confirmations for pay/submit/delete/...)")
        p.add_argument("--dry-run", action="store_true", help="simulate actions; nothing touches the desktop")
        p.add_argument("--allow-app", action="append", metavar="APP",
                       help="only drive these apps (repeatable; globs allowed)")
        p.add_argument("--max-actions", type=int, default=None, metavar="N")
        p.add_argument("--audit", metavar="FILE", help="append a JSON-lines audit log of every tool call")

    p = add("run", "run a YAML/JSON flow")
    p.add_argument("flow", metavar="FLOW")
    p.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="override a flow variable (repeatable)")
    p.add_argument("--report-dir", metavar="DIR", help="write report.json/report.html and error screenshots under DIR/<run-id>/")
    p.add_argument("--validate", action="store_true", help="only validate the flow")

    p = add("record", "record mouse/keyboard input as a Python script or a flow")
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    p.description = "".join(line + "\n" for line in (
        "Record your mouse/keyboard session and write it as a Python script (.py, Desktop API)",
        "or a flow for `locum run` (.yaml/.yml/.json). Clicks and typing are recorded with",
        "accessibility selectors (window 'Title' button 'OK'); screen coordinates are used",
        "only where no element is under the pointer. Text typed into password fields is never",
        "written: the script/flow reads it from the LOCUM_PASSWORD environment variable.",
        "Live recording needs pynput (pip install locum[record]); with --backend mock only the",
        "actions performed on the simulated desktop are recorded.",
    ))
    p.epilog = "\n".join((
        "examples:",
        "  locum record -o login.py                 # stop with Ctrl+C",
        "  locum record -o login.yaml --duration 60",
        "  locum run login.yaml                     # replay the flow",
    ))
    p.add_argument("-o", "--output", default="recording.py", metavar="PATH", help=".py, .yaml/.yml or .json (default recording.py)")
    p.add_argument("--duration", type=float, default=None, metavar="SECONDS", help="stop after SECONDS (default: Ctrl+C)")
    p.add_argument("--no-selectors", action="store_true",
                   help="record screen coordinates only (faster on huge accessibility trees, but brittle)")
    p.add_argument("--name", metavar="NAME", help="flow name for .yaml/.json output (default: the file name)")
    p.add_argument("--source", choices=("auto", "pynput"), default="auto",
                   help="input source: global OS hooks via pynput (auto = pynput, or the simulated "
                        "desktop's own actions with --backend mock)")

    p = add("a11y-audit", "audit a window's accessibility: unnamed controls, unreachable controls, missing labels")
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    p.description = "\n".join((
        "Walk the accessibility tree of WINDOW (default: every window) read-only -- nothing is",
        "clicked or typed -- and report: interactive controls without an accessible name",
        "(unnamed-control), text inputs without a name/label (unlabeled-input), unnamed images,",
        "untitled windows, controls the keyboard cannot focus (not-focusable), click targets",
        "under --min-target-size px that crowd another target (small-target, WCAG 2.5.8),",
        "sibling controls sharing a name (duplicate-name) and offscreen-flagged controls inside",
        "a visible parent (offscreen-visible). Rules are described in docs/A11Y.md.",
        "",
        "Exit code 1 when findings at or above --fail-on exist (default: error).",
    ))
    p.epilog = "\n".join((
        "examples:",
        "  locum a11y-audit Paint",
        "  locum a11y-audit \"Untitled - Notepad\" --fail-on warning --json",
        "  locum --backend mock a11y-audit \"Invoice Entry\"",
    ))
    p.add_argument("window", nargs="?", metavar="WINDOW",
                   help="window title (exact, substring or glob, like --window elsewhere); default: every window")
    p.add_argument("--max-depth", type=int, default=32, metavar="N", help="maximum tree depth (default 32)")
    p.add_argument("--min-target-size", type=int, default=24, metavar="PX",
                   help="minimum click-target size in pixels for small-target (default 24; 0 disables)")
    p.add_argument("--fail-on", choices=("error", "warning", "never"), default="error",
                   help="exit 1 when findings of this severity or worse exist (default error)")
    p.add_argument("--time-budget", type=float, default=30.0, metavar="SECONDS",
                   help="stop walking after SECONDS and report partial results (default 30; 0 = no limit)")
    p.add_argument("--no-selectors", action="store_true", help="do not compute a selector per finding (faster)")
    return parser


# =============================================================================== main


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    desktop: Any = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    client: Any = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> int:
    """Run the CLI and return its exit code.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).
        desktop: Use this :class:`~locum.desktop.Desktop` instead of creating one from ``--backend``.
        stdin / stdout / stderr: Streams (default: the process streams). ``mcp`` speaks JSON-RPC
            on ``stdin``/``stdout``; everything else writes results to ``stdout`` and diagnostics to ``stderr``.
        client: LLM client for ``agent`` (default: ``anthropic.Anthropic()``).
        sleep: Sleep function for ``record`` (tests).
    """
    out = _SafeText(stdout if stdout is not None else sys.stdout)
    err = _SafeText(stderr if stderr is not None else sys.stderr)
    inp = stdin if stdin is not None else sys.stdin
    parser = build_parser()
    try:
        # argparse prints --help/--version to sys.stdout and usage errors to sys.stderr:
        # route them to the streams this call was given.
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # argparse: --help/--version (0) or usage error (2)
        code = exc.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else EXIT_USAGE)
    if not args.command:
        parser.print_help(out)
        return EXIT_USAGE
    if not hasattr(args, "timeout"):
        args.timeout = None
    if args.timeout is not None and args.timeout < 0:
        print("locum: error: --timeout must be >= 0", file=err)
        return EXIT_USAGE

    ctx = _Ctx(args, out, err, inp, desktop=desktop, client=client)
    handler = COMMANDS[args.command]
    try:
        if args.kill_hotkey:
            from .failsafe import install_kill_hotkey

            if install_kill_hotkey(args.kill_combo):
                ctx.info(f"kill hotkey {args.kill_combo} armed: press it to abort")
            else:
                ctx.info("warning: could not install the kill hotkey (pip install locum[record])")
        if handler is cmd_record:
            return cmd_record(ctx, sleep=sleep)
        return handler(ctx)
    except KeyboardInterrupt:
        print("locum: interrupted", file=err)
        return EXIT_INTERRUPTED
    except Exception as exc:
        if args.debug:
            import traceback

            traceback.print_exc(file=err)
        code, label = _classify(exc)
        print(f"locum: {label}: {_describe(exc)}", file=err)
        if ctx.json and not ctx.json_emitted and handler is not cmd_mcp:
            # --json is a contract: stdout carries exactly one JSON document, also when
            # the command failed (the diagnostic stays on stderr).
            try:
                ctx.emit_json({"ok": False, "error": {"type": type(exc).__name__, "label": label,
                                                      "message": _describe(exc), "exit_code": code}})
            except Exception:
                pass
        return code
    finally:
        ctx.close()


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _classify(exc: BaseException) -> Tuple[int, str]:
    """Map an exception to ``(exit code, label)``; unexpected errors are reported, not hidden."""
    if isinstance(exc, _UsageError):
        return EXIT_USAGE, "error"
    if isinstance(exc, FailSafeTriggered):
        return EXIT_ABORTED, "aborted"
    if isinstance(exc, (BackendUnavailable, NotSupported)):
        return EXIT_UNAVAILABLE, "unavailable"
    if isinstance(exc, ImportError):
        return EXIT_UNAVAILABLE, "missing dependency"
    if isinstance(exc, (SelectorSyntaxError, WorkflowError)) and not _is_runtime_workflow_error(exc):
        return EXIT_USAGE, "invalid input"
    if isinstance(exc, LocumError):
        return EXIT_FAILURE, type(exc).__name__
    if isinstance(exc, (FileNotFoundError, IsADirectoryError, PermissionError)):
        return EXIT_FAILURE, "file error"
    if isinstance(exc, OSError):
        return EXIT_FAILURE, "OS error"
    if isinstance(exc, json.JSONDecodeError):
        return EXIT_USAGE, "invalid JSON"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return EXIT_USAGE, "invalid input"
    return EXIT_FAILURE, f"unexpected {type(exc).__name__} (use --debug for the traceback)"


def _is_runtime_workflow_error(exc: BaseException) -> bool:
    """Flow *step* failures are runtime failures (exit 1), not invalid documents (exit 2)."""
    from .rpa.flow import FlowStepError

    return isinstance(exc, FlowStepError)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
