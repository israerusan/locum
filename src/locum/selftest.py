"""Real-desktop self-test: ``python -m locum.selftest``.

Exercises the active backend against the real operating system and writes a
pass/fail report. It is safe by design:

* it only types into an editor window it launches itself (Notepad on Windows,
  TextEdit on macOS, gedit/mousepad/xterm-free editors on Linux) and closes it
  without saving;
* it saves and restores your clipboard and mouse position;
* the fail-safe stays on -- slam the mouse into a screen corner to abort.

Do not touch the mouse or keyboard while it runs (about 30-60 seconds).

Output: a folder ``locum_selftest_<timestamp>/`` with ``report.txt``,
``report.json``, screenshots and the editor's accessibility tree. Send that
folder back when reporting problems.

Options::

    python -m locum.selftest            # asks before starting
    python -m locum.selftest --yes      # no prompt
    python -m locum.selftest --backend mock --yes   # dry run on the simulated desktop
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

# A deliberately mixed string: accents, euro, Greek, CJK and an emoji outside the BMP.
UNICODE_TEXT = "locum ñandú café €1.234,56 Ω 漢字 ✓ 😀"
SYMBOLS_TEXT = "@#$%^&*()_+{}|:\"<>?~`-=[]\\;',./"
SET_TEXT = "Set via accessibility ✓"
DONT_SAVE = r"(?i)^(don.?t save|no guardar|nicht speichern|ne pas enregistrer|não salvar|non salvare)$"


class Check:
    def __init__(self, name: str, status: str, detail: str = "", seconds: float = 0.0):
        self.name, self.status, self.detail, self.seconds = name, status, detail, seconds

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "seconds": round(self.seconds, 3)}


class SelfTest:
    def __init__(self, desktop: Any, out_dir: str, log: Callable[[str], None] = print):
        self.d = desktop
        self.out = out_dir
        self.log = log
        self.checks: List[Check] = []
        self.editor_window: Any = None
        #: True when the editor window already existed (the user's own Notepad): it is never closed
        self.editor_preexisting = False
        #: Whether the user's clipboard was saved (all formats) and will be restored
        self.clipboard_saved = False
        #: the empty file TextEdit edits on macOS; deleted after the run
        self.temp_doc: Optional[str] = None
        self.editor: Any = None
        self.env: Dict[str, Any] = {}
        #: how long to wait for an app's "save changes?" prompt after closing the editor
        self.save_prompt_timeout = 0.2 if desktop.backend.name == "mock" else 4.0

    # ----------------------------------------------------------------- runner
    def run_check(self, name: str, fn: Callable[[], Any], *, needs: Optional[str] = None) -> bool:
        if needs and not self.d.backend.has(needs):
            self.checks.append(Check(name, "SKIP", f"backend has no {needs!r} capability"))
            self.log(f"  SKIP  {name}: backend has no {needs!r} capability")
            return False
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            status = "PASS"
        except _Skip as exc:
            status, detail = "SKIP", str(exc)
        except _Warn as exc:
            status, detail = "WARN", str(exc)
        except Exception as exc:  # noqa: BLE001 - we report every failure
            from .errors import FailSafeTriggered

            if isinstance(exc, FailSafeTriggered):
                raise
            status = "FAIL"
            detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
        dt = time.monotonic() - t0
        self.checks.append(Check(name, status, str(detail), dt))
        first = str(detail).strip().splitlines()[0] if str(detail).strip() else ""
        self.log(f"  {status:4}  {name} ({dt:.1f}s){': ' + first if first else ''}")
        return status == "PASS"

    def path(self, name: str) -> str:
        return os.path.join(self.out, name)

    # ----------------------------------------------------------------- checks
    def c_environment(self) -> str:
        from . import __version__

        be = self.d.backend
        self.env = {
            "locum": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "backend": be.name,
            "capabilities": sorted(be.capabilities),
            "monitors": [m.to_dict() for m in self.d.monitors()],
        }
        if sys.platform == "win32":
            try:
                import ctypes

                self.env["dpi_awareness_context"] = "per-monitor-v2 requested by backend"
                self.env["system_dpi"] = int(ctypes.windll.user32.GetDpiForSystem())
            except Exception:
                pass
        mons = ", ".join(f"#{m.index} {m.rect.width}x{m.rect.height}@({m.rect.left},{m.rect.top}) x{m.scale:g}" for m in self.d.monitors())
        return f"{be.name} backend; monitors: {mons}"

    def c_screenshot(self) -> str:
        img = self.d.screenshot(path=self.path("01_desktop.png"))
        vs = self.d.backend.virtual_screen()
        if img.size != (vs.width, vs.height):
            raise AssertionError(f"screenshot is {img.size}, virtual screen is {vs.width}x{vs.height} (DPI scaling mismatch?)")
        extrema = img.convert("L").getextrema()
        if extrema[0] == extrema[1]:
            raise AssertionError(f"screenshot is a single flat color {extrema} (screen capture blocked?)")
        return f"{img.size[0]}x{img.size[1]} saved"

    def c_mouse(self) -> str:
        start = self.d.position()
        prim = self.d.monitors()[0].rect
        targets = [prim.center, (prim.left + prim.width // 3, prim.top + prim.height // 3)]
        try:
            for t in targets:
                self.d.move(t)
                got = self.d.position()
                if abs(got.x - t[0]) > 1 or abs(got.y - t[1]) > 1:
                    raise AssertionError(f"moved to {tuple(t)} but cursor reports {tuple(got)} (coordinate/DPI mismatch)")
            self.d.move(prim.center, duration=0.4, humanize=True)
            got = self.d.position()
            if got.distance(prim.center) > 1:
                raise AssertionError(f"humanized move ended at {tuple(got)}, expected {tuple(prim.center)}")
        finally:
            self.d.move(start)
        return "absolute + humanized moves land exactly"

    def c_windows(self) -> str:
        wins = self.d.windows()
        if not wins:
            raise AssertionError("no top-level windows listed")
        active = self.d.active_window()
        listing = "; ".join(f"{w.title[:40]!r} [{w.app}]" for w in wins[:8])
        return f"{len(wins)} windows, active={active.title[:40] if active else None!r}: {listing}"

    def c_launch_editor(self) -> str:
        cmd, title = _editor_for_platform(self.d.backend.name)
        if isinstance(cmd, list) and cmd[-1].endswith(".txt"):
            self.temp_doc = cmd[-1]
        try:
            before = {w.handle for w in self.d.windows()}
        except Exception:
            before = set()
        self.editor_window = self.d.launch(cmd, wait_window=title, timeout=25)
        # Windows 11 Notepad opens a new tab in an already-open window instead of a new
        # window: that window is the user's (other documents, maybe sensitive ones) and
        # must not be closed -- only the test tab is.
        self.editor_preexisting = self.editor_window.handle in before
        self.editor_window.activate()
        self.d.sleep(1.0)
        note = " (your existing Notepad window: only the test tab will be closed)" if self.editor_preexisting else ""
        return f"{self.editor_window.title!r} pid={self.editor_window.pid} rect={self.editor_window.rect.as_tuple()}{note}"

    def c_accessibility(self) -> str:
        win = self._win()
        win.activate()
        # Windows 11 Notepad reopens the user's previous tabs. Never type into
        # them: open a fresh tab (Ctrl+N; a plain new document elsewhere) and
        # only continue if the editor we land in is empty.
        if self.d.backend.name != "macos":  # TextEdit already opened an empty file of ours
            self.d.hotkey("mod+n")
            self.d.sleep(1.0)
        tree = self.d.tree(window=win, max_depth=12)
        with open(self.path("02_editor_tree.txt"), "w", encoding="utf-8") as fh:
            fh.write(tree)
        ed = None
        focused = self.d.focused()
        if focused is not None and focused.role in ("document", "edit") and focused.node.pid in (None, win.pid):
            ed = focused
        if ed is None:
            ed = win.find("document, edit", timeout=10)
        if (ed.value or "").strip():
            raise AssertionError(
                f"the editor is not empty ({len(ed.value)} characters) -- refusing to type into what may be "
                "your own document; close other Notepad tabs/documents and run again"
            )
        self.editor = ed
        return f"editor = {ed.role} {ed.name!r} at {ed.bounds} (fresh, empty); tree saved ({len(tree.splitlines())} lines)"

    def _empty_editor(self) -> Any:
        ed = self._editor()
        ed.set_text("")  # setup must not depend on the keystroke path under test
        if self._wait_editor_text("") != "":
            raise AssertionError(f"could not empty the test editor before typing: {self._editor_text()!r}")
        ed.click()
        self._settle()
        return ed

    def c_type_raw(self) -> str:
        """Raw keystrokes (``verify=False``): informational -- a mismatch is a WARN, not a FAIL.

        Synthesized keystrokes can be dropped or garbled by a busy application
        (Windows 11 Notepad does this intermittently); that is why the verified
        paths below exist. This check measures how the OS/app behaves here.
        """
        self._empty_editor()
        self.d.type(UNICODE_TEXT, verify=False)
        got = self._wait_editor_text(UNICODE_TEXT)
        if got != UNICODE_TEXT:
            raise _Warn(f"the app garbled raw keystrokes: typed {UNICODE_TEXT!r}, editor shows {got!r} "
                        "(Desktop.type/Element.type verify and repair this)")
        return "raw keystrokes arrived exactly (incl. emoji/CJK)"

    def c_type_unicode(self) -> str:
        """``Desktop.type`` (default: verifies the focused text field and repairs garbled input)."""
        self._empty_editor()
        self.d.type(UNICODE_TEXT)
        got = self._wait_editor_text(UNICODE_TEXT)
        if got != UNICODE_TEXT:
            raise AssertionError(f"typed {UNICODE_TEXT!r} but the editor contains {got!r}")
        return "typed text read back exactly (incl. emoji/CJK)"

    def c_type_verified(self) -> str:
        """``Element.type`` reads the field back and repairs dropped/garbled keystrokes."""
        ed = self._empty_editor()
        repaired: List[bool] = []
        orig = ed.set_text

        def spy(*a: Any, **kw: Any) -> Any:  # set_text is only called by type() to repair
            repaired.append(True)
            return orig(*a, **kw)

        ed.set_text = spy  # type: ignore[method-assign]
        try:
            ed.type(UNICODE_TEXT)
        finally:
            del ed.set_text
        got = self._wait_editor_text(UNICODE_TEXT)
        if got != UNICODE_TEXT:
            raise AssertionError(f"Element.type left {got!r}, expected {UNICODE_TEXT!r}")
        return "exact" + (" (keystrokes were garbled; repaired via read-back)" if repaired else "")

    def c_clipboard(self) -> str:
        if not self.clipboard_saved:
            raise _Skip("your clipboard could not be saved, so it is not overwritten")
        ed = self._editor()
        ed.focus()
        self.d.hotkey("mod+a")
        self.d.hotkey("mod+c")
        got = ""
        for _ in range(20):
            got = self.d.clipboard_get()
            if got.strip() == UNICODE_TEXT:
                break
            self.d.sleep(0.1)
        if got.strip() != UNICODE_TEXT:
            raise AssertionError(f"Ctrl+A, Ctrl+C put {got!r} on the clipboard")
        self.d.clipboard_set("clipboard ✓ round-trip")
        if self.d.clipboard_get() != "clipboard ✓ round-trip":
            raise AssertionError("clipboard_set/clipboard_get round trip failed")
        return "hotkeys + clipboard read/write OK"

    def c_symbols(self) -> str:
        ed = self._editor()
        ed.focus()
        self.d.hotkey("mod+a")
        self.d.press("delete")
        self._settle()
        self.d.type(SYMBOLS_TEXT, mode="keys")
        got = self._wait_editor_text(SYMBOLS_TEXT)
        if got != SYMBOLS_TEXT:
            raise AssertionError(f"key-by-key typing of symbols gave {got!r} (keyboard layout mapping problem)")
        return "every symbol typed key-by-key through the active keyboard layout"

    def c_set_text(self) -> str:
        ed = self._editor()
        ed.set_text(SET_TEXT)
        got = self._editor_text()
        if SET_TEXT not in got:
            raise AssertionError(f"set_text left {got!r}")
        return "set_text verified"

    def c_element_at(self) -> str:
        ed = self._editor()
        el = self.d.element_at(ed.center)
        if el is None:
            raise AssertionError(f"no element at the editor's center {tuple(ed.center)}")
        win = self._win()
        if win.pid is not None and el.node.pid not in (None, win.pid):
            raise AssertionError(f"element at the editor's center belongs to pid {el.node.pid}, editor pid is {win.pid}")
        return f"{el.role} {el.name!r}"

    def c_vision(self) -> str:
        win = self._win()
        r = win.rect
        patch_rect = (r.left + 8, r.top + 4, min(160, r.width - 16), min(28, r.height - 8))
        patch = self.d.screenshot(region=patch_rect)
        patch.save(self.path("03_title_patch.png"))
        m = self.d.locate(patch, confidence=0.95)
        if m is None:
            raise AssertionError("could not find a just-captured screen patch again")
        if abs(m.rect.left - patch_rect[0]) > 2 or abs(m.rect.top - patch_rect[1]) > 2:
            raise AssertionError(f"patch found at {m.rect.as_tuple()}, captured at {patch_rect}")
        return f"found at {m.rect.as_tuple()} (confidence {m.confidence:.3f})"

    def c_window_ops(self) -> str:
        win = self._win()
        prim = self.d.monitors()[0].rect
        target = (prim.left + 80, prim.top + 80, 700, 450)
        win.set_rect(target)
        self.d.sleep(0.6)
        r = win.refresh().rect
        if abs(r.left - target[0]) > 12 or abs(r.top - target[1]) > 12 or abs(r.width - target[2]) > 24 or abs(r.height - target[3]) > 24:
            raise AssertionError(f"set_rect{target} -> window rect {r.as_tuple()}")
        win.minimize()
        self.d.sleep(0.6)
        if not win.refresh().is_minimized:
            raise AssertionError("minimize() did not minimize")
        win.restore()
        self.d.sleep(0.6)
        win.maximize()
        self.d.sleep(0.6)
        if not win.refresh().is_maximized:
            raise AssertionError("maximize() did not maximize")
        win.restore()
        win.activate()
        self.d.sleep(0.6)
        w = win.refresh()
        if w.is_minimized or w.is_maximized:
            raise AssertionError(f"restore() left minimized={w.is_minimized} maximized={w.is_maximized}")
        self.d.screenshot(path=self.path("04_editor.png"))
        return "move/resize, minimize, maximize, restore, activate"

    def c_close_editor(self) -> str:
        win = self._win()
        self._discard_test_text()
        if self.editor_preexisting:
            self.editor_window = None
            return "closed the test tab; left your existing Notepad window open"
        # Ctrl+W closes the test tab; on a clean machine it is Notepad's only tab, so
        # the window closes with it and there is nothing left to close.
        deadline = time.monotonic() + (0 if self.d.backend.name == "mock" else 1.5)
        while True:
            if not win.exists:
                self.editor_window = None
                return "closed with its only tab"
            if time.monotonic() >= deadline:
                break
            self.d.sleep(0.1)
        win.close()
        note = "no save prompt"
        try:
            btn = self.d.find(f'button[name~="{DONT_SAVE}"]', timeout=self.save_prompt_timeout)
            btn.click()
            note = "clicked the 'don't save' button"
        except Exception:
            pass
        if not win.wait_closed(timeout=10):
            raise AssertionError("editor window did not close")
        self.editor_window = None
        return note

    def _discard_test_text(self) -> None:
        """Empty the test tab and close it (Ctrl+W) so nothing is kept in Notepad's session."""
        if self.editor is None or self.editor_window is None:
            return
        try:
            self.editor_window.activate()
            self.editor.focus()
            self.d.hotkey("mod+a")
            self.d.press("delete")
            if sys.platform == "win32":
                self.d.hotkey("ctrl+w")  # Windows 11 Notepad: close the (now empty) test tab
                self.d.sleep(0.5)
        except Exception:
            pass

    # ---------------------------------------------------------------- helpers
    def _win(self) -> Any:
        if self.editor_window is None:
            raise _Skip("editor window was not opened")
        return self.editor_window

    def _editor(self) -> Any:
        if self.editor is None:
            raise _Skip("editor control was not found")
        return self.editor

    def _editor_text(self) -> str:
        ed = self._editor()
        try:
            ed = ed.refresh()
        except Exception:
            pass
        return (ed.value or "").replace("\r\n", "\n")

    def _wait_editor_text(self, expected: str, timeout: float = 2.0) -> str:
        """Poll the editor until it reads ``expected`` (apps process keystrokes asynchronously)."""
        deadline = time.monotonic() + (0 if self.d.backend.name == "mock" else timeout)
        got = self._editor_text()
        while got != expected and time.monotonic() < deadline:
            self.d.sleep(0.1)
            got = self._editor_text()
        return got

    def _settle(self) -> None:
        """Let the editor finish redrawing/loading before keystrokes are sent to it."""
        if self.d.backend.name == "mock":
            return
        try:
            self.d.wait_until_stable(self._win().rect, stable_for=0.4, timeout=3)
        except Exception:
            self.d.sleep(0.5)

    # ------------------------------------------------------------------- main
    def run(self) -> List[Check]:
        from .backends.base import CAP_ACCESSIBILITY, CAP_CLIPBOARD, CAP_KEYBOARD, CAP_MOUSE, CAP_SCREENSHOT, CAP_WINDOWS

        os.makedirs(self.out, exist_ok=True)
        # The user's clipboard is saved in every format the backend can snapshot (images,
        # file lists, rich text -- not only text) and put back at the end. If it cannot
        # be saved, the checks that overwrite it are skipped rather than destroy it.
        saved_clip: Any = None
        self.clipboard_saved = False
        be = self.d.backend
        if be.has(CAP_CLIPBOARD):
            try:
                save = getattr(be, "save_clipboard", None)
                saved_clip = save() if callable(save) else be.get_clipboard()
                self.clipboard_saved = True
            except Exception:
                saved_clip = None
        start_pos = self.d.position() if self.d.backend.has(CAP_MOUSE) else None
        try:
            self.run_check("environment", self.c_environment)
            self.run_check("screenshot", self.c_screenshot, needs=CAP_SCREENSHOT)
            self.run_check("mouse movement", self.c_mouse, needs=CAP_MOUSE)
            self.run_check("window list", self.c_windows, needs=CAP_WINDOWS)
            if self.run_check("launch editor", self.c_launch_editor, needs=CAP_WINDOWS):
                self.run_check("accessibility tree + selector", self.c_accessibility, needs=CAP_ACCESSIBILITY)
                self.run_check("raw keystrokes (unverified, informational)", self.c_type_raw, needs=CAP_KEYBOARD)
                self.run_check("unicode typing (Desktop.type)", self.c_type_unicode, needs=CAP_KEYBOARD)
                self.run_check("unicode typing (Element.type, verified)", self.c_type_verified, needs=CAP_KEYBOARD)
                self.run_check("hotkeys + clipboard", self.c_clipboard, needs=CAP_CLIPBOARD)
                self.run_check("symbols via keyboard layout", self.c_symbols, needs=CAP_KEYBOARD)
                self.run_check("set_text (native)", self.c_set_text, needs=CAP_ACCESSIBILITY)
                self.run_check("element_at", self.c_element_at, needs=CAP_ACCESSIBILITY)
                self.run_check("vision locate", self.c_vision, needs=CAP_SCREENSHOT)
                self.run_check("window operations", self.c_window_ops, needs=CAP_WINDOWS)
                self.run_check("close editor without saving", self.c_close_editor, needs=CAP_WINDOWS)
        finally:
            if self.editor_window is not None:  # best effort cleanup after a failure
                try:
                    self._discard_test_text()
                    if self.editor_preexisting:
                        raise _Skip("the user's own window: never closed")
                    self.editor_window.close()
                    btn = self.d.find(f'button[name~="{DONT_SAVE}"]', timeout=self.save_prompt_timeout)
                    btn.click()
                except Exception:
                    pass
            if self.clipboard_saved:
                try:
                    restore = getattr(be, "restore_clipboard", None)
                    if callable(restore):
                        restore(saved_clip)
                    else:
                        be.set_clipboard(saved_clip if saved_clip is not None else "")
                except Exception as exc:
                    self.log(f"  WARNING  could not restore your clipboard: {type(exc).__name__}: {exc}")
            if start_pos is not None:
                try:
                    self.d.move(start_pos)
                except Exception:
                    pass
            if self.temp_doc is not None:  # TextEdit autosaves into it
                shutil.rmtree(os.path.dirname(self.temp_doc), ignore_errors=True)
            self.write_report()
        return self.checks

    def write_report(self) -> None:
        counts = {s: sum(1 for c in self.checks if c.status == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
        data = {"environment": self.env, "summary": counts, "checks": [c.to_dict() for c in self.checks]}
        with open(self.path("report.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        lines = [
            "locum self-test report",
            f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            *(f"{k}: {v}" for k, v in self.env.items() if k != "monitors"),
            "monitors: " + "; ".join(json.dumps(m) for m in self.env.get("monitors", [])),
            "",
            f"PASS {counts['PASS']}  WARN {counts['WARN']}  FAIL {counts['FAIL']}  SKIP {counts['SKIP']}",
            "",
        ]
        for c in self.checks:
            lines.append(f"[{c.status}] {c.name} ({c.seconds:.1f}s)")
            if c.detail:
                lines.extend("    " + ln for ln in c.detail.rstrip().splitlines())
        with open(self.path("report.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


class _Skip(Exception):
    pass


class _Warn(Exception):
    """A check that measured something worth knowing but is not a library failure."""


def _editor_for_platform(backend: str = "") -> Any:
    if sys.platform == "win32" or backend in ("mock", "windows"):
        return "notepad.exe", re.compile(r"(?i)notepad|bloc de notas|editor")
    if sys.platform == "darwin":
        # Without a document TextEdit shows its Open panel (iCloud document browser)
        # instead of a new window: open an empty file of our own (removed afterwards).
        path = os.path.join(tempfile.mkdtemp(prefix="locum_selftest_"), "locum selftest.txt")
        open(path, "w").close()
        return ["open", "-n", "-a", "TextEdit", path], re.compile(re.escape("locum selftest"))
    for cmd, pat in (("gedit", "gedit|untitled"), ("mousepad", "mousepad|untitled"), ("kate", "kate|untitled")):
        import shutil

        if shutil.which(cmd):
            return cmd, re.compile("(?i)" + pat)
    raise _Skip("no supported text editor (gedit, mousepad, kate) installed")


def _mock_desktop() -> Any:
    """The simulated desktop, with a Notepad-like editor that appears on launch (for dry runs/tests)."""
    from . import Desktop
    from .backends.mock import MockBackend

    class SelfTestMock(MockBackend):
        def launch(self, command: Any, **kwargs: Any) -> Any:
            super().launch(command, **kwargs)
            win = self.add_window("Untitled - Notepad", (200, 150, 640, 420), app="notepad.exe", pid=9001)
            ed = win.add("edit", "Text editor", (8, 36, 620, 370), value="", fill=(255, 255, 255))
            ed.attrs["multiline"] = True
            return None

    be = SelfTestMock(width=1600, height=900)
    be.add_window("Taskbar", (0, 860, 1600, 40), app="explorer.exe", pid=10)
    return Desktop(be, sleep=lambda s: None)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m locum.selftest", description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", help="backend name (default: this platform's); 'mock' = simulated dry run")
    ap.add_argument("--yes", "-y", action="store_true", help="start without asking")
    ap.add_argument("--out", help="output folder (default: ./locum_selftest_<timestamp>)")
    args = ap.parse_args(argv)

    from . import Desktop

    out = args.out or os.path.abspath(time.strftime("locum_selftest_%Y%m%d_%H%M%S"))
    if not args.yes:
        print("locum self-test: it will open a text editor, type into it, use the clipboard and")
        print("move/resize that window. Do not touch the mouse or keyboard until it finishes.")
        print("Abort anytime by slamming the mouse into a screen corner.")
        if input("Start? [y/N] ").strip().lower() not in ("y", "yes", "s", "si", "sí"):
            print("cancelled")
            return 1
        for i in (3, 2, 1):
            print(f"  starting in {i}...")
            time.sleep(1)
    d = _mock_desktop() if (args.backend or "").lower() in ("mock", "demo") else Desktop(args.backend)
    with d:
        print(f"backend: {d.backend.name}")
        st = SelfTest(d, out)
        try:
            st.run()
        except Exception as exc:  # fail-safe / Ctrl+C: the report is still written
            print(f"stopped: {type(exc).__name__}: {exc}")
    fails = sum(1 for c in st.checks if c.status == "FAIL")
    warns = sum(1 for c in st.checks if c.status == "WARN")
    print(f"\nreport: {os.path.join(out, 'report.txt')}")
    result = "ALL PASSED" if fails == 0 else f"{fails} FAILED"
    print("RESULT:", result + (f" ({warns} warning{'s' if warns != 1 else ''})" if warns else ""))
    return 0 if fails == 0 else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
