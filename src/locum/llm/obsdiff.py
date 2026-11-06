"""What changed between two observations (``observe(changes_only=True)``).

A full observation re-sends every element and a full screenshot (~290 KB of
PNG on a 1080p desktop) after every action. :func:`compare` instead

* matches the new elements to the previous ones -- by the backend's runtime id
  when it has one (UI Automation), else by window, role, name and position --
  and **keeps their ids**, so ``[12]`` still means the same button. Two elements
  whose runtime ids are both known and differ are never paired (a new "Delete"
  button where "Cancel" was is a new element, not Cancel renamed), and a control
  whose *name* changed is matched by position only when its name is content (a
  text display, an edit, a status bar) or its automation id is the same;
* lists only the elements that are new, changed (name, value, states, position)
  or gone, and the windows that opened or closed;
* finds the screen area whose pixels changed (ignoring tiny isolated changes such
  as a blinking caret or the taskbar clock) and picks one crop around it -- or no
  image at all when nothing visible changed, or the full screenshot when most of
  the screen did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Hashable, List, Optional, Sequence, Tuple

from ..types import Rect, WindowInfo

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

    from .observe import Observation, ObservedElement

#: Above this share of the screenshot area, the full screenshot is sent instead of a crop.
FULL_IMAGE_SHARE = 0.5


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _runtime_id(el: "ObservedElement") -> Optional[Tuple[Any, ...]]:
    rid = _safe(lambda: getattr(el.node, "runtime_id", None))
    if not rid:
        return None
    try:
        return tuple(rid)
    except TypeError:
        return None


def _b(r: Rect) -> Tuple[int, int, int, int]:
    return (r.left, r.top, r.width, r.height)


#: Roles whose name is their *content* (it changes while the element stays the same):
#: only these may be matched by position alone when the name changed. A button,
#: checkbox, menu item ... with a different name at the same place is a different control.
CONTENT_ROLES = frozenset({
    "text", "statictext", "label", "edit", "document", "statusbar", "progressbar", "slider",
    "spinbutton", "combobox", "cell", "heading", "paragraph", "tooltip", "titlebar", "value",
})


def _automation_id(el: "ObservedElement") -> str:
    aid = _safe(lambda: getattr(el.node, "automation_id", ""), "")
    return aid if isinstance(aid, str) else ""


def _position_key(e: "ObservedElement") -> Optional[Hashable]:
    """Same place, new name: a display ("Display is 0" -> "Display is 15"), or any
    control whose automation id says it is the same one ("Play" -> "Pause")."""
    aid = _automation_id(e)
    if aid:
        return (e.window_title, e.role, _b(e.bounds), "aid", aid)
    if e.role in CONTENT_ROLES:
        return (e.window_title, e.role, _b(e.bounds))
    return None


# Match tiers, strongest first. Each maps an element to a key (or None = no key).
_TIERS: Sequence[Callable[["ObservedElement"], Optional[Hashable]]] = (
    lambda e: ("rid", _runtime_id(e)) if _runtime_id(e) else None,
    lambda e: (e.window_title, e.role, e.name, _b(e.bounds)),
    _position_key,
    lambda e: (e.window_title, e.role, e.name) if e.name else None,  # same control, moved
)


def match(prev: Sequence["ObservedElement"], new: Sequence["ObservedElement"]) -> List[Tuple["ObservedElement", "ObservedElement"]]:
    """Pairs ``(old, new)`` of the same element, matched tier by tier."""
    pairs: List[Tuple["ObservedElement", "ObservedElement"]] = []
    left_prev = list(prev)
    left_new = list(new)
    # Known, different runtime ids = different elements, whatever a later tier says.
    rids: Dict[int, Optional[Tuple[Any, ...]]] = {id(e): _runtime_id(e) for e in (*left_prev, *left_new)}

    def compatible(p: "ObservedElement", n: "ObservedElement") -> bool:
        a, b = rids.get(id(p)), rids.get(id(n))
        return a is None or b is None or a == b

    for key_of in _TIERS:
        if not left_prev or not left_new:
            break
        index: Dict[Hashable, List["ObservedElement"]] = {}
        for p in left_prev:
            k = _safe(lambda: key_of(p))
            if k is not None:
                index.setdefault(k, []).append(p)
        if not index:
            continue
        used = set()
        still_new = []
        for n in left_new:
            k = _safe(lambda: key_of(n))
            bucket = index.get(k) if k is not None else None
            pick = next((i for i, p in enumerate(bucket) if compatible(p, n)), None) if bucket else None
            if pick is not None:
                p = bucket.pop(pick)
                used.add(id(p))
                pairs.append((p, n))
            else:
                still_new.append(n)
        left_new = still_new
        left_prev = [p for p in left_prev if id(p) not in used]
    return pairs


def _changes(old: "ObservedElement", new: "ObservedElement") -> List[str]:
    out = []
    if old.name != new.name:
        out.append(f"name {old.name!r}")
    if (old.value or "") != (new.value or ""):
        out.append(f"value {old.value!r}")
    st_old, st_new = set(old.states), set(new.states)
    if st_old != st_new:
        gained, lost = sorted(st_new - st_old), sorted(st_old - st_new)
        out.append("states " + ", ".join([f"+{s}" for s in gained] + [f"-{s}" for s in lost]))
    if old.bounds != new.bounds:
        out.append("moved/resized")
    return out


@dataclass
class ObservationDiff:
    """Result of :func:`compare` (element ids of ``new`` already carried over)."""

    added: List["ObservedElement"] = field(default_factory=list)
    removed: List["ObservedElement"] = field(default_factory=list)
    changed: List[Tuple["ObservedElement", "ObservedElement", List[str]]] = field(default_factory=list)
    unchanged: int = 0
    windows_opened: List[WindowInfo] = field(default_factory=list)
    windows_closed: List[WindowInfo] = field(default_factory=list)
    active_was: Optional[str] = None  # previous active title when the active window changed
    #: Screenshot-pixel rect worth showing (``None``: nothing visible changed).
    crop: Optional[Rect] = None
    full_image: bool = False
    #: Both observations hit the ``max_elements`` cap (elements near it come and go).
    capped: bool = False

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.windows_opened
                    or self.windows_closed or self.active_was is not None or self.crop is not None)

    def text(self, obs: "Observation", max_items: int = 100) -> str:
        from .observe import _quote

        lines = [
            f"Changes since the previous observation: {len(self.added)} new, {len(self.changed)} changed, "
            f"{len(self.removed)} gone, {self.unchanged} unchanged (unchanged and changed elements keep their ids)."
        ]
        if self.active_was is not None:
            aw = obs.active_window
            lines.append(f"Active window: {_quote(aw.title) if aw else '(none)'} (was {_quote(self.active_was)})")
        for label, wins in (("Windows opened", self.windows_opened), ("Windows closed", self.windows_closed)):
            if wins:
                lines.append(f"{label}: " + ", ".join(_quote(w.title) + (f" (app {w.app})" if w.app else "") for w in wins))
        if self.added:
            lines.append("New elements:")
            lines.extend("  " + obs.describe_element(e) for e in self.added[:max_items])
        if self.changed:
            lines.append("Changed elements (was ...):")
            lines.extend("  " + obs.describe_element(n) + "  (was " + "; ".join(c) + ")" for _, n, c in self.changed[:max_items])
        if self.removed:
            gone = ", ".join(f"[{e.id}] {e.role}" + (f" {_quote(e.name)}" if e.name else "") for e in self.removed[:max_items])
            lines.append(f"Gone (these ids are no longer valid): {gone}")
        hidden = sum(max(0, len(x) - max_items) for x in (self.added, self.changed, self.removed))
        if hidden:
            lines.append(f"  ... {hidden} more changes not listed (observe without changes_only for everything)")
        if obs.screenshot is not None:
            if self.full_image:
                lines.append("Screenshot: full (most of the screen changed).")
            elif self.crop is not None:
                c = self.crop
                lines.append(
                    f"Screenshot: only the changed area, a crop at ({c.left}, {c.top}) size {c.width}x{c.height} of the "
                    f"{obs.image_size[0]}x{obs.image_size[1]} screenshot space. Tools take full-screenshot coordinates: "
                    f"add ({c.left}, {c.top}) to a point you read inside the crop (or act by element id)."
                )
            else:
                lines.append("Screenshot: nothing visible changed (no image attached).")
        if self.empty:
            lines.append("Nothing changed.")
        if self.capped and (self.added or self.removed):
            lines.append(f"Note: both observations reached the {len(obs.elements)}-element cap, so some new/gone "
                         "entries may be elements of back windows crossing the cap rather than real changes; "
                         "observe one window (window=...) for an exact picture.")
        lines.extend(f"Note: {n}" for n in obs.notes)
        return "\n".join(lines)


def changed_regions(a: "Image", b: "Image", *, tolerance: int = 24, tile: int = 16, min_tiles: int = 3) -> List[Rect]:
    """Rects (pixel coordinates) of groups of changed tiles between two same-size images.

    A tile changed when any pixel channel differs by more than ``tolerance``.
    Connected groups (8-neighbourhood) of fewer than ``min_tiles`` tiles -- a
    caret blink, a clock digit -- are dropped.
    """
    import numpy as np

    if a.size != b.size:
        return [Rect(0, 0, b.size[0], b.size[1])]
    x = np.asarray(a.convert("RGB"), dtype=np.int16)
    y = np.asarray(b.convert("RGB"), dtype=np.int16)
    mask = (np.abs(x - y).max(axis=2) > tolerance)
    h, w = mask.shape
    gh, gw = (h + tile - 1) // tile, (w + tile - 1) // tile
    pad = np.zeros((gh * tile, gw * tile), dtype=bool)
    pad[:h, :w] = mask
    grid = pad.reshape(gh, tile, gw, tile).any(axis=(1, 3))
    seen = np.zeros_like(grid)
    rects: List[Rect] = []
    for gy, gx in zip(*np.nonzero(grid)):
        if seen[gy, gx]:
            continue
        stack = [(gy, gx)]
        seen[gy, gx] = True
        cells = []
        while stack:
            cy, cx = stack.pop()
            cells.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < gh and 0 <= nx < gw and grid[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if len(cells) < min_tiles:
            continue
        ys = [c[0] for c in cells]
        xs = [c[1] for c in cells]
        rects.append(Rect.from_ltrb(min(xs) * tile, min(ys) * tile, min(w, (max(xs) + 1) * tile), min(h, (max(ys) + 1) * tile)))
    return rects


def _union(rects: Sequence[Rect]) -> Optional[Rect]:
    if not rects:
        return None
    return Rect.from_ltrb(min(r.left for r in rects), min(r.top for r in rects),
                          max(r.right for r in rects), max(r.bottom for r in rects))


def compare(prev: "Observation", new: "Observation") -> ObservationDiff:
    """Diff ``new`` against ``prev``; gives ``new``'s elements the ids they had in ``prev``.

    Elements matched to a previous one keep its id; new elements get ids above
    every id used before, so an id never silently starts meaning something else.
    """
    pairs = match(prev.elements, new.elements)
    matched_new = {id(n) for _, n in pairs}
    matched_old = {id(p) for p, _ in pairs}
    diff = ObservationDiff()
    for old, n in pairs:
        n.id = old.id
        c = _changes(old, n)
        if c:
            diff.changed.append((old, n, c))
        else:
            diff.unchanged += 1
    # Above every id ever used in the chain (ids reported gone are never handed out again).
    next_id = max([e.id for e in prev.elements] + [int(getattr(prev, "_max_id", 0) or 0)], default=0) + 1
    for n in new.elements:
        if id(n) not in matched_new:
            n.id = next_id
            next_id += 1
            diff.added.append(n)
    new._max_id = next_id - 1
    diff.removed = [p for p in prev.elements if id(p) not in matched_old]
    diff.capped = bool(getattr(prev, "capped", False) and getattr(new, "capped", False))

    old_w = {w.handle: w for w in prev.windows}
    new_w = {w.handle: w for w in new.windows}
    diff.windows_opened = [w for h, w in new_w.items() if h not in old_w]
    diff.windows_closed = [w for h, w in old_w.items() if h not in new_w]
    pa, na = prev.active_window, new.active_window
    if (pa.handle if pa else None) != (na.handle if na else None):
        diff.active_was = pa.title if pa else "(none)"

    if new.screenshot is not None:
        iw, ih = new.image_size
        areas: List[Rect] = []
        if prev.screenshot is not None:
            areas.extend(changed_regions(prev.screenshot, new.screenshot))
        # Only what can be seen: an element behind another window adds nothing to the image.
        for e in [*diff.added, *(n for _, n, _ in diff.changed)]:
            if not e.occluded:
                areas.append(new.rect_to_image(e.bounds))
        for e in diff.removed:
            if not e.occluded:
                areas.append(prev.rect_to_image(e.bounds))
        box = _union([r for r in areas if r.width > 0 and r.height > 0])
        if box is not None:
            pad = 8
            box = Rect.from_ltrb(max(0, box.left - pad), max(0, box.top - pad), min(iw, box.right + pad), min(ih, box.bottom + pad))
            if box.width > 0 and box.height > 0:
                if box.width * box.height > FULL_IMAGE_SHARE * iw * ih:
                    diff.full_image = True
                else:
                    diff.crop = box
    return diff


__all__ = ["CONTENT_ROLES", "FULL_IMAGE_SHARE", "ObservationDiff", "changed_regions", "compare", "match"]
