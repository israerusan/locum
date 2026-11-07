"""Self-repairing selectors (opt-in).

When a selector stops matching -- a button renamed from ``Save`` to
``Save as``, moved to another pane, re-created with a new name but the same
automation id -- :func:`heal` looks for the element the selector most likely
meant instead of failing:

* the candidates are the elements inside the selector's window scope (the
  windows its first compound still matches; the whole desktop for a
  one-compound selector, the element/window for scoped lookups);
* each one is scored on role (same role, or a close role such as
  ``button``/``togglebutton``), name similarity (``difflib`` ratio, token
  overlap and whole-word containment, case-insensitive), automation id (a
  strong signal), class name and -- when a previous :class:`Fingerprint` is
  known -- distance to the previous position, size and window title;
* only a **clear winner** is accepted: its score must reach ``threshold`` and
  beat the runner-up by ``margin``. Two plausible candidates fail, they are
  never guessed between.

Every heal is logged at ``WARNING`` on the ``locum.heal`` logger with a
human-readable diff (``name 'Save' -> 'Save as'; moved 40px``) and returned
as a :class:`HealInfo` (``Element.healed`` on elements found that way).

Healing is **off by default**: acting on the wrong element is dangerous. Turn
it on per desktop (``Desktop(heal_selectors=True)``), per call
(``desktop.find(sel, heal=True)``) or in flows (``heal: true`` in
``settings`` or on a step). Code that must never heal (the LLM tools: a
model should re-observe instead) runs inside :func:`suppressed`.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from ._cancel import check as _cancel_check
from .backends.base import AccessibleNode
from .types import Rect

__all__ = [
    "Fingerprint",
    "HealInfo",
    "HealOptions",
    "HealOutcome",
    "heal",
    "log_heal",
    "name_similarity",
    "suppressed",
    "is_suppressed",
    "logger",
    "DEFAULT_THRESHOLD",
    "DEFAULT_MARGIN",
]

#: Logger every accepted heal is reported on (WARNING level).
logger = logging.getLogger("locum.heal")

#: Minimum score (0..1) a candidate needs.
DEFAULT_THRESHOLD = 0.7
#: Minimum lead over the runner-up.
DEFAULT_MARGIN = 0.1
#: Cap on the nodes one heal scan visits.
DEFAULT_MAX_NODES = 20000

# Score weights (only the signals available for a target count).
_W_ROLE = 1.0
_W_NAME = 2.0
_W_ID_MATCH = 3.0
_W_ID_MISMATCH = 2.0
_W_CLASS = 0.5
_W_POS = 1.5
_W_SIZE = 0.5
_W_WINDOW = 0.5
#: Center distance (px) at which the position score reaches 0.
_POS_RANGE = 400.0

#: Roles that are close enough to be the "same" control after a UI change.
_ROLE_GROUPS = (
    frozenset({"button", "togglebutton", "splitbutton", "menuitem", "link"}),
    frozenset({"checkbox", "radiobutton", "togglebutton", "switch"}),
    frozenset({"edit", "document", "combobox", "spinbutton", "searchbox"}),
    frozenset({"listitem", "treeitem", "menuitem", "row", "cell"}),
    frozenset({"text", "label", "heading"}),
    frozenset({"window", "dialog", "pane"}),
    frozenset({"tab", "tabitem"}),
    frozenset({"list", "tree", "table", "grid"}),
    frozenset({"menu", "menubar", "toolbar"}),
)

_local = threading.local()


# ============================================================ suppression

@contextlib.contextmanager
def suppressed() -> Iterator[None]:
    """Disable healing on this thread inside the block, whatever the desktop/call says.

    For code that must never act on a guessed element (the LLM tool layer:
    a model should re-observe the screen instead).
    """
    prev = getattr(_local, "off", 0)
    _local.off = prev + 1
    try:
        yield
    finally:
        _local.off = prev


def is_suppressed() -> bool:
    return bool(getattr(_local, "off", 0))


def enabled(call: Optional[bool], default: Any) -> bool:
    """Whether a lookup heals: the per-call flag wins over the desktop default; :func:`suppressed` wins over both."""
    if is_suppressed():
        return False
    if call is not None:
        return bool(call)
    if isinstance(default, HealOptions):
        return True
    return bool(default)


# ============================================================ data

def _coerce_rect(value: Any) -> Optional[Rect]:
    if value is None:
        return None
    if isinstance(value, Rect):
        return value
    try:
        return Rect.coerce(value)
    except Exception:
        if isinstance(value, Mapping) and {"x", "y", "width", "height"} <= set(value):
            return Rect(int(value["x"]), int(value["y"]), int(value["width"]), int(value["height"]))
        raise ValueError(f"bounds must be [left, top, width, height] or a rect mapping, got {value!r}") from None


def _safe(fn: Any, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


@dataclass(frozen=True)
class Fingerprint:
    """What an element looked like when it was last found.

    Store one (``Fingerprint.from_element(el).to_dict()``) next to a selector
    in a script or flow; pass it back as ``fingerprint=`` so a heal can also
    use the previous position, size and window title.
    """

    role: str = ""
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    bounds: Optional[Rect] = None
    window: str = ""
    app: str = ""

    @classmethod
    def from_node(cls, node: AccessibleNode, *, window: Optional[str] = None) -> "Fingerprint":
        from .selectors import _chain

        role = (_safe(lambda: node.role, "") or "").lower()
        name = _safe(lambda: node.name, "") or ""
        aid = _safe(lambda: node.automation_id, "") or ""
        cls_ = _safe(lambda: node.class_name, "") or ""
        bounds = _safe(lambda: node.bounds)
        attrs = _safe(lambda: dict(node.attributes()), {}) or {}
        app = str(attrs.get("app") or attrs.get("process") or "")
        if window is None:
            top = _safe(lambda: _chain(node, None)[0])
            window = (_safe(lambda: top.name, "") or "") if top is not None and top is not node else ""
        return cls(role=role, name=name, automation_id=aid, class_name=cls_,
                   bounds=bounds if isinstance(bounds, Rect) else None, window=window or "", app=app)

    @classmethod
    def from_element(cls, element: Any) -> "Fingerprint":
        """Fingerprint of an :class:`~locum.element.Element` (or a bare node)."""
        node = getattr(element, "node", element)
        return cls.from_node(node)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Fingerprint":
        if not isinstance(data, Mapping):
            raise TypeError(f"a fingerprint must be a mapping, got {type(data).__name__}")
        unknown = set(data) - set(FINGERPRINT_KEYS)
        if unknown:
            raise ValueError(f"unknown fingerprint key(s): {', '.join(sorted(map(str, unknown)))}")
        return cls(
            role=str(data.get("role") or "").lower(),
            name=str(data.get("name") or ""),
            automation_id=str(data.get("automation_id") or data.get("id") or ""),
            class_name=str(data.get("class_name") or ""),
            bounds=_coerce_rect(data.get("bounds")),
            window=str(data.get("window") or ""),
            app=str(data.get("app") or ""),
        )

    @classmethod
    def coerce(cls, value: Any) -> Optional["Fingerprint"]:
        """``None``, a :class:`Fingerprint`, a mapping, an Element or a node."""
        if value is None or isinstance(value, Fingerprint):
            return value
        if isinstance(value, Mapping):
            return cls.from_dict(value)
        if isinstance(value, AccessibleNode) or hasattr(value, "node"):
            return cls.from_element(value)
        raise TypeError(f"fingerprint must be a Fingerprint, mapping or Element, not {type(value).__name__}")

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k in ("role", "name", "automation_id", "class_name", "window", "app"):
            v = getattr(self, k)
            if v:
                out[k] = v
        if self.bounds is not None:
            out["bounds"] = list(self.bounds.as_tuple())
        return out


FINGERPRINT_KEYS = ("role", "name", "automation_id", "id", "class_name", "bounds", "window", "app")


@dataclass(frozen=True)
class HealOptions:
    """Tuning for :func:`heal` (``Desktop(heal_selectors=HealOptions(...))`` enables healing with these).

    Attributes:
        threshold: Minimum score (0..1) the winner needs.
        margin: Minimum lead of the winner over the runner-up.
        max_nodes: Cap on the nodes one heal scan visits.
        allow_partial: Decide even when the scan stopped at ``max_nodes``. Off by
            default: a "clear winner" among the nodes scanned so far may not be the
            best match overall, and the real target may not have been scanned at
            all, so a truncated scan refuses to heal.
    """

    threshold: float = DEFAULT_THRESHOLD
    margin: float = DEFAULT_MARGIN
    max_nodes: int = DEFAULT_MAX_NODES
    allow_partial: bool = False

    def __post_init__(self) -> None:
        if not (0.0 < float(self.threshold) <= 1.0):
            raise ValueError(f"threshold must be in (0, 1], got {self.threshold!r}")
        if not (0.0 <= float(self.margin) < 1.0):
            raise ValueError(f"margin must be in [0, 1), got {self.margin!r}")
        if int(self.max_nodes) <= 0:
            raise ValueError(f"max_nodes must be > 0, got {self.max_nodes!r}")


@dataclass
class HealInfo:
    """What a heal changed: attached to the returned element as ``element.healed``."""

    old_selector: str
    new_selector: str
    score: float
    runner_up: Optional[float]
    changes: List[str]
    old: Fingerprint
    new: Fingerprint

    @property
    def diff(self) -> str:
        """Human-readable change list, e.g. ``name 'Save' -> 'Save as'; moved 40px``."""
        return "; ".join(self.changes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "old_selector": self.old_selector,
            "new_selector": self.new_selector,
            "score": round(self.score, 3),
            "runner_up": None if self.runner_up is None else round(self.runner_up, 3),
            "diff": self.diff,
            "changes": list(self.changes),
            "old": self.old.to_dict(),
            "new": self.new.to_dict(),
        }

    def __str__(self) -> str:
        return f"healed {self.old_selector!r} -> {self.new_selector!r} (score {self.score:.2f}): {self.diff}"


@dataclass
class HealOutcome:
    """Result of :func:`heal`: ``node``/``info`` on success, else ``reason`` says why not."""

    node: Optional[AccessibleNode] = None
    info: Optional[HealInfo] = None
    reason: str = ""
    candidates: List[Tuple[float, str]] = field(default_factory=list)
    #: The scan stopped at :attr:`HealOptions.max_nodes` (the candidates are partial).
    truncated: bool = False

    def __bool__(self) -> bool:
        return self.node is not None


# ============================================================ selector analysis

@dataclass
class _Target:
    role: str = ""
    name: str = ""
    name_partial: bool = False  # the selector only constrained part of the name
    automation_id: str = ""
    class_name: str = ""
    bounds: Optional[Rect] = None
    window: str = ""
    app: str = ""
    scope: Any = None  # the first compound (window part) of a multi-step branch


def _strip_glob(v: str) -> Tuple[str, bool]:
    if "*" in v or "?" in v:
        return re.sub(r"[*?]+", " ", v).strip(), True
    return v, False


def _targets(sel: Any) -> List[_Target]:
    """One :class:`_Target` per selector branch, from its last compound (and first, for scope)."""
    from .selectors import _AttrCond, _HasTextCond

    out: List[_Target] = []
    for branch in sel.branches:
        comp = branch.steps[-1][1]
        t = _Target(role=comp.role or "")
        for c in comp.conds:
            if isinstance(c, _AttrCond) and c.value is not None and c.op in ("=", "*=", "^=", "$=", "%="):
                value, globbed = _strip_glob(c.value) if c.op == "%=" else (c.value, False)
                partial = globbed or c.op in ("*=", "^=", "$=")
                if c.key in ("name", "text") and value and not t.name:
                    t.name, t.name_partial = value, partial
                elif c.key == "automation_id" and c.op == "=" and not t.automation_id:
                    t.automation_id = value
                elif c.key == "class_name" and c.op == "=" and not t.class_name:
                    t.class_name = value
                elif c.key == "role" and c.op == "=" and not t.role:
                    t.role = value.lower()
            elif isinstance(c, _HasTextCond) and not t.name:
                t.name, t.name_partial = c.text, True
        if len(branch.steps) > 1 and branch.relative is None:
            # Step 0 is an ancestor of the target or, after ``+``/``~``, a sibling of
            # one of its ancestors (or of the target): either way it lies in the same
            # top-level root, which is all the scope is used for.
            t.scope = branch.steps[0][1]
        out.append(t)
    return out


# ============================================================ scoring

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_NEG_WORD_RE = re.compile(r"\w+(?:['’]\w+)*", re.UNICODE)
# Words that turn a label into its opposite or into a way out ("Save" / "Don't Save",
# "Order" / "Cancel order"): English, German, Spanish, French, apostrophes removed.
_NEGATIONS = frozenset({
    "not", "no", "dont", "doesnt", "didnt", "cant", "cannot", "wont", "never", "without", "none",
    "cancel", "discard", "abort", "decline", "reject", "deny", "refuse", "undo",
    "nicht", "kein", "keine", "keinen", "keiner", "nein", "nie", "niemals", "ohne", "abbrechen",
    "verwerfen", "ablehnen",
    "nunca", "sin", "cancelar", "descartar", "abortar", "rechazar", "deshacer",
    "ne", "pas", "non", "jamais", "sans", "annuler", "abandonner", "rejeter", "refuser", "ignorer",
})
_NEG_PREFIXES = ("un", "dis", "de", "non", "in")
# "Un-lock" / "De-select": the prefix joined to its word ("un" alone is not a negation: it is
# the French / Spanish article in "Ajouter un fichier" / "Añadir un archivo").
_NEG_HYPHEN_RE = re.compile(r"\b(un|dis|de|in)-(?=\w)", re.UNICODE)


def _neg_words(name: str) -> List[str]:
    return [w.replace("'", "").replace("’", "") for w in _NEG_WORD_RE.findall(_NEG_HYPHEN_RE.sub(r"\1", name))]


def _negations(name: str, other_words: frozenset) -> frozenset:
    """The negating/cancelling words of ``name`` (``un``/``dis``... + a word of ``other_words`` included)."""
    out = set()
    for w in _neg_words(name):
        if w in _NEGATIONS:
            out.add(w)
        elif any(w.startswith(p) and w[len(p):] in other_words for p in _NEG_PREFIXES):
            out.add(w)  # "Unlock" vs "Lock", "Disconnect" vs "Connect"
    return frozenset(out)


def _negation_differs(a: str, b: str) -> bool:
    """Whether one name negates/cancels what the other one does (``Save`` vs ``Don't Save``)."""
    wa, wb = frozenset(_neg_words(a)), frozenset(_neg_words(b))
    return _negations(a, wb) != _negations(b, wa)


def name_similarity(a: str, b: str, *, partial: bool = False) -> float:
    """0..1 similarity of two names (case-insensitive ratio, token overlap, word containment).

    0 when one name adds a negating or cancelling word the other lacks (``Save`` vs
    ``Don't Save``/``Nicht speichern``, ``Order`` vs ``Cancel order``): those are
    opposite actions, never a rename. Extra or missing words get no bonus.
    """
    a, b = (a or "").casefold().strip(), (b or "").casefold().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _negation_differs(a, b):
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(_WORD_RE.findall(a)), set(_WORD_RE.findall(b))
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    best = max(ratio, jac)
    if ta and ta == tb:
        best = max(best, 0.9)  # "Save as..." -> "Save as": same words, other punctuation
    if partial and a in b:
        best = max(best, 0.95)
    return best


def _role_score(want: str, have: str) -> Optional[float]:
    """1 same role, 0.5 a close role, ``None`` an unrelated role (the candidate is rejected)."""
    if not want:
        return None
    if want == have:
        return 1.0
    if any(want in g and have in g for g in _ROLE_GROUPS):
        return 0.5
    return -1.0


def _center_distance(a: Rect, b: Rect) -> float:
    ca, cb = a.center, b.center
    return math.hypot(ca.x - cb.x, ca.y - cb.y)


def _size_similarity(a: Rect, b: Rect) -> float:
    def r(x: int, y: int) -> float:
        if x <= 0 and y <= 0:
            return 1.0
        return min(x, y) / max(x, y) if max(x, y) > 0 else 0.0

    return (r(a.width, b.width) + r(a.height, b.height)) / 2.0


def _score(t: _Target, fp: Fingerprint, window: str) -> Optional[float]:
    total = 0.0
    weight = 0.0
    if t.role:
        rs = _role_score(t.role, fp.role)
        if rs is None or rs < 0:
            return None
        total += _W_ROLE * rs
        weight += _W_ROLE
    id_match = False
    if t.automation_id:
        if fp.automation_id == t.automation_id:
            total += _W_ID_MATCH
            weight += _W_ID_MATCH
            id_match = True
        elif fp.automation_id:
            weight += _W_ID_MISMATCH  # a different id counts against the candidate
        else:
            weight += _W_ID_MISMATCH * 0.5
    if t.name:
        if _negation_differs(t.name.casefold(), (fp.name or "").casefold()):
            return None  # "Save" -> "Don't Save" is the opposite action: never healed, whatever else matches
        total += _W_NAME * name_similarity(t.name, fp.name, partial=t.name_partial)
        weight += _W_NAME
    if t.class_name:
        total += _W_CLASS * (1.0 if fp.class_name == t.class_name else 0.0)
        weight += _W_CLASS
    if t.bounds is not None:
        if fp.bounds is not None:
            total += _W_POS * max(0.0, 1.0 - _center_distance(t.bounds, fp.bounds) / _POS_RANGE)
            total += _W_SIZE * _size_similarity(t.bounds, fp.bounds)
        weight += _W_POS + _W_SIZE
    if t.window:
        total += _W_WINDOW * name_similarity(t.window, window)
        weight += _W_WINDOW
    if weight <= 0:
        return None
    score = total / weight
    if id_match:
        score = max(score, 0.9)  # the same automation id is the strongest identity signal
    return score


def _changes(old: Fingerprint, new: Fingerprint, *, partial_name: bool) -> List[str]:
    out: List[str] = []
    if old.role and new.role and old.role != new.role:
        out.append(f"role {old.role!r} -> {new.role!r}")
    if old.name and old.name != new.name and not (partial_name and old.name.casefold() in new.name.casefold()):
        out.append(f"name {old.name!r} -> {new.name!r}")
    if old.automation_id and old.automation_id != new.automation_id:
        out.append(f"automation_id {old.automation_id!r} -> {new.automation_id!r}")
    if old.class_name and old.class_name != new.class_name:
        out.append(f"class_name {old.class_name!r} -> {new.class_name!r}")
    if old.bounds is not None and new.bounds is not None:
        dist = _center_distance(old.bounds, new.bounds)
        if dist >= 1:
            out.append(f"moved {dist:.0f}px")
        if (old.bounds.width, old.bounds.height) != (new.bounds.width, new.bounds.height):
            out.append(f"resized {old.bounds.width}x{old.bounds.height} -> {new.bounds.width}x{new.bounds.height}")
    if old.window and new.window and old.window != new.window:
        out.append(f"window {old.window!r} -> {new.window!r}")
    if not out:
        out.append("position in the tree changed")
    return out


def _merge(t: _Target, fp: Optional[Fingerprint]) -> _Target:
    """The fingerprint (what the element really looked like) refines the selector's parts."""
    if fp is None:
        return t
    m = _Target(**{k: getattr(t, k) for k in t.__dataclass_fields__})
    if fp.role:
        m.role = fp.role
    if fp.name:
        m.name, m.name_partial = fp.name, False
    if fp.automation_id:
        m.automation_id = fp.automation_id
    if fp.class_name and not m.class_name:
        m.class_name = fp.class_name
    m.bounds = fp.bounds
    m.window = fp.window
    m.app = fp.app
    return m


# ============================================================ heal

def heal(
    selector: Any,
    roots: Union[AccessibleNode, Sequence[AccessibleNode]],
    *,
    fingerprint: Any = None,
    options: Optional[HealOptions] = None,
    include_roots: bool = True,
    selector_text: Optional[str] = None,
    selector_root: Optional[AccessibleNode] = None,
    selector_roots: Optional[Sequence[AccessibleNode]] = None,
    max_depth: int = 64,
    log: bool = True,
) -> HealOutcome:
    """Find the element a non-matching ``selector`` most likely meant (one scan of ``roots``).

    Args:
        selector: The selector (text, dict or :class:`~locum.selectors.Selector`) that found nothing.
        roots: Where the selector was searched.
        fingerprint: A previous :class:`Fingerprint` (or its dict, or an element) of the target.
        options: Threshold / margin / scan cap (default :class:`HealOptions`).
        include_roots: Whether ``roots`` themselves may be the target (``False`` for element-scoped finds).
        selector_text: The selector as the caller wrote it (for messages/logs).
        selector_root: Scope the new selector is relative to (element/window lookups).
        selector_roots: All search roots the new selector must be unique among (desktop lookups).
        log: Log an accepted heal on :data:`logger` at WARNING.

    Returns:
        A :class:`HealOutcome`; ``outcome.node`` is ``None`` when there is no clear winner.
    """
    from .selectors import _Branch, _Snapshot, _as_roots, parse, selector_for

    opts = options or HealOptions()
    sel = parse(selector)
    text = selector_text if selector_text is not None else (selector if isinstance(selector, str) else str(sel))
    fp = Fingerprint.coerce(fingerprint)
    targets = [_merge(t, fp) for t in _targets(sel)]
    targets = [t for t in targets if t.name or t.automation_id]
    if not targets:
        return HealOutcome(reason="the selector names no element (no name or automation id) to heal on")

    root_list = _as_roots(roots)
    snap = _Snapshot(root_list, max_depth)
    best: Dict[int, Tuple[float, Any, _Target, str]] = {}
    visited = 0
    for t in targets:
        # Scope: the top-level roots containing a match of the selector's first compound.
        if t.scope is not None:
            hits = _Branch([(None, t.scope)]).search(snap, snap.roots, None, True, None)
            scope_ids = sorted({e.path[0] for e in hits})
            if not scope_ids:
                continue
            scope_roots = [snap.roots[i] for i in scope_ids]
            allow_root = False
        else:
            scope_roots = list(snap.roots)
            allow_root = include_roots
        for r in scope_roots:
            window = r.prop("name") or ""
            if t.app:
                rapp = str(_safe(lambda: dict(r.node.attributes()), {}).get("app") or "")
                if rapp and rapp.casefold() != t.app.casefold():
                    continue
            for e in snap.walk(r):
                _cancel_check()
                visited += 1
                if visited > opts.max_nodes:
                    break
                if e is r and not allow_root:
                    continue
                cand = Fingerprint(
                    role=e.prop("role"), name=e.prop("name"), automation_id=e.prop("automation_id"),
                    class_name=e.prop("class_name"),
                    bounds=e.prop("bounds") if isinstance(e.prop("bounds"), Rect) else None,
                    window=window,
                )
                s = _score(t, cand, window)
                if s is None:
                    continue
                prev = best.get(id(e))
                if prev is None or s > prev[0]:
                    best[id(e)] = (s, e, t, window)
            if visited > opts.max_nodes:
                break
    truncated = visited > opts.max_nodes
    ranked = sorted(best.values(), key=lambda x: (-x[0], x[1].path))
    shown = [(round(s, 3), _describe(e.node)) for s, e, _, _ in ranked[:3]]
    if truncated and not opts.allow_partial:
        # A winner among the first max_nodes nodes is not a winner overall: the real
        # target (or a closer runner-up) may be among the nodes never scanned.
        return HealOutcome(
            reason=(f"scan truncated after {int(opts.max_nodes)} nodes: not healing on a partial scan "
                    "(raise HealOptions.max_nodes, narrow the selector's scope, or pass allow_partial=True)"),
            candidates=shown, truncated=True,
        )
    if not best:
        return HealOutcome(reason="no candidate with a compatible role (and no negated or cancelling name) "
                                  "in the selector's scope", truncated=truncated)
    top_score, top_e, top_t, top_win = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else None
    if top_score < opts.threshold:
        return HealOutcome(reason=f"best candidate {shown[0][1]} scored {top_score:.2f} < {opts.threshold:g}",
                           candidates=shown, truncated=truncated)
    if runner is not None and top_score - runner < opts.margin:
        return HealOutcome(
            reason=(f"ambiguous: {shown[0][1]} ({top_score:.2f}) and {shown[1][1]} ({runner:.2f}) "
                    f"are within {opts.margin:g} of each other"),
            candidates=shown, truncated=truncated,
        )
    node = top_e.node
    new_fp = Fingerprint.from_node(node, window=top_win if top_e.depth > 0 else "")
    old_fp = Fingerprint(role=top_t.role, name=top_t.name, automation_id=top_t.automation_id,
                         class_name=top_t.class_name, bounds=top_t.bounds, window=top_t.window, app=top_t.app)
    try:
        if selector_root is not None:
            new_sel = selector_for(node, root=selector_root)
        else:
            new_sel = selector_for(node, roots=list(selector_roots) if selector_roots is not None else root_list)
    except Exception:  # pragma: no cover - defensive: keep the heal, fall back to the describe text
        new_sel = text
    info = HealInfo(old_selector=text, new_selector=new_sel, score=top_score, runner_up=runner,
                    changes=_changes(old_fp, new_fp, partial_name=top_t.name_partial), old=old_fp, new=new_fp)
    if log:
        log_heal(info)
    return HealOutcome(node=node, info=info, candidates=shown, truncated=truncated)


def log_heal(info: HealInfo) -> None:
    """Report an accepted heal on :data:`logger` (WARNING; the record carries ``locum_heal`` = ``info.to_dict()``)."""
    logger.warning("healed selector %r -> %r (score %.2f): %s", info.old_selector, info.new_selector,
                   info.score, info.diff, extra={"locum_heal": info.to_dict()})


def _describe(node: AccessibleNode) -> str:
    from .selectors import describe_node

    try:
        return describe_node(node, include_bounds=False)
    except Exception:  # pragma: no cover - defensive
        return repr(node)
