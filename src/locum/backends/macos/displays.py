"""Display geometry: per-display Retina scaling (``DisplayInfo``), the global layout with
point/pixel conversion (``DisplayLayout``) and screenshot planning (``capture_plan``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple

from ...errors import BackendError
from ...types import Monitor, Point, Rect


# =============================================================================
# Display geometry: points <-> pixels
# =============================================================================


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


@dataclass(frozen=True)
class DisplayInfo:
    """One display. ``x, y, width, height`` are Quartz global *points*;
    ``pixel_left``/``pixel_top`` are assigned by :class:`DisplayLayout`.
    ``work_area`` is the visible frame (minus menu bar / Dock) in points."""

    display_id: int
    x: float
    y: float
    width: float
    height: float
    scale: float = 1.0
    is_primary: bool = False
    name: str = ""
    pixel_left: int = 0
    pixel_top: int = 0
    work_area: Optional[Tuple[float, float, float, float]] = None

    @property
    def pixel_width(self) -> int:
        return _round(self.width * self.scale)

    @property
    def pixel_height(self) -> int:
        return _round(self.height * self.scale)

    @property
    def pixel_rect(self) -> Rect:
        return Rect(self.pixel_left, self.pixel_top, self.pixel_width, self.pixel_height)

    def contains_points(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def contains_pixels(self, x: float, y: float) -> bool:
        return (self.pixel_left <= x < self.pixel_left + self.pixel_width
                and self.pixel_top <= y < self.pixel_top + self.pixel_height)

    def points_to_pixels(self, x: float, y: float) -> Tuple[float, float]:
        return (self.pixel_left + (x - self.x) * self.scale, self.pixel_top + (y - self.y) * self.scale)

    def pixels_to_points(self, x: float, y: float) -> Tuple[float, float]:
        return (self.x + (x - self.pixel_left) / self.scale, self.y + (y - self.pixel_top) / self.scale)

    def distance_points(self, x: float, y: float) -> float:
        dx = max(self.x - x, 0.0, x - (self.x + self.width))
        dy = max(self.y - y, 0.0, y - (self.y + self.height))
        return math.hypot(dx, dy)

    def distance_pixels(self, x: float, y: float) -> float:
        r = self.pixel_rect
        dx = max(r.left - x, 0.0, x - r.right)
        dy = max(r.top - y, 0.0, y - r.bottom)
        return math.hypot(dx, dy)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)


def _adjacent_origin(p: DisplayInfo, d: DisplayInfo, eps: float = 0.5) -> Optional[Tuple[int, int]]:
    """Pixel origin for ``d`` if it shares an edge with the placed display ``p``."""
    found = _adjacent_placement(p, d, eps)
    return None if found is None else found[0]


def _adjacent_placement(p: DisplayInfo, d: DisplayInfo,
                        eps: float = 0.5) -> Optional[Tuple[Tuple[int, int], str]]:
    """Like :func:`_adjacent_origin` but also reports which side of ``p`` ``d`` is on
    (``"right"``, ``"left"``, ``"below"`` or ``"above"``)."""
    if _overlap(d.y, d.y + d.height, p.y, p.y + p.height) > 0:
        top = p.pixel_top + _round((d.y - p.y) * p.scale)
        if abs(d.x - (p.x + p.width)) <= eps:  # d right of p
            return (p.pixel_left + p.pixel_width, top), "right"
        if abs((d.x + d.width) - p.x) <= eps:  # d left of p
            return (p.pixel_left - d.pixel_width, top), "left"
    if _overlap(d.x, d.x + d.width, p.x, p.x + p.width) > 0:
        left = p.pixel_left + _round((d.x - p.x) * p.scale)
        if abs(d.y - (p.y + p.height)) <= eps:  # d below p
            return (left, p.pixel_top + p.pixel_height), "below"
        if abs((d.y + d.height) - p.y) <= eps:  # d above p
            return (left, p.pixel_top - d.pixel_height), "above"
    return None


def _rects_intersect(a: Tuple[int, int, int, int], b: Rect) -> bool:
    """``a`` is ``(left, top, width, height)``; touching edges do not intersect."""
    return (a[0] < b.right and b.left < a[0] + a[2] and a[1] < b.bottom and b.top < a[1] + a[3])


def _is_free(rect: Tuple[int, int, int, int], placed: Sequence[DisplayInfo]) -> bool:
    return not any(_rects_intersect(rect, q.pixel_rect) for q in placed)


def _slide_along_edge(origin: Tuple[int, int], side: str, d: DisplayInfo, p: DisplayInfo,
                      placed: Sequence[DisplayInfo]) -> Optional[Tuple[int, int]]:
    """Slide ``d`` along its shared edge with ``p`` to the nearest free position.

    Positions that keep the two displays in contact are preferred; among those
    (and then among the remaining ones) the smallest shift wins.
    """
    w, h = d.pixel_width, d.pixel_height
    vertical_edge = side in ("left", "right")  # shared edge is vertical -> slide on y
    base = origin[1] if vertical_edge else origin[0]
    size = h if vertical_edge else w
    cands = set()
    for q in placed:
        r = q.pixel_rect
        lo, hi = (r.top, r.bottom) if vertical_edge else (r.left, r.right)
        cands.add(hi)          # just after q
        cands.add(lo - size)   # just before q
    p_lo, p_hi = (p.pixel_top, p.pixel_top + p.pixel_height) if vertical_edge else (
        p.pixel_left, p.pixel_left + p.pixel_width)
    best: Optional[Tuple[int, int, int]] = None  # (no_contact, shift, value)
    for v in cands:
        rect = (origin[0], v, w, h) if vertical_edge else (v, origin[1], w, h)
        if not _is_free(rect, placed):
            continue
        contact = min(v + size, p_hi) - max(v, p_lo) > 0
        key = (0 if contact else 1, abs(v - base), v)
        if best is None or key < best:
            best = key
    if best is None:
        return None
    v = best[2]
    return (origin[0], v) if vertical_edge else (v, origin[1])


def _push_outward(origin: Tuple[int, int], d: DisplayInfo, primary: DisplayInfo,
                  placed: Sequence[DisplayInfo]) -> Tuple[int, int]:
    """Last resort: move ``d`` away from the primary display until it overlaps nothing."""
    w, h = d.pixel_width, d.pixel_height
    # direction: where ``d`` lies relative to the primary in point space (falling
    # back to pixel space for a display centred on the primary)
    ox = (d.x + d.width / 2) - (primary.x + primary.width / 2)
    oy = (d.y + d.height / 2) - (primary.y + primary.height / 2)
    if not ox and not oy:
        ox = origin[0] + w / 2 - (primary.pixel_left + primary.pixel_width / 2)
        oy = origin[1] + h / 2 - (primary.pixel_top + primary.pixel_height / 2)
    horizontal = abs(ox) >= abs(oy)
    sign = 1 if (ox if horizontal else oy) >= 0 else -1
    left, top = origin
    for _ in range(len(placed) + 1):
        rect = (left, top, w, h)
        hits = [q.pixel_rect for q in placed if _rects_intersect(rect, q.pixel_rect)]
        if not hits:
            break
        if horizontal:
            left = max(r.right for r in hits) if sign > 0 else min(r.left for r in hits) - w
        else:
            top = max(r.bottom for r in hits) if sign > 0 else min(r.top for r in hits) - h
    else:  # still overlapping something: go beyond every placed display
        if horizontal:
            left = (max(q.pixel_rect.right for q in placed) if sign > 0
                    else min(q.pixel_rect.left for q in placed) - w)
        else:
            top = (max(q.pixel_rect.bottom for q in placed) if sign > 0
                   else min(q.pixel_rect.top for q in placed) - h)
    return (left, top)


class DisplayLayout:
    """Maps Quartz points to the backend's virtual *pixel* space and back.

    See the module docstring for the placement rules. ``displays`` is ordered
    with the primary display first (index 0 == primary monitor).
    """

    def __init__(self, displays: Sequence[DisplayInfo]):
        if not displays:
            raise BackendError("No active displays")
        primary_idx = next((i for i, d in enumerate(displays) if d.is_primary), None)
        if primary_idx is None:
            primary_idx = next((i for i, d in enumerate(displays) if d.contains_points(0, 0)), 0)
        ordered = [displays[primary_idx]] + [d for i, d in enumerate(displays) if i != primary_idx]
        first = ordered[0]
        placed = [replace(first, is_primary=True, pixel_left=_round(first.x * first.scale),
                          pixel_top=_round(first.y * first.scale))]
        pending = [replace(d, is_primary=False) for d in ordered[1:]]
        while pending:
            choice = self._choose_next(placed, pending)
            i, (left, top) = choice
            placed.append(replace(pending.pop(i), pixel_left=left, pixel_top=top))
        self.displays: List[DisplayInfo] = placed

    @staticmethod
    def _choose_next(placed: Sequence[DisplayInfo],
                     pending: Sequence[DisplayInfo]) -> Tuple[int, Tuple[int, int]]:
        """Pick the next display to place and its pixel origin.

        The result never overlaps an already placed display's pixel rect: with
        mixed scale factors, offsets along a shared edge are measured in the
        neighbour's scale, which can push the new rect onto another display. In
        that case the other adjacent neighbours are tried, then the display is
        slid along its shared edge, and as a last resort it is pushed outward
        (away from the primary) until it is free.
        """
        primary = placed[0]
        fallback: Optional[Tuple[int, Tuple[int, int], str, DisplayInfo]] = None
        for i, d in enumerate(pending):
            w, h = d.pixel_width, d.pixel_height
            adjacent = []
            for p in placed:
                found = _adjacent_placement(p, d)
                if found is not None:
                    adjacent.append((found[0], found[1], p))
            for origin, _side, _p in adjacent:
                if _is_free((origin[0], origin[1], w, h), placed):
                    return i, origin
            if adjacent and fallback is None:
                fallback = (i, adjacent[0][0], adjacent[0][1], adjacent[0][2])
        if fallback is not None:
            i, origin, side, p = fallback
            d = pending[i]
            for o2, s2, p2 in [(origin, side, p)] + [
                    (f[0], f[1], q) for q in placed if q is not p
                    for f in [_adjacent_placement(q, d)] if f is not None]:
                slid = _slide_along_edge(o2, s2, d, p2, placed)
                if slid is not None:
                    return i, slid
            return i, _push_outward(origin, d, primary, placed)
        # nothing touches: place relative to the nearest display
        best = None
        for i, d in enumerate(pending):
            cx, cy = d.x + d.width / 2, d.y + d.height / 2
            for p in placed:
                dist = p.distance_points(cx, cy)
                if best is None or dist < best[0]:
                    best = (dist, i, p)
        assert best is not None
        _, i, p = best
        d = pending[i]
        origin = (p.pixel_left + _round((d.x - p.x) * p.scale),
                  p.pixel_top + _round((d.y - p.y) * p.scale))
        if not _is_free((origin[0], origin[1], d.pixel_width, d.pixel_height), placed):
            origin = _push_outward(origin, d, primary, placed)
        return i, origin

    # -- lookup -------------------------------------------------------------------
    @property
    def primary(self) -> DisplayInfo:
        return self.displays[0]

    def display_at_points(self, x: float, y: float) -> DisplayInfo:
        """Display containing the point, else the nearest one."""
        for d in self.displays:
            if d.contains_points(x, y):
                return d
        return min(self.displays, key=lambda d: d.distance_points(x, y))

    def display_at_pixels(self, x: float, y: float) -> DisplayInfo:
        for d in self.displays:
            if d.contains_pixels(x, y):
                return d
        return min(self.displays, key=lambda d: d.distance_pixels(x, y))

    # -- conversions ---------------------------------------------------------------
    def point_to_pixel(self, x: float, y: float) -> Point:
        px, py = self.display_at_points(x, y).points_to_pixels(x, y)
        return Point(_round(px), _round(py))

    def pixel_to_point(self, x: float, y: float) -> Tuple[float, float]:
        return self.display_at_pixels(x, y).pixels_to_points(x, y)

    def points_rect_to_pixels(self, x: float, y: float, w: float, h: float) -> Rect:
        d = self.display_at_points(x + w / 2, y + h / 2)
        left, top = d.points_to_pixels(x, y)
        right, bottom = d.points_to_pixels(x + w, y + h)
        return Rect.from_ltrb(_round(left), _round(top), _round(right), _round(bottom))

    def pixels_rect_to_points(self, rect: Rect) -> Tuple[float, float, float, float]:
        d = self.display_at_pixels(rect.left + rect.width / 2, rect.top + rect.height / 2)
        x, y = d.pixels_to_points(rect.left, rect.top)
        return (x, y, rect.width / d.scale, rect.height / d.scale)

    @property
    def virtual_pixel_rect(self) -> Rect:
        r = self.displays[0].pixel_rect
        for d in self.displays[1:]:
            r = r.union(d.pixel_rect)
        return r

    def monitors(self) -> List[Monitor]:
        return [Monitor(index=i, rect=d.pixel_rect, scale=d.scale, is_primary=(i == 0), name=d.name)
                for i, d in enumerate(self.displays)]


def capture_plan(display: DisplayInfo, region: Rect) -> Tuple[Tuple[int, int, int, int], Tuple[int, int], Tuple[int, int, int, int]]:
    """Plan the capture of ``region`` (pixels, already clipped to ``display``).

    Returns ``(points_rect, expected_size, crop_box)``: grab ``points_rect``
    (``x, y, w, h`` integer global points enclosing the region), resize the
    result to ``expected_size`` pixels if the grabber returned another size, and
    crop ``crop_box`` (PIL ``ltrb``) to get exactly ``region``.
    """
    s = display.scale
    lx0 = region.left - display.pixel_left
    ly0 = region.top - display.pixel_top
    lx1 = region.right - display.pixel_left
    ly1 = region.bottom - display.pixel_top
    px0 = max(0, int(math.floor(lx0 / s + 1e-9)))
    py0 = max(0, int(math.floor(ly0 / s + 1e-9)))
    px1 = min(int(math.ceil(display.width)), int(math.ceil(lx1 / s - 1e-9)))
    py1 = min(int(math.ceil(display.height)), int(math.ceil(ly1 / s - 1e-9)))
    px1 = max(px1, px0 + 1)
    py1 = max(py1, py0 + 1)
    pts = (_round(display.x) + px0, _round(display.y) + py0, px1 - px0, py1 - py0)
    expected = (_round((px1 - px0) * s), _round((py1 - py0) * s))
    ox = lx0 - _round(px0 * s)
    oy = ly0 - _round(py0 * s)
    crop = (ox, oy, ox + region.width, oy + region.height)
    return pts, expected, crop
