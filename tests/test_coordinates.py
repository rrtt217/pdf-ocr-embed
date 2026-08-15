"""Invariant: every bbox is integer raw-pixel [x1,y1,x2,y2], x1<=x2, y1<=y2."""
from __future__ import annotations

from backend.sources.base import map_normalized_to_pixels, normalize_bbox


def test_map_corners():
    assert map_normalized_to_pixels(0, 0, 100, 50) == (0.0, 0.0)
    assert map_normalized_to_pixels(1000, 1000, 100, 50) == (100.0, 50.0)


def test_map_non_uniform_per_axis():
    # Each dimension scales independently to 1000 (DESIGN.md: bbox 归一化画布).
    assert map_normalized_to_pixels(250, 500, 100, 50) == (25.0, 25.0)
    assert map_normalized_to_pixels(500, 500, 200, 100) == (100.0, 50.0)


def test_normalize_scales_north_east():
    bbox = normalize_bbox([0, 0, 500, 250], 100, 50)
    assert bbox == [0, 0, 50, 12]  # 12.5 -> banker rounding -> 12


def test_normalize_orders_and_uses_ints():
    bbox = normalize_bbox([200, 300, 100, 60], 100, 50)
    assert bbox == [10, 3, 20, 15]
    assert all(isinstance(v, int) for v in bbox)
    assert bbox[0] <= bbox[2] and bbox[1] <= bbox[3]


def test_normalize_identity_canvas():
    bbox = normalize_bbox([200, 300, 100, 50], 1000, 1000)
    assert bbox == [100, 50, 200, 300]
