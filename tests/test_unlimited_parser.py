"""<|det|> marker parsing: kinds, caption pairing, bbox remap, robustness."""
from __future__ import annotations

import pytest

from backend.ocrmypad.engine_client import UnlimitedOcrClient
from backend.ocrmypad.parser import (
    _parse_bbox,
    parse_response,
    split_multi_page_stream,
)
from backend.ocrmypad.text_norm import (
    clean_math_spacing,
    latex_to_plain,
    table_html_to_text,
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
    page = parse_response(SAMPLE, width, height, 0)
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
    page = parse_response(raw, 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].kind == "image_caption"
    assert page.blocks[0].text == "Lone caption"  # text field → embeddable
    assert page.blocks[0].caption_bbox is None


def test_marker_regex_accepts_float_and_negative_bbox():
    # A float/negative bbox must survive _MARKER_RE (int-coerced downstream),
    # not drop the whole marker including its content.  1000x1000 canvas ->
    # identity scaling so the values stay readable.
    page = parse_response(
        "<|det|>text [10.5,20.2,30,40]<|/det|>hello", 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].text == "hello"
    assert page.blocks[0].bbox == [10, 20, 30, 40]

    page2 = parse_response(
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
    page = parse_response(raw, 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].text == "good" and page.blocks[0].bbox == [10, 10, 50, 40]


def test_unpaired_image_keeps_caption_empty():
    raw = "<|det|>image [1,2,3,4]<|/det|>"
    page = parse_response(raw, 1000, 1000, 0)
    assert len(page.blocks) == 1
    assert page.blocks[0].kind == "image" and page.blocks[0].caption == ""
    assert page.blocks[0].caption_bbox is None


def test_parse_bbox_cases():
    p = _parse_bbox
    assert p(None) is None
    assert p("") is None
    assert p("1,2,3") is None and p("a,b,c,d") is None
    assert p("0 10 20 30") == [0, 10, 20, 30]
    assert p("1.5,2.5,3.5,4.9") == [1, 2, 3, 4]


def test_extract_content():
    e = UnlimitedOcrClient._extract_content
    assert e({}) == "" and e({"choices": []}) == ""
    assert e({"choices": [{"message": {"content": "hi"}}]}) == "hi"


def testlatex_to_plain_noop_for_plain_text():
    assert latex_to_plain("hello world") == "hello world"
    assert latex_to_plain("") == ""


def test_latex_double_arrow_maps_to_single_arrow():
    # chr(92) is a backslash.
    assert latex_to_plain(chr(92) + "Rightarrow") == "→"


def test_table_html_is_converted_to_plain_rows():
    raw = ("<table><tr><td>x_i</td><td>0</td><td>1</td></tr>"
           "<tr><td>f(x_i)</td><td>2 &amp;</td><td>3</td></tr></table>")
    assert table_html_to_text(raw) == "x_i\t0\t1\nf(x_i)\t2 &\t3"
    page = parse_response(
        f"<|det|>table [0,0,100,100]<|/det|>{raw}", 1000, 1000, 0)
    assert page.blocks[0].text == "x_i\t0\t1\nf(x_i)\t2 &\t3"
    assert "<table" not in page.blocks[0].text


def testlatex_to_plain_keeps_newlines_without_display_math():
    # A single backslash (LaTeX escape, path, ...) must not flatten a whole
    # paragraph's line structure: only blank-line runs collapse.  Joining is
    # reserved for equation blocks (join_lines=True).
    t = "line one\nline two\n50\\% off\nline four"
    assert latex_to_plain(t) == "line one\nline two\n50% off\nline four"
    assert latex_to_plain(t, join_lines=True) == "line one line two 50% off line four"
    # Blank-line runs still collapse (display-math padding).
    assert latex_to_plain("a\n\n\nb\\&c") == "a\nb&c"


def test_single_digit_tokens_separated_by_spaces_are_never_merged_by_parser():
    raw = "<|det|>text [0,0,100,100]<|/det|>坐标 3 4 5"
    page = parse_response(raw, 1000, 1000, 0)
    assert page.blocks[0].text == "坐标 3 4 5"


def testclean_math_spacing_tightens_tokenized_formulas():
    assert clean_math_spacing("X _ p = f (x)") == "X_p = f(x)"
    assert clean_math_spacing("||A||_ 1 = ∑_ i = 1^n|a _ i j|") == "||A||_1 = ∑_i = 1^n|a_i j|"
    # content between two digits is significant; never merged.
    assert clean_math_spacing("A = [ 26 32; 32 68 ]") == "A = [ 26 32; 32 68 ]"


def test_extract_finish_reason():
    e = UnlimitedOcrClient._extract_finish_reason
    assert e({}) is None
    assert e({"choices": []}) is None
    assert e({"choices": [{"finish_reason": "stop"}]}) == "stop"
    assert e({"choices": [{"finish_reason": "length"}]}) == "length"


def test_truncated_response_raises_not_cached_as_success():
    chk = UnlimitedOcrClient._assert_not_truncated
    ok = {"choices": [{"finish_reason": "stop"}], "usage": {"completion_tokens": 100}}
    chk(ok, 16384)  # fine — no raise

    by_reason = {"choices": [{"finish_reason": "length"}]}
    try:
        chk(by_reason, 16384)
        raise AssertionError("expected RuntimeError for finish_reason=length")
    except RuntimeError as exc:
        assert "truncated" in str(exc) and "max_tokens=16384" in str(exc)

    by_usage = {"choices": [{"finish_reason": None}],
                "usage": {"completion_tokens": 16384}}
    try:
        chk(by_usage, 16384)
        raise AssertionError("expected RuntimeError for completion_tokens>=max")
    except RuntimeError as exc:
        assert "truncated" in str(exc)


def test_max_tokens_param_is_clamped_below_hard_limit():
    assert UnlimitedOcrClient(max_tokens=99999).max_tokens == 32767
    assert UnlimitedOcrClient(max_tokens=8192).max_tokens == 8192
    assert UnlimitedOcrClient(max_tokens=None).max_tokens == 16384


# --- multi-page <PAGE> stream splitting --------------------------------------

def test_split_multi_page_stream_basic():
    raw = ("<PAGE><|det|>text [0,0,100,100]<|/det|>page one\n"
           "<PAGE><|det|>text [0,0,100,100]<|/det|>page two")
    sections = split_multi_page_stream(raw, 2)
    assert len(sections) == 2
    assert "page one" in sections[0] and "<PAGE>" not in sections[0]
    assert "page two" in sections[1]


def test_split_multi_page_stream_pads_missing_sections():
    # One <PAGE> marker for a 3-image request -> sections 1,2 empty.
    raw = "<PAGE><|det|>text [0,0,100,100]<|/det|>only page"
    sections = split_multi_page_stream(raw, 3)
    assert sections == ["<|det|>text [0,0,100,100]<|/det|>only page", "", ""]


def test_split_multi_page_stream_trims_extra_sections():
    raw = ("<PAGE>a\n<PAGE>b\n<PAGE>c")
    assert split_multi_page_stream(raw, 2) == ["a", "b"]


def test_split_multi_page_stream_no_marker_is_single_section():
    raw = "<|det|>text [0,0,100,100]<|/det|>single"
    # A single-image-style response with no <PAGE> markers: whole stream is
    # section 0; a >1 expectation still pads the rest (caller falls back).
    assert split_multi_page_stream(raw, 1) == [raw]
    assert split_multi_page_stream(raw, 2) == [raw, ""]


def test_split_multi_page_stream_each_section_parses_independently():
    raw = ("<PAGE><|det|>title [0,0,100,100]<|/det|>T1\n"
           "<PAGE><|det|>text [0,0,100,100]<|/det|>hello")
    p0 = parse_response(split_multi_page_stream(raw, 2)[0], 1000, 1000, 0)
    p1 = parse_response(split_multi_page_stream(raw, 2)[1], 1000, 1000, 1)
    assert p0.blocks[0].kind == "title" and p0.blocks[0].text == "T1"
    assert p1.blocks[0].kind == "text" and p1.blocks[0].text == "hello"


# --- multi-page payload / recognize_multi ------------------------------------

def _make_png(tmp_path, name="p.png"):
    import PIL.Image
    p = tmp_path / name
    PIL.Image.new("RGB", (16, 16), "white").save(p)
    return p


def test_multi_payload_structure(tmp_path):
    p1 = _make_png(tmp_path, "a.png")
    p2 = _make_png(tmp_path, "b.png")
    client = UnlimitedOcrClient(base_url="http://x", api_key="k", model="m",
                                max_tokens=16384)
    payload = client.multi_payload([p1, p2])
    assert payload["model"] == "m"
    assert payload["skip_special_tokens"] is False
    assert payload["temperature"] == 0.0
    assert payload["messages"][0]["role"] == "user"
    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Multi page parsing."}
    assert [c["type"] for c in content] == ["text", "image_url", "image_url"]
    # budget = per_page (default 2048) * 2, capped by configured max_tokens
    assert payload["max_tokens"] == 4096
    # data URLs carry base64 PNGs
    assert "data:image/png;base64," in content[1]["image_url"]["url"]


def test_multi_payload_max_tokens_never_exceeds_the_hard_cap():
    p = [_make_png(__import__("pathlib").Path("."))]  # never used below
    client = UnlimitedOcrClient(base_url="http://x", api_key="k", max_tokens=32767,
                                config={"ocr_batch_per_page_tokens": "100000"})
    payload = client.multi_payload(p)
    assert payload["max_tokens"] < 32768


def test_recognize_multi_splits_by_page(tmp_path, monkeypatch):
    p1 = _make_png(tmp_path, "a.png")
    p2 = _make_png(tmp_path, "b.png")
    client = UnlimitedOcrClient(base_url="http://x", api_key="k")
    raw_response = {
        "choices": [{"finish_reason": "stop",
                     "message": {"content":
                         "<PAGE><|det|>text [0,0,100,100]<|/det|>one\n"
                         "<PAGE><|det|>text [0,0,100,100]<|/det|>two"}}],
        "usage": {"completion_tokens": 40},
    }
    posted = []
    monkeypatch.setattr(client, "_post", lambda payload: (posted.append(payload), raw_response)[1])
    sections = client.recognize_multi([p1, p2])
    assert sections == ["<|det|>text [0,0,100,100]<|/det|>one",
                        "<|det|>text [0,0,100,100]<|/det|>two"]
    assert len(posted) == 1 and posted[0]["messages"][0]["content"][0]["text"] \
        == "Multi page parsing."


def test_recognize_multi_empty_response_raises(tmp_path, monkeypatch):
    p1 = _make_png(tmp_path, "a.png")
    p2 = _make_png(tmp_path, "b.png")
    client = UnlimitedOcrClient(base_url="http://x", api_key="k")
    monkeypatch.setattr(client, "_post",
                        lambda payload: {"choices": [{"finish_reason": "stop",
                                                      "message": {"content": ""}}],
                                         "usage": {}})
    with pytest.raises(RuntimeError, match="[Ee]mpty multi-page"):
        client.recognize_multi([p1, p2])


def test_recognize_multi_truncation_raises(tmp_path, monkeypatch):
    p1 = _make_png(tmp_path, "a.png")
    p2 = _make_png(tmp_path, "b.png")
    client = UnlimitedOcrClient(base_url="http://x", api_key="k")
    monkeypatch.setattr(client, "_post",
                        lambda payload: {"choices": [{"finish_reason": "length",
                                                      "message": {"content": "x"}}],
                                         "usage": {"completion_tokens": 4096}})
    with pytest.raises(RuntimeError, match="truncated"):
        client.recognize_multi([p1, p2])


def test_recognize_multi_single_image_delegates_to_recognize(tmp_path, monkeypatch):
    p1 = _make_png(tmp_path, "a.png")
    client = UnlimitedOcrClient(base_url="http://x", api_key="k")
    calls = []
    monkeypatch.setattr(client, "recognize",
                        lambda path: calls.append(str(path)) or "single")
    assert client.recognize_multi([p1]) == ["single"]
    assert calls == [str(p1)]
