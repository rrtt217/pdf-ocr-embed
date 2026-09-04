"""1000x1000 normalized canvas -> raw pixel coordinate mapping.

The unlimited model reports every bbox on a normalized 1000x1000 canvas
(each dimension independently scaled).  These are the only functions that
undo that mapping — **before anything else in the pipeline sees a bbox** —
so every block that leaves the parser is already in integer raw-pixel space
(top-left origin), the plugin's and the host apps' shared invariant.
"""
from __future__ import annotations


def map_normalized_to_pixels(
    x: float, y: float, width: int, height: int
) -> tuple[float, float]:
    """Map a normalized 0..1000 canvas coordinate to original pixel space.

    Each dimension is independently normalized to 1000 (non-uniform scale), so
    the inverse is a per-axis linear scale.
    """
    px = x * (width / 1000.0)
    py = y * (height / 1000.0)
    return px, py


def normalize_bbox(bbox, width: int, height: int) -> list:
    """Convert a normalized (0..1000) bbox into raw pixel coordinates.

    Values outside the canvas (e.g. a stray ``-3`` or ``1005`` from the
    engine) are clamped to the page image bounds: an out-of-range coordinate
    would corrupt the embedded text-layer position.  Clamping keeps the
    "integers in raw pixel space" invariant even for malformed engine output.
    """
    x1, y1, x2, y2 = (float(v) for v in bbox)
    px1, py1 = map_normalized_to_pixels(x1, y1, width, height)
    px2, py2 = map_normalized_to_pixels(x2, y2, width, height)
    x1p, x2p = sorted((px1, px2))
    y1p, y2p = sorted((py1, py2))
    return [int(round(min(max(x1p, 0.0), float(width)))),
            int(round(min(max(y1p, 0.0), float(height)))),
            int(round(min(max(x2p, 0.0), float(width)))),
            int(round(min(max(y2p, 0.0), float(height))))]
