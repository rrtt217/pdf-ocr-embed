"""Normalized schema survives its own JSON round-trip (the cache depends on it)."""
from __future__ import annotations

from backend.models import OcrBlock, OcrPage


def _page():
    return OcrPage(
        page_index=3,
        width=1654,
        height=2339,
        blocks=[
            OcrBlock(kind="heading", bbox=[10, 20, 300, 40], text="标题"),
            OcrBlock(kind="text", bbox=[10, 50, 300, 90], text="正文",
                     conf=0.93, font_scale=1.25),
            OcrBlock(kind="image", bbox=[400, 100, 500, 200],
                     caption="图 1"),
        ],
    )


def test_page_roundtrip_preserves_everything():
    page = _page()
    clone = OcrPage.from_dict(page.to_dict())
    assert clone.page_index == 3 and clone.width == 1654 and clone.height == 2339
    assert clone.blocks == page.blocks


def test_block_roundtrip():
    b = OcrBlock(kind="equation", bbox=[1, 2, 3, 4], text="x^2",
                 conf=0.99, font_scale=0.8)
    d = b.to_dict()
    assert set(d) == {"kind", "bbox", "text", "conf", "font_scale"}
    assert OcrBlock.from_dict(d) == b


def test_defaults_are_omitted_and_restored():
    d = OcrBlock(kind="text", bbox=[0, 0, 10, 10], text="x").to_dict()
    assert "caption" not in d and "conf" not in d and "font_scale" not in d
    b = OcrBlock.from_dict(d)
    assert b.caption == "" and b.conf is None and b.font_scale == 1.0


def test_bad_font_scale_falls_back_to_one():
    b = OcrBlock.from_dict({"kind": "text", "bbox": [0, 0, 1, 1],
                            "font_scale": "not-a-number"})
    assert b.font_scale == 1.0
