"""The plugin's generate_raw option: resolution chain + parse/hOCR effects.

``generate_raw`` (default OFF) keeps each block's raw (pre-normalization)
content: the block sidecar gains a ``raw`` field and the hOCR gains
``x_kind``/``x_raw`` engine properties on each ``ocr_par`` title (the hOCR 1.2
extension mechanism).  ocrmypdf's hocrtransform parser ignores unknown title
properties, so the embed render is unaffected (verified separately, see
research/hocr-raw-placement.md).
"""
from __future__ import annotations

import re

from ocrmypdf_unlimited import settings as unlimited_settings
from ocrmypdf_unlimited.parser import (
    Block,
    decode_raw_prop,
    parse_response,
)

TABLE_HTML = "<table><tr><td>N_k</td><td>2^k</td></tr></table>"

SAMPLE = (
    "<|det|>title [50,100,200,120]<|/det|>Document Title\n"
    "<|det|>text [50,150,300,300]<|/det|>Hello world\n"
    "<|det|>equation [60,320,400,360]<|/det|>x _ k = \\frac{1}{2}\n"
    f"<|det|>table [0,0,100,100]<|/det|>{TABLE_HTML}\n"
)

# "Hello world" is unchanged by normalization; the equation/table are.
UNCHANGED = "Hello world"


class _Opts:
    """Duck-typed OcrOptions stand-in (settings.from_options reads attrs)."""

    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


def test_default_off_no_raw_field():
    page = parse_response(SAMPLE, 1000, 1000, 0)
    assert all(block.raw == "" for block in page.blocks)
    # to_dict stays exactly the pre-feature shape (no "raw" key).
    assert all("raw" not in block.to_dict() for block in page.blocks)


def test_save_raw_keeps_pre_normalization_content():
    page = parse_response(SAMPLE, 1000, 1000, 0, save_raw=True)
    by_kind = {b.kind: b for b in page.blocks}
    # equation: raw keeps the LaTeX command the normalization flattens
    assert "\\frac{1}{2}" in by_kind["equation"].raw
    assert "\\frac" not in by_kind["equation"].text
    # table: raw keeps the HTML fragment the normalization strips
    assert "<table>" in by_kind["table"].raw
    assert "<table" not in by_kind["table"].text
    # unchanged content carries no redundant raw copy
    assert by_kind["text"].raw == ""
    assert by_kind["text"].text == UNCHANGED


def test_save_raw_sidecar_shape_roundtrip():
    page = parse_response(SAMPLE, 1000, 1000, 0, save_raw=True)
    for block in page.blocks:
        data = block.to_dict()
        assert Block.from_dict(data).raw == block.raw
    # normalized text is still what the embed path reads
    assert all(d["text"] for d in (b.to_dict() for b in page.blocks))


def test_from_options_generate_raw_resolution():
    cfg = unlimited_settings.from_options(_Opts(unlimited_generate_raw=True))
    assert cfg.get("generate_raw") is True
    # absent flag -> absent key (never shadows env/snapshot layers)
    assert "generate_raw" not in unlimited_settings.from_options(_Opts())
    assert "generate_raw" not in unlimited_settings.from_options(None)


def test_generate_raw_env_alias():
    import os
    prev = os.environ.get("OCR_UNLIMITED_GENERATE_RAW")
    os.environ["OCR_UNLIMITED_GENERATE_RAW"] = "1"
    try:
        assert unlimited_settings.from_env().get("generate_raw") == "1"
        cfg = unlimited_settings.effective(_Opts())
        assert unlimited_settings.as_bool(cfg.get("generate_raw")) is True
    finally:
        if prev is None:
            os.environ.pop("OCR_UNLIMITED_GENERATE_RAW", None)
        else:
            os.environ["OCR_UNLIMITED_GENERATE_RAW"] = prev


def test_as_bool_string_forms():
    assert unlimited_settings.as_bool("true") is True
    assert unlimited_settings.as_bool("1") is True
    assert unlimited_settings.as_bool("false") is False
    assert unlimited_settings.as_bool("0") is False
    assert unlimited_settings.as_bool(True) is True
    assert unlimited_settings.as_bool(None) is False


def test_blocks_to_hocr_carries_x_kind_x_raw_and_ignores_by_ocrmypdf():
    """The hOCR gains x_kind/x_raw on the ocr_par title; ocrmypdf's own
    hocrtransform parser must produce an IDENTICAL OcrElement tree with and
    without them (unknown title properties are ignored -> embed unaffected)."""
    from ocrmypdf_unlimited.parser import blocks_to_hocr
    from ocrmypdf.hocrtransform.hocr_parser import HocrParser

    blocks = [
        Block(kind="equation", bbox=[60, 320, 400, 360], text="x_k = 1/2",
              lines=["x_k = 1/2"], raw="x_k = \\frac{1}{2}"),
        Block(kind="text", bbox=[50, 150, 300, 300], text="Hello",
              lines=["Hello"], raw=""),
    ]
    hocr_with = blocks_to_hocr(1000, 2000, blocks, dpi=300, ppageno=0)
    hocr_without = blocks_to_hocr(
        1000, 2000,
        [Block(kind=b.kind, bbox=b.bbox, text=b.text, lines=b.lines)
         for b in blocks], dpi=300, ppageno=0)

    # the raw-carrying hOCR carries the properties + capability declaration
    assert "; x_kind equation; x_raw " in hocr_with
    assert "x_kind" not in hocr_without and "x_raw" not in hocr_without
    assert "ocrp_x_raw" in hocr_with and "ocrp_x_raw" not in hocr_without

    # the property value decodes back to the raw content (anchored on the
    # par-title form so the ocrp_x_* capability declaration cannot match)
    m = re.search(r"; x_kind equation; x_raw ([A-Za-z0-9_=-]+)", hocr_with)
    assert m and decode_raw_prop(m.group(1)) == "x_k = \\frac{1}{2}"

    # ocrmypdf's own parser: identical tree with and without the properties
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p_with = Path(td) / "with.hocr"
        p_without = Path(td) / "without.hocr"
        p_with.write_text(hocr_with, encoding="utf-8")
        p_without.write_text(hocr_without, encoding="utf-8")

        def words(tree):
            out = []
            def walk(el):
                for c in el.children:
                    if c.text:
                        out.append(c.text)
                    walk(c)
            walk(tree)
            return out

        t_with, t_without = HocrParser(p_with).parse(), HocrParser(p_without).parse()
        assert words(t_with) == words(t_without) == ["x_k = 1/2", "Hello"]
        assert t_with.bbox == t_without.bbox
        assert t_with.dpi == t_without.dpi == 300.0


def test_host_side_raw_prop_copy_matches_plugin():
    """backend.page_store's x_raw encoder is an IDENTICAL copy of the plugin's
    (the backend never imports plugin internals) — same pattern as
    normalize_bbox in backend.errors."""
    from backend.page_store import _raw_prop_value as host_encode
    from ocrmypdf_unlimited.parser import _raw_prop_value as plugin_encode

    for raw in ("", "plain", "电压\t二值逻辑\n(N)_n = \\sum K_k×2^k",
                "a; b = c", "emoji \U0001f600"):
        assert host_encode(raw) == plugin_encode(raw)


def test_page_store_blocks_to_hocr_reemits_x_kind_x_raw():
    """The edit path regenerates a page's hOCR from the (edited) sidecar:
    raw-carrying blocks must keep their x_kind/x_raw properties."""
    from backend.page_store import blocks_to_hocr

    blocks = [{"kind": "equation", "bbox": [0, 0, 100, 40],
               "text": "x_k = 1/2", "raw": "x_k = \\frac{1}{2}"},
              {"kind": "text", "bbox": [0, 50, 100, 90], "text": "Hello"}]
    hocr = blocks_to_hocr(100, 200, blocks, dpi=300, ppageno=0)
    assert "; x_kind equation; x_raw " in hocr
    assert "x_kind text" not in hocr  # no raw -> no properties
    # ocrmypdf's own parser still reads it identically (embed unaffected)
    from ocrmypdf.hocrtransform.hocr_parser import HocrParser
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "p.hocr"
        p.write_text(hocr, encoding="utf-8")
        tree = HocrParser(p).parse()
        words = []
        def walk(el):
            for c in el.children:
                if c.text:
                    words.append(c.text)
                walk(c)
        walk(tree)
        assert words == ["x_k = 1/2", "Hello"]
