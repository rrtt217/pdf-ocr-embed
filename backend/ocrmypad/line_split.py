"""Image-based per-line bbox recovery for paragraph-level OCR blocks.

The unlimited model reports ONE bbox per paragraph block; the block's text
lines (``Block.lines``) carry no per-line geometry.  The hOCR text layer has
therefore been placing each line on an EQUAL vertical slice of the block bbox
(``_line_bboxes``) — approximate at best, and wrong for irregular layouts
(varied line heights, headings, display math, first-line indents).

This module re-derives accurate per-line bboxes from the page image with a
horizontal projection profile (a.k.a. x-height / unitive line segmentation):

1. binarize the block crop (Otsu threshold) and count ink per row,
2. extract text bands (rows with ink, small gaps merged for descender /
   ascender overhang),
3. reconcile the band count to the number of text lines the block actually has
   (merge over-splits, split under-splits at the deepest profile valley), and
4. tighten each band's x-extent to its own ink columns.

If the image cannot support the split (no bands, degenerate crop, or the band
count cannot be reconciled to the line count), every function gracefully falls
back to equal vertical slices — so the invisible text layer is never worse
than the current behavior.

Deliberately dependency-light: pure Python + PIL (already a project
dependency), no numpy.  All bboxes returned are integer raw-pixel
[x1, y1, x2, y2] inside ``bbox``, preserving the project's coordinate
invariant.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PIL import Image  # noqa: PLC0415  (project dependency, not plugin-local)

# Rows with fewer dark pixels than this are treated as inter-line whitespace.
_MIN_INK = 2
# Inter-band gaps at most this many rows are merged (ascender/descender
# overhang looks like a 1-6px "gap" between the cap line and the band bottom).
_MIN_GAP = 8


def split_block_into_lines(image: Image.Image, bbox: Sequence[int],
                           n_lines: int) -> List[List[int]]:
    """Split one block bbox into ``n_lines`` accurate line bboxes.

    Args:
        image: the whole page image (any mode; converted to grayscale),
            in raw pixel space, top-left origin.
        bbox: integer [x1, y1, x2, y2] block bbox in raw pixels.
        n_lines: number of renderable lines in the block (from its text).

    Returns:
        ``n_lines`` integer line bboxes ``[x1, y1, x2, y2]``, ordered
        top-to-bottom, each aligned to the ink of one printed line inside the
        block.  Falls back to equal vertical slices when the image cannot
        support the split.
    """
    x1, y1, x2, y2 = (int(round(v)) for v in bbox)
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    if n_lines <= 1:
        # A single line occupies the block: the block bbox IS the line box.
        return [[x1, y1, x2, y2]] if n_lines == 1 else []
    if x2 <= x1 or y2 <= y1:
        return _equal_slices(x1, y1, x2, y2, n_lines)

    gray = image.convert("L")
    crop = gray.crop((x1, y1, x2, y2))

    thresh = _otsu_threshold(crop)
    if not 40 <= thresh <= 245 or _ink_fraction(crop, thresh) < 0.0005:
        return _equal_slices(x1, y1, x2, y2, n_lines)

    profile = _row_profile(crop, thresh)
    bands = _band_scan(profile, min_ink=_MIN_INK, min_gap=_MIN_GAP)
    bands = _reconcile(profile, bands, n_lines)
    if not bands:
        return _equal_slices(x1, y1, x2, y2, n_lines)

    out: List[List[int]] = []
    for start, end in bands:
        bx1, bx2 = _band_x_extent(crop, start, end, thresh)
        out.append([
            max(x1, min(x1 + bx1, x2 - 1)),
            y1 + start,
            max(min(x1 + bx2, x2), x1 + 1),
            max(y1 + end, y1 + start + 1),
        ])
    return out


# --- helpers -----------------------------------------------------------------


def _otsu_threshold(crop: Image.Image) -> int:
    """Otsu's method on the crop's downsampled histogram (ink = value < t)."""
    small = _downscale(crop, max_w=512)
    hist = small.histogram()[:256]
    total = sum(hist)
    if total <= 0:
        return 200
    w_b = sum_b = 0.0
    best_var = -1.0
    best_t = 200
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        mean_b = sum_b / w_b
        mean_f = (total - w_b) * 0.0
        # mean of foreground = (sum_all - sum_b) / w_f
        sum_all = sum(i * hist[i] for i in range(256))
        mean_f = (sum_all - sum_b) / w_f if w_f else 0.0
        var = w_b * w_f * (mean_b - mean_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t if 0 < best_t < 255 else 200


def _downscale(crop: Image.Image, max_w: int) -> Image.Image:
    """Box-downscale a crop keeping aspect, capped at ``max_w`` wide."""
    w, h = crop.size
    if w <= max_w:
        return crop
    scale = max_w / float(w)
    return crop.resize((int(round(w * scale)), max(1, int(round(h * scale)))),
                       Image.BOX)


def _ink_fraction(crop: Image.Image, thresh: int) -> float:
    small = _downscale(crop, max_w=256)
    hist = small.histogram()[:256]
    dark = sum(hist[:thresh])
    total = sum(hist)
    return dark / max(1, total)


def _row_profile(crop: Image.Image, thresh: int) -> List[int]:
    """Dark-pixel count per row of the crop (sampled, not every pixel)."""
    w, h = crop.size
    data = crop.load()
    step = 2 if w > 900 else 1  # sample 1-in-2 columns on wide blocks
    profile: List[int] = []
    for y in range(h):
        dark = 0
        for x in range(0, w, step):
            if data[x, y] < thresh:
                dark += 1
        profile.append(dark)
    return profile


def _band_scan(profile: Sequence[int], min_ink: int,
               min_gap: int) -> List[Tuple[int, int]]:
    """Runs of rows with ink >= min_ink; gaps <= min_gap rows are merged.

    Returns (start, end) pairs with end EXCLUSIVE, indices into profile.
    """
    runs: List[Tuple[int, int]] = []
    in_run = False
    start = 0
    for y, v in enumerate(profile):
        if v >= min_ink and not in_run:
            in_run, start = True, y
        elif v < min_ink and in_run:
            in_run = False
            if y - start > 1:
                runs.append((start, y))
    if in_run:
        runs.append((start, len(profile)))

    merged: List[List[int]] = []
    for s, e in runs:
        if merged and s - merged[-1][1] <= min_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _deepest_valley(profile: Sequence[int], lo: int, hi: int) -> Optional[int]:
    """Row index in [lo, hi) with the minimum ink (used to split a band)."""
    if hi - lo < 2:
        return None
    best_y, best_v = lo, profile[lo]
    for y in range(lo, hi):
        if profile[y] < best_v:
            best_v, best_y = profile[y], y
    return best_y


def _reconcile(profile: Sequence[int], bands: List[Tuple[int, int]],
               n: int) -> Optional[List[Tuple[int, int]]]:
    """Adjust band count to exactly ``n``; None when impossible.

    Over-split bands (more bands than lines) are merged two-by-two at the
    smallest separating gap; under-split bands (fewer than lines) are split at
    their deepest ink valley.  Returns None when the adjustment cannot reach
    ``n`` (caller falls back to equal slices).
    """
    bands = list(bands)
    if not bands:
        return None
    if len(bands) == n:
        return bands

    if len(bands) > n:
        while len(bands) > n:
            gaps = [(bands[i + 1][0] - bands[i][1], i)
                    for i in range(len(bands) - 1)]
            if not gaps:
                return None
            _, i = min(gaps, key=lambda g: g[0])
            bands[i] = (bands[i][0], bands[i + 1][1])
            del bands[i + 1]
        return bands

    # Fewer bands than lines: split the tallest band at its deepest valley.
    for _ in range(n - len(bands)):
        candidates = [(e - s, i) for i, (s, e) in enumerate(bands) if e - s >= 6]
        if not candidates:
            return None
        _, i = max(candidates, key=lambda c: c[0])
        s, e = bands[i]
        v = _deepest_valley(profile, s, e)
        if v is None or v <= s or v >= e - 1:
            return None
        bands[i] = (s, v)
        bands.insert(i + 1, (v, e))
    return bands


def _band_x_extent(crop: Image.Image, start: int, end: int,
                   thresh: int, pad: int = 3) -> Tuple[int, int]:
    """Leftmost/rightmost ink column of a band (crop-x coords, padded)."""
    w, _ = crop.size
    data = crop.load()
    left, right = w, 0
    step = 2 if w > 900 else 1
    for y in range(start, end):
        for x in range(0, w, step):
            if data[x, y] < thresh:
                if x < left:
                    left = x
                if x > right:
                    right = x
    if right < left:
        return 0, w
    return max(0, left - pad), min(w, right + pad)


def split_block_text_across_bands(
        image: Image.Image, bbox: Sequence[int], text: str,
        max_lines: int = 6) -> Optional[List[Tuple[str, List[int]]]]:
    """Split a single-line paragraph's ``text`` across its printed line bands.

    The unlimited model frequently reports a whole paragraph as ONE text line
    (no newlines) while its bbox covers several printed lines.  In that case
    there is no per-line text to align — but the image still shows the true
    line rows, so the text can be distributed across them proportionally to
    each band's ink width.  Each returned item is ``(chunk, line_bbox)`` with
    ``"".join(chunks)`` reconstructing ``text`` (spaces may shift across the
    chunk seam).

    Conservative by design — returns ``None`` (caller keeps the single-line
    behavior) unless:

    * the bbox contains 2+ clearly text-like bands (roughly uniform height,
      low ink density — rejects rules, graphics and noise), and
    * the text is long enough to plausibly fill several lines.

    The renderer stretches each line's word to its bbox width, so a slightly
    off chunk seam only affects horizontal tightness, never which printed line
    a chunk lands on — vertical placement stays accurate.
    """
    text = text.strip()
    if len(text) < 6:
        return None
    x1, y1, x2, y2 = (int(round(v)) for v in bbox)
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    if x2 <= x1 or y2 <= y1:
        return None

    gray = image.convert("L")
    crop = gray.crop((x1, y1, x2, y2))
    thresh = _otsu_threshold(crop)
    if not 40 <= thresh <= 245:
        return None
    profile = _row_profile(crop, thresh)
    bands = _band_scan(profile, min_ink=_MIN_INK, min_gap=_MIN_GAP)
    if len(bands) < 2 or len(bands) > max_lines:
        return None
    if not _textlike_bands(crop, bands, thresh):
        return None

    extents = [_band_x_extent(crop, s, e, thresh) for s, e in bands]
    weights = [max(1, ex[1] - ex[0]) for ex in extents]
    chunks = _allocate_text(text, weights)
    if chunks is None or len(chunks) != len(bands) or any(not c for c in chunks):
        return None

    out: List[Tuple[str, List[int]]] = []
    for (s, e), (ex0, ex1), chunk in zip(bands, extents, chunks):
        out.append((
            chunk,
            [max(x1, x1 + ex0), y1 + s, min(x2, x1 + ex1), y1 + e],
        ))
    return out


def _textlike_bands(crop: Image.Image, bands: List[Tuple[int, int]],
                    thresh: int, min_ink_frac: float = 0.004,
                    max_ink_frac: float = 0.5) -> bool:
    """True when every band looks like a text row (not a rule / graphic).

    Text rows are roughly uniform in height with a small to moderate ink
    density; rules and solid graphics are thin and/or nearly fully inked.
    """
    if not bands:
        return False
    heights = [e - s for s, e in bands]
    med = sorted(heights)[len(heights) // 2]
    if med < 6:
        return False
    w = crop.width
    for (s, e), h in zip(bands, heights):
        if not (0.4 * med <= h <= 2.5 * med):
            return False
        dark = 0
        total = w * h
        if total <= 0:
            return False
        data = crop.load()
        for y in range(s, e):
            for x in range(0, w, 2):
                if data[x, y] < thresh:
                    dark += 1
        frac = (2.0 * dark) / total
        if not (min_ink_frac <= frac <= max_ink_frac):
            return False
    return True


def _allocate_text(text: str, weights: Sequence[int]) -> Optional[List[str]]:
    """Proportionally split ``text`` into ``len(weights)`` chunks by weight.

    Chunk boundaries snap to the nearest space so words are not cut mid-word
    (harmless either way for an invisible layer, but nicer for copy).
    """
    n_total = len(weights)
    total_w = sum(weights)
    if total_w <= 0 or n_total <= 0:
        return None
    chunks: List[str] = []
    start = 0
    acc = 0.0
    for i in range(n_total):
        acc += weights[i]
        target = int(round(len(text) * acc / total_w))
        if i == n_total - 1:
            end = len(text)
        else:
            end = target
            best = target
            best_d = abs(len(text[start:target]))
            for off in range(-4, 5):
                j = target + off
                if 0 <= j <= len(text):
                    boundary = text[j:j + 1] in ("", " ") or text[j - 1:j] == " "
                    if boundary:
                        d = abs(len(text[start:j]))
                        if d < best_d:
                            best, best_d = j, d
            end = best
        if end <= start:
            # degenerate target: give this band nothing (fall back later)
            chunks.append("")
            continue
        chunks.append(text[start:end])
        start = end
    if start < len(text):
        chunks[-1] += text[start:]
    return chunks


def _equal_slices(x1: int, y1: int, x2: int, y2: int,
                  n: int) -> List[List[int]]:
    """The legacy fallback: split the block bbox into ``n`` equal rows."""
    if n <= 0:
        return []
    height = y2 - y1
    out: List[List[int]] = []
    for i in range(n):
        top = y1 + int(round(height * i / n))
        bottom = y1 + int(round(height * (i + 1) / n))
        if bottom <= top:
            bottom = top + 1
        out.append([x1, top, x2, min(bottom, y2)])
    return out
