"""<|det|> marker parsing: kinds, caption pairing, bbox remap, robustness."""
from __future__ import annotations

from backend.sources.unlimited_ocr_adapter import (UnlimitedOcrAdapter,
                                                   _latex_to_plain)

SAMPLE = """
<|det|>title [50,100,200,120]<|/det|>Document Title
<|det|>text [50,150,300,300]<|/det|>Hello world
<|det|>image [300,200,500,400]<|/det|>
<|det|>image_caption [310,405,490,420]<|/det|>Fig. 1
<|det|>equation [60,320,400,360]<|/det|>x^2 + 1
"""


def test_parse_response_blocks():
    width, height = 1000, 2000
    page = UnlimitedOcrAdapter().parse_response(SAMPLE, width, height, 0)
    assert page.width == width and page.height == height
    assert [b.kind for b in page.blocks] == ["title", "text", "image",
            "equation"]

    title, text, image, equation = page.blocks
    # canvas bbox [x1,y1,x2,y2] scaled per-axis: x * w/1000, y * h/1000
    assert title.bbox == [50, 200, 200, 240]
    assert text.bbox == [50, 300, 300, 600]
    assert title.text == "Document Title"

    assert image.kind == "image" and image.text == ""
    assert image.caption == "Fig. 1"          # caption paired to image
    assert image.bbox == [300, 400, 500, 800]
    assert equation.bbox == [60, 640, 400, 720]
    assert all(isinstance(v, int) for b in page.blocks for v in b.bbox)


def test_standalone_caption_without_image():
    raw = "<|det|>image_caption [1,2,3,4]<|/det|>Lone caption"
    page = UnlimitedOcrAdapter().parse_response(raw, 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].kind == "image_caption"
    assert page.blocks[0].caption == "Lone caption"


def test_invalid_bbox_entries_are_skipped():
    raw = """
<|det|>text<|/det|>no bbox
<|det|>text [1,2,3]<|/det|>bad bbox
<|det|>text [a,b,c,d]<|/det|>also bad
<|det|>text [10,10,50,40]<|/det|>good
    """
    page = UnlimitedOcrAdapter().parse_response(raw, 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].text == "good" and page.blocks[0].bbox == [10, 10, 50, 40]


def test_unpaired_image_keeps_caption_empty():
    raw = "<|det|>image [1,2,3,4]<|/det|>"
    page = UnlimitedOcrAdapter().parse_response(raw, 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].kind == "image" and page.blocks[0].caption == ""


def test_parse_bbox_cases():
    p = UnlimitedOcrAdapter._parse_bbox
    assert p(None) is None
    assert p("") is None
    assert p("1,2,3") is None and p("a,b,c,d") is None
    assert p("0 10 20 30") == [0, 10, 20, 30]
    assert p("1.5,2.5,3.5,4.9") == [1, 2, 3, 4]


def test_extract_content():
    e = UnlimitedOcrAdapter._extract_content
    assert e({}) == "" and e({"choices": []}) == ""
    assert e({"choices": [{"message": {"content": "hi"}}]}) == "hi"


def test_latex_to_plain_noop_for_plain_text():
    assert _latex_to_plain("hello world") == "hello world"
    assert _latex_to_plain("") == ""


def test_latex_double_arrow_maps_to_single_arrow():
    # chr(92) is a backslash, so this JS-hosted source file stays escape-free
    assert _latex_to_plain(chr(92) + "Rightarrow") == "→"
