"""locum in 60 seconds: windows, selectors, typing, screenshots and vision.

Run it against the built-in demo desktop (safe anywhere, no real input):

    LOCUM_BACKEND=mock python examples/basic_automation.py

or for real (opens Notepad / TextEdit / gedit and types into it):

    python examples/basic_automation.py

Move the mouse into any screen corner to abort (fail-safe).
"""

from __future__ import annotations

import os
import sys
import tempfile

from locum import Desktop, Humanize, WindowNotFound


def make_desktop() -> Desktop:
    """The real desktop, or the in-memory demo desktop when LOCUM_BACKEND=mock."""
    if os.environ.get("LOCUM_BACKEND", "").lower() in ("mock", "demo"):
        from locum.cli import DemoBackend

        return Desktop(DemoBackend(), humanize=Humanize(seed=7), sleep=lambda s: None)
    return Desktop(humanize=True)  # human-like Bezier mouse motion


def editor_for_platform():
    """(launch command, window title glob) of a plain text editor."""
    if sys.platform == "win32":
        return "notepad.exe", "*Notepad*"
    if sys.platform == "darwin":
        return ["open", "-a", "TextEdit"], "Untitled*"
    return "gedit", "*gedit*"


def main() -> int:
    with make_desktop() as d:
        width, height = d.size()
        print(f"screen {width}x{height}, cursor at {tuple(d.position())}, backend {d.backend.name}")

        print("windows (front-most first):")
        for w in d.windows():
            print(f"  {w.title!r:40} app={w.app}")

        # 1. Find (or start) a text editor. Titles accept exact text, substrings, globs or regexes.
        command, title = editor_for_platform()
        try:
            win = d.window(title, timeout=0)
        except WindowNotFound:
            try:
                win = d.window("*Notepad*", timeout=0)  # the demo desktop's editor
            except WindowNotFound:
                win = d.launch(command, wait_window=title, timeout=20)
        win.activate()
        print(f"using window {win.title!r} at {win.rect}")

        # 2. Accessibility selectors: CSS-like, robust to layout changes.
        editor = win.find("edit, document")
        d.move(editor)                       # humanized motion when enabled
        editor.click()
        d.type("Hello from locum!\n")      # layout-independent unicode typing
        d.type("Selectors > coordinates.\n")
        d.hotkey("mod+end")                  # mod = ctrl, or cmd on macOS
        print("editor now contains:", repr(editor.refresh().value))

        # 3. Screenshots and template matching (alpha channel of a needle = mask).
        shot_path = os.path.join(tempfile.gettempdir(), "locum_window.png")
        win.screenshot().save(shot_path)
        title_bar = d.screenshot((win.rect.left, win.rect.top, min(160, win.rect.width), 24))
        match = d.locate(title_bar, confidence=0.95, region=win.rect)   # region= keeps it fast
        print(f"window screenshot saved to {shot_path}")
        if match is not None:
            print(f"found the title bar again at {match.rect} (confidence {match.confidence:.3f})")

        # 4. What would an LLM see? A numbered, set-of-marks view of the screen.
        from locum.llm import observe

        obs = observe(d, window=win, screenshot=False)
        print(obs.text().splitlines()[-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
