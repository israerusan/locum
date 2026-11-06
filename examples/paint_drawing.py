"""Draw a star and a spiral in Paint with mouse drags.

The canvas is found by accessibility selector when the app exposes it; otherwise
the drawing goes into the middle of the window's client area (below the ribbon).

Real Windows desktop (launches Paint and draws with its current tool, normally the pencil/brush):

    python examples/paint_drawing.py

Built-in mock Paint (no real input, safe anywhere, used by the tests; its canvas
renders the strokes into screenshots):

    python examples/paint_drawing.py --backend mock

Fail-safe: move the mouse into any screen corner to abort a real run (a drag in
progress releases the button first).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Callable, List, Optional, Tuple

from locum import Desktop, ElementNotFound, Rect

# Tried in order; the first one that matches inside the Paint window is the canvas.
CANVAS_SELECTORS = [
    'group#image',  # Windows 11 Paint: group "Using Brush tool on Canvas", automation id "image"
    'group[name*="canvas" i], pane[name*="canvas" i], custom[name*="canvas" i], image[name*="canvas" i]',
    '[class_name="MSPaintView"]',  # classic mspaint
]

StepHook = Optional[Callable[[Desktop, str], None]]


def find_canvas(win) -> Rect:
    for sel in CANVAS_SELECTORS:
        try:
            bounds = win.find(sel, timeout=0).bounds
        except ElementNotFound:
            continue
        if bounds is not None and bounds.width > 100 and bounds.height > 100:
            return bounds
    # Fallback: the window minus the title bar/ribbon on top and the status bar below.
    r = win.rect
    top = r.top + int(r.height * 0.30)
    return Rect(r.left + 20, top, r.width - 40, r.bottom - top - 40)


def star(cx: int, cy: int, radius: int) -> List[Tuple[int, int]]:
    """A five-pointed star drawn in one go: every second vertex of a pentagon."""
    pts = [(cx + radius * math.sin(2 * math.pi * k / 5), cy - radius * math.cos(2 * math.pi * k / 5)) for k in range(5)]
    return [(int(pts[(2 * i) % 5][0]), int(pts[(2 * i) % 5][1])) for i in range(6)]


def spiral(cx: int, cy: int, radius: int, turns: float = 3.0, steps: int = 90) -> List[Tuple[int, int]]:
    return [(int(cx + radius * t / steps * math.cos(turns * 2 * math.pi * t / steps)),
             int(cy + radius * t / steps * math.sin(turns * 2 * math.pi * t / steps))) for t in range(steps + 1)]


def run(d: Desktop, *, on_step: StepHook = None) -> int:
    """Launch Paint, draw the figures, return the number of drags performed."""
    step = on_step or (lambda desk, label: None)
    already_open = {w.handle for w in d.windows("*Paint*")}
    paint = d.launch("mspaint.exe", wait_window="*Paint*", timeout=20)
    if paint.handle in already_open:  # never draw into a Paint window you already had open
        raise SystemExit("Paint did not open a new window; close yours and retry")
    paint.activate()
    canvas = find_canvas(paint)
    step(d, "Paint is open")

    size = min(canvas.width, canvas.height) // 2 - 20
    left = (canvas.left + canvas.width // 4, canvas.top + canvas.height // 2)
    right = (canvas.left + 3 * canvas.width // 4, canvas.top + canvas.height // 2)
    radius = min(size, canvas.width // 4 - 20)
    drags = 0

    # 1. A star: one d.drag() per edge (press, move, release).
    pts = star(left[0], left[1], radius)
    for a, b in zip(pts, pts[1:]):
        d.drag(a, b, duration=0.15)
        drags += 1
        step(d, f"star edge {drags}")

    # 2. A spiral: one continuous stroke with the button held down.
    path = spiral(right[0], right[1], radius)
    d.move(path[0])
    d.mouse_down()
    try:
        for i, p in enumerate(path[1:], 1):
            d.move(p, duration=0.01)
            if i % 10 == 0:
                step(d, "spiral")
    finally:
        d.mouse_up()  # never leave the button stuck, even on a fail-safe abort
    drags += 1
    step(d, "done")
    return drags


# ------------------------------------------------------------------------------ mock Paint


def build_mock_desktop(width: int = 800, height: int = 600) -> Desktop:
    """A mock desktop where ``launch("mspaint.exe")`` opens a window with a drawable canvas.

    The stock mock backend records mouse events but paints nothing; this subclass
    turns left-button moves over the canvas into line segments and renders them.
    """
    from PIL import ImageDraw

    from locum.backends.mock import MockBackend
    from locum.types import MouseButton

    class PaintBackend(MockBackend):
        canvas = None
        segments: List[Tuple[Tuple[int, int], Tuple[int, int]]] = []

        def launch(self, command, **kwargs):
            super().launch(command, **kwargs)
            win = self.add_window("Untitled - Paint", (40, 30, width - 80, height - 60), app="mspaint")
            win.add_text("File   Edit   View      Pencil  Brush  Shapes", (8, 32, width - 96, 40), fill=(230, 233, 240))
            self.canvas = win.add("pane", "Canvas", (12, 80, width - 104, height - 160), fill=(255, 255, 255))
            self.segments = []
            return None

        def mouse_move(self, x, y):
            before = self.mouse_position()
            super().mouse_move(x, y)
            after = self.mouse_position()
            c = self.canvas
            if (c is not None and MouseButton.LEFT in self.pressed_buttons and self._down_target is c
                    and c.screen_rect.contains(before) and c.screen_rect.contains(after)):
                self.segments.append(((before.x, before.y), (after.x, after.y)))

        def render(self):
            img = super().render()
            draw = ImageDraw.Draw(img)
            for a, b in self.segments:
                draw.line([a, b], fill=(20, 20, 20), width=3)
            return img

    return Desktop(PaintBackend(width=width, height=height), sleep=lambda s: None, default_timeout=2)


# ------------------------------------------------------------------------------ entry point


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--backend", choices=["auto", "mock", "windows"], default=None,
                   help="mock = built-in fake Paint; default: $LOCUM_BACKEND, else the real desktop")
    args = p.parse_args(argv)
    backend = (args.backend or os.environ.get("LOCUM_BACKEND") or "auto").lower()

    if backend in ("mock", "demo"):
        d = build_mock_desktop()
    elif sys.platform != "win32":
        print("The real-desktop run needs Windows Paint; try --backend mock", file=sys.stderr)
        return 3
    else:
        print("Drawing in the real Paint. Move the mouse into a screen corner to abort.")
        d = Desktop()  # fail-safe is on by default

    with d:
        drags = run(d)  # Paint is left open, unsaved: close it yourself
        segments = getattr(d.backend, "segments", None)
    print(f"performed {drags} drags" + (f" ({len(segments)} line segments on the mock canvas)" if segments is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
