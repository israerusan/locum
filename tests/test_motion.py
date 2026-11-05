from __future__ import annotations

import math

import pytest

from locum.motion import TWEENS, Humanize, fitts_duration, get_tween, path_length, plan_path
from locum.types import Point, Rect

EXPECTED_TWEENS = {
    "linear", "ease_in_quad", "ease_out_quad", "ease_in_out_quad", "ease_in_cubic",
    "ease_out_cubic", "ease_in_out_cubic", "ease_out_back", "ease_in_out_sine",
}


def test_tween_names_and_endpoints():
    assert EXPECTED_TWEENS <= set(TWEENS)
    for name, fn in TWEENS.items():
        assert fn(0.0) == pytest.approx(0.0, abs=1e-9), name
        assert fn(1.0) == pytest.approx(1.0, abs=1e-9), name


def test_tweens_monotonic_except_back():
    for name, fn in TWEENS.items():
        if name == "ease_out_back":
            continue
        vals = [fn(i / 100) for i in range(101)]
        assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:])), name


def test_ease_out_back_overshoots():
    assert max(TWEENS["ease_out_back"](i / 100) for i in range(101)) > 1.0


def test_get_tween_aliases():
    assert get_tween("easeInOutQuad") is TWEENS["ease_in_out_quad"]
    assert get_tween("ease-out-cubic") is TWEENS["ease_out_cubic"]
    f = lambda t: t  # noqa: E731
    assert get_tween(f) is f
    with pytest.raises(ValueError):
        get_tween("bouncy")


def test_straight_path_ends_exactly_and_times():
    path = plan_path((0, 0), (300, 150), duration=0.5, rate=100)
    assert path[-1] == (Point(300, 150), 0.5)
    times = [t for _, t in path]
    assert times == sorted(times)
    assert all(0 <= t <= 0.5 for t in times)
    # all points lie on the segment
    for p, _ in path:
        assert abs(p.y - p.x / 2) <= 1
    # no consecutive duplicates
    assert all(a[0] != b[0] for a, b in zip(path, path[1:]))


def test_linear_path_is_uniform():
    path = plan_path((0, 0), (100, 0), duration=1.0, tween="linear", rate=10)
    assert [p.x for p, _ in path] == [10 * i for i in range(1, 11)]


def test_zero_duration_and_zero_distance():
    assert plan_path((0, 0), (50, 60), duration=0) == [(Point(50, 60), 0.0)]
    assert plan_path((5, 5), (5, 5), duration=1.0) == [(Point(5, 5), 0.0)]


def test_float_and_point_like_inputs():
    path = plan_path(Point(1, 1), {"x": 40.4, "y": 20.6}, duration=0.1)
    assert path[-1][0] == Point(40, 21)


def test_humanized_deterministic_with_seed():
    a = plan_path((10, 10), (800, 500), duration=0.6, humanize=Humanize(seed=42))
    b = plan_path((10, 10), (800, 500), duration=0.6, humanize=Humanize(seed=42))
    c = plan_path((10, 10), (800, 500), duration=0.6, humanize=Humanize(seed=43))
    assert a == b
    assert a != c
    assert a[-1] == (Point(800, 500), 0.6)


def test_humanize_rng_persists_and_reseeds():
    h = Humanize(seed=7)
    a = plan_path((0, 0), (600, 400), duration=0.4, humanize=h)
    b = plan_path((0, 0), (600, 400), duration=0.4, humanize=h)
    assert a != b  # successive moves differ...
    h.reseed()
    assert plan_path((0, 0), (600, 400), duration=0.4, humanize=h) == a  # ...but sequence reproducible


def test_humanized_path_is_curved_and_overshoots():
    start, end = (0, 0), (1000, 0)
    curved = 0
    overshoots = 0
    for seed in range(20):
        path = plan_path(start, end, duration=0.8, humanize=Humanize(seed=seed, jitter=0))
        assert path[-1][0] == Point(*end)
        if max(abs(p.y) for p, _ in path) >= 10:
            curved += 1
        if max(p.x for p, _ in path) > 1000:
            overshoots += 1
        assert path_length(path, start) < 1000 * 1.6
    assert curved >= 15
    assert overshoots == 20


def test_humanized_no_overshoot_option_and_short_moves():
    path = plan_path((0, 0), (1000, 0), duration=0.5, humanize=Humanize(seed=1, overshoot=False, jitter=0))
    assert max(p.x for p, _ in path) <= 1000
    short = plan_path((0, 0), (30, 0), duration=0.2, humanize=Humanize(seed=1, jitter=0))
    assert max(p.x for p, _ in short) <= 30  # no overshoot for short moves
    straight = plan_path((0, 0), (500, 0), duration=0.3, humanize=Humanize(seed=1, curvature=0, jitter=0, overshoot=False))
    assert all(p.y == 0 for p, _ in straight)


def test_humanized_bounds_clamp():
    bounds = Rect(0, 0, 1001, 600)
    for seed in range(10):
        path = plan_path((0, 300), (1000, 300), duration=0.5, humanize=Humanize(seed=seed), bounds=bounds)
        assert all(bounds.contains(p) for p, _ in path)
        assert path[-1][0] == Point(1000, 300)


def test_humanize_validation():
    with pytest.raises(ValueError):
        Humanize(speed=0)
    with pytest.raises(ValueError):
        Humanize(jitter=-1)


def test_fitts_duration():
    assert fitts_duration(0) == 0.0
    d_short, d_long = fitts_duration(50), fitts_duration(800)
    assert 0.08 <= d_short < d_long <= 1.2
    assert fitts_duration(10**7) == 1.2
    assert fitts_duration(2) == pytest.approx(0.08)
    # bigger targets are faster, faster speed is faster
    assert fitts_duration(500, target_width=100) < fitts_duration(500, target_width=10)
    assert fitts_duration(500, speed=2.0) < fitts_duration(500)
    expected = 0.05 + 0.13 * math.log2(400 / 20 + 1)
    assert fitts_duration(400) == pytest.approx(expected)
    with pytest.raises(ValueError):
        fitts_duration(100, speed=0)
