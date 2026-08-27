"""<|det|> marker parsing: kinds, caption pairing, bbox remap, robustness."""
from __future__ import annotations

from backend.sources.unlimited_ocr_adapter import (
    UnlimitedOcrAdapter,
    _clean_math_spacing,
    _latex_to_plain,
    _table_html_to_text,
)

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
    assert image.caption_bbox == [310, 810, 490, 840]  # caption keeps its own bbox
    assert image.bbox == [300, 400, 500, 800]
    assert equation.bbox == [60, 640, 400, 720]
    assert all(isinstance(v, int) for b in page.blocks for v in b.bbox)


def test_standalone_caption_without_image():
    raw = "<|det|>image_caption [1,2,3,4]<|/det|>Lone caption"
    page = UnlimitedOcrAdapter().parse_response(raw, 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].kind == "image_caption"
    assert page.blocks[0].text == "Lone caption"  # text field → embeddable
    assert page.blocks[0].caption_bbox is None


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
    assert page.blocks[0].caption_bbox is None


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
    # chr(92) is a backslash.
    assert _latex_to_plain(chr(92) + "Rightarrow") == "→"


def test_table_html_is_converted_to_plain_rows():
    raw = ("<table><tr><td>x_i</td><td>0</td><td>1</td></tr>"
           "<tr><td>f(x_i)</td><td>2 &amp;</td><td>3</td></tr></table>")
    assert _table_html_to_text(raw) == "x_i\t0\t1\nf(x_i)\t2 &\t3"
    page = UnlimitedOcrAdapter().parse_response(
        f"<|det|>table [0,0,100,100]<|/det|>{raw}", 1000, 1000, 0)
    assert page.blocks[0].text == "x_i\t0\t1\nf(x_i)\t2 &\t3"
    assert "<table" not in page.blocks[0].text


def test_single_digit_tokens_separated_by_spaces_are_never_merged_by_parser():
    raw = "<|det|>text [0,0,100,100]<|/det|>坐标 3 4 5"
    page = UnlimitedOcrAdapter().parse_response(raw, 1000, 1000, 0)
    assert page.blocks[0].text == "坐标 3 4 5"


def test_clean_math_spacing_tightens_tokenized_formulas():
    assert _clean_math_spacing("X _ p = f (x)") == "X_p = f(x)"
    assert _clean_math_spacing("||A||_ 1 = ∑_ i = 1^n|a _ i j|") == "||A||_1 = ∑_i = 1^n|a_i j|"
    # content between two digits is significant; never merged.
    assert _clean_math_spacing("A = [ 26 32; 32 68 ]") == "A = [ 26 32; 32 68 ]"


def test_extract_finish_reason():
    e = UnlimitedOcrAdapter._extract_finish_reason
    assert e({}) is None
    assert e({"choices": []}) is None
    assert e({"choices": [{"finish_reason": "stop"}]}) == "stop"
    assert e({"choices": [{"finish_reason": "length"}]}) == "length"


def test_truncated_response_raises_not_cached_as_success():
    a = UnlimitedOcrAdapter.__new__(UnlimitedOcrAdapter)  # skip network config
    chk = UnlimitedOcrAdapter._assert_not_truncated
    ok = {"choices": [{"finish_reason": "stop"}], "usage": {"completion_tokens": 100}}
    chk(ok, 16384, 7)  # fine — no raise

    by_reason = {"choices": [{"finish_reason": "length"}]}
    try:
        chk(by_reason, 16384, 7)
        raise AssertionError("expected RuntimeError for finish_reason=length")
    except RuntimeError as exc:
        assert "truncated" in str(exc) and "max_tokens=16384" in str(exc)

    by_usage = {"choices": [{"finish_reason": None}],
                "usage": {"completion_tokens": 16384}}
    try:
        chk(by_usage, 16384, 7)
        raise AssertionError("expected RuntimeError for completion_tokens>=max")
    except RuntimeError as exc:
        assert "truncated" in str(exc)


def test_max_tokens_param_is_clamped_below_hard_limit():
    assert UnlimitedOcrAdapter(max_tokens=99999).max_tokens == 32767
    assert UnlimitedOcrAdapter(max_tokens=8192).max_tokens == 8192
    assert UnlimitedOcrAdapter(max_tokens=None).max_tokens == 16384


def test_parser_version_changes_cache_fingerprint():
    fp = UnlimitedOcrAdapter().cache_fingerprint()
    assert fp["parser_version"] == UnlimitedOcrAdapter.PARSE_VERSION
    old = dict(fp, parser_version=fp["parser_version"] - 1)
    import backend.ocr_cache as ocr_cache
    assert ocr_cache.build_key(fp) != ocr_cache.build_key(old)
