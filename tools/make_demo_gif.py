"""Record the example demos as an animated GIF.

    python tools/make_demo_gif.py                 # mock desktop -> docs/demo.gif
    python tools/make_demo_gif.py out.gif --width 560
    python tools/make_demo_gif.py --real          # the real Windows Calculator and Paint

``--real`` drives the real desktop (launches Calculator and Paint, clicks and
drags in them, then closes only the windows it opened, answering "Don't save"
in Paint). Frames are the app's own window; the caption says so.

Runs ``examples/calculator_selectors.py`` and ``examples/paint_drawing.py``
against their built-in mock apps (no real input, nothing on your screen), grabs
``Desktop.screenshot()`` after every step, draws the mock cursor, and adds a
caption bar saying it is the mock desktop. Needs only Pillow.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPTION = "locum demo (mock desktop)"
REAL_CAPTION = "locum driving Windows 11 Calculator and Paint"
BAR = 30


def load_example(name: str):
    path = ROOT / "examples" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_demo_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def frame(shot: Image.Image, cursor: Tuple[int, int], label: str, width: int, caption: str = CAPTION,
          height: Optional[int] = None) -> Image.Image:
    """Screenshot + an arrow cursor, scaled to ``width`` (and, given ``height``, fitted into
    ``width`` x ``height`` and centred), under a caption bar."""
    img = shot.convert("RGB")
    draw = ImageDraw.Draw(img)
    x, y = cursor
    arrow = [(x, y), (x, y + 18), (x + 5, y + 14), (x + 9, y + 22), (x + 12, y + 20), (x + 8, y + 13), (x + 14, y + 13)]
    draw.polygon(arrow, fill=(255, 255, 255), outline=(0, 0, 0))
    scale = width / img.width if height is None else min(width / img.width, height / img.height)
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    box_h = img.height if height is None else height
    out = Image.new("RGB", (width, box_h + BAR), (24, 24, 28))
    out.paste(img, ((width - img.width) // 2, BAR + (box_h - img.height) // 2))
    bar = ImageDraw.Draw(out)
    bar.text((10, 8), caption, fill=(255, 200, 80), font=_font(15))
    bar.text((width - 10, 8), label, fill=(220, 220, 220), font=_font(14), anchor="ra")
    return out


def record(width: int) -> List[Tuple[Image.Image, int]]:
    frames: List[Tuple[Image.Image, int]] = []

    def capture(d, label: str, ms: int = 220) -> None:
        frames.append((frame(d.screenshot(), tuple(d.position()), label, width), ms))

    calc = load_example("calculator_selectors")
    with calc.build_mock_desktop() as d:
        calc.run(d, "12+30", on_step=capture)
        frames[-1] = (frames[-1][0], 1800)  # hold on the answer

    paint = load_example("paint_drawing")
    with paint.build_mock_desktop() as d:
        paint.run(d, on_step=lambda desk, label: capture(desk, label, 150))
        frames[-1] = (frames[-1][0], 2500)
    return frames


def _new_windows(d, before, app_word: str):
    return [w for w in d.windows() if w.handle not in before and app_word in w.title]


def _close_launched(d, before, app_word: str) -> None:
    """Close only the windows this run opened (Paint asks to save: answer "Don't save")."""
    import time

    for w in _new_windows(d, before, app_word):
        try:
            w.close()
        except Exception:
            continue
        for _ in range(20):
            time.sleep(0.3)
            if not any(x.handle == w.handle for x in d.windows()):
                break
            try:
                w.find("button#SecondaryButton", timeout=0).click()  # "Don’t save"
            except Exception:
                pass


def record_real(width: int) -> List[Tuple[Image.Image, int]]:
    from locum import Desktop

    frames: List[Tuple[Image.Image, int]] = []
    d = Desktop()

    def capture(desk, label: str, ms: int = 260) -> None:
        aw = desk.backend.active_window()
        region = aw.rect.intersect(desk.virtual_rect()) if aw is not None else desk.virtual_rect()
        shot = desk.screenshot(region)
        pos = desk.position()
        frames.append((frame(shot, (pos.x - region.left, pos.y - region.top), label, width, REAL_CAPTION,
                             height=round(width * 2 / 3)), ms))

    calc = load_example("calculator_selectors")
    before = {w.handle for w in d.windows()}
    try:
        calc.run(d, "12+30", on_step=capture)
        frames[-1] = (frames[-1][0], 1800)
    finally:
        _close_launched(d, before, "Calculator")
    paint = load_example("paint_drawing")
    before = {w.handle for w in d.windows()}
    try:
        paint.run(d, on_step=lambda desk, label: capture(desk, label, 180))
        frames[-1] = (frames[-1][0], 2500)
    finally:
        _close_launched(d, before, "Paint")
    return frames


def _uniform(frames: List[Tuple[Image.Image, int]]) -> List[Tuple[Image.Image, int]]:
    """Pad frames of different heights (Calculator vs Paint) to one size, centred."""
    w = max(f.width for f, _ in frames)
    h = max(f.height for f, _ in frames)
    out = []
    for f, ms in frames:
        if f.size != (w, h):
            canvas = Image.new("RGB", (w, h), (24, 24, 28))
            canvas.paste(f, ((w - f.width) // 2, (h - f.height) // 2))
            f = canvas
        out.append((f, ms))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Record the demos on the mock desktop as a GIF.")
    p.add_argument("output", nargs="?", default=str(ROOT / "docs" / "demo.gif"))
    p.add_argument("--width", type=int, default=640, help="frame width in pixels (default 640)")
    p.add_argument("--real", action="store_true",
                   help="drive the real Windows Calculator and Paint instead of the mock desktop")
    args = p.parse_args(argv)

    frames = _uniform(record_real(args.width) if args.real else record(args.width))
    # One shared palette keeps the file small and the colours stable between frames.
    first, last = frames[0][0], frames[-1][0]
    sheet = Image.new("RGB", (first.width, first.height + last.height))
    sheet.paste(first, (0, 0))
    sheet.paste(last, (0, first.height))
    palette = sheet.quantize(colors=128 if args.real else 64, method=Image.Quantize.MEDIANCUT)
    images = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f, _ in frames]
    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(out, save_all=True, append_images=images[1:], duration=[ms for _, ms in frames],
                   loop=0, optimize=True, disposal=1)
    print(f"wrote {out} ({len(images)} frames, {out.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
