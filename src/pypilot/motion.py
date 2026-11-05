"""Cursor motion planning: tweening, humanized Bézier paths and Fitts's-law timing.

This module is pure math -- it never touches a backend. :class:`pypilot.mouse.Mouse`
turns the waypoints produced by :func:`plan_path` into timed ``mouse_move`` calls.

Two kinds of paths are produced:

* **Mechanical** (``humanize=None``): a straight line from ``start`` to ``end``
  whose progress over time follows a *tween* (easing) function -- the same
  model PyAutoGUI uses.
* **Humanized** (``humanize=Humanize(...)``): a randomized cubic Bézier curve
  with a slight overshoot past the target followed by a short correction, plus
  small hand tremor that fades out as the cursor approaches the target. Paths
  are fully deterministic for a given ``Humanize(seed=...)``, and the final
  waypoint is always *exactly* ``end``.

:func:`fitts_duration` estimates how long a human takes to move a given
distance onto a target of a given size (Fitts's law, Shannon formulation).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from .types import Point, PointLike, Rect, RectLike

Tween = Callable[[float], float]

# --------------------------------------------------------------------------- tweens


def linear(t: float) -> float:
    """Constant speed."""
    return t


def ease_in_quad(t: float) -> float:
    """Accelerate from zero velocity."""
    return t * t


def ease_out_quad(t: float) -> float:
    """Decelerate to zero velocity."""
    return t * (2.0 - t)


def ease_in_out_quad(t: float) -> float:
    """Accelerate until halfway, then decelerate."""
    if t < 0.5:
        return 2.0 * t * t
    return -1.0 + (4.0 - 2.0 * t) * t


def ease_in_cubic(t: float) -> float:
    """Cubic acceleration from zero velocity."""
    return t * t * t


def ease_out_cubic(t: float) -> float:
    """Cubic deceleration to zero velocity."""
    u = t - 1.0
    return u * u * u + 1.0


def ease_in_out_cubic(t: float) -> float:
    """Cubic acceleration until halfway, then cubic deceleration."""
    if t < 0.5:
        return 4.0 * t * t * t
    u = 2.0 * t - 2.0
    return 0.5 * u * u * u + 1.0


def ease_out_back(t: float) -> float:
    """Decelerate past the target, then settle back onto it (built-in overshoot)."""
    c1 = 1.70158
    c3 = c1 + 1.0
    u = t - 1.0
    return 1.0 + c3 * u * u * u + c1 * u * u


def ease_in_out_sine(t: float) -> float:
    """Sinusoidal acceleration/deceleration -- the smoothest of the in-out curves."""
    return -(math.cos(math.pi * t) - 1.0) / 2.0


TWEENS: Dict[str, Tween] = {
    "linear": linear,
    "ease_in_quad": ease_in_quad,
    "ease_out_quad": ease_out_quad,
    "ease_in_out_quad": ease_in_out_quad,
    "ease_in_cubic": ease_in_cubic,
    "ease_out_cubic": ease_out_cubic,
    "ease_in_out_cubic": ease_in_out_cubic,
    "ease_out_back": ease_out_back,
    "ease_in_out_sine": ease_in_out_sine,
}


def get_tween(tween: Union[str, Tween, None]) -> Tween:
    """Resolve a tween name (``"ease_in_out_quad"``, ``"easeInOutQuad"``, ``"ease-in-out-quad"``) or callable."""
    if tween is None:
        return linear
    if callable(tween):
        return tween
    if not isinstance(tween, str):
        raise TypeError(f"tween must be a name or a callable, not {type(tween).__name__}")
    key = tween.strip()
    if key in TWEENS:
        return TWEENS[key]
    # camelCase (PyAutoGUI style) / kebab-case / spaces -> snake_case
    snake = "".join("_" + c.lower() if c.isupper() else c for c in key).lstrip("_")
    snake = snake.replace("-", "_").replace(" ", "_").lower()
    while "__" in snake:
        snake = snake.replace("__", "_")
    if snake in TWEENS:
        return TWEENS[snake]
    raise ValueError(f"Unknown tween {tween!r}; known: {', '.join(sorted(TWEENS))}")


# ------------------------------------------------------------------------ humanize


@dataclass
class Humanize:
    """Parameters for human-like cursor motion.

    Args:
        speed: Movement speed multiplier (2.0 = twice as fast). Affects Fitts timing.
        jitter: Hand-tremor amplitude multiplier (0 disables tremor).
        overshoot: Whether long moves overshoot the target slightly and correct back.
        curvature: Path bend multiplier (0 = straight line, 2 = very curved).
        seed: Random seed. With a seed every sequence of paths is reproducible.

    The :attr:`rng` is created lazily from ``seed`` and *persists*, so successive
    moves made with the same ``Humanize`` differ from each other while the whole
    sequence stays reproducible. Call :meth:`reseed` to restart the sequence.
    """

    speed: float = 1.0
    jitter: float = 1.0
    overshoot: bool = True
    curvature: float = 1.0
    seed: Optional[int] = None
    _rng: Optional[random.Random] = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.speed <= 0:
            raise ValueError("Humanize.speed must be > 0")
        if self.jitter < 0:
            raise ValueError("Humanize.jitter must be >= 0")
        if self.curvature < 0:
            raise ValueError("Humanize.curvature must be >= 0")

    @property
    def rng(self) -> random.Random:
        """The (lazily created, persistent) random generator seeded with :attr:`seed`."""
        if self._rng is None:
            self._rng = random.Random(self.seed)
        return self._rng

    def reseed(self, seed: Optional[int] = None) -> "Humanize":
        """Restart the random sequence (optionally with a new seed). Returns ``self``."""
        if seed is not None:
            self.seed = seed
        self._rng = random.Random(self.seed)
        return self


# --------------------------------------------------------------------------- timing


def fitts_duration(distance: float, target_width: float = 20, speed: float = 1.0) -> float:
    """Natural human movement time in seconds for a pointing motion (Fitts's law).

    Uses the Shannon formulation ``MT = a + b * log2(D / W + 1)`` with constants
    fitted to typical mouse pointing (a = 50 ms, b = 130 ms/bit), divided by
    ``speed`` and clamped to ``[0.08, 1.2]`` seconds. A zero distance returns
    ``0.0`` (no movement is needed).
    """
    if speed <= 0:
        raise ValueError("speed must be > 0")
    distance = abs(float(distance))
    if distance < 0.5:
        return 0.0
    width = max(1.0, float(target_width))
    mt = (0.05 + 0.13 * math.log2(distance / width + 1.0)) / speed
    return min(1.2, max(0.08, mt))


# ---------------------------------------------------------------------- path utils


def _as_xy(p: PointLike) -> Tuple[float, float]:
    from .types import coerce_point

    if isinstance(p, (tuple, list)) and len(p) == 2:
        return float(p[0]), float(p[1])
    q = coerce_point(p)
    return float(q.x), float(q.y)


def _cubic_bezier(p0, p1, p2, p3, u: float) -> Tuple[float, float]:
    v = 1.0 - u
    a, b, c, d = v * v * v, 3 * v * v * u, 3 * v * u * u, u * u * u
    return (
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    )


def _clamp_point(x: float, y: float, bounds: Optional[Rect]) -> Tuple[float, float]:
    if bounds is None:
        return x, y
    return (
        min(max(x, bounds.left), bounds.right - 1),
        min(max(y, bounds.top), bounds.bottom - 1),
    )


def _finalize(raw: Sequence[Tuple[float, float, float]], end: Point, duration: float, bounds: Optional[Rect]) -> List[Tuple[Point, float]]:
    """Round to pixels, drop consecutive duplicates and pin the last waypoint to ``end`` at ``duration``."""
    out: List[Tuple[Point, float]] = []
    for x, y, t in raw:
        x, y = _clamp_point(x, y, bounds)
        p = Point(int(round(x)), int(round(y)))
        if out and out[-1][0] == p:
            continue
        out.append((p, max(0.0, min(duration, t))))
    if out and out[-1][0] == end:
        out[-1] = (end, duration)
    else:
        out.append((end, duration))
    return out


# ---------------------------------------------------------------------- plan_path


def plan_path(
    start: PointLike,
    end: PointLike,
    *,
    duration: float,
    tween: Union[str, Tween] = "ease_in_out_quad",
    humanize: Optional[Humanize] = None,
    rate: int = 120,
    bounds: Optional[RectLike] = None,
) -> List[Tuple[Point, float]]:
    """Plan a cursor path from ``start`` to ``end``.

    Args:
        start: Starting point (point-like).
        end: Target point (point-like). The last waypoint always equals it exactly.
        duration: Total movement time in seconds. ``<= 0`` yields a single jump.
        tween: Easing applied to progress along the path (name or callable ``[0,1]->[0,1]``).
        humanize: When given, produce a randomized human-like path (see module docs).
        rate: Waypoints per second (motion sampling rate, Hz).
        bounds: Optional rect (e.g. the virtual screen) that waypoints are clamped into.

    Returns:
        A list of ``(Point, t_offset_seconds)`` with non-decreasing offsets that
        starts after ``start`` (the start point itself is not included) and ends
        with ``(end, duration)``.
    """
    ease = get_tween(tween)
    sx, sy = _as_xy(start)
    ex, ey = _as_xy(end)
    end_pt = Point(int(round(ex)), int(round(ey)))
    clamp = Rect.coerce(bounds) if bounds is not None else None
    duration = max(0.0, float(duration))
    rate = max(1, int(rate))
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy)

    if duration <= 0.0 or dist < 0.5:
        return [(end_pt, duration if dist >= 0.5 else 0.0)]

    steps = max(2, int(math.ceil(duration * rate)))

    if humanize is None:
        raw = []
        for i in range(1, steps + 1):
            f = i / steps
            p = ease(f)
            raw.append((sx + dx * p, sy + dy * p, f * duration))
        return _finalize(raw, end_pt, duration, clamp)

    return _finalize(_humanized(sx, sy, ex, ey, dist, duration, steps, ease, humanize), end_pt, duration, clamp)


def _humanized(
    sx: float, sy: float, ex: float, ey: float, dist: float, duration: float, steps: int, ease: Tween, h: Humanize
) -> List[Tuple[float, float, float]]:
    rng = h.rng
    ux, uy = (ex - sx) / dist, (ey - sy) / dist  # unit direction
    nx, ny = -uy, ux  # unit normal

    # --- overshoot target: a few % past the target (capped), slightly off-axis.
    do_overshoot = h.overshoot and dist >= 80.0
    if do_overshoot:
        over = min(28.0, dist * rng.uniform(0.025, 0.06))
        side = rng.uniform(-0.35, 0.35) * over
        tx, ty = ex + ux * over + nx * side, ey + uy * over + ny * side
        main_frac = rng.uniform(0.78, 0.86)  # share of the time spent on the main stroke
    else:
        tx, ty = ex, ey
        main_frac = 1.0

    # --- cubic Bézier control points: bend to one side (sometimes S-shaped).
    mdist = math.hypot(tx - sx, ty - sy)
    bend = mdist * h.curvature
    sign = 1.0 if rng.random() < 0.5 else -1.0
    off1 = sign * bend * rng.uniform(0.05, 0.25)
    off2 = (sign if rng.random() < 0.8 else -sign) * bend * rng.uniform(0.03, 0.2)
    a1, a2 = rng.uniform(0.2, 0.4), rng.uniform(0.6, 0.85)
    mux, muy = (tx - sx), (ty - sy)
    c1 = (sx + mux * a1 + nx * off1, sy + muy * a1 + ny * off1)
    c2 = (sx + mux * a2 + nx * off2, sy + muy * a2 + ny * off2)
    p0, p3 = (sx, sy), (tx, ty)

    # --- tremor amplitude grows sub-linearly with distance; fades out near the end.
    sigma = h.jitter * min(1.6, 0.35 + dist / 600.0)
    phase_x, phase_y = rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)
    freq = rng.uniform(1.5, 3.5)

    raw: List[Tuple[float, float, float]] = []
    main_steps = max(1, int(round(steps * main_frac))) if do_overshoot else steps
    if do_overshoot and main_steps >= steps:
        main_steps = steps - 1
    for i in range(1, main_steps + 1):
        f = i / main_steps
        x, y = _cubic_bezier(p0, c1, c2, p3, min(1.0, max(0.0, ease(f))))
        if sigma > 0.0 and i < main_steps:
            fade = math.sin(math.pi * f)  # zero at both ends of the stroke
            x += fade * (sigma * math.sin(2 * math.pi * freq * f + phase_x) + rng.gauss(0.0, sigma * 0.35))
            y += fade * (sigma * math.sin(2 * math.pi * freq * f + phase_y) + rng.gauss(0.0, sigma * 0.35))
        raw.append((x, y, f * main_frac * duration))

    if do_overshoot:
        corr_steps = steps - main_steps
        t0 = main_frac * duration
        for i in range(1, corr_steps + 1):
            f = i / corr_steps
            p = ease_out_quad(f)
            raw.append((tx + (ex - tx) * p, ty + (ey - ty) * p, t0 + f * (duration - t0)))
    return raw


def path_length(path: Sequence[Tuple[Point, float]], start: Optional[PointLike] = None) -> float:
    """Total Euclidean length of a planned path (optionally including the segment from ``start``)."""
    total = 0.0
    prev = _as_xy(start) if start is not None else None
    for p, _t in path:
        if prev is not None:
            total += math.hypot(p[0] - prev[0], p[1] - prev[1])
        prev = (float(p[0]), float(p[1]))
    return total


__all__ = [
    "TWEENS",
    "Tween",
    "Humanize",
    "plan_path",
    "fitts_duration",
    "get_tween",
    "path_length",
    *TWEENS.keys(),
]
