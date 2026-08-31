"""<|det|> marker parsing: kinds, caption pairing, bbox remap, robustness."""
from __future__ import annotations

import pytest

from backend.sources.base import PageSpec
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


def test_marker_regex_accepts_float_and_negative_bbox():
    # A float/negative bbox must survive _MARKER_RE (int-coerced downstream),
    # not drop the whole marker including its content.  1000x1000 canvas ->
    # identity scaling so the values stay readable.
    page = UnlimitedOcrAdapter().parse_response(
        "<|det|>text [10.5,20.2,30,40]<|/det|>hello", 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].text == "hello"
    assert page.blocks[0].bbox == [10, 20, 30, 40]

    page2 = UnlimitedOcrAdapter().parse_response(
        "<|det|>text [-5,20,30,40]<|/det|>world", 1000, 1000, 0)
    assert len(page2.blocks) == 1
    assert page2.blocks[0].text == "world"
    assert page2.blocks[0].bbox == [0, 20, 30, 40]  # x1 clamped to canvas


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


def test_latex_to_plain_keeps_newlines_without_display_math():
    # A single backslash (LaTeX escape, path, ...) must not flatten a whole
    # paragraph's line structure: only blank-line runs collapse.  Joining is
    # reserved for equation blocks (join_lines=True).
    t = "line one\nline two\n50\\% off\nline four"
    assert _latex_to_plain(t) == "line one\nline two\n50% off\nline four"
    assert _latex_to_plain(t, join_lines=True) == "line one line two 50% off line four"
    # Blank-line runs still collapse (display-math padding).
    assert _latex_to_plain("a\n\n\nb\\&c") == "a\nb&c"


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


def test_max_batch_pages_zero_means_auto_default(monkeypatch):
    # 0 (or absent) = auto default, as documented in config.example.toml;
    # batching is disabled via unlimited_batch_enabled = false instead.
    # Patch the adapter module's own `resolve` binding (it imports the name
    # directly, so patching backend.config.resolve would not take effect).
    monkeypatch.setattr(
        "backend.sources.unlimited_ocr_adapter.resolve",
        lambda: {"unlimited_max_pages_per_batch": "0"})
    assert UnlimitedOcrAdapter().max_batch_pages == \
        UnlimitedOcrAdapter.DEFAULT_BATCH_PAGES

    monkeypatch.setattr("backend.sources.unlimited_ocr_adapter.resolve",
                        lambda: {})
    assert UnlimitedOcrAdapter().max_batch_pages == \
        UnlimitedOcrAdapter.DEFAULT_BATCH_PAGES

    monkeypatch.setattr(
        "backend.sources.unlimited_ocr_adapter.resolve",
        lambda: {"unlimited_max_pages_per_batch": "5"})
    assert UnlimitedOcrAdapter().max_batch_pages == 5

    # Explicit disable still wins over the auto default.
    monkeypatch.setattr(
        "backend.sources.unlimited_ocr_adapter.resolve",
        lambda: {"unlimited_batch_enabled": "false",
                 "unlimited_max_pages_per_batch": "5"})
    assert UnlimitedOcrAdapter().max_batch_pages == 0


# ---------------------------------------------------------------------------
# Document-level (multi-page) parsing: <PAGE> splitting + batch recognition
# ---------------------------------------------------------------------------

def _adapter(**kw):
    """Adapter instance without network/config side effects.

    ``_post`` and ``_encode_image`` are stubbed so tests exercise the batch
    logic (payload building, <PAGE> splitting, truncation fallback) only.
    """
    a = UnlimitedOcrAdapter.__new__(UnlimitedOcrAdapter)
    a.max_tokens = kw.get("max_tokens", 16384)
    a.batch_enabled = kw.get("batch_enabled", True)
    a.max_batch_pages = kw.get("max_batch_pages", 12)
    a.model = "unlimited-ocr"
    a.base_url = "http://test/v1"
    a.api_key = "k"
    a._encode_image = lambda path: "QUJD"
    a._post = kw.get("post")
    return a


def _resp(content: str, finish: str = "stop", comp: int | None = None) -> dict:
    return {"choices": [{"message": {"content": content},
                         "finish_reason": finish}],
            "usage": {"completion_tokens": comp if comp is not None
                      else len(content)}}


def test_split_multipage_basic():
    text = ("<PAGE><|det|>title [0,0,10,10]<|/det|>A"
            "<PAGE><|det|>text [0,0,10,10]<|/det|>B")
    assert UnlimitedOcrAdapter._split_multipage(text, 2) == [
        "<|det|>title [0,0,10,10]<|/det|>A",
        "<|det|>text [0,0,10,10]<|/det|>B",
    ]


def test_split_multipage_keeps_blank_page_alignment():
    # Page 1 blank: the model still emits its leading <PAGE> separator, so
    # the first chunk is empty — alignment of later pages must not shift.
    text = "<PAGE><PAGE><|det|>text [0,0,10,10]<|/det|>B"
    chunks = UnlimitedOcrAdapter._split_multipage(text, 2)
    assert chunks[0] == ""
    assert "<|det|>" in chunks[1]


def test_split_multipage_without_leading_separator_still_works():
    text = ("<|det|>text [0,0,10,10]<|/det|>A"
            "<PAGE><|det|>text [0,0,10,10]<|/det|>B")
    chunks = UnlimitedOcrAdapter._split_multipage(text, 2)
    assert "<|det|>" in chunks[0] and "<|det|>" in chunks[1]


def test_split_multipage_trailing_separator_tolerated():
    text = ("<PAGE><|det|>text [0,0,10,10]<|/det|>A"
            "<PAGE><|det|>text [0,0,10,10]<|/det|>B<PAGE>")
    chunks = UnlimitedOcrAdapter._split_multipage(text, 2)
    assert len(chunks) == 2 and "<|det|>" in chunks[1]


def test_split_multipage_blank_last_page_is_not_an_artifact():
    # A blank LAST page also produces a trailing empty chunk (one <PAGE>
    # prefix per input image).  Dropping it as an artifact raises a spurious
    # mismatch and wastes a full re-request + recursive split — it must parse.
    text = ("<PAGE><|det|>text [0,0,10,10]<|/det|>A"
            "<PAGE><|det|>text [0,0,10,10]<|/det|>B"
            "<PAGE>")
    chunks = UnlimitedOcrAdapter._split_multipage(text, 3)
    assert chunks[0].count("<|det|>") == 1
    assert chunks[1].count("<|det|>") == 1
    assert chunks[2] == ""  # blank last page keeps its empty string


def test_split_multipage_empty_text_maps_to_blank_pages():
    assert UnlimitedOcrAdapter._split_multipage("", 3) == ["", "", ""]


def test_split_multipage_mismatch_raises():
    text = "<PAGE><|det|>text [0,0,10,10]<|/det|>A"
    with pytest.raises(RuntimeError, match="parse mismatch"):
        UnlimitedOcrAdapter._split_multipage(text, 2)


def test_recognize_pages_batch_single_request_per_page_normalization():
    calls = []

    def fake_post(payload):
        calls.append(payload)
        return _resp("<PAGE><|det|>title [10,10,100,50]<|/det|>Five"
                     "<PAGE><|det|>text [20,20,200,100]<|/det|>Six")

    a = _adapter(post=fake_post)
    specs = [PageSpec("p1.png", 1000, 2000, 5),
             PageSpec("p2.png", 500, 1000, 6)]
    pages = a.recognize_pages(specs)
    assert len(calls) == 1  # one request for both pages
    content = calls[0]["messages"][0]["content"]
    assert content[0]["text"] == "Multi page parsing."
    assert len(content) == 3  # text + 2 image parts, in spec order
    assert calls[0]["skip_special_tokens"] is False
    assert [p.page_index for p in pages] == [5, 6]
    assert pages[0].width == 1000 and pages[1].width == 500
    # Each chunk normalizes against ITS OWN page image: same canvas bbox
    # [20,20,200,100] -> [20,40,200,200] in 1000x2000 vs [10,20,100,100] in
    # 500x1000.
    assert pages[0].blocks[0].bbox == [10, 20, 100, 100]
    assert pages[1].blocks[0].bbox == [10, 20, 100, 100]
    assert pages[0].blocks[0].kind == "title"
    assert pages[1].blocks[0].kind == "text"


def test_recognize_pages_truncation_splits_batch_in_half():
    responses = [
        _resp("", finish="length"),                    # whole batch truncated
        _resp("<|det|>text [0,0,10,10]<|/det|>Five"),  # single-page path
        _resp("<|det|>text [0,0,10,10]<|/det|>Six"),   # single-page path
    ]

    def fake_post(payload):
        return responses.pop(0)

    a = _adapter(post=fake_post)
    pages = a.recognize_pages([PageSpec("p1.png", 1000, 1000, 5),
                               PageSpec("p2.png", 1000, 1000, 6)])
    assert len(responses) == 0  # all three requests consumed
    assert [p.page_index for p in pages] == [5, 6]
    assert pages[0].blocks[0].text == "Five"
    assert pages[1].blocks[0].text == "Six"


def test_recognize_pages_incomplete_batch_retries_once_then_succeeds():
    # Model stops after page 1 on the first attempt, completes on the retry.
    responses = [
        _resp("<PAGE><|det|>text [0,0,10,10]<|/det|>OnlyOne"),
        _resp("<PAGE><|det|>text [0,0,10,10]<|/det|>Five"
              "<PAGE><|det|>text [0,0,10,10]<|/det|>Six"),
    ]

    def fake_post(payload):
        return responses.pop(0)

    a = _adapter(post=fake_post)
    pages = a.recognize_pages([PageSpec("p1.png", 1000, 1000, 5),
                               PageSpec("p2.png", 1000, 1000, 6)])
    assert len(responses) == 0
    assert [p.blocks[0].text for p in pages] == ["Five", "Six"]


def test_recognize_pages_incomplete_batch_splits_after_retry_fails():
    # Two incomplete attempts (page 1 only), then the split bottoms out at
    # single pages which complete via the per-page path.
    responses = [
        _resp("<PAGE><|det|>text [0,0,10,10]<|/det|>OnlyOne"),
        _resp("<PAGE><|det|>text [0,0,10,10]<|/det|>OnlyOneAgain"),
        _resp("<|det|>text [0,0,10,10]<|/det|>Five"),
        _resp("<|det|>text [0,0,10,10]<|/det|>Six"),
    ]

    def fake_post(payload):
        return responses.pop(0)

    a = _adapter(post=fake_post)
    pages = a.recognize_pages([PageSpec("p1.png", 1000, 1000, 5),
                               PageSpec("p2.png", 1000, 1000, 6)])
    assert len(responses) == 0  # 2 attempts + 2 single-page calls
    assert [p.blocks[0].text for p in pages] == ["Five", "Six"]


def test_recognize_pages_single_page_truncation_raises():
    def fake_post(payload):
        return _resp("", finish="length")

    a = _adapter(post=fake_post)
    with pytest.raises(RuntimeError, match="truncated"):
        a.recognize_pages([PageSpec("p1.png", 1000, 1000, 5)])


def test_recognize_pages_disabled_falls_back_to_per_page():
    calls = []

    def fake_post(payload):
        calls.append(payload)
        return _resp("<|det|>text [0,0,10,10]<|/det|>single")

    a = _adapter(post=fake_post, batch_enabled=False)
    pages = a.recognize_pages([PageSpec("p1.png", 1000, 1000, 5),
                               PageSpec("p2.png", 1000, 1000, 6)])
    assert len(calls) == 2  # one request per page
    assert [p.page_index for p in pages] == [5, 6]
    for payload in calls:
        content = payload["messages"][0]["content"]
        assert content[0]["text"] == "document parsing."
        assert len(content) == 2  # text + 1 image


def test_recognize_pages_single_spec_uses_single_prompt():
    calls = []

    def fake_post(payload):
        calls.append(payload)
        return _resp("<|det|>text [0,0,10,10]<|/det|>solo")

    a = _adapter(post=fake_post)
    pages = a.recognize_pages([PageSpec("p1.png", 1000, 1000, 5)])
    assert len(calls) == 1
    assert calls[0]["messages"][0]["content"][0]["text"] == "document parsing."
    assert pages[0].blocks[0].text == "solo"


def test_recognize_pixels_suspicious_empty_retries_once():
    """finish=stop + empty content is retried once (the hosted endpoint's
    blank-page / vision-degeneration shape)."""
    calls = []

    def fake_post(payload):
        calls.append(payload)
        if len(calls) == 1:
            return _resp("", finish="stop", comp=1)
        return _resp("<|det|>text [0,0,10,10]<|/det|>recovered")

    a = _adapter(post=fake_post)
    page = a.recognize_pixels("p1.png", 1000, 1000, 5)
    assert len(calls) == 2
    assert page.blocks[0].text == "recovered"


def test_recognize_pixels_persistent_empty_stays_blank_page():
    calls = []

    def fake_post(payload):
        calls.append(payload)
        return _resp("", finish="stop", comp=1)

    a = _adapter(post=fake_post)
    page = a.recognize_pixels("p1.png", 1000, 1000, 5)
    assert len(calls) == 2  # retried once, then accepted as blank
    assert page.blocks == []


def test_recognize_pixels_image_only_result_is_retried():
    """A lone whole-page image marker (the 'only one <image> block' symptom)
    is treated as a degenerate read and retried."""
    calls = []

    def fake_post(payload):
        calls.append(payload)
        if len(calls) == 1:
            return _resp("<|det|>image [0,0,100,100]<|/det|>")
        return _resp("<|det|>text [0,0,10,10]<|/det|>real text")

    a = _adapter(post=fake_post)
    page = a.recognize_pixels("p1.png", 1000, 1000, 5)
    assert len(calls) == 2
    assert page.blocks[0].kind == "text"
    assert page.blocks[0].text == "real text"


def test_recognize_pixels_persistent_image_only_is_kept():
    """A page that is genuinely a figure stays a single image block after
    the one retry."""
    calls = []

    def fake_post(payload):
        calls.append(payload)
        return _resp("<|det|>image [0,0,100,100]<|/det|>")

    a = _adapter(post=fake_post)
    page = a.recognize_pixels("p1.png", 1000, 1000, 5)
    assert len(calls) == 2
    assert len(page.blocks) == 1 and page.blocks[0].kind == "image"


def test_looks_degenerate():
    from backend.models import OcrBlock
    L = UnlimitedOcrAdapter._looks_degenerate
    from backend.models import OcrPage as OP
    assert L(OP(0, 100, 100, [])) is True
    assert L(OP(0, 100, 100, [OcrBlock(kind="image", bbox=[0, 0, 10, 10])])) is True
    assert L(OP(0, 100, 100, [OcrBlock(kind="image", bbox=[0, 0, 10, 10],
                                       caption="Fig 1")])) is False
    assert L(OP(0, 100, 100, [OcrBlock(kind="text", bbox=[0, 0, 10, 10],
                                       text="hi")])) is False


def test_recognize_pages_batch_degenerate_chunk_reocrs_single():
    """A batch chunk that parses to a lone image marker is re-OCRed through
    the single-page path (which retries degenerate results)."""
    calls = []

    def fake_post(payload):
        calls.append(payload)
        n = len(calls)
        if n == 1:  # the batch: chunk 1 image-only, chunk 2 fine
            return _resp("<PAGE><|det|>image [0,0,100,100]<|/det|>"
                         "<PAGE><|det|>text [0,0,10,10]<|/det|>page two")
        if n == 2:  # single-page re-OCR of page 1: still image-only
            return _resp("<|det|>image [0,0,100,100]<|/det|>")
        return _resp("<|det|>text [0,0,10,10]<|/det|>page one recovered")  # page 1 retry

    a = _adapter(post=fake_post)
    pages = a.recognize_pages([PageSpec("p1.png", 1000, 1000, 5),
                               PageSpec("p2.png", 1000, 1000, 6)])
    assert len(calls) == 3
    assert pages[0].blocks[0].kind == "text"
    assert pages[0].blocks[0].text == "page one recovered"
    assert pages[1].blocks[0].text == "page two"


def test_cache_fingerprint_includes_batch_knobs():
    fp = _adapter().cache_fingerprint()
    assert fp["batch_enabled"] is True
    assert fp["batch_max_pages"] == 12
    fp2 = _adapter(batch_enabled=False, max_batch_pages=0).cache_fingerprint()
    assert fp2["batch_enabled"] is False and fp2["batch_max_pages"] == 0
    assert fp != fp2
