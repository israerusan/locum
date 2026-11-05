import base64
import io
import time

import numpy as np
import pytest
from PIL import Image

from locum import vision
from locum.backends.mock import MockBackend
from locum.types import Match, Rect


def noise(h, w, seed=0, c=3):
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, c)) * 255).astype(np.uint8)


def textured_needle(h=20, w=24, seed=1):
    return noise(h, w, seed)


# ----------------------------------------------------------------------- load_image
class TestLoadImage:
    def test_pil_passthrough_and_modes(self):
        img = Image.new("RGB", (4, 3), (1, 2, 3))
        assert vision.load_image(img) is img
        assert vision.load_image(Image.new("RGBA", (2, 2))).mode == "RGBA"
        assert vision.load_image(Image.new("1", (2, 2))).mode == "L"
        p = Image.new("P", (2, 2))
        assert vision.load_image(p).mode == "RGB"
        p.info["transparency"] = 0
        assert vision.load_image(p).mode == "RGBA"
        i16 = Image.fromarray(np.array([[0, 1000], [2000, 4000]], dtype=np.int32))
        out = vision.load_image(i16)
        assert out.mode == "L" and np.asarray(out).max() == 255

    def test_numpy_arrays(self):
        assert vision.load_image(np.zeros((5, 6), np.uint8)).size == (6, 5)
        assert vision.load_image(np.zeros((5, 6, 3), np.uint8)).mode == "RGB"
        assert vision.load_image(np.zeros((5, 6, 4), np.uint8)).mode == "RGBA"
        assert vision.load_image(np.zeros((5, 6, 1), np.uint8)).mode == "L"
        f = vision.load_image(np.ones((2, 2, 3), np.float32) * 0.5)
        assert np.asarray(f)[0, 0, 0] == 128
        b = vision.load_image(np.ones((2, 2), bool))
        assert np.asarray(b)[0, 0] == 255
        with pytest.raises(ValueError):
            vision.load_image(np.zeros((2, 2, 7), np.uint8))

    def test_paths_bytes_base64_dataurl_filelike(self, tmp_path):
        img = Image.fromarray(noise(8, 9))
        path = tmp_path / "x.png"
        img.save(path)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        b64 = base64.b64encode(raw).decode()
        for src in (path, str(path), raw, bytearray(raw), b64, "data:image/png;base64," + b64, io.BytesIO(raw)):
            out = vision.load_image(src)
            assert out.size == (9, 8)
            assert np.array_equal(np.asarray(out.convert("RGB")), np.asarray(img))

    def test_missing_path(self):
        with pytest.raises(FileNotFoundError):
            vision.load_image("/no/such/file.png")
        with pytest.raises(TypeError):
            vision.load_image(12.5)


def test_parse_color():
    assert vision.parse_color("#ff0080") == (255, 0, 128)
    assert vision.parse_color("red") == (255, 0, 0)
    assert vision.parse_color((1, 2, 3, 4)) == (1, 2, 3)
    assert vision.parse_color(0x102030) == (16, 32, 48)
    with pytest.raises(ValueError):
        vision.parse_color((1, 2))


# --------------------------------------------------------------------------- locate
class TestLocate:
    def test_exact_match_rgb_and_gray(self):
        hay = noise(120, 160)
        needle = hay[37:57, 91:115].copy()
        m = vision.locate(needle, hay)
        assert isinstance(m, Match)
        assert m.rect == Rect(91, 37, 24, 20)
        assert m.confidence > 0.999
        g = vision.locate(needle, hay, grayscale=True)
        assert g.rect == Rect(91, 37, 24, 20)

    def test_matches_brute_force_ncc(self):
        hay = noise(30, 40, seed=3).astype(np.float64)
        ndl = noise(6, 7, seed=4).astype(np.float64)
        score = vision.match_template(ndl.astype(np.uint8), hay.astype(np.uint8))
        assert score.shape == (25, 34)
        for (y, x) in [(0, 0), (10, 20), (24, 33), (5, 7)]:
            win = hay[y : y + 6, x : x + 7]
            a = win - win.reshape(-1, 3).mean(0)
            b = ndl - ndl.reshape(-1, 3).mean(0)
            ref = (a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum())
            assert score[y, x] == pytest.approx(ref, abs=1e-9)

    def test_offset_and_region(self):
        hay = noise(100, 100, seed=5)
        needle = hay[60:70, 10:25]
        m = vision.locate(needle, hay, offset=(1000, -50))
        assert m.rect == Rect(1010, 10, 15, 10)
        m2 = vision.locate(needle, hay, region=(5, 55, 40, 30), offset=(1, 1))
        assert m2.rect == Rect(11, 61, 15, 10)
        # region excluding the target
        assert vision.locate(needle, hay, region=(50, 0, 50, 50)) is None
        # region completely outside the haystack
        assert vision.locate(needle, hay, region=(500, 500, 10, 10)) is None
        assert vision.locate_all(needle, hay, region=(500, 500, 10, 10)) == []

    def test_confidence_threshold_and_not_found(self):
        hay = noise(80, 80, seed=6)
        other = noise(10, 10, seed=99)
        assert vision.locate(other, hay) is None
        m = vision.locate(other, hay, confidence=0.0)
        assert m is not None and 0.0 <= m.confidence < 0.9

    def test_needle_larger_than_haystack(self):
        hay = noise(10, 10)
        big = noise(20, 5)
        assert vision.locate(big, hay) is None
        assert vision.locate_all(big, hay) == []
        assert vision.match_template(big, hay).shape == (0, 0)

    def test_needle_equal_to_haystack(self):
        hay = noise(12, 15)
        m = vision.locate(hay, hay)
        assert m.rect == Rect(0, 0, 15, 12)

    def test_brightness_contrast_invariance(self):
        hay = noise(60, 60, seed=8).astype(np.float64)
        needle = hay[20:35, 30:50] * 0.6 + 40
        m = vision.locate(needle.astype(np.uint8), hay.astype(np.uint8), confidence=0.98)
        assert m is not None and m.rect.left == 30 and m.rect.top == 20

    def test_flat_needle_matches_solid_region(self):
        hay = noise(100, 100, seed=9)
        hay[40:60, 10:40] = (200, 30, 30)
        needle = np.zeros((10, 10, 3), np.uint8)
        needle[:] = (200, 30, 30)
        m = vision.locate(needle, hay)
        assert m is not None and m.confidence == pytest.approx(1.0)
        assert Rect(10, 40, 30, 20).contains(m.rect.center)
        ms = vision.locate_all(needle, hay, overlap=0.0)
        assert 2 <= len(ms) <= 6  # at most 3 x 2 non-overlapping tiles fit in 30x20
        assert ms[0].rect == Rect(10, 40, 10, 10)  # exact ties resolve in reading order
        for i, mm in enumerate(ms):
            assert mm.rect.intersect(Rect(10, 40, 30, 20)) == mm.rect
            assert mm.confidence == 1.0
            for other in ms[i + 1 :]:
                assert mm.rect.iou(other.rect) == 0.0
        # a different solid color does not match
        assert vision.locate(np.full((10, 10, 3), 90, np.uint8), hay) is None

    def test_textured_needle_does_not_match_flat_haystack(self):
        hay = np.full((50, 50, 3), 128, np.uint8)
        assert vision.locate(textured_needle(), hay, confidence=0.1) is None
        score = vision.match_template(textured_needle(), hay)
        assert np.all(score == 0) and np.all(np.isfinite(score))

    def test_alpha_mask_ignores_transparent_pixels(self):
        rng = np.random.default_rng(11)
        hay = noise(100, 120, seed=10)
        icon = noise(16, 16, seed=12)
        hay[30:46, 70:86] = icon
        # same icon elsewhere but with its corners replaced by garbage
        hay[60:76, 10:26] = icon
        hay[60:64, 10:14] = 255 - hay[60:64, 10:14]
        rgba = np.dstack([icon, np.full((16, 16), 255, np.uint8)])
        rgba[:4, :4, 3] = 0  # corner transparent
        # needle pixels under the mask are random garbage too
        rgba[:4, :4, :3] = rng.integers(0, 255, (4, 4, 3))
        ms = vision.locate_all(rgba, hay, confidence=0.99)
        rects = sorted((m.rect.left, m.rect.top) for m in ms)
        assert rects == [(10, 60), (70, 30)]
        # without the mask the damaged copy does not reach 0.99
        plain = vision.locate_all(icon, hay, confidence=0.99)
        assert [(m.rect.left, m.rect.top) for m in plain] == [(70, 30)]

    def test_alpha_masked_icon_on_any_background(self):
        # a circle icon on transparent background, rendered on two different bgs
        size = 21
        yy, xx = np.mgrid[:size, :size]
        circle = (yy - 10) ** 2 + (xx - 10) ** 2 <= 81
        fg = noise(size, size, seed=21)
        icon = np.zeros((size, size, 4), np.uint8)
        icon[..., :3] = fg
        icon[..., 3] = np.where(circle, 255, 0)
        hay = np.zeros((60, 100, 3), np.uint8)
        hay[:, :50] = (240, 240, 240)
        hay[:, 50:] = (20, 20, 90)
        for (x, y) in [(5, 10), (70, 30)]:
            region = hay[y : y + size, x : x + size]
            region[circle] = fg[circle]
        ms = vision.locate_all(icon, hay, confidence=0.98)
        assert sorted((m.rect.left, m.rect.top) for m in ms) == [(5, 10), (70, 30)]

    def test_fully_transparent_needle_raises(self):
        with pytest.raises(ValueError):
            vision.locate(np.zeros((5, 5, 4), np.uint8), noise(20, 20))

    def test_masked_flat_needle(self):
        # a solid green "button" with a transparent (cut-off) corner
        hay = noise(50, 50, seed=2)
        hay[10:20, 10:20] = (0, 255, 0)
        hay[10, 10] = (200, 30, 30)
        needle = np.zeros((10, 10, 4), np.uint8)
        needle[..., 1] = 255
        needle[..., 3] = 255
        needle[0, 0, 3] = 0
        needle[0, 0, :3] = (9, 9, 9)
        m = vision.locate(needle, hay)
        assert m is not None and m.rect == Rect(10, 10, 10, 10)
        # inside a larger solid green area the cut-off corner's shape is not there
        hay[30:45, 30:45] = (0, 255, 0)
        assert [m.rect for m in vision.locate_all(needle, hay)] == [Rect(10, 10, 10, 10)]

    def test_monochrome_glyph_needs_its_shape(self):
        # a 16x16 dark "X" glyph with a transparent background (typical icon PNG)
        glyph = np.zeros((16, 16, 4), np.uint8)
        glyph[..., :3] = 40
        for i in range(16):
            for d in (-1, 0, 1):
                for j in (i + d, 15 - i + d):
                    if 0 <= j < 16:
                        glyph[i, j, 3] = 255
        icon = glyph[..., 3] > 0
        for gray in (False, True):
            hay = np.full((300, 400, 3), 245, np.uint8)
            hay[0:30, :] = 40  # dark title bar of exactly the glyph color
            hay[200:216, 300:316][icon] = 40  # the real icon
            m = vision.locate(glyph, hay, grayscale=gray)
            assert m is not None and m.rect == Rect(300, 200, 16, 16) and m.confidence == 1.0
            assert [m.rect for m in vision.locate_all(glyph, hay, grayscale=gray)] == [Rect(300, 200, 16, 16)]
            # mostly-dark surroundings are not the glyph either
            hay[100:120, 100:120] = 40
            hay[100:103, 100:120] = 245
            assert vision.locate(glyph, hay, region=(90, 90, 40, 40), grayscale=gray) is None
            # the strip alone: no match at all (used to be confidence 1.0 at (0, 0))
            assert vision.locate(glyph, hay[:30], grayscale=gray) is None

    def test_multiscale(self):
        base = Image.fromarray(noise(12, 12, seed=30)).resize((24, 24), Image.NEAREST)
        hay = np.array(Image.fromarray(noise(150, 150, seed=31)))
        scaled = np.asarray(base.resize((36, 36), Image.BILINEAR))
        hay[50:86, 70:106] = scaled
        assert vision.locate(base, hay, confidence=0.9) is None
        m = vision.locate(base, hay, scales=(0.75, 1.0, 1.5, 2.0), confidence=0.9)
        assert m is not None and m.scale == 1.5
        assert abs(m.rect.left - 70) <= 1 and abs(m.rect.top - 50) <= 1
        assert (m.rect.width, m.rect.height) == (36, 36)
        # scales making the needle too big are silently skipped
        assert vision.locate(base, hay, scales=(100.0,)) is None
        with pytest.raises(ValueError):
            vision.locate(base, hay, scales=(0.0,))

    def test_rgba_haystack_and_gray_needle(self):
        hay = noise(40, 40, seed=40, c=1)[..., 0]
        hay_rgba = np.dstack([hay, hay, hay, np.full_like(hay, 255)])
        needle = hay[5:15, 20:32]  # 2D gray
        m = vision.locate(needle, hay_rgba)
        assert m.rect == Rect(20, 5, 12, 10)

    def test_disabled_cv2_env(self, monkeypatch):
        monkeypatch.setenv("LOCUM_DISABLE_CV2", "1")
        assert vision._cv2() is None

    def test_fast_len(self):
        for n in (1, 7, 17, 97, 1000, 1081, 1921):
            f = vision._fast_len(n)
            assert f >= n
            k = f
            for p in (2, 3, 5):
                while k % p == 0:
                    k //= p
            assert k == 1


class TestLocateAll:
    def test_finds_all_copies_sorted(self):
        hay = noise(200, 300, seed=50)
        icon = noise(15, 15, seed=51)
        spots = [(10, 10), (100, 40), (250, 170), (180, 120)]
        for x, y in spots:
            hay[y : y + 15, x : x + 15] = icon
        ms = vision.locate_all(icon, hay)
        assert sorted((m.rect.left, m.rect.top) for m in ms) == sorted(spots)
        confs = [m.confidence for m in ms]
        assert confs == sorted(confs, reverse=True)
        assert vision.locate_all(icon, hay, limit=2) == ms[:2]
        assert vision.locate_all(icon, hay, limit=0) == []

    def test_adjacent_copies_kept_overlapping_suppressed(self):
        hay = noise(60, 120, seed=52)
        icon = noise(10, 10, seed=53)
        hay[20:30, 20:30] = icon
        hay[20:30, 30:40] = icon  # touching neighbour
        ms = vision.locate_all(icon, hay, confidence=0.95)
        assert sorted(m.rect.left for m in ms) == [20, 30]
        # lower confidence: near-duplicates (shifted by 1px) must be suppressed
        ms = vision.locate_all(icon, hay, confidence=0.3)
        lefts = sorted(m.rect.left for m in ms if m.rect.top in range(15, 26))
        assert 20 in lefts and 30 in lefts
        for a in ms:
            for b in ms:
                if a is not b:
                    assert a.rect.iou(b.rect) <= 0.3 + 1e-9

    def test_nms_across_scales(self):
        hay = noise(100, 100, seed=54)
        icon = noise(20, 20, seed=55)
        hay[30:50, 30:50] = icon
        ms = vision.locate_all(icon, hay, scales=(0.95, 1.0, 1.05), confidence=0.6)
        near = [m for m in ms if m.rect.iou(Rect(30, 30, 20, 20)) > 0.3]
        assert len(near) == 1 and near[0].scale == 1.0

    def test_plateau_is_fast_and_bounded(self):
        hay = np.full((300, 400, 3), 50, np.uint8)
        needle = np.full((10, 10, 3), 50, np.uint8)
        t = time.perf_counter()
        ms = vision.locate_all(needle, hay, limit=50)
        assert len(ms) == 50
        assert time.perf_counter() - t < 2.0
        for i, a in enumerate(ms):
            for b in ms[i + 1 :]:
                assert a.rect.iou(b.rect) <= 0.3 + 1e-9


def test_benchmark_full_hd_locate_under_budget():
    hay = noise(1080, 1920, seed=60)
    needle = hay[700:750, 1500:1550].copy()
    vision.locate(needle, hay[:200, :200])  # warm FFT planner
    t = time.perf_counter()
    m = vision.locate(needle, hay)
    elapsed = time.perf_counter() - t
    assert m.rect == Rect(1500, 700, 50, 50)
    assert elapsed < 2.5  # typically ~0.3s; generous for slow CI machines


def test_locate_on_mock_backend_screenshot():
    be = MockBackend(width=800, height=600)
    win = be.add_window("Invoice", (100, 80, 400, 300))
    win.add_button("OK", (20, 240, 80, 30))
    win.add_button("Cancel", (120, 240, 80, 30))
    icon = Image.fromarray(noise(16, 16, seed=70))
    be.paint(icon, (650, 500))
    shot = be.screenshot()
    m = vision.locate(icon, shot)
    assert m.rect == Rect(650, 500, 16, 16)
    # crop the OK button from the render and find it again (it is unique)
    ok = shot.crop((120, 320, 200, 350))
    found = vision.locate(ok, shot)
    assert found.rect == Rect(120, 320, 80, 30)


# ------------------------------------------------------------------------ find_color
class TestFindColor:
    def test_regions_and_ordering(self):
        img = np.zeros((50, 60, 3), np.uint8)
        img[5:10, 40:55] = (255, 0, 0)
        img[20:30, 2:8] = (255, 0, 0)
        img[40:41, 10:11] = (255, 0, 0)
        rects = vision.find_color(img, (255, 0, 0))
        assert rects == [Rect(40, 5, 15, 5), Rect(2, 20, 6, 10), Rect(10, 40, 1, 1)]
        assert vision.find_color(img, "#ff0000", min_area=2) == rects[:2]
        assert vision.find_color(img, (0, 255, 0)) == []

    def test_tolerance_offset_region(self):
        img = np.zeros((20, 20, 3), np.uint8)
        img[2:5, 2:5] = (100, 100, 100)
        img[10:12, 10:15] = (104, 97, 100)
        assert len(vision.find_color(img, (100, 100, 100))) == 1
        rects = vision.find_color(img, (100, 100, 100), tolerance=4, offset=(100, 200))
        assert rects == [Rect(102, 202, 3, 3), Rect(110, 210, 5, 2)]
        r = vision.find_color(img, (104, 97, 100), region=(8, 8, 10, 10), offset=(1, 1))
        assert r == [Rect(11, 11, 5, 2)]
        assert vision.find_color(img, (1, 1, 1), region=(100, 100, 5, 5)) == []

    def test_connectivity_diagonal(self):
        img = np.zeros((4, 4, 3), np.uint8)
        for i in range(4):
            img[i, i] = (9, 9, 9)
        assert vision.find_color(img, (9, 9, 9)) == [Rect(0, 0, 4, 4)]
        assert len(vision.find_color(img, (9, 9, 9), connectivity=4)) == 4
        with pytest.raises(ValueError):
            vision.find_color(img, (9, 9, 9), connectivity=6)

    def test_complex_shapes_match_reference_labelling(self):
        # U shape and spiral: components that merge late in the scan
        img = np.zeros((12, 12, 3), np.uint8)
        c = (1, 2, 3)
        img[1:10, 1] = c
        img[1:10, 5] = c
        img[9, 1:6] = c  # U
        img[1, 8:11] = c
        img[1:6, 10] = c
        img[5, 7:11] = c
        img[3:6, 7] = c
        rects = vision.find_color(img, c, connectivity=4)
        assert rects == [Rect(1, 1, 5, 9), Rect(7, 1, 4, 5)]

    def test_random_masks_against_flood_fill(self):
        rng = np.random.default_rng(80)
        for trial in range(15):
            mask = rng.random((25, 31)) < 0.45
            img = np.zeros((25, 31, 3), np.uint8)
            img[mask] = (7, 7, 7)
            for conn in (4, 8):
                got = sorted(r.as_tuple() for r in vision.find_color(img, (7, 7, 7), connectivity=conn))
                assert got == sorted(_flood_fill_rects(mask, conn))

    def test_large_mask_fast(self):
        img = np.zeros((1080, 1920, 3), np.uint8)
        img[::2, ::2] = (5, 5, 5)  # 500k isolated pixels for 4-connectivity
        t = time.perf_counter()
        rects = vision.find_color(img, (5, 5, 5), connectivity=8, min_area=2)
        assert rects == []
        full = vision.find_color(np.zeros((1080, 1920, 3), np.uint8), (0, 0, 0))
        assert full == [Rect(0, 0, 1920, 1080)]
        assert time.perf_counter() - t < 5


def _flood_fill_rects(mask, conn):
    h, w = mask.shape
    seen = np.zeros_like(mask)
    out = []
    nbrs = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if conn == 8:
        nbrs += [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    for y in range(h):
        for x in range(w):
            if mask[y, x] and not seen[y, x]:
                stack = [(y, x)]
                seen[y, x] = True
                ys, xs = [], []
                while stack:
                    cy, cx = stack.pop()
                    ys.append(cy)
                    xs.append(cx)
                    for dy, dx in nbrs:
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
                out.append((min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1))
    return out


# -------------------------------------------------------------------------- diffs
def test_image_diff_and_changed_fraction():
    a = np.zeros((10, 10, 3), np.uint8)
    b = a.copy()
    assert vision.image_diff(a, b) == 0.0
    assert vision.image_diff(a, np.full_like(a, 255)) == 1.0
    b[0, 0] = 255
    assert vision.image_diff(a, b) == pytest.approx(1 / 100)
    assert vision.changed_fraction(a, b) == pytest.approx(0.01)
    b2 = a.copy()
    b2[:5] = 3
    assert vision.changed_fraction(a, b2, pixel_tolerance=5) == 0.0
    assert vision.changed_fraction(a, b2) == 0.5
    assert vision.image_diff(a, np.zeros((5, 5, 3), np.uint8)) == 1.0
    assert vision.changed_fraction(a, np.zeros((5, 5, 3), np.uint8)) == 1.0


def test_tiled_diffs_see_local_changes():
    a = np.full((1080, 1920, 3), 235, np.uint8)
    b = a.copy()
    b[500:532, 940:972] = 0  # a 32x32 spinner frame, straddling block borders
    assert vision.image_diff(a, b) < 0.001  # averaged away over the full screen
    assert vision.image_diff(a, b, tile=64) == pytest.approx(0.25 * 235 / 255)
    assert vision.changed_fraction(a, b) < 0.001
    assert vision.changed_fraction(a, b, tile=64) == pytest.approx(0.25)
    # an image smaller than a tile compares as a whole; identical -> 0
    small = np.zeros((10, 10, 3), np.uint8)
    small2 = small.copy()
    small2[0, 0] = 255
    assert vision.image_diff(small, small2, tile=64) == pytest.approx(0.01)
    assert vision.changed_fraction(small, small2, tile=64) == pytest.approx(0.01)
    assert vision.image_diff(a, a, tile=64) == 0.0
    # a change at the right/bottom edge of an odd-sized image is not amplified
    c = np.zeros((101, 99, 3), np.uint8)
    d = c.copy()
    d[100, 98] = 255
    assert vision.changed_fraction(c, d, tile=64) == pytest.approx(1 / 4096)


# ------------------------------------------------------------------------ annotate
class TestAnnotate:
    def test_draws_boxes_and_labels_on_copy(self):
        img = Image.new("RGB", (200, 150), (255, 255, 255))
        out = vision.annotate(img, [((50, 50, 40, 30), "1"), (Rect(120, 60, 50, 50), "22")], color=(255, 0, 0))
        assert out is not img and out.size == img.size
        assert np.asarray(img).min() == 255  # original untouched
        arr = np.asarray(out)
        assert tuple(arr[65, 50]) == (255, 0, 0)  # left edge of box 1
        assert tuple(arr[80, 70]) == (255, 0, 0) or tuple(arr[79, 70]) == (255, 0, 0)  # bottom edge
        assert tuple(arr[65, 70]) == (255, 255, 255)  # interior untouched
        # label tag above the box (outside) is filled with the box color + white text
        tag = arr[35:50, 50:60]
        assert ((tag == (255, 0, 0)).all(axis=2)).sum() > 20
        assert ((tag == (255, 255, 255)).all(axis=2)).sum() > 0

    def test_offset_scale_palette_and_custom_colors(self):
        img = Image.new("RGB", (100, 100), (0, 0, 0))
        out = vision.annotate(
            img,
            [((1000, 1000, 40, 40), "a"), ((1100, 1100, 40, 40), "b", "#00ff00")],
            color=None,
            offset=(1000, 1000),
            scale=0.5,
        )
        arr = np.asarray(out)
        assert tuple(arr[10, 0]) == vision._PALETTE[0]  # left edge of first box at x=0
        assert tuple(arr[60, 50]) == (0, 255, 0)  # second box at (50,50)

    def test_labels_avoid_each_other(self):
        img = Image.new("RGB", (300, 200), "white")
        boxes = [((100, 100, 30, 30), "10"), ((100, 100, 30, 30), "11"), ((100, 100, 30, 30), "12")]
        out = vision.annotate(img, boxes, font_size=12)
        # three distinct tag positions -> colored pixels in at least 3 separate places around the box
        arr = np.asarray(out)
        red = (arr == (255, 0, 0)).all(axis=2)
        assert red[:100, :].any()  # above
        assert red[100:130, 100:130].sum() > red[100:130, 100:130].size * 0.1  # inside

    def test_offscreen_and_bad_boxes(self):
        img = Image.new("RGB", (50, 50), "white")
        out = vision.annotate(img, [((500, 500, 10, 10), "x"), ((-100, -100, 10, 10), "y")])
        assert np.asarray(out).min() == 255
        with pytest.raises(ValueError):
            vision.annotate(img, [((0, 0, 1, 1),)])

    def test_dark_label_text_on_light_color(self):
        img = Image.new("RGB", (100, 100), (0, 0, 0))
        out = np.asarray(vision.annotate(img, [((40, 40, 20, 20), "8")], color=(255, 255, 0), font_size=14))
        tag = out[10:40, 40:70]
        assert ((tag == (0, 0, 0)).all(axis=2)).any() and ((tag == (255, 255, 0)).all(axis=2)).any()


# --------------------------------------------------------------- downscale/base64
def test_downscale():
    img = Image.new("RGB", (3000, 1500))
    small, s = vision.downscale(img, 1500)
    assert small.size == (1500, 750) and s == 0.5
    same, s1 = vision.downscale(Image.new("RGB", (100, 50)), 1500)
    assert s1 == 1.0 and same.size == (100, 50)
    odd, s2 = vision.downscale(Image.new("RGB", (1001, 333)), 100)
    assert max(odd.size) == 100 and s2 == pytest.approx(100 / 1001)
    with pytest.raises(ValueError):
        vision.downscale(img, 0)


def test_to_base64_png_roundtrip():
    arr = noise(30, 40)
    b64 = vision.to_base64_png(arr)
    back = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert back.size == (40, 30)
    assert np.array_equal(np.asarray(back.convert("RGB")), arr)
    small = vision.to_base64_png(Image.new("RGB", (400, 200)), max_size=100)
    assert Image.open(io.BytesIO(base64.b64decode(small))).size == (100, 50)
    assert vision.load_image(b64).size == (40, 30)
    assert vision.to_png_bytes(arr)[:8] == b"\x89PNG\r\n\x1a\n"


def test_crop():
    arr = noise(20, 20)
    c = vision.crop(arr, (5, 6, 7, 8))
    assert c.size == (7, 8)
    assert np.array_equal(np.asarray(c), arr[6:14, 5:12])


def test_locate_prefers_reading_order_among_identical_copies():
    hay = noise(100, 100, seed=90)
    icon = noise(10, 10, seed=91)
    for x, y in [(70, 70), (60, 5), (5, 40)]:
        hay[y : y + 10, x : x + 10] = icon
    m = vision.locate(icon, hay)
    assert (m.rect.left, m.rect.top) == (60, 5)
    assert m.confidence == 1.0


def test_cv2_fast_path_is_used_and_post_processed(monkeypatch):
    """Exercise the optional OpenCV path with a stand-in implementation."""
    calls = []

    class FakeCV2:
        TM_CCOEFF_NORMED = 5

        @staticmethod
        def matchTemplate(hay, ndl, method):
            calls.append((hay.dtype, hay.shape, ndl.shape, method))
            h3 = hay if hay.ndim == 3 else hay[:, :, None]
            n3 = ndl if ndl.ndim == 3 else ndl[:, :, None]
            th, tw = n3.shape[:2]
            out = np.zeros((h3.shape[0] - th + 1, h3.shape[1] - tw + 1), np.float32)
            b = n3 - n3.reshape(-1, n3.shape[2]).mean(0)
            for y in range(out.shape[0]):
                for x in range(out.shape[1]):
                    win = h3[y : y + th, x : x + tw]
                    a = win - win.reshape(-1, n3.shape[2]).mean(0)
                    den = np.sqrt((a * a).sum() * (b * b).sum())
                    out[y, x] = (a * b).sum() / den if den else np.nan  # real cv2 can emit nan/inf here
            return out

    monkeypatch.delenv("LOCUM_DISABLE_CV2", raising=False)
    monkeypatch.setattr(vision, "_CV2", FakeCV2)
    monkeypatch.setattr(vision, "_CV2_CHECKED", True)
    hay = noise(30, 30, seed=100)
    hay[:, 20:] = 77  # flat area -> must score 0, never nan
    needle = hay[5:12, 3:11].copy()
    score = vision.match_template(needle, hay)
    assert calls and calls[0][3] == FakeCV2.TM_CCOEFF_NORMED and calls[0][0] == np.float32
    assert np.all(np.isfinite(score)) and np.all(score[:, 20:] == 0)
    assert vision.locate(needle, hay).rect == Rect(3, 5, 8, 7)
    assert vision.locate(needle, hay, grayscale=True).rect == Rect(3, 5, 8, 7)
    assert calls[-1][1] == (30, 30) and calls[-1][2] == (7, 8)  # 2-D arrays in grayscale mode
    # masked needles never use cv2
    n = len(calls)
    rgba = np.dstack([needle, np.full(needle.shape[:2], 255, np.uint8)])
    rgba[0, 0, 3] = 0
    assert vision.locate(rgba, hay).rect == Rect(3, 5, 8, 7)
    assert len(calls) == n
