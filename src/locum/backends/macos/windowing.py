"""Window matching: pairing CGWindowList records with AX windows (``match_window_record``,
``pair_ax_windows``).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple


# =============================================================================
# Window matching (CGWindowList record <-> AX window)
# =============================================================================


def _record_bounds(rec: Dict[str, Any]) -> Tuple[float, float, float, float]:
    b = rec.get("kCGWindowBounds") or {}
    try:
        return (float(b.get("X", 0)), float(b.get("Y", 0)), float(b.get("Width", 0)), float(b.get("Height", 0)))
    except AttributeError:  # pragma: no cover - CGRect-like
        return (float(b[0][0]), float(b[0][1]), float(b[1][0]), float(b[1][1]))


def match_window_record(records: Iterable[Dict[str, Any]], pid: Optional[int],
                        bounds: Optional[Tuple[float, float, float, float]], title: str = "",
                        tolerance: float = 2.0) -> Optional[int]:
    """Find the ``kCGWindowNumber`` of the CG window matching an AX window.

    Candidates must belong to ``pid`` and be on layer 0. Bounds must match
    within ``tolerance`` points; when several match, the one whose title equals
    ``title`` wins. Falls back to a unique title match, then to the pid's only
    window.
    """
    cands = [r for r in records
             if (pid is None or int(r.get("kCGWindowOwnerPID", -1)) == pid)
             and int(r.get("kCGWindowLayer", 0)) == 0]
    if not cands:
        return None

    def num(r: Dict[str, Any]) -> int:
        return int(r.get("kCGWindowNumber", 0))

    if bounds is not None:
        hits = [r for r in cands
                if all(abs(a - b) <= tolerance for a, b in zip(_record_bounds(r), bounds))]
        if len(hits) == 1:
            return num(hits[0])
        if hits:
            titled = [r for r in hits if title and (r.get("kCGWindowName") or "") == title]
            return num(titled[0] if titled else hits[0])
    if title:
        titled = [r for r in cands if (r.get("kCGWindowName") or "") == title]
        if len(titled) == 1:
            return num(titled[0])
    if len(cands) == 1:
        return num(cands[0])
    return None


def _plausible_record(rec: Dict[str, Any]) -> bool:
    """A layer-0, non-transparent, non-degenerate window (what a user would call a window)."""
    if int(rec.get("kCGWindowLayer", 0)) != 0:
        return False
    if float(rec.get("kCGWindowAlpha", 1.0) or 0.0) <= 0.0:
        return False
    if str(rec.get("kCGWindowOwnerName") or "") == "Window Server":
        return False
    _, _, w, h = _record_bounds(rec)
    return w > 1 and h > 1


def pair_ax_windows(records: Iterable[Dict[str, Any]],
                    ax_windows: Sequence[Tuple[Optional[Tuple[float, float, float, float]], str, Optional[int]]],
                    tolerance: float = 2.0) -> Dict[int, int]:
    """Pair one app's AX windows with its CG windows, one-to-one.

    ``records`` are the app's ``CGWindowList`` records; ``ax_windows`` holds
    ``(bounds_points, title, window_id)`` per AX window, where ``window_id`` is
    the exact ``CGWindowID`` when known (``_AXUIElementGetWindow``) or ``None``.
    Returns ``{ax_index: kCGWindowNumber}``.

    Exact ids win. The rest are paired in rounds, each accepting only pairs that
    are unique on *both* sides: equal frame and equal title; equal frame and no
    title conflict; equal frame (CG and AX titles can differ, e.g. Java apps);
    equal (non-empty) title; and finally a single leftover AX
    window with a single leftover record whose titles do not conflict. Windows
    that stay ambiguous (two untitled windows with the same frame) are left
    unpaired rather than guessed, so two AX windows never share a handle and an
    action never lands on the wrong window.
    """
    recs = [r for r in records if _plausible_record(r)]
    nums = [int(r.get("kCGWindowNumber", 0)) for r in recs]
    rtitles = [str(r.get("kCGWindowName") or "") for r in recs]
    rbounds = [_record_bounds(r) for r in recs]
    out: Dict[int, int] = {}
    used: Set[int] = set()
    for i, (_, _, wid) in enumerate(ax_windows):
        if wid is not None and wid in nums:
            j = nums.index(wid)
            if j not in used:
                out[i] = j
                used.add(j)

    def same_frame(i: int, j: int) -> bool:
        b = ax_windows[i][0]
        return b is not None and all(abs(p - q) <= tolerance for p, q in zip(b, rbounds[j]))

    def same_title(i: int, j: int) -> bool:
        return bool(ax_windows[i][1]) and ax_windows[i][1] == rtitles[j]

    def no_conflict(i: int, j: int) -> bool:
        return not ax_windows[i][1] or not rtitles[j] or ax_windows[i][1] == rtitles[j]

    rounds: List[Callable[[int, int], bool]] = [
        lambda i, j: same_frame(i, j) and same_title(i, j),
        lambda i, j: same_frame(i, j) and no_conflict(i, j),
        same_frame,
        same_title,
        no_conflict,
    ]
    for k, ok in enumerate(rounds):
        progress = True
        while progress:
            progress = False
            free_a = [i for i in range(len(ax_windows)) if i not in out]
            free_r = [j for j in range(len(recs)) if j not in used]
            if k == len(rounds) - 1 and (len(free_a) != 1 or len(free_r) != 1):
                break
            for i in free_a:
                cands = [j for j in free_r if ok(i, j)]
                if len(cands) == 1 and sum(1 for i2 in free_a if ok(i2, cands[0])) == 1:
                    out[i] = cands[0]
                    used.add(cands[0])
                    progress = True
                    break
    return {i: nums[j] for i, j in out.items()}
