"""Tesseract TSV word clustering: reading order, CJK joins, outlier trim."""
from __future__ import annotations

from backend.sources.tesseract_adapter import TesseractAdapter


def _rows(*rows):
    """rows: (text, conf, block, par, line, left, top, width, height)."""
    data = {k: [] for k in ("text", "conf", "block_num", "par_num",
                            "line_num", "left", "top", "width", "height")}
    for text, conf, bl, par, ln, left, top, w, h in rows:
        data["text"].append(text)
        data["conf"].append(conf)
        data["block_num"].append(bl)
        data["par_num"].append(par)
        data["line_num"].append(ln)
        data["left"].append(left)
        data["top"].append(top)
        data["width"].append(w)
        data["height"].append(h)
    return data


def test_cluster_one_block_per_line_in_reading_order():
    data = _rows(
        ("Hello", 80, 1, 1, 1, 10, 60, 40, 12),
        ("world", 90, 1, 1, 1, 60, 60, 40, 12),
        ("这", 85, 1, 1, 2, 10, 20, 30, 14),
        ("是", 85, 1, 1, 2, 40, 20, 30, 14),
    )
    blocks = TesseractAdapter()._cluster_words(data)
    assert len(blocks) == 2
    # reading order: by top y (line 2 has top=20, so it comes first)
    assert blocks[0].text == "这是"
    assert blocks[1].text == "Hello world"
    assert blocks[0].bbox == [10, 20, 70, 34]
    assert blocks[1].bbox == [10, 60, 100, 72]
    assert blocks[1].conf == 85.0


def test_make_block_trims_outlier_word_from_bbox_not_text():
    words = [
        {"text": "a", "x1": 10, "y1": 20, "x2": 30, "y2": 32,
         "conf": 90, "height": 12},
        {"text": "b", "x1": 40, "y1": 21, "x2": 60, "y2": 34,
         "conf": 90, "height": 13},
        {"text": "c", "x1": 70, "y1": 20, "x2": 90, "y2": 32,
         "conf": 90, "height": 12},
        {"text": "BIG", "x1": 95, "y1": -40, "x2": 100, "y2": 80,
         "conf": 90, "height": 120},
    ]
    block = TesseractAdapter()._make_block(words)
    # bbox comes from the three normal words only (outlier trimmed)
    assert block.bbox == [10, 20, 90, 34]
    assert block.text == "a b c BIG"    # text keeps every word


def test_classify():
    c = TesseractAdapter._classify
    assert c("x + y = z", 12, [{"height": 12}]) == "equation"
    assert c("1.2", 12, [{"height": 12}]) == "heading"
    assert c("CHAPTER", 12, [{"height": 12}]) == "heading"
    assert c("引言", 12, [{"height": 12}]) == "heading"
    # numbered title + trailing words is body text under the current regex
    assert c("1.2 计算方法", 12, [{"height": 12}]) == "text"
    assert c("This is a normal body sentence about OCR.", 12,
             [{"height": 12}]) == "text"
    assert c("Title", 50, [{"height": 60}]) == "heading"
