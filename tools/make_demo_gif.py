"""Record the example demos on locum's mock desktop as an animated GIF.

    python tools/make_demo_gif.py                 # -> docs/demo.gif
    python tools/make_demo_gif.py out.gif --width 560

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
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPTION = "locum demo (mock desktop)"
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


def frame(shot: Image.Image, cursor: Tuple[int, int], label: str, width: int) -> Image.Image:
    """Screenshot + an arrow cursor, scaled to ``width``, under a caption bar."""
    img = shot.convert("RGB")
    draw = ImageDraw.Draw(img)
    x, y = cursor
    arrow = [(x, y), (x, y + 18), (x + 5, y + 14), (x + 9, y + 22), (x + 12, y + 20), (x + 8, y + 13), (x + 14, y + 13)]
    draw.polygon(arrow, fill=(255, 255, 255), outline=(0, 0, 0))
    height = round(img.height * width / img.width)
    img = img.resize((width, height), Image.LANCZOS)
    out = Image.new("RGB", (width, height + BAR), (24, 24, 28))
    out.paste(img, (0, BAR))
    bar = ImageDraw.Draw(out)
    bar.text((10, 8), CAPTION, fill=(255, 200, 80), font=_font(15))
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Record the demos on the mock desktop as a GIF.")
    p.add_argument("output", nargs="?", default=str(ROOT / "docs" / "demo.gif"))
    p.add_argument("--width", type=int, default=640, help="frame width in pixels (default 640)")
    args = p.parse_args(argv)

    frames = record(args.width)
    # One shared palette keeps the file small and the colours stable between frames.
    first, last = frames[0][0], frames[-1][0]
    sheet = Image.new("RGB", (first.width, first.height + last.height))
    sheet.paste(first, (0, 0))
    sheet.paste(last, (0, first.height))
    palette = sheet.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    images = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f, _ in frames]
    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(out, save_all=True, append_images=images[1:], duration=[ms for _, ms in frames],
                   loop=0, optimize=True, disposal=1)
    print(f"wrote {out} ({len(images)} frames, {out.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
