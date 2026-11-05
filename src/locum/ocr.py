"""Optical character recognition with pluggable engines.

Engines are tiny adapters with a single method, ``read(image) -> List[OCRWord]``.
Three adapters are built in and imported lazily (none is a hard dependency).
The ``locum[ocr]`` extra installs only `pytesseract`; the other engines are
optional, separate installs:

* ``"tesseract"`` -- `pytesseract` (``pip install locum[ocr]``) + the Tesseract binary (system install)
* ``"rapidocr"``  -- ``pip install rapidocr-onnxruntime`` (or ``rapidocr``; pure pip, no system deps)
* ``"easyocr"``   -- ``pip install easyocr`` (pulls in PyTorch)

``get_ocr_engine()`` without a name picks the configured default (see
:func:`set_default_ocr_engine` or the ``LOCUM_OCR_ENGINE`` environment
variable), else the first built-in engine that is installed, else any other
registered engine. Register your own (a cloud OCR API, an LLM, a test fake)
with :func:`register_ocr_engine`.

:func:`find_text` matches single words or multi-word phrases on one line,
optionally case-sensitive, by regex, or fuzzily (``difflib`` ratio), and
returns :class:`~locum.types.Match` objects in image coordinates + ``offset``.
"""

from __future__ import annotations

import difflib
import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .errors import NotSupported
from .types import Match, Rect

try:  # Python 3.8+: typing.Protocol
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

__all__ = [
    "OCRWord",
    "OCREngine",
    "register_ocr_engine",
    "unregister_ocr_engine",
    "available_ocr_engines",
    "set_default_ocr_engine",
    "get_ocr_engine",
    "read_words",
    "read_lines",
    "read_text",
    "find_text",
    "TesseractEngine",
    "RapidOCREngine",
    "EasyOCREngine",
]


@dataclass
class OCRWord:
    """One recognized word.

    Attributes:
        text: the word.
        rect: bounding box in image pixels.
        confidence: recognition confidence in ``0..1``.
        line: index of the text line (reading order, 0-based) the word belongs to.
    """

    text: str
    rect: Rect
    confidence: float
    line: int = 0

    def to_dict(self) -> dict:
        return {"text": self.text, "rect": self.rect.to_dict(), "confidence": round(float(self.confidence), 4), "line": self.line}


@runtime_checkable
class OCREngine(Protocol):
    """Anything with ``read(image) -> List[OCRWord]`` (``image`` is an RGB PIL image)."""

    def read(self, image: Any) -> List[OCRWord]: ...


EngineFactory = Callable[[], OCREngine]

_BUILTIN_ORDER = ("tesseract", "rapidocr", "easyocr")
_lock = threading.RLock()
_factories: Dict[str, Any] = {}  # name -> factory callable or engine instance
_instances: Dict[str, OCREngine] = {}
_default_name: Optional[str] = None

_INSTALL_HINT = (
    "No OCR engine is available. Install one of: "
    "`pip install locum[ocr]` plus the Tesseract binary (apt install tesseract-ocr / "
    "brew install tesseract / UB-Mannheim installer on Windows), "
    "`pip install rapidocr-onnxruntime`, or `pip install easyocr`; "
    "or register your own with locum.ocr.register_ocr_engine()."
)


# ---------------------------------------------------------------------- helpers
def _to_rgb(image: Any) -> Any:
    from .vision import load_image

    img = load_image(image)
    return img if img.mode == "RGB" else img.convert("RGB")


def _quad_to_rect(quad: Any) -> Rect:
    """Axis-aligned bounding rect of a polygon ``[[x, y], ...]`` (or ``[x0,y0,x1,y1]``)."""
    pts = [tuple(p) for p in quad] if quad is not None and len(quad) and hasattr(quad[0], "__len__") else None
    if pts is None:
        x0, y0, x1, y1 = [float(v) for v in quad]
        pts = [(x0, y0), (x1, y1)]
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    left, top = int(round(min(xs))), int(round(min(ys)))
    return Rect(left, top, max(1, int(round(max(xs))) - left), max(1, int(round(max(ys))) - top))


def _split_line(text: str, rect: Rect, confidence: float) -> List[OCRWord]:
    """Split a recognized *line* box into word boxes proportionally to character counts."""
    text = text.strip()
    if not text:
        return []
    n = len(text)
    words = []
    for m in re.finditer(r"\S+", text):
        x0 = rect.left + int(round(rect.width * m.start() / n))
        x1 = rect.left + int(round(rect.width * m.end() / n))
        words.append(OCRWord(m.group(0), Rect(x0, rect.top, max(1, x1 - x0), rect.height), float(confidence)))
    return words


def _assign_lines(words: List[OCRWord]) -> List[OCRWord]:
    """Group words into lines by vertical overlap and number them in reading order.

    Words are clustered when their vertical centers fall inside a line's band;
    lines are ordered top-to-bottom, words inside a line left-to-right.
    """
    if not words:
        return []
    order = sorted(words, key=lambda w: (w.rect.top + w.rect.height / 2.0, w.rect.left))
    lines: List[List[OCRWord]] = []
    bands: List[Tuple[float, float]] = []  # (center, half height)
    for w in order:
        cy = w.rect.top + w.rect.height / 2.0
        placed = False
        for i in range(len(lines) - 1, max(-1, len(lines) - 4), -1):
            center, half = bands[i]
            if abs(cy - center) <= max(half, w.rect.height / 2.0) * 0.6:
                lines[i].append(w)
                n = len(lines[i])
                bands[i] = ((center * (n - 1) + cy) / n, max(half, w.rect.height / 2.0))
                placed = True
                break
        if not placed:
            lines.append([w])
            bands.append((cy, w.rect.height / 2.0))
    result = []
    for idx, ln in enumerate(sorted(lines, key=lambda ws: min(x.rect.top for x in ws))):
        for w in sorted(ln, key=lambda x: x.rect.left):
            result.append(OCRWord(w.text, w.rect, w.confidence, idx))
    return result


# ---------------------------------------------------------------------- engines
class TesseractEngine:
    """Tesseract via ``pytesseract`` (word-level boxes and line structure).

    Args:
        lang: Tesseract language(s), e.g. ``"eng"`` or ``"eng+deu"``.
        config: extra Tesseract CLI flags (default ``"--oem 3 --psm 3"``;
            ``--psm 11`` can be better for sparse UI text).
        upscale: resize factor applied before recognition; screen text is small
            and Tesseract is tuned for ~30px glyphs, so 2x helps a lot. Boxes are
            mapped back to original pixels.
        min_confidence: drop words below this confidence (0..1).
        tesseract_cmd: path of the ``tesseract`` executable if not on PATH.
    """

    name = "tesseract"

    def __init__(
        self,
        lang: str = "eng",
        config: str = "--oem 3 --psm 3",
        upscale: float = 2.0,
        min_confidence: float = 0.0,
        tesseract_cmd: Optional[str] = None,
    ):
        try:
            import pytesseract  # type: ignore
        except ImportError as exc:
            raise NotSupported("Tesseract OCR needs pytesseract: pip install locum[ocr]") from exc
        cmd = tesseract_cmd or os.environ.get("TESSERACT_CMD")
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:  # TesseractNotFoundError, OSError
            raise NotSupported(
                "The tesseract executable was not found. Install it (apt install tesseract-ocr / "
                "brew install tesseract / https://github.com/UB-Mannheim/tesseract) or set TESSERACT_CMD."
            ) from exc
        self._pt = pytesseract
        self.lang = lang
        self.config = config
        self.upscale = max(1.0, float(upscale))
        self.min_confidence = float(min_confidence)

    def read(self, image: Any) -> List[OCRWord]:
        from PIL import Image, ImageOps

        img = _to_rgb(image)
        gray = ImageOps.grayscale(img)
        # Tesseract prefers dark text on light background.
        import numpy as np

        if float(np.asarray(gray).mean()) < 110:
            gray = ImageOps.invert(gray)
        s = self.upscale
        if s != 1.0:
            gray = gray.resize((max(1, int(gray.width * s)), max(1, int(gray.height * s))), getattr(Image, "Resampling", Image).LANCZOS)
        data = self._pt.image_to_data(gray, lang=self.lang, config=self.config, output_type=self._pt.Output.DICT)
        words: List[OCRWord] = []
        line_ids: Dict[Tuple[int, int, int, int], int] = {}
        for i, txt in enumerate(data.get("text", [])):
            txt = (txt or "").strip()
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            if not txt or conf < 0:
                continue
            conf = max(0.0, min(1.0, conf / 100.0))
            if conf < self.min_confidence:
                continue
            key = (int(data["page_num"][i]), int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
            line = line_ids.setdefault(key, len(line_ids))
            left, top = int(data["left"][i] / s), int(data["top"][i] / s)
            w, h = max(1, int(round(data["width"][i] / s))), max(1, int(round(data["height"][i] / s)))
            words.append(OCRWord(txt, Rect(left, top, w, h), conf, line))
        return _renumber_lines(words)


class RapidOCREngine:
    """RapidOCR (PaddleOCR models on ONNX runtime). Line results are split into words."""

    name = "rapidocr"

    def __init__(self, **kwargs: Any):
        engine = None
        errors = []
        for mod_name in ("rapidocr", "rapidocr_onnxruntime"):
            try:
                mod = __import__(mod_name, fromlist=["RapidOCR"])
                engine = mod.RapidOCR(**kwargs)
                break
            except ImportError as exc:
                errors.append(str(exc))
        if engine is None:
            raise NotSupported("RapidOCR is not installed: pip install rapidocr-onnxruntime")
        self._engine = engine

    def read(self, image: Any) -> List[OCRWord]:
        import numpy as np

        arr = np.asarray(_to_rgb(image))
        out = self._engine(arr)
        triples: List[Tuple[Any, str, float]] = []
        if isinstance(out, tuple):  # rapidocr_onnxruntime: (result, elapse)
            result = out[0] or []
            for item in result:
                box, text, score = item[0], item[1], item[2]
                triples.append((box, str(text), float(score)))
        else:  # rapidocr >= 2: RapidOCROutput with boxes / txts / scores
            boxes = getattr(out, "boxes", None)
            txts = getattr(out, "txts", None) or ()
            scores = getattr(out, "scores", None) or ()
            if boxes is not None:
                for box, text, score in zip(boxes, txts, scores):
                    triples.append((box, str(text), float(score)))
        words: List[OCRWord] = []
        for box, text, score in triples:
            words.extend(_split_line(text, _quad_to_rect(box), score))
        return _assign_lines(words)


class EasyOCREngine:
    """EasyOCR (PyTorch). Line/phrase results are split into words."""

    name = "easyocr"

    def __init__(self, languages: Sequence[str] = ("en",), gpu: bool = False, **kwargs: Any):
        try:
            import easyocr  # type: ignore
        except ImportError as exc:
            raise NotSupported("EasyOCR is not installed: pip install easyocr") from exc
        self._reader = easyocr.Reader(list(languages), gpu=gpu, verbose=False, **kwargs)

    def read(self, image: Any) -> List[OCRWord]:
        import numpy as np

        arr = np.asarray(_to_rgb(image))
        words: List[OCRWord] = []
        for box, text, score in self._reader.readtext(arr):
            words.extend(_split_line(str(text), _quad_to_rect(box), float(score)))
        return _assign_lines(words)


def _renumber_lines(words: List[OCRWord]) -> List[OCRWord]:
    """Renumber engine-provided line ids in reading order (top of each line)."""
    if not words:
        return []
    tops: Dict[int, int] = {}
    lefts: Dict[int, int] = {}
    for w in words:
        tops[w.line] = min(tops.get(w.line, w.rect.top), w.rect.top)
        lefts[w.line] = min(lefts.get(w.line, w.rect.left), w.rect.left)
    order = sorted(tops, key=lambda k: (tops[k], lefts[k]))
    remap = {old: new for new, old in enumerate(order)}
    out = [OCRWord(w.text, w.rect, w.confidence, remap[w.line]) for w in words]
    out.sort(key=lambda w: (w.line, w.rect.left))
    return out


# --------------------------------------------------------------------- registry
def register_ocr_engine(name: str, factory: Union[EngineFactory, OCREngine], *, default: bool = False) -> None:
    """Register an OCR engine under ``name``.

    Args:
        name: lookup key (case-insensitive).
        factory: zero-argument callable returning an engine, **or** an engine
            instance (anything with ``read(image)``). Factories are called once
            on first use and the instance is cached. Factories should raise
            ``ImportError``/:class:`NotSupported` when their dependency is missing.
        default: also make it the default for ``get_ocr_engine()``.

    Re-registering a name replaces it (and drops the cached instance).
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("OCR engine name must not be empty")
    if not callable(factory) and not hasattr(factory, "read"):
        raise TypeError("factory must be a callable returning an engine, or an object with read(image)")
    with _lock:
        _factories[key] = factory
        _instances.pop(key, None)
        if default:
            set_default_ocr_engine(key)


def unregister_ocr_engine(name: str) -> None:
    """Remove a registered engine (no error if unknown)."""
    global _default_name
    key = name.strip().lower()
    with _lock:
        _factories.pop(key, None)
        _instances.pop(key, None)
        if _default_name == key:
            _default_name = None


def available_ocr_engines() -> List[str]:
    """Names of all registered engines (built-ins first; not all may be installed)."""
    with _lock:
        names = list(_factories)
    return [n for n in _BUILTIN_ORDER if n in names] + [n for n in names if n not in _BUILTIN_ORDER]


def set_default_ocr_engine(name: Optional[str]) -> None:
    """Make ``name`` the engine used when none is specified (``None`` = auto-detect)."""
    global _default_name
    with _lock:
        if name is None:
            _default_name = None
            return
        key = name.strip().lower()
        if key not in _factories:
            raise NotSupported(f"Unknown OCR engine {name!r}; registered: {available_ocr_engines()}")
        _default_name = key


def _instantiate(key: str) -> OCREngine:
    with _lock:
        if key in _instances:
            return _instances[key]
        factory = _factories.get(key)
    if factory is None:
        raise NotSupported(f"Unknown OCR engine {key!r}; registered: {available_ocr_engines()}")
    if hasattr(factory, "read") and not isinstance(factory, type):
        engine = factory
    else:
        try:
            engine = factory()
        except NotSupported:
            raise
        except ImportError as exc:
            raise NotSupported(f"OCR engine {key!r} is unavailable: {exc}") from exc
    if not hasattr(engine, "read"):
        raise TypeError(f"OCR engine factory {key!r} returned {engine!r}, which has no read(image) method")
    with _lock:
        _instances[key] = engine
    return engine


def get_ocr_engine(name: Optional[str] = None) -> OCREngine:
    """Return a (cached) engine instance.

    ``name=None`` resolves to the default set with :func:`set_default_ocr_engine`
    / ``register_ocr_engine(..., default=True)``, then ``$LOCUM_OCR_ENGINE``,
    then the first working built-in (tesseract -> rapidocr -> easyocr), then
    any other registered engine.

    Raises:
        NotSupported: no engine available (message includes install hints).
    """
    if name:
        return _instantiate(name.strip().lower())
    with _lock:
        default = _default_name
    if default:
        return _instantiate(default)
    env = os.environ.get("LOCUM_OCR_ENGINE", "").strip().lower()
    if env:
        return _instantiate(env)
    errors = []
    for key in available_ocr_engines():
        try:
            return _instantiate(key)
        except (NotSupported, ImportError, OSError) as exc:
            errors.append(f"{key}: {exc}")
    detail = ("\nTried: " + "; ".join(errors)) if errors else ""
    raise NotSupported(_INSTALL_HINT + detail)


def _resolve(engine: Any) -> OCREngine:
    if engine is None or isinstance(engine, str):
        return get_ocr_engine(engine)
    if hasattr(engine, "read"):
        return engine
    raise TypeError(f"engine must be None, an engine name or an object with read(image), got {engine!r}")


register_ocr_engine("tesseract", TesseractEngine)
register_ocr_engine("rapidocr", RapidOCREngine)
register_ocr_engine("easyocr", EasyOCREngine)


# ------------------------------------------------------------------- high level
def read_words(image: Any, *, engine: Any = None) -> List[OCRWord]:
    """Recognize ``image`` and return words sorted in reading order (line, then x).

    ``image`` is anything :func:`locum.vision.load_image` accepts; ``engine`` is
    ``None`` (auto), a registered name or an engine object.
    """
    eng = _resolve(engine)
    words = [w for w in eng.read(_to_rgb(image)) if w.text and w.text.strip()]
    words = [w if isinstance(w.rect, Rect) else OCRWord(w.text, Rect.coerce(w.rect), w.confidence, w.line) for w in words]
    words.sort(key=lambda w: (w.line, w.rect.left))
    return words


def _group_lines(words: Sequence[OCRWord]) -> List[List[OCRWord]]:
    lines: Dict[int, List[OCRWord]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    return [sorted(lines[k], key=lambda w: w.rect.left) for k in sorted(lines)]


def read_lines(image: Any, *, engine: Any = None) -> List[Tuple[str, Rect]]:
    """Recognized lines as ``(text, bounding rect)`` in reading order."""
    out = []
    for ln in _group_lines(read_words(image, engine=engine)):
        rect = ln[0].rect
        for w in ln[1:]:
            rect = rect.union(w.rect)
        out.append((" ".join(w.text for w in ln), rect))
    return out


def read_text(image: Any, *, engine: Any = None) -> str:
    """All recognized text: words joined by spaces, lines joined by ``\\n``."""
    return "\n".join(" ".join(w.text for w in ln) for ln in _group_lines(read_words(image, engine=engine)))


_EDGE_PUNCT = ".,:;!?\"'`()[]{}<>|*~_-…“”‘’"
# Tokens with a digit are amounts, dates, IDs: signs ("-1,500.00", SAP's trailing
# "1,500.00-", "+5"), accounting parentheses ("(250.00)") and a leading decimal
# point (".50") change their meaning, so those are kept; quotes, brackets, grid
# lines read as "|", ALV total markers ("*") and trailing sentence punctuation
# are still dropped.
_NUM_LEAD_PUNCT = "\"'`“”‘’[]{}<>|*~_"
_NUM_TRAIL_PUNCT = ".,:;!?\"'`…“”‘’[]{}<>|*~_"


def _norm_token(token: str, case_sensitive: bool) -> str:
    """Normalize a token for comparison: NFKC, strip edge punctuation, casefold.

    Numeric tokens (containing a digit) keep their sign characters, parentheses and
    a leading decimal point.
    """
    t = unicodedata.normalize("NFKC", token).strip()
    if any(c.isdigit() for c in t):
        stripped = t.lstrip(_NUM_LEAD_PUNCT).rstrip(_NUM_TRAIL_PUNCT)
    else:
        stripped = t.strip(_EDGE_PUNCT)
    t = stripped or t
    return t if case_sensitive else t.casefold()


# Max horizontal gap between consecutive words, in average character widths, for
# them to form a phrase (a column/cell boundary in a grid is wider) or to be joined
# into one split word ("Log in" read for "Login").
_PHRASE_GAP = 3.0
_JOIN_GAP = 1.5


def _gap_chars(a: OCRWord, b: OCRWord) -> float:
    """Horizontal gap between words ``a`` and ``b`` (``b`` right of ``a``) in character widths."""
    chars = max(1, len(a.text.strip())) + max(1, len(b.text.strip()))
    char_w = max(1.0, (a.rect.width + b.rect.width) / chars)
    return max(0, b.rect.left - a.rect.right) / char_w


def _union_rect(words: Sequence[OCRWord]) -> Rect:
    r = words[0].rect
    for w in words[1:]:
        r = r.union(w.rect)
    return r


def find_text(
    image: Any,
    query: str,
    *,
    engine: Any = None,
    regex: bool = False,
    case_sensitive: bool = False,
    fuzzy: float = 1.0,
    offset: Tuple[int, int] = (0, 0),
    words: Optional[Sequence[OCRWord]] = None,
) -> List[Match]:
    """Locate ``query`` in ``image`` via OCR.

    Args:
        image: picture to search (anything :func:`locum.vision.load_image` accepts).
        query: a word or phrase (whitespace separated words must be adjacent on
            the same line, at most about three character widths apart, so words
            from different grid columns never form a phrase), or a regular
            expression if ``regex=True`` (searched in each line's text; the
            matching words' boxes are united). An invalid regex raises
            :class:`ValueError` before OCR runs.
        engine: ``None`` (auto), engine name, or engine object.
        regex: treat ``query`` as a regex.
        case_sensitive: compare case-sensitively (default: casefolded).
        fuzzy: ``1.0`` = exact token match (edge punctuation ignored, so
            ``"Save"`` matches ``"Save:"``; in tokens with a digit only trailing
            sentence punctuation and quotes are, so ``"-1,500.00"``,
            ``"1,500.00-"`` and ``"(250.00)"`` never equal ``"1,500.00"`` or
            ``"250.00"``). A word OCR split in two (``"Log in"`` for ``"Login"``)
            also matches exactly when the gap is at most ~1.5 character widths,
            ranked after genuine exact matches. ``< 1`` = accept phrases whose
            :class:`difflib.SequenceMatcher` ratio is ``>= fuzzy`` (OCR typos,
            merged/split words).
        offset: added to every rect (screen position of image pixel (0, 0)).
        words: pre-computed OCR words (skips running the engine).

    Returns:
        Matches (``text`` = the recognized text, ``confidence`` = similarity x
        mean OCR confidence, ``extra`` = ``{"similarity", "ocr_confidence",
        "line"}``), best similarity first (exact matches before joined split
        words), then reading order.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("find_text() needs a non-empty query string")
    if not (0.0 < fuzzy <= 1.0):
        raise ValueError("fuzzy must be in (0, 1]")
    pattern = None
    if regex:
        # Validate before running OCR. re.error is not a ValueError, and callers such
        # as Desktop.find_text treat only ValueError/TypeError as fatal: a typo in
        # the pattern must not look like "text not on screen" after the full timeout.
        try:
            pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regex {query!r}: {exc}") from exc
    if words is None:
        words = read_words(image, engine=engine)
    lines = _group_lines(words)
    ox, oy = int(offset[0]), int(offset[1])
    # (similarity, kind, line, words, text); kind 0 = exact, 1 = joined split word
    found: List[Tuple[float, int, int, List[OCRWord], str]] = []

    if pattern is not None:
        for li, ln in enumerate(lines):
            spans = []
            parts = []
            pos = 0
            for w in ln:
                spans.append((pos, pos + len(w.text)))
                parts.append(w.text)
                pos += len(w.text) + 1
            text = " ".join(parts)
            for m in pattern.finditer(text):
                if m.end() == m.start():
                    continue
                idx = [k for k, (a, b) in enumerate(spans) if a < m.end() and b > m.start()]
                if idx:
                    found.append((1.0, 0, li, [ln[k] for k in idx], m.group(0)))
    else:
        q_tokens = [_norm_token(t, case_sensitive) for t in query.split()]
        q_tokens = [t for t in q_tokens if t]
        if not q_tokens:
            q_tokens = [query.strip() if case_sensitive else query.strip().casefold()]
        q_joined = " ".join(q_tokens)
        q_compact = "".join(q_tokens)
        n = len(q_tokens)
        sizes = range(max(1, n - 1), n + 2)
        for li, ln in enumerate(lines):
            norm = [_norm_token(w.text, case_sensitive) for w in ln]
            # gap[k] = horizontal gap between ln[k] and ln[k+1] in character widths
            gap = [_gap_chars(ln[k], ln[k + 1]) for k in range(len(ln) - 1)]
            cands: List[Tuple[float, int, int, int]] = []
            for size in sizes:
                for i in range(0, len(ln) - size + 1):
                    widest = max(gap[i : i + size - 1], default=0.0)
                    if widest > _PHRASE_GAP:
                        continue  # words from different columns/cells are no phrase
                    toks = norm[i : i + size]
                    joined = " ".join(toks)
                    kind = 0
                    if size == n and joined == q_joined:
                        sim = 1.0
                    elif "".join(toks) == q_compact and widest <= _JOIN_GAP:
                        sim, kind = 1.0, 1  # "Log in" vs "Login"
                    elif fuzzy < 1.0:
                        sim = max(
                            difflib.SequenceMatcher(None, joined, q_joined).ratio(),
                            difflib.SequenceMatcher(None, "".join(toks), q_compact).ratio(),
                        )
                        if sim < fuzzy:
                            continue
                    else:
                        continue
                    cands.append((sim, kind, i, i + size))
            # keep the best non-overlapping spans on this line
            cands.sort(key=lambda c: (-c[0], c[1], abs((c[3] - c[2]) - n), c[2]))
            taken: List[Tuple[int, int]] = []
            for sim, kind, i, j in cands:
                if any(i < b and a < j for a, b in taken):
                    continue
                taken.append((i, j))
                sel = ln[i:j]
                found.append((sim, kind, li, sel, " ".join(w.text for w in sel)))

    # best similarity first; an exact match outranks a joined split word; then reading order
    found.sort(key=lambda f: (-round(f[0], 6), f[1], f[2], f[3][0].rect.left))
    matches = []
    for sim, _kind, _li, sel, text in found:
        ocr_conf = sum(w.confidence for w in sel) / len(sel)
        matches.append(
            Match(
                _union_rect(sel).offset(ox, oy),
                confidence=max(0.0, min(1.0, sim * ocr_conf)),
                text=text,
                extra={"similarity": round(sim, 4), "ocr_confidence": round(ocr_conf, 4), "line": sel[0].line},
            )
        )
    return matches
