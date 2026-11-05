"""Image utilities and template matching.

Everything here is a *pure function* over images: nothing touches the screen,
so the module is equally usable on screenshots, files and synthetic arrays.

Highlights
----------
* :func:`locate` / :func:`locate_all` -- zero-mean normalized cross-correlation
  (the same score as OpenCV's ``TM_CCOEFF_NORMED``) computed with numpy FFTs,
  so no OpenCV is required. If ``cv2`` happens to be importable it is used as a
  fast path for the unmasked case (set ``PYPILOT_DISABLE_CV2=1`` to opt out).

  - The needle's **alpha channel is a mask**: transparent pixels are ignored
    (weighted / masked NCC), so icons with rounded corners or irregular
    shapes match on any background.
  - **Flat (constant) needles** -- where NCC is mathematically undefined --
    are scored by RMS color distance instead, so locating a solid swatch works.
  - Haystack windows without any contrast never match a textured needle
    (avoids the classic NaN/inf garbage from naive implementations).
  - **Multi-scale** search via ``scales=(0.8, 0.9, 1.0, 1.1, ...)``.
  - :func:`locate_all` uses block max-pooling + vectorized non-max
    suppression, so even pathological inputs stay fast.

* :func:`find_color` -- connected regions of a color (8- or 4-connectivity),
  with a vectorized run-length labelling that needs no SciPy.
* :func:`annotate` -- set-of-marks drawing with readable, collision-avoiding
  labels (used by the LLM observation layer).
* :func:`load_image`, :func:`downscale`, :func:`to_base64_png`,
  :func:`image_diff` helpers.

Coordinate convention: every rectangle returned is in *haystack pixel
coordinates* plus the ``offset`` argument. Pass the screen position of the
haystack's top-left pixel as ``offset`` to get screen coordinates.
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import os
import pathlib
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont

from .types import Match, Rect, RectLike

__all__ = [
    "load_image",
    "locate",
    "locate_all",
    "match_template",
    "find_color",
    "image_diff",
    "changed_fraction",
    "annotate",
    "to_base64_png",
    "to_png_bytes",
    "downscale",
    "parse_color",
    "crop",
]

ImageSource = Any  # path / Path / PIL image / numpy array / bytes / base64 str / data URL

# A haystack window whose RMS deviation (per channel, in 0..255 units) is below
# this has "no contrast": it can't meaningfully correlate with a textured needle.
_FLAT_WINDOW_STD = 0.5
# A needle whose RMS deviation is below this is treated as a solid color.
_FLAT_NEEDLE_STD = 0.5
# For solid-color needles: score = 1 - rms_distance / _FLAT_SCALE (clipped to 0..1).
# confidence 0.9 => RMS color error <= 12.8 levels; 0.99 => <= 1.28.
_FLAT_SCALE = 128.0
# For solid-color needles with transparent pixels (monochrome icons/glyphs): a
# haystack pixel within this RMS color distance of the glyph color counts as
# "glyph colored". At least _GLYPH_MIN_CONTRAST of the needle's transparent area
# must *not* be glyph colored for a full score (less scales the score down
# linearly), so the glyph's shape has to be there: a solid area of the glyph
# color (title bar, toolbar) does not match.
_GLYPH_TOLERANCE = 32.0
_GLYPH_MIN_CONTRAST = 0.5
# Maximum number of candidate peaks fed into non-max suppression per scale.
_MAX_CANDIDATES = 20000

_RESAMPLE_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
_RESAMPLE_BILINEAR = getattr(Image, "Resampling", Image).BILINEAR


# ----------------------------------------------------------------------------- loading
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w/+.-]*)?(?P<params>(;[\w-]+=[^;,]*)*)(?P<b64>;base64)?,(?P<data>.*)$", re.S)
_B64_RE = re.compile(r"^[A-Za-z0-9+/=_\-\s]+$")


def _open_bytes(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _normalize_mode(img: Image.Image) -> Image.Image:
    """Bring exotic modes into one of RGB / RGBA / L / LA (keeping transparency)."""
    mode = img.mode
    if mode in ("RGB", "RGBA", "L", "LA"):
        return img
    if mode == "P" or mode == "PA":
        has_alpha = mode == "PA" or "transparency" in img.info
        return img.convert("RGBA" if has_alpha else "RGB")
    if mode == "1":
        return img.convert("L")
    if mode in ("I", "I;16", "I;16B", "I;16L", "I;16N", "F"):
        arr = np.asarray(img, dtype=np.float64)
        if arr.size:
            lo, hi = float(arr.min()), float(arr.max())
            if hi > 255 or lo < 0:
                # Rescale wide-range data into 0..255.
                arr = (arr - lo) * (255.0 / (hi - lo)) if hi > lo else np.zeros_like(arr)
        # uint8 HxW is inferred as "L"; the explicit ``mode`` argument is deprecated in Pillow >= 11.3
        return Image.fromarray(np.ascontiguousarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8)))
    if mode in ("RGBa", "La"):
        return img.convert("RGBA" if mode == "RGBa" else "LA")
    if "A" in mode or "transparency" in img.info:
        return img.convert("RGBA")
    return img.convert("RGB")


def _from_array(arr: np.ndarray) -> Image.Image:
    a = np.asarray(arr)
    if a.ndim == 3 and a.shape[2] == 1:
        a = a[:, :, 0]
    if a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[2] not in (2, 3, 4)):
        raise ValueError(f"Cannot interpret array of shape {a.shape} as an image (expected HxW, HxWx3 or HxWx4)")
    if a.dtype == np.bool_:
        a = a.astype(np.uint8) * 255
    elif np.issubdtype(a.dtype, np.floating):
        finite = a[np.isfinite(a)]
        peak = float(finite.max()) if finite.size else 0.0
        a = np.nan_to_num(a.astype(np.float64))
        if peak <= 1.0:  # float images in 0..1
            a = a * 255.0
        a = np.clip(np.rint(a), 0, 255).astype(np.uint8)
    elif a.dtype != np.uint8:
        if a.size and (a.max() > 255 or a.min() < 0):
            if a.dtype == np.uint16:
                a = (a // 257).astype(np.uint8)
            else:
                a = np.clip(a, 0, 255).astype(np.uint8)
        else:
            a = a.astype(np.uint8)
    # A contiguous uint8 array of shape HxW / HxWx2 / HxWx3 / HxWx4 is inferred as
    # L / LA / RGB / RGBA; passing ``mode`` explicitly is deprecated in Pillow >= 11.3.
    return Image.fromarray(np.ascontiguousarray(a))


def load_image(src: ImageSource) -> Image.Image:
    """Load an image from almost anything.

    Accepted inputs:

    * a :class:`PIL.Image.Image` (returned as-is unless its mode is exotic),
    * a file path (``str`` or :class:`pathlib.Path`),
    * a numpy array (``HxW`` gray, ``HxWx3`` RGB, ``HxWx4`` RGBA; ``uint8``,
      ``bool``, or float in ``0..1``/``0..255``),
    * raw encoded bytes (PNG/JPEG/...), ``bytearray``/``memoryview``,
    * a file-like object with ``.read()``,
    * a base64 string or a ``data:image/png;base64,...`` URL.

    The result keeps its alpha channel (``RGBA``/``LA``) when present, because
    :func:`locate` uses it as a mask; other modes are normalized to ``RGB``/``L``.

    Raises:
        FileNotFoundError: a string that is neither an existing path nor valid base64.
        ValueError / TypeError: unsupported input.
    """
    if isinstance(src, Image.Image):
        return _normalize_mode(src)
    if isinstance(src, np.ndarray):
        return _from_array(src)
    if isinstance(src, pathlib.PurePath):
        with Image.open(str(src)) as im:
            im.load()
            return _normalize_mode(im.copy())
    if isinstance(src, (bytes, bytearray, memoryview)):
        return _normalize_mode(_open_bytes(bytes(src)))
    if isinstance(src, str):
        s = src.strip()
        m = _DATA_URL_RE.match(s)
        if m:
            payload = m.group("data")
            if m.group("b64"):
                raw = base64.b64decode(payload + "=" * (-len(payload) % 4))
            else:
                from urllib.parse import unquote_to_bytes

                raw = unquote_to_bytes(payload)
            return _normalize_mode(_open_bytes(raw))
        # Short strings are paths; look on disk first.
        if len(s) < 4096 and os.path.exists(os.path.expanduser(s)):
            return load_image(pathlib.Path(os.path.expanduser(s)))
        if len(s) >= 16 and _B64_RE.match(s):
            compact = re.sub(r"\s+", "", s)
            try:
                if "-" in compact or "_" in compact:
                    raw = base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))
                else:
                    raw = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
                return _normalize_mode(_open_bytes(raw))
            except (binascii.Error, ValueError, OSError):
                pass
        raise FileNotFoundError(f"Image not found (not a file path, data URL or base64 image): {s[:120]!r}")
    if hasattr(src, "read"):
        data = src.read()
        if isinstance(data, str):
            data = data.encode("latin-1")
        return _normalize_mode(_open_bytes(data))
    if hasattr(src, "__array__"):
        return _from_array(np.asarray(src))
    raise TypeError(f"Cannot load an image from {type(src).__name__}")


def parse_color(color: Any) -> Tuple[int, int, int]:
    """``(r, g, b)`` from a tuple/list (alpha ignored), ``"#rrggbb"``/``"#rgb"``, a CSS
    color name (``"red"``), ``"rgb(1,2,3)"`` or a packed ``0xRRGGBB`` int."""
    if isinstance(color, str):
        rgb = ImageColor.getrgb(color.strip())
        return (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    if isinstance(color, (int, np.integer)) and not isinstance(color, bool):
        c = int(color)
        return ((c >> 16) & 255, (c >> 8) & 255, c & 255)
    try:
        vals = [int(round(float(v))) for v in color]
    except TypeError:
        raise TypeError(f"Cannot interpret {color!r} as a color") from None
    if len(vals) == 1:
        vals = vals * 3
    if len(vals) < 3:
        raise ValueError(f"Color needs 3 components, got {color!r}")
    return (vals[0], vals[1], vals[2])


def _rgb_array(img: Image.Image) -> np.ndarray:
    if img.mode != "RGB":
        img = img.convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _split_alpha(img: Image.Image) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """RGB uint8 array plus alpha weights in 0..1 (``None`` if fully opaque / absent)."""
    if img.mode in ("RGBA", "LA"):
        rgba = np.asarray(img.convert("RGBA"), dtype=np.uint8)
        alpha = rgba[:, :, 3]
        rgb = rgba[:, :, :3]
        if alpha.size and alpha.min() >= 255:
            return np.ascontiguousarray(rgb), None
        return np.ascontiguousarray(rgb), alpha.astype(np.float64) / 255.0
    return _rgb_array(img), None


def _gray(arr: np.ndarray) -> np.ndarray:
    """ITU-R 601 luma (same weights as PIL's ``convert("L")``), float64 HxWx1."""
    a = arr.astype(np.float64)
    g = a[:, :, 0] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.114
    return g[:, :, None]


def crop(image: ImageSource, region: RectLike) -> Image.Image:
    """Crop ``image`` to ``region`` (``Rect``-like, image coordinates; areas outside are black)."""
    img = load_image(image)
    return img.crop(Rect.coerce(region).as_ltrb())


# ------------------------------------------------------------------- FFT machinery
def _fast_len(n: int) -> int:
    """Smallest 5-smooth integer >= n (fast FFT sizes)."""
    if n <= 1:
        return 1
    best = 1 << (n - 1).bit_length()
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            # smallest power of two that brings p35 >= n
            quotient = -(-n // p35)
            p2 = 1 << (quotient - 1).bit_length()
            cand = p2 * p35
            if cand < best:
                best = cand
            p35 *= 3
        p5 *= 5
    return best


def _window_sum(ii: np.ndarray, th: int, tw: int) -> np.ndarray:
    """Sliding-window sums from a zero-padded integral image (valid positions only)."""
    return ii[th:, tw:] - ii[:-th, tw:] - ii[th:, :-tw] + ii[:-th, :-tw]


def _integral(a: np.ndarray) -> np.ndarray:
    """Zero-padded summed-area table (``out[y, x]`` = sum of ``a[:y, :x]``)."""
    out = np.zeros((a.shape[0] + 1, a.shape[1] + 1), dtype=np.float64)
    inner = out[1:, 1:]
    np.cumsum(a, axis=0, dtype=np.float64, out=inner)
    np.cumsum(inner, axis=1, out=inner)
    return out


_CV2: Any = None
_CV2_CHECKED = False


def _cv2() -> Any:
    """The ``cv2`` module if importable and not disabled, else ``None`` (cached)."""
    global _CV2, _CV2_CHECKED
    if os.environ.get("PYPILOT_DISABLE_CV2", "").strip() not in ("", "0", "false", "False"):
        return None
    if not _CV2_CHECKED:
        _CV2_CHECKED = True
        try:
            import cv2  # type: ignore

            _CV2 = cv2
        except Exception:  # ImportError, or broken native libs
            _CV2 = None
    return _CV2


class _Haystack:
    """A haystack prepared for repeated correlation with several needles/scales.

    Values are shifted by their per-channel mean (NCC is shift invariant, and
    it keeps the FFT / integral-image arithmetic numerically well conditioned).
    FFTs and integral images are computed lazily and cached.
    """

    def __init__(self, rgb: np.ndarray, grayscale: bool):
        self.raw = rgb  # uint8 HxWx3 (for the cv2 fast path)
        self.grayscale = grayscale
        if grayscale:
            planes = _gray(rgb)[:, :, 0][None, :, :]
        else:
            # channel-first, contiguous planes: much faster FFTs / cumsums than strided views
            planes = np.ascontiguousarray(rgb.transpose(2, 0, 1), dtype=np.float64)
        self.mean = planes.reshape(planes.shape[0], -1).mean(axis=1)
        planes -= self.mean[:, None, None]
        self.data = planes  # CxHxW float64, zero mean per channel
        self.c, self.h, self.w = planes.shape
        self.fshape = (_fast_len(self.h), _fast_len(self.w))
        self._fft: Optional[List[np.ndarray]] = None
        self._fft_sq: Optional[List[np.ndarray]] = None
        self._ii: Optional[List[np.ndarray]] = None
        self._ii_sq: Optional[np.ndarray] = None

    # -- cached transforms
    def fft(self) -> List[np.ndarray]:
        if self._fft is None:
            self._fft = [np.fft.rfft2(self.data[k], s=self.fshape) for k in range(self.c)]
        return self._fft

    def fft_sq(self) -> List[np.ndarray]:
        if self._fft_sq is None:
            self._fft_sq = [np.fft.rfft2(np.square(self.data[k]), s=self.fshape) for k in range(self.c)]
        return self._fft_sq

    def integrals(self) -> Tuple[List[np.ndarray], np.ndarray]:
        if self._ii is None:
            self._ii = [_integral(self.data[k]) for k in range(self.c)]
            sq = np.square(self.data[0])
            for k in range(1, self.c):
                sq += np.square(self.data[k])
            self._ii_sq = _integral(sq)
        assert self._ii_sq is not None
        return self._ii, self._ii_sq

    def kernel_spectrum(self, kernel: np.ndarray) -> np.ndarray:
        """FFT of a (small) kernel, flipped so that a product with a haystack
        spectrum yields *correlation* (zero-padded to the haystack FFT size)."""
        return np.fft.rfft2(kernel[::-1, ::-1], s=self.fshape)

    def correlate(self, spectra: Sequence[np.ndarray], kernels: Sequence[Any], th: int, tw: int) -> np.ndarray:
        """``sum_k corr(channel_k, kernel_k)`` over valid positions, via one inverse FFT.

        ``kernels`` items are spatial kernels (``th x tw`` float arrays) or spectra
        from :meth:`kernel_spectrum` (complex arrays) to reuse a transform.
        Correlation is computed as convolution with the flipped kernel; with an
        FFT size >= the haystack size the valid region never wraps around.
        """
        acc: Optional[np.ndarray] = None
        tmp: Optional[np.ndarray] = None
        for spec, ker in zip(spectra, kernels):
            kspec = ker if np.iscomplexobj(ker) else self.kernel_spectrum(ker)
            if acc is None:
                acc = spec * kspec
            else:
                tmp = np.multiply(spec, kspec, out=tmp)
                acc += tmp
        assert acc is not None
        out = np.fft.irfft2(acc, s=self.fshape)
        return out[th - 1 : self.h, tw - 1 : self.w]


def _prepare_needle(needle_img: Image.Image, scale: float, grayscale: bool) -> Optional[Tuple[np.ndarray, Optional[np.ndarray]]]:
    """Resized needle as float64 HxWxC (raw values) plus optional weight mask."""
    img = needle_img
    if scale != 1.0:
        w = max(1, int(round(img.width * scale)))
        h = max(1, int(round(img.height * scale)))
        if (w, h) != img.size:
            if img.mode == "LA":
                img = img.convert("RGBA")
            img = img.resize((w, h), _RESAMPLE_LANCZOS)
    rgb, alpha = _split_alpha(img)
    data = _gray(rgb) if grayscale else rgb.astype(np.float64)
    if alpha is not None:
        alpha = np.where(alpha < 1.0 / 255.0, 0.0, alpha)
        if not np.any(alpha > 0):
            return None
    return data, alpha


def _window_variance(s1: Sequence[np.ndarray], s2: np.ndarray, n: float) -> np.ndarray:
    """``sum_k (S2 - S1_k^2 / n)``: n * variance of each window, summed over channels."""
    var = s2.copy()
    tmp = None
    for s in s1:
        tmp = np.square(s, out=tmp)
        tmp /= n
        var -= tmp
    return var


def _normalize_ncc(num: np.ndarray, var_h: np.ndarray, var_t: float, floor: float) -> np.ndarray:
    """``num / sqrt(var_h * var_t)`` in place; windows with ``var_h <= floor`` score 0."""
    flat = var_h <= floor
    denom = np.maximum(var_h, floor if floor > 0 else 1e-12)
    denom *= var_t
    np.sqrt(denom, out=denom)
    num /= denom
    num[flat] = 0.0
    np.clip(num, -1.0, 1.0, out=num)
    return num


def _flat_needle_score(s1: Sequence[np.ndarray], s2: np.ndarray, tmean: np.ndarray, n: float) -> np.ndarray:
    """Score for a solid-color needle: ``1 - rms_color_distance / _FLAT_SCALE``."""
    ssd = s2.copy()
    for k, s in enumerate(s1):
        ssd -= (2.0 * float(tmean[k])) * s
    ssd += n * float((tmean ** 2).sum())
    # Snap rounding noise (cancellation in the window sums) to an exact match.
    ssd[ssd < 1e-3 * n * len(s1)] = 0.0
    ssd /= n * len(s1)
    np.sqrt(ssd, out=ssd)
    ssd /= -_FLAT_SCALE
    ssd += 1.0
    np.clip(ssd, 0.0, 1.0, out=ssd)
    return ssd


def _apply_shape_contrast(score: np.ndarray, hay: _Haystack, tmean: np.ndarray, mask: np.ndarray) -> None:
    """Scale a masked solid-color needle's ``score`` (in place) by its shape contrast.

    The opaque pixels of a monochrome glyph carry no shape information on their
    own: color matching under the mask scores any solid area of the glyph color
    1.0. The shape lives in the alpha channel, so a window also has to be mostly
    *not* glyph colored where the needle is transparent: the score is multiplied
    by ``min(1, contrast / _GLYPH_MIN_CONTRAST)`` where ``contrast`` is
    ``1 - (transparency-weighted mean "glyph-coloredness" of the window)``.
    Needles without transparent pixels (uniform alpha) are left alone.
    """
    peak = float(mask.max())
    bg = 1.0 - mask / peak  # transparency weights, 0 for fully opaque pixels
    bsum = float(bg.sum())
    if bsum < 0.5:
        return
    th, tw = mask.shape
    # Per-pixel closeness to the glyph color: 1 = same color, 0 = >= tolerance away.
    dist = np.zeros((hay.h, hay.w), dtype=np.float64)
    for k in range(hay.c):
        d = hay.data[k] - float(tmean[k])
        d *= d
        dist += d
    dist /= hay.c
    np.sqrt(dist, out=dist)
    close = np.clip(1.0 - dist / _GLYPH_TOLERANCE, 0.0, 1.0)
    spec = np.fft.rfft2(close, s=hay.fshape)
    covered = hay.correlate([spec], [bg], th, tw)
    # contrast factor: 1 - covered, scaled so that _GLYPH_MIN_CONTRAST counts as full
    covered /= -bsum * _GLYPH_MIN_CONTRAST
    covered += 1.0 / _GLYPH_MIN_CONTRAST
    np.clip(covered, 0.0, 1.0, out=covered)
    score *= covered


def _score_map(hay: _Haystack, needle: np.ndarray, mask: Optional[np.ndarray]) -> np.ndarray:
    """Zero-mean NCC score for every valid needle position (values in -1..1).

    ``needle`` is float64 HxWxC in raw 0..255 units; ``mask`` optional weights HxW.
    """
    th, tw, c = needle.shape
    shifted = needle - hay.mean  # into the haystack's shifted space
    if mask is None:
        n = float(th * tw)
        floor = (_FLAT_WINDOW_STD ** 2) * n * c
        ii, ii_sq = hay.integrals()
        s1 = [_window_sum(ii[k], th, tw) for k in range(c)]
        s2 = _window_sum(ii_sq, th, tw)
        tmean = shifted.reshape(-1, c).mean(axis=0)
        tz = shifted - tmean
        var_t = float((tz ** 2).sum())
        if var_t <= (_FLAT_NEEDLE_STD ** 2) * n * c:
            return _flat_needle_score(s1, s2, tmean, n)
        var_h = _window_variance(s1, s2, n)
        cv2 = _cv2()
        if cv2 is not None:
            hay_src = hay.raw if not hay.grayscale else (hay.data[0] + hay.mean[0])
            ndl_src = needle if not hay.grayscale else needle[:, :, 0]
            score = cv2.matchTemplate(
                np.ascontiguousarray(hay_src, dtype=np.float32),
                np.ascontiguousarray(ndl_src, dtype=np.float32),
                cv2.TM_CCOEFF_NORMED,
            ).astype(np.float64)
            score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)
            score[var_h <= floor] = 0.0
            return np.clip(score, -1.0, 1.0)
        num = hay.correlate(hay.fft(), [tz[:, :, k] for k in range(c)], th, tw)
        return _normalize_ncc(num, var_h, var_t, floor)

    # ---- masked (weighted) NCC: transparent needle pixels carry zero weight
    w = mask
    nw = float(w.sum())
    floor = (_FLAT_WINDOW_STD ** 2) * nw * c
    tmean = (shifted * w[:, :, None]).reshape(-1, c).sum(axis=0) / nw
    dev = shifted - tmean
    var_t = float(((dev ** 2) * w[:, :, None]).sum())
    spectra = hay.fft()
    wspec = hay.kernel_spectrum(w)
    s1 = [hay.correlate([spectra[k]], [wspec], th, tw) for k in range(c)]
    s2 = hay.correlate(hay.fft_sq(), [wspec] * c, th, tw)
    if var_t <= (_FLAT_NEEDLE_STD ** 2) * nw * c:
        score = _flat_needle_score(s1, s2, tmean, nw)
        _apply_shape_contrast(score, hay, tmean, w)
        return score
    num = hay.correlate(spectra, [dev[:, :, k] * w for k in range(c)], th, tw)
    var_h = _window_variance(s1, s2, nw)
    return _normalize_ncc(num, var_h, var_t, floor)


def _crop_region(img: Image.Image, region: Optional[RectLike]) -> Optional[Tuple[Image.Image, int, int]]:
    """Crop ``img`` to ``region`` clipped to the image; ``None`` if nothing remains."""
    if region is None:
        return img, 0, 0
    r = Rect.coerce(region).intersect(Rect(0, 0, img.width, img.height))
    if r is None:
        return None
    if (r.left, r.top, r.width, r.height) == (0, 0, img.width, img.height):
        return img, 0, 0
    return img.crop(r.as_ltrb()), r.left, r.top


def _iter_score_maps(
    needle: ImageSource,
    haystack: ImageSource,
    grayscale: bool,
    scales: Sequence[float],
    region: Optional[RectLike],
) -> Iterable[Tuple[np.ndarray, int, int, float, int, int]]:
    """Yield ``(score_map, needle_w, needle_h, scale, dx, dy)`` for every usable scale."""
    needle_img = load_image(needle)
    hay_img = load_image(haystack)
    cropped = _crop_region(hay_img, region)
    if cropped is None:
        return
    hay_img, dx, dy = cropped
    if needle_img.width < 1 or needle_img.height < 1 or hay_img.width < 1 or hay_img.height < 1:
        return
    scale_list = [float(s) for s in (scales if scales else (1.0,))]
    hay: Optional[_Haystack] = None
    seen = set()
    for s in scale_list:
        if not (s > 0 and math.isfinite(s)):
            raise ValueError(f"Invalid scale {s!r}; scales must be positive")
        tw = max(1, int(round(needle_img.width * s)))
        th = max(1, int(round(needle_img.height * s)))
        if (tw, th) in seen:
            continue
        seen.add((tw, th))
        if tw > hay_img.width or th > hay_img.height:
            continue  # needle larger than haystack at this scale: no possible match
        prepared = _prepare_needle(needle_img, s, grayscale)
        if prepared is None:
            raise ValueError("The needle image is fully transparent; nothing to match")
        data, mask = prepared
        if hay is None:
            hay = _Haystack(_rgb_array(hay_img), grayscale)
        yield _score_map(hay, data, mask), tw, th, s, dx, dy


def match_template(
    needle: ImageSource, haystack: ImageSource, *, grayscale: bool = False, scale: float = 1.0
) -> np.ndarray:
    """Raw score map: ``out[y, x]`` = similarity of the needle placed at ``(x, y)``.

    Shape ``(H - h + 1, W - w + 1)``; empty (0x0) when the needle is larger than
    the haystack. Scores are zero-mean NCC in ``-1..1`` (``0..1`` for solid needles).
    """
    for score, *_ in _iter_score_maps(needle, haystack, grayscale, (scale,), None):
        return score
    return np.zeros((0, 0), dtype=np.float64)


def locate(
    needle: ImageSource,
    haystack: ImageSource,
    *,
    confidence: float = 0.9,
    grayscale: bool = False,
    scales: Sequence[float] = (1.0,),
    region: Optional[RectLike] = None,
    offset: Tuple[int, int] = (0, 0),
) -> Optional[Match]:
    """Find the best occurrence of ``needle`` in ``haystack``.

    Args:
        needle: image to look for (anything :func:`load_image` accepts). Its alpha
            channel, if any, masks out transparent pixels. For a needle whose
            opaque pixels are one solid color (a monochrome icon/glyph) the
            alpha channel *is* the shape: its transparent area must mostly not be
            that color where it matches, so a solid area of the glyph color
            (a dark title bar) is not a match.
        haystack: image to search in (e.g. a screenshot).
        confidence: minimum score in ``0..1`` (zero-mean normalized cross-correlation).
        grayscale: compare luminance only (faster, color-blind).
        scales: needle scale factors to try (e.g. ``(0.9, 1.0, 1.1)`` for DPI drift).
        region: restrict the search to this rect of the haystack (haystack coords).
        offset: added to the resulting rect (screen position of haystack pixel (0, 0)).

    Returns:
        The best :class:`~pypilot.types.Match` (rect in haystack coords + offset,
        ``scale`` = the winning scale) or ``None`` if nothing reaches ``confidence``
        (also when the needle is larger than the searched area).
    """
    best: Optional[Tuple[float, int, int, int, int, float]] = None
    for score, tw, th, s, dx, dy in _iter_score_maps(needle, haystack, grayscale, scales, region):
        if score.size == 0:
            continue
        peak = float(score.max())
        # among (numerically) tied peaks prefer the first in reading order
        idx = int(np.argmax(score >= peak - 1e-9))
        y, x = divmod(idx, score.shape[1])
        val = peak
        if best is None or val > best[0] + 1e-9:
            best = (val, x + dx, y + dy, tw, th, s)
    if best is None or best[0] < confidence:
        return None
    val, x, y, tw, th, s = best
    ox, oy = int(offset[0]), int(offset[1])
    return Match(Rect(int(x) + ox, int(y) + oy, tw, th), confidence=_conf(val), scale=s)


def _conf(value: float) -> float:
    """Clip to 0..1 and drop floating-point noise (a perfect match reports exactly 1.0)."""
    return round(max(0.0, min(1.0, float(value))), 9)


def _pool_block(th: int, tw: int, overlap: float) -> Tuple[int, int]:
    """Block size such that two positions in one block always overlap by IoU > ``overlap``."""
    if overlap >= 1.0:
        return 1, 1
    ov = max(0.0, overlap)
    frac = (1.0 - math.sqrt(2.0 * ov / (1.0 + ov))) * 0.9
    return max(1, int(th * frac)), max(1, int(tw * frac))


def _candidates(score: np.ndarray, threshold: float, bh: int, bw: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Candidate positions ``(ys, xs, values)`` with value >= threshold.

    When at most ``_MAX_CANDIDATES`` positions pass the threshold they are all
    returned, so the greedy NMS that follows is exact (pooling first could drop
    a weaker peak whose block winner is later suppressed by a third match).
    Only above that bound are the positions block-max-pooled (``bh`` x ``bw``
    blocks, see :func:`_pool_block`) to keep the candidate count manageable.
    """
    h, w = score.shape
    mask = score >= threshold
    if (bh > 1 or bw > 1) and int(np.count_nonzero(mask)) > _MAX_CANDIDATES:
        nby, nbx = -(-h // bh), -(-w // bw)
        padded = np.full((nby * bh, nbx * bw), -np.inf)
        padded[:h, :w] = score
        blocks = padded.reshape(nby, bh, nbx, bw).transpose(0, 2, 1, 3).reshape(nby, nbx, bh * bw)
        arg = blocks.argmax(axis=2)
        vals = np.take_along_axis(blocks, arg[:, :, None], axis=2)[:, :, 0]
        keep = vals >= threshold
        by, bx = np.nonzero(keep)
        a = arg[by, bx]
        ys = by * bh + a // bw
        xs = bx * bw + a % bw
        v = vals[by, bx]
    else:
        ys, xs = np.nonzero(mask)
        v = score[ys, xs]
    if v.size > _MAX_CANDIDATES:
        top = np.argpartition(-v, _MAX_CANDIDATES - 1)[:_MAX_CANDIDATES]
        ys, xs, v = ys[top], xs[top], v[top]
    return ys, xs, v


def _nms(
    xs: np.ndarray, ys: np.ndarray, ws: np.ndarray, hs: np.ndarray, scores: np.ndarray, overlap: float, limit: int
) -> List[int]:
    """Greedy non-max suppression; returns kept indices in descending score order."""
    # Sort by score desc, ties broken top-to-bottom then left-to-right (deterministic).
    order = np.lexsort((xs, ys, -np.round(scores, 9)))
    x1, y1 = xs[order].astype(np.float64), ys[order].astype(np.float64)
    x2, y2 = x1 + ws[order], y1 + hs[order]
    areas = (x2 - x1) * (y2 - y1)
    kept: List[int] = []
    alive = np.arange(order.size)
    while alive.size and len(kept) < limit:
        i = alive[0]
        kept.append(int(order[i]))
        rest = alive[1:]
        if not rest.size:
            break
        iw = np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest])
        ih = np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest])
        inter = np.clip(iw, 0, None) * np.clip(ih, 0, None)
        iou = inter / (areas[i] + areas[rest] - inter)
        alive = rest[iou <= overlap]
    return kept


def locate_all(
    needle: ImageSource,
    haystack: ImageSource,
    *,
    confidence: float = 0.9,
    grayscale: bool = False,
    scales: Sequence[float] = (1.0,),
    limit: int = 100,
    overlap: float = 0.3,
    region: Optional[RectLike] = None,
    offset: Tuple[int, int] = (0, 0),
) -> List[Match]:
    """Find every occurrence of ``needle`` in ``haystack``.

    Same arguments as :func:`locate`, plus:

    Args:
        limit: maximum number of matches returned.
        overlap: IoU above which a weaker overlapping match is suppressed
            (non-max suppression, applied across all scales).

    Returns:
        Matches sorted by confidence (descending; ties in reading order).
    """
    if limit <= 0:
        return []
    all_x: List[np.ndarray] = []
    all_y: List[np.ndarray] = []
    all_w: List[np.ndarray] = []
    all_h: List[np.ndarray] = []
    all_v: List[np.ndarray] = []
    all_s: List[np.ndarray] = []
    for score, tw, th, s, dx, dy in _iter_score_maps(needle, haystack, grayscale, scales, region):
        if score.size == 0:
            continue
        bh, bw = _pool_block(th, tw, overlap)
        ys, xs, v = _candidates(score, confidence, bh, bw)
        if not v.size:
            continue
        all_x.append(xs + dx)
        all_y.append(ys + dy)
        all_w.append(np.full(v.size, tw))
        all_h.append(np.full(v.size, th))
        all_v.append(v)
        all_s.append(np.full(v.size, s))
    if not all_v:
        return []
    xs, ys = np.concatenate(all_x), np.concatenate(all_y)
    ws, hs = np.concatenate(all_w), np.concatenate(all_h)
    vs, ss = np.concatenate(all_v), np.concatenate(all_s)
    kept = _nms(xs, ys, ws, hs, vs, overlap, int(limit))
    ox, oy = int(offset[0]), int(offset[1])
    return [
        Match(
            Rect(int(xs[i]) + ox, int(ys[i]) + oy, int(ws[i]), int(hs[i])),
            confidence=_conf(vs[i]),
            scale=float(ss[i]),
        )
        for i in kept
    ]


# --------------------------------------------------------------------------- color
def _connected_runs(mask: np.ndarray, connectivity: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Label connected components of a boolean mask via run-length encoding.

    Returns ``(rows, starts, ends, labels)`` per horizontal run (``ends`` exclusive);
    runs sharing a label belong to the same component. Fully vectorized: runs are
    linked row-to-row with ``searchsorted`` and merged with a hook-and-compress
    (Shiloach-Vishkin style) union-find in numpy.
    """
    h, w = mask.shape
    padded = np.zeros((h, w + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    d = np.diff(padded, axis=1)
    rows, starts = np.nonzero(d == 1)
    _, ends = np.nonzero(d == -1)
    n = rows.size
    labels = np.arange(n)
    if n == 0:
        return rows, starts, ends, labels
    k = w + 2
    start_key = rows.astype(np.int64) * k + starts
    end_key = rows.astype(np.int64) * k + ends
    # For each run b (row r+1), the overlapping runs a (row r) form a contiguous index range.
    has_prev = rows > 0
    b_idx = np.nonzero(has_prev)[0]
    prev_row = rows[b_idx].astype(np.int64) - 1
    if connectivity == 8:
        lo = np.searchsorted(end_key, prev_row * k + starts[b_idx], side="left")
        hi = np.searchsorted(start_key, prev_row * k + ends[b_idx], side="right")
    else:
        lo = np.searchsorted(end_key, prev_row * k + starts[b_idx], side="right")
        hi = np.searchsorted(start_key, prev_row * k + ends[b_idx], side="left")
    counts = np.maximum(hi - lo, 0)
    total = int(counts.sum())
    if total:
        first = np.repeat(lo, counts)
        within = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
        ea = first + within
        eb = np.repeat(b_idx, counts)
        while True:
            la, lb = labels[ea], labels[eb]
            if np.array_equal(la, lb):
                break
            low = np.minimum(la, lb)
            np.minimum.at(labels, la, low)
            np.minimum.at(labels, lb, low)
            # pointer jumping until every label is a root
            while True:
                nxt = labels[labels]
                if np.array_equal(nxt, labels):
                    break
                labels = nxt
    return rows, starts, ends, labels


def find_color(
    haystack: ImageSource,
    color: Any,
    *,
    tolerance: int = 0,
    region: Optional[RectLike] = None,
    offset: Tuple[int, int] = (0, 0),
    min_area: int = 1,
    connectivity: int = 8,
) -> List[Rect]:
    """Bounding rects of connected regions whose pixels match ``color``.

    A pixel matches when every RGB channel is within ``tolerance`` of ``color``
    (Chebyshev distance). Regions are 8-connected by default (``connectivity=4``
    for edge-only adjacency); regions smaller than ``min_area`` pixels are dropped.

    Returns:
        Rects in haystack coordinates (+ region origin + ``offset``), sorted in
        reading order (top-to-bottom, then left-to-right).
    """
    if connectivity not in (4, 8):
        raise ValueError("connectivity must be 4 or 8")
    target = np.array(parse_color(color), dtype=np.int16)
    img = load_image(haystack)
    cropped = _crop_region(img, region)
    if cropped is None:
        return []
    img, dx, dy = cropped
    arr = _rgb_array(img).astype(np.int16)
    tol = max(0, int(tolerance))
    mask = np.all(np.abs(arr - target) <= tol, axis=2)
    if not mask.any():
        return []
    rows, starts, ends, labels = _connected_runs(mask, connectivity)
    uniq, inv = np.unique(labels, return_inverse=True)
    m = uniq.size
    left = np.full(m, np.iinfo(np.int64).max)
    right = np.zeros(m, dtype=np.int64)
    top = np.full(m, np.iinfo(np.int64).max)
    bottom = np.zeros(m, dtype=np.int64)
    np.minimum.at(left, inv, starts)
    np.maximum.at(right, inv, ends)
    np.minimum.at(top, inv, rows)
    np.maximum.at(bottom, inv, rows)
    area = np.bincount(inv, weights=(ends - starts), minlength=m)
    keep = np.nonzero(area >= max(1, int(min_area)))[0]
    order = keep[np.lexsort((left[keep], top[keep]))]
    ox, oy = int(offset[0]) + dx, int(offset[1]) + dy
    return [
        Rect(int(left[i]) + ox, int(top[i]) + oy, int(right[i] - left[i]), int(bottom[i] - top[i] + 1)) for i in order
    ]


# ---------------------------------------------------------------------------- diffs
def _pair_arrays(a: ImageSource, b: ImageSource) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    ia, ib = load_image(a), load_image(b)
    if ia.size != ib.size:
        return None
    return _rgb_array(ia).astype(np.int16), _rgb_array(ib).astype(np.int16)


def _max_tile_mean(m: np.ndarray, tile: int) -> float:
    """Largest mean of ``m`` (2-D) over ``tile`` x ``tile`` windows on a half-overlapping grid.

    Each window is normalized by the full window area (clipped to the image
    size), so a window hanging off an edge never weighs a change more, and an
    image smaller than ``tile`` is one window over everything.
    """
    h, w = m.shape
    th, tw = min(int(tile), h), min(int(tile), w)
    ch, cw = max(1, th // 2), max(1, tw // 2)  # cells: two per window side
    ph, pw = -h % ch, -w % cw
    if ph or pw:
        m = np.pad(m, ((0, ph), (0, pw)))
    cells = m.reshape(m.shape[0] // ch, ch, m.shape[1] // cw, cw).sum(axis=(1, 3), dtype=np.float64)
    ny, nx = (th + ch - 1) // ch, (tw + cw - 1) // cw  # cells per window (1 or 2, 3 if odd)
    ii = _integral(cells)
    gy, gx = max(1, cells.shape[0] - ny + 1), max(1, cells.shape[1] - nx + 1)
    ny, nx = min(ny, cells.shape[0]), min(nx, cells.shape[1])
    sums = ii[ny : ny + gy, nx : nx + gx] - ii[:gy, nx : nx + gx] - ii[ny : ny + gy, :gx] + ii[:gy, :gx]
    return float(sums.max() / (th * tw))


def image_diff(a: ImageSource, b: ImageSource, *, tile: Optional[int] = None) -> float:
    """Normalized mean absolute RGB difference: ``0.0`` identical .. ``1.0`` opposite.

    With ``tile`` (pixels), the result is the largest mean over ``tile`` x ``tile``
    blocks instead of over the whole image, so a small local change (a spinner,
    a message bar) on a big capture is not averaged away.
    Images of different sizes are considered totally different (``1.0``).
    Empty images compare as identical.
    """
    pair = _pair_arrays(a, b)
    if pair is None:
        return 1.0
    xa, xb = pair
    if xa.size == 0:
        return 0.0
    if tile:
        diff = np.abs(xa - xb).sum(axis=2, dtype=np.int32)
        return _max_tile_mean(diff, tile) / (255.0 * xa.shape[2])
    return float(np.abs(xa - xb).mean() / 255.0)


def changed_fraction(a: ImageSource, b: ImageSource, *, pixel_tolerance: int = 0, tile: Optional[int] = None) -> float:
    """Fraction (0..1) of pixels whose largest channel difference exceeds ``pixel_tolerance``.

    More sensitive than :func:`image_diff` to small, local changes (a checkbox
    toggling on a full-screen capture). With ``tile`` (pixels), the largest
    fraction over ``tile`` x ``tile`` blocks. Different sizes -> ``1.0``.
    """
    pair = _pair_arrays(a, b)
    if pair is None:
        return 1.0
    xa, xb = pair
    if xa.size == 0:
        return 0.0
    changed = np.abs(xa - xb).max(axis=2) > int(pixel_tolerance)
    if tile:
        return _max_tile_mean(changed.astype(np.int32), tile)
    return float(changed.mean())


# ------------------------------------------------------------------------- drawing
_PALETTE = [
    (230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180),
    (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 212), (0, 128, 128),
    (170, 110, 40), (128, 0, 0), (0, 0, 128), (128, 128, 0), (255, 225, 25),
]

_FONT_CACHE: Dict[int, Any] = {}


def _font(size: int) -> Any:
    if size not in _FONT_CACHE:
        font = None
        for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf", "Helvetica.ttc", "DejaVuSans.ttf", "Arial.ttf"):
            try:
                font = ImageFont.truetype(name, size)
                break
            except (OSError, IOError):
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:  # Pillow < 10.1
                font = ImageFont.load_default()
        _FONT_CACHE[size] = font
    return _FONT_CACHE[size]


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: Any) -> Tuple[int, int, int, int]:
    """``(width, height, x_off, y_off)`` of rendered text."""
    if hasattr(draw, "textbbox"):
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return int(r - l), int(b - t), int(l), int(t)
    w, h = draw.textsize(text, font=font)  # pragma: no cover - very old Pillow
    return int(w), int(h), 0, 0


def _contrast_text(color: Tuple[int, int, int]) -> Tuple[int, int, int]:
    r, g, b = color
    return (0, 0, 0) if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else (255, 255, 255)


def _overlap_area(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def annotate(
    image: ImageSource,
    boxes: Sequence[Tuple[Any, ...]],
    *,
    color: Optional[Any] = (255, 0, 0),
    offset: Tuple[int, int] = (0, 0),
    scale: float = 1.0,
    width: Optional[int] = None,
    font_size: Optional[int] = None,
) -> Image.Image:
    """Draw labeled boxes (set-of-marks) on a copy of ``image``.

    Args:
        image: the picture to draw on (not modified).
        boxes: ``(rect, label)`` or ``(rect, label, color)`` tuples; ``rect`` is
            any ``RectLike`` in the *same coordinate space as offset* (usually screen).
        color: outline/label color; ``None`` cycles a palette of distinct colors.
        offset: coordinate of the image's top-left pixel in the boxes' space;
            boxes are drawn at ``(rect - offset) * scale``.
        scale: factor mapping box coordinates to image pixels (e.g. the factor
            returned by :func:`downscale`).
        width: outline width in px (default: proportional to image size).
        font_size: label font size (default: proportional to image size).

    Labels sit on a solid tag in the box color with contrasting text. They are
    placed outside the box's top-left corner when possible and moved to avoid
    overlapping previously placed labels; all labels are drawn above all boxes.
    """
    base = load_image(image)
    img = base.convert("RGB") if base.mode != "RGB" else base.copy()
    draw = ImageDraw.Draw(img)
    iw, ih = img.size
    short = max(1, min(iw, ih))
    lw = int(width) if width else max(1, min(4, int(round(short / 400.0)) + 1))
    fs = int(font_size) if font_size else max(10, min(24, int(round(short / 55.0))))
    font = _font(fs)
    ox, oy = offset
    sc = float(scale)

    items = []
    for i, box in enumerate(boxes):
        if len(box) < 2:
            raise ValueError("annotate() boxes must be (rect, label) or (rect, label, color) tuples")
        r = Rect.coerce(box[0])
        label = "" if box[1] is None else str(box[1])
        if len(box) > 2 and box[2] is not None:
            c = parse_color(box[2])
        elif color is None:
            c = _PALETTE[i % len(_PALETTE)]
        else:
            c = parse_color(color)
        x0 = int(round((r.left - ox) * sc))
        y0 = int(round((r.top - oy) * sc))
        x1 = int(round((r.right - ox) * sc)) - 1
        y1 = int(round((r.bottom - oy) * sc)) - 1
        x1, y1 = max(x1, x0), max(y1, y0)
        items.append((x0, y0, x1, y1, label, c))

    for x0, y0, x1, y1, _label, c in items:
        if x1 < 0 or y1 < 0 or x0 >= iw or y0 >= ih:
            continue
        draw.rectangle([x0, y0, x1, y1], outline=c, width=lw)

    placed: List[Tuple[int, int, int, int]] = []
    pad = max(1, fs // 6)
    for x0, y0, x1, y1, label, c in items:
        if not label or x1 < 0 or y1 < 0 or x0 >= iw or y0 >= ih:
            continue
        tw, th, tx_off, ty_off = _text_size(draw, label, font)
        bw, bh = tw + 2 * pad, th + 2 * pad
        candidates = [
            (x0, y0 - bh),          # above, left-aligned (outside)
            (x0, y0),               # inside top-left
            (x1 - bw + 1, y0 - bh), # above, right-aligned
            (x0, y1 + 1),           # below, left-aligned
            (x1 + 1, y0),           # right of the box
            (x0 - bw, y0),          # left of the box
            (x1 - bw + 1, y1 - bh + 1),  # inside bottom-right
        ]
        best = None
        best_cost = None
        for cx, cy in candidates:
            cx = min(max(0, cx), max(0, iw - bw))
            cy = min(max(0, cy), max(0, ih - bh))
            rect = (cx, cy, cx + bw, cy + bh)
            cost = sum(_overlap_area(rect, p) for p in placed)
            if best_cost is None or cost < best_cost:
                best, best_cost = rect, cost
                if cost == 0:
                    break
        assert best is not None
        placed.append(best)
        draw.rectangle([best[0], best[1], best[2] - 1, best[3] - 1], fill=c)
        draw.text((best[0] + pad - tx_off, best[1] + pad - ty_off), label, fill=_contrast_text(c), font=font)
    return img


# ------------------------------------------------------------------- scaling/encoding
def downscale(image: ImageSource, max_long_edge: int) -> Tuple[Image.Image, float]:
    """Shrink ``image`` so its longest edge is at most ``max_long_edge`` pixels.

    Returns ``(image, scale)`` where ``scale <= 1`` is the factor applied
    (``image_coord = original_coord * scale``). Images already small enough are
    returned unchanged with ``scale == 1.0``.
    """
    img = load_image(image)
    if max_long_edge is None or max_long_edge <= 0:
        raise ValueError("max_long_edge must be a positive integer")
    long_edge = max(img.width, img.height)
    if long_edge <= max_long_edge:
        return img, 1.0
    s = float(max_long_edge) / float(long_edge)
    new_size = (max(1, int(round(img.width * s))), max(1, int(round(img.height * s))))
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGB")
    return img.resize(new_size, _RESAMPLE_LANCZOS), s


def to_png_bytes(image: ImageSource, *, max_size: Optional[int] = None) -> bytes:
    """Encode ``image`` as PNG bytes (optionally downscaled to ``max_size`` long edge)."""
    img = load_image(image)
    if max_size:
        img, _ = downscale(img, max_size)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def to_base64_png(image: ImageSource, *, max_size: Optional[int] = None) -> str:
    """Base64 (ASCII, no ``data:`` prefix) PNG encoding of ``image``.

    ``max_size`` limits the long edge (aspect ratio preserved).
    """
    return base64.b64encode(to_png_bytes(image, max_size=max_size)).decode("ascii")
