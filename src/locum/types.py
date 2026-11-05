"""Core value types shared by every locum module.

These are intentionally small, immutable and dependency-free so that backends,
vision, selectors and the LLM layer can all exchange them freely (and so that
they serialize cleanly to JSON for LLM tool calls).
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, NamedTuple, Optional, Tuple, Union


class Point(NamedTuple):
    """A screen coordinate in *virtual-desktop physical pixels*."""

    x: int
    y: int

    def offset(self, dx: int = 0, dy: int = 0) -> "Point":
        return Point(self.x + dx, self.y + dy)

    def distance(self, other: "PointLike") -> float:
        ox, oy = other
        return ((self.x - ox) ** 2 + (self.y - oy) ** 2) ** 0.5

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y}


PointLike = Union[Point, Tuple[int, int]]


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle: ``left``/``top`` inclusive, ``width``/``height`` in px."""

    left: int
    top: int
    width: int
    height: int

    @classmethod
    def from_ltrb(cls, left: int, top: int, right: int, bottom: int) -> "Rect":
        return cls(int(left), int(top), int(right - left), int(bottom - top))

    @classmethod
    def coerce(cls, value: "RectLike") -> "Rect":
        """Accept a Rect, a ``(left, top, width, height)`` tuple or a dict."""
        if isinstance(value, Rect):
            return value
        if isinstance(value, dict):
            return cls(int(value["left"]), int(value["top"]), int(value["width"]), int(value["height"]))
        left, top, width, height = value
        return cls(int(left), int(top), int(width), int(height))

    @property
    def right(self) -> int:
        """Exclusive right edge."""
        return self.left + self.width

    @property
    def bottom(self) -> int:
        """Exclusive bottom edge."""
        return self.top + self.height

    @property
    def center(self) -> Point:
        return Point(self.left + self.width // 2, self.top + self.height // 2)

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def contains(self, point: PointLike) -> bool:
        x, y = point
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersect(self, other: "Rect") -> Optional["Rect"]:
        left, top = max(self.left, other.left), max(self.top, other.top)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect.from_ltrb(left, top, right, bottom)

    def union(self, other: "Rect") -> "Rect":
        return Rect.from_ltrb(
            min(self.left, other.left),
            min(self.top, other.top),
            max(self.right, other.right),
            max(self.bottom, other.bottom),
        )

    def iou(self, other: "Rect") -> float:
        inter = self.intersect(other)
        if inter is None:
            return 0.0
        union_area = self.area + other.area - inter.area
        return inter.area / union_area if union_area else 0.0

    def offset(self, dx: int = 0, dy: int = 0) -> "Rect":
        return Rect(self.left + dx, self.top + dy, self.width, self.height)

    def inflate(self, dx: int, dy: Optional[int] = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(self.left - dx, self.top - dy, self.width + 2 * dx, self.height + 2 * dy)

    def as_tuple(self) -> Tuple[int, int, int, int]:
        """``(left, top, width, height)``."""
        return (self.left, self.top, self.width, self.height)

    def as_ltrb(self) -> Tuple[int, int, int, int]:
        """``(left, top, right, bottom)`` -- the PIL ``crop`` box convention."""
        return (self.left, self.top, self.right, self.bottom)

    def to_dict(self) -> dict:
        return asdict(self)

    def __iter__(self) -> Iterator[int]:  # allows ``left, top, w, h = rect``
        return iter(self.as_tuple())


RectLike = Union[Rect, Tuple[int, int, int, int], dict]


class MouseButton(str, enum.Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"
    X1 = "x1"  # "back"
    X2 = "x2"  # "forward"

    @classmethod
    def coerce(cls, value: Union["MouseButton", str]) -> "MouseButton":
        if isinstance(value, MouseButton):
            return value
        v = str(value).strip().lower()
        aliases = {"primary": "left", "secondary": "right", "back": "x1", "forward": "x2", "wheel": "middle"}
        return cls(aliases.get(v, v))


@dataclass(frozen=True)
class Monitor:
    """One physical display, in virtual-desktop coordinates."""

    index: int
    rect: Rect
    scale: float = 1.0  # DPI scale factor (1.0 = 96 dpi on Windows, 2.0 = Retina)
    is_primary: bool = False
    name: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "rect": self.rect.to_dict(),
            "scale": self.scale,
            "is_primary": self.is_primary,
            "name": self.name,
        }


@dataclass(frozen=True)
class WindowInfo:
    """A snapshot of a top-level window. ``handle`` is backend specific (HWND, XID, CGWindowID...)."""

    handle: Any
    title: str
    rect: Rect
    app: str = ""  # process / application name, e.g. "EXCEL.EXE" or "firefox"
    pid: Optional[int] = None
    is_active: bool = False
    is_minimized: bool = False
    is_maximized: bool = False
    is_visible: bool = True
    class_name: str = ""

    def to_dict(self) -> dict:
        return {
            "handle": self.handle if isinstance(self.handle, (int, str)) else str(self.handle),
            "title": self.title,
            "rect": self.rect.to_dict(),
            "app": self.app,
            "pid": self.pid,
            "is_active": self.is_active,
            "is_minimized": self.is_minimized,
            "is_maximized": self.is_maximized,
            "is_visible": self.is_visible,
            "class_name": self.class_name,
        }


@dataclass(frozen=True)
class Match:
    """A visual match (template / text / color) on screen."""

    rect: Rect
    confidence: float
    text: str = ""
    scale: float = 1.0
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def center(self) -> Point:
        return self.rect.center

    def to_dict(self) -> dict:
        d = {"rect": self.rect.to_dict(), "confidence": round(float(self.confidence), 4), "center": self.center.to_dict()}
        if self.text:
            d["text"] = self.text
        if self.scale != 1.0:
            d["scale"] = self.scale
        return d


def coerce_point(value: Any) -> Point:
    """Accept ``Point``, ``(x, y)``, ``{"x":..,"y":..}``, a ``Rect`` (its center),
    a ``Match`` (its center), or anything with a ``.center`` / ``.bounds`` attribute."""
    if isinstance(value, Point):
        return value
    if isinstance(value, Rect):
        return value.center
    if isinstance(value, Match):
        return value.center
    if isinstance(value, dict):
        return Point(int(round(value["x"])), int(round(value["y"])))
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Point(int(round(value[0])), int(round(value[1])))
    center = getattr(value, "center", None)
    if center is not None:
        return coerce_point(center() if callable(center) else center)
    bounds = getattr(value, "bounds", None)
    if bounds is not None:
        return Rect.coerce(bounds).center
    raise TypeError(f"Cannot interpret {value!r} as a screen point")
