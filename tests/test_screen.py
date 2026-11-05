import numpy as np
import pytest
from PIL import Image

from locum import ocr
from locum.backends.mock import MockBackend
from locum.screen import Screen
from locum.types import Match, Point, Rect


class FakeClock:
    """Deterministic clock: sleep() advances time and runs an optional hook."""

    def __init__(self, on_sleep=None):
        self.t = 100.0
        self.sleeps = []
        self.on_sleep = on_sleep

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        if self.on_sleep:
            self.on_sleep(self.t)


@pytest.fixture
def be():
    return MockBackend(width=640, height=480, background=(10, 20, 30))


@pytest.fixture
def scr(be):
    return Screen(be)


# ------------------------------------------------------------------ geometry
def test_monitors_size_virtual_rect():
    be = MockBackend(monitors=[(0, 0, 800, 600), (-1024, 0, 1024, 768)])
    s = Screen(be)
    assert len(s.monitors()) == 2
    assert s.size() == (800, 600)
    assert s.virtual_rect() == Rect(-1024, 0, 1824, 768)
    assert s.monitor_rect(1) == Rect(-1024, 0, 1024, 768)
    assert s.on_screen(-5, 10) and not s.on_screen(-5, 780) and not s.on_screen(5, 700) and not s.on_screen(900, 10)
    with pytest.raises(ValueError):
        s.monitor_rect(5)
    assert "mock" in repr(s)


# ------------------------------------------------------------------- capture
def test_capture_full_and_region(scr, be):
    full = scr.capture()
    assert full.mode == "RGB" and full.size == (640, 480)
    assert full.getpixel((5, 5)) == (10, 20, 30)
    assert ("screenshot", None) in be.events
    part = scr.capture((10, 20, 30, 40))
    assert part.size == (30, 40)
    assert ("screenshot", (10, 20, 30, 40)) in be.events
    assert scr.capture(Rect(0, 0, 5, 5)).size == (5, 5)
    assert scr.capture({"left": 1, "top": 2, "width": 3, "height": 4}).size == (3, 4)


def test_capture_region_partially_and_fully_offscreen(scr, be):
    be.paint(Image.new("RGB", (10, 10), (255, 0, 0)), (0, 0))
    img = scr.capture((-5, -5, 20, 20))
    assert img.size == (20, 20)
    assert img.getpixel((0, 0)) == (0, 0, 0)  # off-screen part is black
    assert img.getpixel((5, 5)) == (255, 0, 0)  # pixel (0,0) of the screen
    assert img.getpixel((15, 15)) == (10, 20, 30)
    be.clear_events()
    off = scr.capture((5000, 5000, 7, 3))
    assert off.size == (7, 3) and off.getpixel((0, 0)) == (0, 0, 0)
    assert not be.events  # backend not touched
    with pytest.raises(ValueError):
        scr.capture((0, 0, 0, 10))


def test_capture_monitor_and_relative_region():
    be = MockBackend(monitors=[(0, 0, 400, 300), (400, 0, 200, 100)])
    be.paint(Image.new("RGB", (4, 4), (1, 2, 3)), (410, 10))
    s = Screen(be)
    m1 = s.capture(monitor=1)
    assert m1.size == (200, 100)
    assert m1.getpixel((10, 10)) == (1, 2, 3)
    rel = s.capture((10, 10, 4, 4), monitor=1)
    assert rel.size == (4, 4) and rel.getpixel((0, 0)) == (1, 2, 3)
    img, origin = s.grab((10, 10, 4, 4), monitor=1)
    assert origin == Point(410, 10)
    # full virtual screen with a negative origin
    be2 = MockBackend(monitors=[(0, 0, 100, 100), (-100, 0, 100, 100)])
    be2.paint(Image.new("RGB", (1, 1), (9, 9, 9)), (-100, 0))
    img2, origin2 = Screen(be2).grab()
    assert origin2 == Point(-100, 0) and img2.size == (200, 100)
    assert img2.getpixel((0, 0)) == (9, 9, 9)


class _HiDpiBackend(MockBackend):
    def screenshot(self, region=None):
        img = super().screenshot(region)
        return img.resize((img.width * 2, img.height * 2)).convert("RGBA")


def test_capture_normalizes_size_and_mode():
    s = Screen(_HiDpiBackend(width=100, height=80))
    img = s.capture((0, 0, 50, 40))
    assert img.size == (50, 40) and img.mode == "RGB"


def test_save(scr, tmp_path):
    out = scr.save(tmp_path / "sub" / "shot.png", region=(0, 0, 20, 10))
    assert out.endswith("shot.png")
    assert Image.open(out).size == (20, 10)
    noext = scr.save(str(tmp_path / "plain"))
    assert Image.open(noext).format == "PNG"
    jpg = scr.save(tmp_path / "a.jpg")
    assert Image.open(jpg).format == "JPEG"


# -------------------------------------------------------------------- pixels
def test_pixel_and_matches(scr, be):
    be.paint(Image.new("RGB", (2, 2), (200, 100, 50)), (30, 40))
    assert scr.pixel(30, 40) == (200, 100, 50)
    assert scr.pixel(0, 0) == (10, 20, 30)
    assert scr.pixel_matches(30, 40, (200, 100, 50))
    assert scr.pixel_matches(30, 40, "#c86432")
    assert not scr.pixel_matches(30, 40, (205, 100, 50))
    assert scr.pixel_matches(30, 40, (205, 95, 50), tolerance=5)
    with pytest.raises(ValueError):
        scr.pixel(640, 0)
    with pytest.raises(ValueError):
        scr.pixel(-1, 5)


# ------------------------------------------------------------------- waiting
def test_wait_for_change_detects_change(be):
    calls = []

    def mutate(t):
        calls.append(t)
        if len(calls) == 3:
            be.paint(Image.new("RGB", (50, 50), (255, 255, 255)), (0, 0))

    fc = FakeClock(mutate)
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    assert s.wait_for_change((0, 0, 100, 100), timeout=5, interval=0.2) is True
    assert len(fc.sleeps) == 3
    assert all(x == pytest.approx(0.2) for x in fc.sleeps)


def test_wait_for_change_timeout(be):
    fc = FakeClock()
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    assert s.wait_for_change((0, 0, 100, 100), timeout=1.0, interval=0.3) is False
    assert sum(fc.sleeps) == pytest.approx(1.0)  # last sleep is trimmed to the deadline
    assert fc.sleeps[-1] == pytest.approx(0.1)


def test_wait_for_change_timeout_zero_single_check(be):
    fc = FakeClock()
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    assert s.wait_for_change(timeout=0) is False
    assert fc.sleeps == []


def test_wait_for_change_threshold_and_pixel_metric(be):
    # a tiny change on a big region: below the mean threshold, but visible as changed pixels
    def mutate(t):
        be.paint(Image.new("RGB", (4, 4), (255, 255, 255)), (300, 300))

    fc = FakeClock(mutate)
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    assert s.wait_for_change(timeout=1, interval=0.5) is False
    be.overlays.clear()
    fc2 = FakeClock(mutate)
    assert s.wait_for_change(timeout=1, interval=0.5, metric="pixels", threshold=0.0, sleep=fc2.sleep, clock=fc2.clock) is True
    with pytest.raises(ValueError):
        s.wait_for_change(metric="bogus")


def test_wait_helpers_see_local_changes_on_full_screen():
    # defaults on a whole 1920x1080 screen: a small busy spinner is not "stable",
    # and a 400x40 message bar appearing is a change (used to be averaged away)
    be = MockBackend(width=1920, height=1080, background=(235, 235, 235))
    frames = {"n": 0}

    def spin(t):
        frames["n"] += 1
        v = 0 if frames["n"] % 2 else 255
        be.overlays[:] = [(Image.new("RGB", (32, 32), (v, v, v)), Point(940, 500))]

    fc = FakeClock(spin)
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    assert s.wait_until_stable(timeout=3) is False
    assert s.wait_until_stable(metric="pixels", timeout=3) is False

    for metric in ("mean", "pixels"):
        be.overlays.clear()

        def bar(t):
            be.overlays[:] = [(Image.new("RGB", (400, 40), (30, 30, 30)), Point(760, 520))]

        fc = FakeClock(bar)
        s = Screen(be, sleep=fc.sleep, clock=fc.clock)
        assert s.wait_for_change(metric=metric, timeout=5) is True
        assert fc.t - 100.0 == pytest.approx(0.2)


def test_wait_until_stable_after_animation(be):
    frames = {"n": 0}

    def animate(t):
        frames["n"] += 1
        if frames["n"] <= 5:  # 5 frames of animation, then static
            be.overlays[:] = [(Image.new("RGB", (40, 40), (frames["n"] * 40, 0, 0)), Point(0, 0))]

    fc = FakeClock(animate)
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    start = fc.t
    assert s.wait_until_stable((0, 0, 40, 40), stable_for=0.5, timeout=10, interval=0.1) is True
    # last change happened at frame 5 (t=+0.5) -> stable at >= +1.0
    assert fc.t - start == pytest.approx(1.0)


def test_wait_until_stable_already_stable(be):
    fc = FakeClock()
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    assert s.wait_until_stable(stable_for=0.3, interval=0.1) is True
    assert len(fc.sleeps) == 3


def test_wait_until_stable_timeout_on_constant_animation(be):
    def animate(t):
        v = int(t * 10) % 2 * 255
        be.overlays[:] = [(Image.new("RGB", (40, 40), (v, v, v)), Point(0, 0))]

    fc = FakeClock(animate)
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    assert s.wait_until_stable((0, 0, 40, 40), stable_for=0.5, timeout=2, interval=0.1) is False
    assert fc.t - 100.0 == pytest.approx(2.0)


def test_wait_until_stable_slow_drift_is_not_stable(be):
    # each frame changes slightly (below threshold vs previous frame) but drifts overall
    def drift(t):
        v = min(255, int((t - 100.0) * 100))
        be.overlays[:] = [(Image.new("RGB", (40, 40), (v, v, v)), Point(0, 0))]

    fc = FakeClock(drift)
    s = Screen(be, sleep=fc.sleep, clock=fc.clock)
    # per 0.01s step the overlay changes by 1 level -> diff ~0.004 per step < threshold 0.01,
    # but compared with the start of the quiet period it exceeds it quickly
    assert s.wait_until_stable((0, 0, 40, 40), stable_for=0.2, timeout=1.0, interval=0.01, threshold=0.01) is False


# ------------------------------------------------------- vision shortcuts
def _noise(h, w, seed):
    rng = np.random.default_rng(seed)
    return Image.fromarray((rng.random((h, w, 3)) * 255).astype(np.uint8))


def test_locate_in_screen_coordinates():
    be = MockBackend(monitors=[(0, 0, 300, 200), (-300, 0, 300, 200)])
    icon = _noise(12, 12, 1)
    be.paint(icon, (-250, 50))
    be.paint(icon, (100, 150))
    s = Screen(be)
    m = s.locate(icon)
    assert isinstance(m, Match)
    assert m.rect == Rect(-250, 50, 12, 12)
    assert sorted(x.rect.left for x in s.locate_all(icon)) == [-250, 100]
    assert s.locate(icon, region=(0, 0, 300, 200)).rect == Rect(100, 150, 12, 12)
    assert s.locate(icon, monitor=0).rect == Rect(100, 150, 12, 12)
    assert s.locate(_noise(12, 12, 2)) is None


def test_find_color_in_screen_coordinates(scr, be):
    be.paint(Image.new("RGB", (5, 6), (0, 255, 0)), (100, 200))
    assert scr.find_color((0, 255, 0)) == [Rect(100, 200, 5, 6)]
    assert scr.find_color("#00ff00", region=(90, 190, 50, 50)) == [Rect(100, 200, 5, 6)]


class _FakeEngine:
    def __init__(self):
        self.images = []

    def read(self, image):
        self.images.append(image)
        return [ocr.OCRWord("Hello", Rect(2, 3, 30, 10), 0.9, 0), ocr.OCRWord("world", Rect(36, 3, 30, 10), 0.8, 0)]


def test_find_text_and_read_text_shortcuts(scr):
    eng = _FakeEngine()
    ms = scr.find_text("hello world", region=(100, 50, 200, 100), engine=eng)
    assert len(ms) == 1 and ms[0].rect == Rect(102, 53, 64, 10)
    assert eng.images[0].size == (200, 100)
    assert scr.read_text((0, 0, 10, 10), engine=eng) == "Hello world"
