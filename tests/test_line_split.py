"""Projection-profile per-line bbox recovery (backend.ocrmypad.line_split).

All tests are self-contained: they synthesize a page image with PIL text drawn
at KNOWN rows (including deliberately uneven line spacing), then assert the
recovered line bboxes track the real ink — something the old equal-slice
behavior would fail.

Invariants asserted throughout: integer raw-pixel bboxes, x1<=x2, y1<=y2,
inside the block bbox.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.ocrmypad.line_split import (  # noqa: E402
    split_block_into_lines,
    split_block_text_across_bands,
)
from backend.ocrmypad.parser import (  # noqa: E402
    Block,
    blocks_to_hocr,
    parse_response,
)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
]


def _font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _page_with_rows(row_tops, text="The quick brown fox jumps over the lazy dog",
                    font_size=48, width=640, height=400, x=30,
                    add_bottom_rule=False):
    """Synthesize a white page with text rows at explicit top positions."""
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)
    font = _font(font_size)
    for top in row_tops:
        d.text((x, top), text, fill=0, font=font)
    if add_bottom_rule:
        d.rectangle([30, 340, 600, 348], fill=0)  # solid horizontal rule
    return img


def _row_centers(img, x1, y1, x2, y2, min_gap=3):
    """Independent ground truth: ink rows via a plain threshold scan."""
    px = img.load()
    bands = []
    in_band = False
    start = 0
    for y in range(y1, y2):
        dark = any(px[x, y] < 128 for x in range(x1, x2, 2))
        if dark and not in_band:
            in_band, start = True, y
        elif not dark and in_band:
            in_band = False
            if y - start > 1:
                bands.append((start, y))
    if in_band:
        bands.append((start, y2))
    merged = []
    for s, e in bands:
        if merged and s - merged[-1][1] <= min_gap:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append([s, e])
    return [(s + e) / 2.0 for s, e in merged]


def _assert_bboxes_well_formed(boxes, block_bbox):
    for b in boxes:
        x1, y1, x2, y2 = b
        assert all(isinstance(v, int) for v in b), b
        assert x1 <= x2 and y1 <= y2, b
        assert x1 >= block_bbox[0] and y1 >= block_bbox[1]
        assert x2 <= block_bbox[2] and y2 <= block_bbox[3]
        assert y2 > y1, b


def test_tracks_uneven_printed_rows():
    """Lines recovered at the real rows, NOT equal slices of the block."""
    tops = [40, 180, 250]  # deliberately uneven (big gap, then small gap)
    img = _page_with_rows(tops, font_size=40)
    block = [20, 30, 620, 320]
    n = 3
    boxes = split_block_into_lines(img, block, n)
    assert len(boxes) == n
    _assert_bboxes_well_formed(boxes, block)

    centers = [(b[1] + b[3]) / 2.0 for b in boxes]
    truth = _row_centers(img, *block)
    assert len(truth) >= 3, truth
    for center, t in zip(centers, truth):
        assert abs(center - t) <= 16, (centers, truth)


def test_equal_slices_would_fail_where_we_succeed():
    """The old `_line_bboxes` equal-slice logic cannot track these rows."""
    from backend.ocrmypad.parser import _line_bboxes
    tops = [40, 180, 250]
    img = _page_with_rows(tops, font_size=40)
    block_bbox = [20, 30, 620, 320]
    old = _line_bboxes(Block(kind="text", bbox=block_bbox), 3)
    old_centers = [(b[1] + b[3]) / 2.0 for b in old]
    truth = _row_centers(img, *block_bbox)
    # At least one old slice center is far off a real text row center.
    assert any(abs(old_c - t) > 16 for old_c, t in zip(old_centers, truth))


def test_reconciles_oversegmentation_to_line_count():
    """More image bands than text lines: merge the smallest gap, keep n."""
    tops = [40, 160, 180, 260]  # rows 2-3 nearly touching -> treated as 2 bands
    img = _page_with_rows(tops, text="short")
    # Deliberately draw a 4th band via a thin separator so bands=4
    d = ImageDraw.Draw(img)
    d.rectangle([30, 300, 600, 303], fill=0)
    num_bands_guess = len(_row_centers(img, 20, 30, 620, 360))
    block = [20, 30, 620, 360]
    n = 3
    boxes = split_block_into_lines(img, block, n)
    assert len(boxes) == n
    _assert_bboxes_well_formed(boxes, block)


def test_blank_block_falls_back_to_equal_slices():
    """No ink -> graceful equal-slice fallback, invariant preserved."""
    img = Image.new("L", (800, 600), 255)
    block = [100, 100, 700, 400]
    boxes = split_block_into_lines(img, block, 4)
    assert len(boxes) == 4
    _assert_bboxes_well_formed(boxes, block)
    # equal slices of a 300px-tall block
    assert [b[1] for b in boxes] == [100, 175, 250, 325]
    assert [b[3] for b in boxes] == [175, 250, 325, 400]


def test_single_line_returns_block_bbox():
    img = _page_with_rows([50])
    block = [30, 40, 600, 100]
    assert split_block_into_lines(img, block, 1) == [block]
    assert split_block_into_lines(img, block, 0) == []


def test_integer_bboxes_clamped_inside_block():
    img = _page_with_rows([40, 170, 210, 300])
    block = [20, 30, 620, 360]
    for n in (1, 2, 3, 5):
        boxes = split_block_into_lines(img, block, n)
        assert len(boxes) == n
        _assert_bboxes_well_formed(boxes, block)


def test_text_across_bands_reconstructs_and_aligns():
    """Single-line paragraph over 2 printed rows -> 2 (chunk, bbox) pairs."""
    text = "The quick brown fox jumps over the lazy dog and runs home."
    img = _page_with_rows([40, 200], text=text)
    block = [20, 30, 620, 300]
    split = split_block_text_across_bands(img, block, text)
    assert split is not None and len(split) == 2
    chunks = [c for c, _ in split]
    assert "".join(chunks) == text
    _assert_bboxes_well_formed([bb for _, bb in split], block)
    centers = [(bb[1] + bb[3]) / 2.0 for _, bb in split]
    truth = _row_centers(img, *block)
    for center, t in zip(centers, truth):
        assert abs(center - t) <= 16, (centers, truth)


def test_text_across_bands_single_band_returns_none():
    img = _page_with_rows([40], text="Only one printed line here.")
    block = [20, 30, 620, 100]
    assert split_block_text_across_bands(img, block, "Only one line.") is None


def test_text_across_bands_rejects_rule_graphics():
    """A solid rule band is not text-like -> conservative None (no split)."""
    img = _page_with_rows([40, 180], text="word", add_bottom_rule=True)
    block = [20, 30, 620, 360]
    text = "A single line of text that the image thinks is a block."
    split = split_block_text_across_bands(img, block, text)
    # The rule band is ~fully inked -> text-like check fails -> no split.
    assert split is None


def test_text_across_bands_drops_edge_fragment_band():
    """A thin sparse band clipped at the crop edge must not veto a split."""
    img = _page_with_rows([40, 200], text="The quick brown fox and the lazy dog")
    d = ImageDraw.Draw(img)
    # 1px sparse mark at the very top of the crop: a partial glyph fragment.
    d.line([(30, 31), (45, 31)], fill=0)
    block = [20, 30, 620, 300]
    text = ("The quick brown fox jumps over the lazy dog and runs far away "
            "into the deep dark woods near the river bank.")
    split = split_block_text_across_bands(img, block, text)
    assert split is not None and len(split) == 2, split
    chunks = [c for c, _ in split]
    assert "".join(chunks) == text
    _assert_bboxes_well_formed([bb for _, bb in split], block)


def test_text_across_bands_drops_interior_speck_band():
    """A stray 2px speck row between real lines must not veto a split."""
    img = _page_with_rows([40, 200], text="The quick brown fox and the lazy dog")
    d = ImageDraw.Draw(img)
    d.line([(400, 140), (412, 140)], fill=0)  # speck between the two rows
    block = [20, 30, 620, 300]
    text = ("The quick brown fox jumps over the lazy dog and runs far away "
            "into the deep dark woods near the river bank.")
    split = split_block_text_across_bands(img, block, text)
    assert split is not None and len(split) == 2, split
    chunks = [c for c, _ in split]
    assert "".join(chunks) == text


def test_text_across_bands_many_rows_scales_with_text_length():
    """A dense 8-row paragraph is split (was vetoed by max_lines=6)."""
    tops = [40 + 80 * i for i in range(8)]  # 8 evenly spaced rows
    img = _page_with_rows(tops, text="word", font_size=40,
                          width=640, height=700)
    text = ("The quick brown fox jumps over the lazy dog and runs far away "
            "into the deep dark woods near the river bank by the old mill "
            "where the water wheel turns slowly on warm summer afternoons "
            "while children play in the shallow end of the pond nearby.")
    block = [20, 30, 620, 690]
    split = split_block_text_across_bands(img, block, text)
    assert split is not None and len(split) == 8, split
    chunks = [c for c, _ in split]
    assert "".join(chunks) == text
    _assert_bboxes_well_formed([bb for _, bb in split], block)


def test_text_across_bands_short_text_still_capped():
    """A few-band block with short text must not balloon into many chunks."""
    img = _page_with_rows([40, 200], text="word")
    block = [20, 30, 620, 300]
    text = "A short line that spans two rows."
    split = split_block_text_across_bands(img, block, text)
    assert split is not None and len(split) == 2, split


def test_text_across_bands_rule_inside_text_still_rejected():
    """A dense thin band (rule) still rejects the split (fragment vs rule)."""
    img = _page_with_rows([40, 200], text="word")
    d = ImageDraw.Draw(img)
    d.rectangle([30, 100, 600, 103], fill=0)  # dense 3px rule mid-block
    block = [20, 30, 620, 300]
    text = ("The quick brown fox jumps over the lazy dog and runs far away "
            "into the deep dark woods near the river bank.")
    split = split_block_text_across_bands(img, block, text)
    assert split is None


def test_engine_wiring_receives_per_line_placement(tmp_path):
    """generate_hocr's helper builds overrides the hOCR emission consumes."""
    from backend.ocrmypad import unlimited_engine
    from backend.ocrmypad.parser import Block, Page

    text = ("The quick brown fox jumps over the lazy dog and runs far away "
            "into the deep dark woods near the river bank.")
    # Two printed rows inside one paragraph bbox, single-line text.
    img = Image.new("L", (640, 400), 255)
    d = ImageDraw.Draw(img)
    f = _font(40)
    d.text((34, 40), text[:40], fill=0, font=f)
    d.text((30, 200), text[40:], fill=0, font=f)
    png = tmp_path / "page.png"
    img.save(png)

    page = Page(
        page_index=0, width=img.width, height=img.height,
        blocks=[Block(kind="text", bbox=[20, 30, 620, 310], text=text,
                      lines=[text])],
    )
    overrides = unlimited_engine._per_line_overrides(png, page)
    assert 0 in overrides
    pairs = overrides[0]
    assert len(pairs) == 2
    assert "".join(c for c, _ in pairs) == text
    # bboxes are integer and inside the block bbox
    for _, bb in pairs:
        assert all(isinstance(v, int) for v in bb)
        assert bb[0] >= 20 and bb[1] >= 30 and bb[2] <= 620 and bb[3] <= 310
    # and the hOCR emitted from them carries the recovered line rows
    hocr = blocks_to_hocr(img.width, img.height, page.blocks, dpi=300.0,
                          per_line_overrides=overrides)
    assert hocr.count("ocr_line") == 2
    assert f"bbox {pairs[0][1][0]} {pairs[0][1][1]}" in hocr


def test_engine_wiring_multiline_block_keeps_text_lines(tmp_path):
    """n_lines > 1 path: each of the block's lines gets its own bbox."""
    from backend.ocrmypad import unlimited_engine
    from backend.ocrmypad.parser import Block, Page

    img = _page_with_rows([40, 200, 260], font_size=40)
    png = tmp_path / "page.png"
    img.save(png)
    lines = ["First line of the paragraph.", "Second line, indented start.",
             "Third line here."]
    page = Page(
        page_index=0, width=img.width, height=img.height,
        blocks=[Block(kind="text", bbox=[20, 30, 620, 310],
                      text="\n".join(lines), lines=list(lines))],
    )
    overrides = unlimited_engine._per_line_overrides(png, page)
    assert 0 in overrides
    pairs = overrides[0]
    assert len(pairs) == 3
    assert [t for t, _ in pairs] == lines
    centers = [(bb[1] + bb[3]) / 2.0 for _, bb in pairs]
    truth = _row_centers(img, 20, 30, 620, 310)
    for center, t in zip(centers, truth):
        assert abs(center - t) <= 16, (centers, truth)
    """hOCR emission path: override (text, bbox) pairs become ocr_lines."""
    page = parse_response(
        "<|det|>text [100,100,900,400]<|/det|>line one\nline two\nline three",
        1000, 1000, 0)
    overrides = {
        0: [
            ("line one", [100, 110, 800, 160]),
            ("line two", [100, 200, 850, 260]),
            ("line three", [100, 300, 760, 350]),
        ]
    }
    hocr = blocks_to_hocr(1000, 1000, page.blocks, dpi=300.0, ppageno=0,
                          per_line_overrides=overrides)
    assert 'bbox 100 110 800 160' in hocr
    assert 'bbox 100 200 850 260' in hocr
    assert 'bbox 100 300 760 350' in hocr
    # scan_res still present, words present
    assert "scan_res 300 300" in hocr
    assert hocr.count("ocr_line") == 3
    assert hocr.count("ocrx_word") == 3

    # Without overrides the old equal-slice behavior is unchanged.
    plain = blocks_to_hocr(1000, 1000, page.blocks, dpi=300.0, ppageno=0)
    assert 'bbox 100 100 900 200' in plain  # 600px/3 = 200px slices
