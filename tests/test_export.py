"""Export formats: markdown + LaTeX from the stored OCR pages.

Pins the pure builders (backend.export) and the raw-content contract: blocks
whose sidecar carries a ``raw`` field (written by the unlimited engine when
its ``generate_raw`` option is on) are exported in preference to the lossy
normalized ``text``.
"""
from __future__ import annotations

from backend.export import (
    block_to_latex,
    block_to_markdown,
    block_source,
    export_document,
    heading_level,
    latex_escape,
    pages_to_latex,
    pages_to_markdown,
)


# --- raw-content contract -----------------------------------------------------

def test_block_source_prefers_raw_when_present():
    block = {"kind": "equation", "text": "N_k = Σ_k K_k×2^k",
             "raw": "N_k = \\sum_{k=0}^{\\infty} K_k \\times 2^k"}
    assert block_source(block) == block["raw"]
    assert block_source(block, use_raw=False) == block["text"]


def test_block_source_falls_back_to_normalized_text():
    block = {"kind": "text", "text": "normalized"}
    assert block_source(block) == "normalized"
    assert block_source({"kind": "text"}) == ""


def test_heading_level_from_numbering():
    assert heading_level("1. 数字逻辑概论") == 1
    assert heading_level("1.2 二进制") == 2
    assert heading_level("1.2.3 编码") == 3
    assert heading_level("2、无点号编号") == 1
    assert heading_level("无编号标题") == 2


# --- markdown ------------------------------------------------------------------

def test_markdown_table_from_raw_html():
    block = {"kind": "table", "text": "电压\t逻辑\n3.5~5 V\t1",
             "raw": "<table><tr><td>电压</td><td>逻辑</td></tr>"
                    "<tr><td>3.5~5 V</td><td>1</td></tr></table>"}
    md = block_to_markdown(block)
    assert "| 电压 | 逻辑 |" in md
    assert "| 3.5~5 V | 1 |" in md
    assert "<table" not in md
    # the separator row sits between header and body (padded to the widths)
    lines = md.splitlines()
    assert set(lines[1]) <= {"|", "-", " "}


def test_markdown_table_falls_back_to_tab_text():
    block = {"kind": "table", "text": "a\tb\nc\td"}
    md = block_to_markdown(block)
    assert "| a | b |" in md and "| c | d |" in md


def test_markdown_equation_uses_raw_latex_body():
    block = {"kind": "equation", "text": "N_k = Σ K_k×2^k",
             "raw": "\\[ N_k = \\sum_{k=0}^{\\infty} K_k 2^k \\]"}
    md = block_to_markdown(block)
    assert md.startswith("$$\n") and md.endswith("\n$$")
    assert "\\sum" in md and "\\[" not in md  # delimiters stripped inside


def test_markdown_equation_falls_back_to_normalized_text():
    block = {"kind": "equation", "text": "x^2 + 1"}
    assert block_to_markdown(block) == "$$\nx^2 + 1\n$$"


def test_markdown_heading_levels():
    assert block_to_markdown(
        {"kind": "title", "text": "1. 概论"}) == "# 1. 概论"
    assert block_to_markdown(
        {"kind": "title", "text": "2.1 二进制"}) == "## 2.1 二进制"


def test_markdown_image_placeholder_uses_caption():
    block = {"kind": "image", "text": "", "caption": "图 1.11 模拟量", "bbox": [0, 0, 1, 1]}
    md = block_to_markdown(block, page_index=10)
    assert md.startswith("> [图]") and "图 1.11 模拟量" in md and "page 11" in md


def test_markdown_image_resolver_renders_real_image():
    """An image_resolver turn the placeholder into a real markdown image."""
    block = {"kind": "image", "text": "", "caption": "图 1", "bbox": [0, 0, 1, 1]}
    resolver = lambda b, pi, bi: f"images/page-{(pi or 0) + 1:03d}-img-{bi}.png"
    md = block_to_markdown(block, page_index=0, block_index=1,
                           image_resolver=resolver)
    assert md == "![图 1](images/page-001-img-1.png)"


def test_markdown_image_resolver_failure_falls_back_to_placeholder():
    block = {"kind": "image", "text": "", "caption": "图 1", "bbox": [0, 0, 1, 1]}
    def broken(block, page_index, block_index):
        raise RuntimeError("boom")
    md = block_to_markdown(block, page_index=0, block_index=0,
                           image_resolver=broken)
    assert md.startswith("> [图]")


def test_markdown_image_resolver_none_link_falls_back():
    block = {"kind": "image", "text": "", "caption": "图 1", "bbox": [0, 0, 1, 1]}
    md = block_to_markdown(block, page_index=0, block_index=0,
                           image_resolver=lambda b, pi, bi: None)
    assert md.startswith("> [图]")


def test_pages_to_markdown_passes_block_index_to_resolver():
    seen = []
    pages = [{"page_index": 2, "width": 1, "height": 1, "blocks": [
        {"kind": "text", "text": "a", "bbox": [0, 0, 1, 1]},
        {"kind": "image", "text": "", "caption": "c", "bbox": [0, 0, 1, 1]},
    ]}]
    pages_to_markdown(pages, image_resolver=lambda b, pi, bi: seen.append((pi, bi)))
    assert (2, 1) in seen


def test_markdown_furniture_blocks_are_skipped():
    pages = [{"page_index": 0, "width": 100, "height": 100, "blocks": [
        {"kind": "page_number", "text": "12", "bbox": [0, 0, 1, 1]},
        {"kind": "header", "text": "1. 数字逻辑概论", "bbox": [0, 0, 1, 1]},
        {"kind": "text", "text": "正文", "bbox": [0, 0, 1, 1]},
    ]}]
    md = pages_to_markdown(pages)
    assert "12" not in md and "数字逻辑概论" not in md and "正文" in md


def test_pages_to_markdown_with_title_and_page_markers():
    pages = [{"page_index": 0, "width": 1, "height": 1, "blocks": [
        {"kind": "text", "text": "hello", "bbox": [0, 0, 1, 1]}]}]
    md = pages_to_markdown(pages, title="Doc")
    assert md.startswith("# Doc\n\nhello")
    md2 = pages_to_markdown(pages, page_markers=True)
    assert "<!-- page 1 -->" in md2


# --- latex ----------------------------------------------------------------------

def test_latex_escape_specials():
    assert latex_escape("a_b & c%") == r"a\_b \& c\%"
    assert latex_escape("100%") == r"100\%"


def test_latex_table_from_raw_html():
    block = {"kind": "table", "text": "a\tb",
             "raw": "<table><tr><th>x</th><th>y</th></tr>"
                    "<tr><td>1</td><td>a_b</td></tr></table>"}
    tex = block_to_latex(block)
    assert "\\begin{tabular}{cc}" in tex
    assert "x & y" in tex and "1 & a\\_b \\\\" in tex
    assert "\\end{tabular}" in tex


def test_latex_equation_keeps_raw_latex():
    block = {"kind": "equation", "text": "N_k = Σ K_k×2^k",
             "raw": "N_k = \\sum_{k=0}^{\\infty} K_k 2^k"}
    tex = block_to_latex(block)
    assert "\\begin{equation*}" in tex and "\\sum_{k=0}^{\\infty}" in tex
    # raw LaTeX commands must NOT be escaped
    assert "\\sum" in tex and "textbackslash" not in tex


def test_latex_heading_commands():
    assert block_to_latex(
        {"kind": "title", "text": "1. 概论"}) == "\\section{1. 概论}"
    assert block_to_latex(
        {"kind": "title", "text": "2.1 二进制"}) == "\\subsection{2.1 二进制}"
    assert block_to_latex(
        {"kind": "title", "text": "2.1.3 编码"}) == "\\subsubsection{2.1.3 编码}"


def test_latex_document_shape():
    pages = [{"page_index": 0, "width": 1, "height": 1, "blocks": [
        {"kind": "title", "text": "1. 概论", "bbox": [0, 0, 1, 1]},
        {"kind": "text", "text": "正文 100%", "bbox": [0, 0, 1, 1]},
    ]}]
    tex = pages_to_latex(pages, title="Doc")
    assert tex.startswith("% Generated by pdf-ocr-embed")
    assert "\\documentclass[11pt]{article}" in tex
    assert "\\usepackage[UTF8]{ctex}" in tex  # CJK support
    assert "\\usepackage{amsmath}" in tex
    assert "\\section{1. 概论}" in tex
    assert "正文 100\\%" in tex
    assert tex.rstrip().endswith("\\end{document}")


def test_export_document_dispatch():
    pages = [{"page_index": 0, "width": 1, "height": 1, "blocks": [
        {"kind": "text", "text": "hi", "bbox": [0, 0, 1, 1]}]}]
    assert "hi" in export_document("markdown", pages)
    assert "hi" in export_document("tex", pages, title="T")
    assert "hi" in export_document("md", pages)
    try:
        export_document("html", pages)
        raise AssertionError("expected ValueError for unknown format")
    except ValueError:
        pass


# --- raw prop round-trip (hOCR x_raw contract) ------------------------------------

def test_raw_prop_roundtrip_cjk_and_newlines():
    from ocrmypdf_unlimited.parser import (
        _raw_prop_value,
        decode_raw_prop,
        raw_prop_value,
    )
    assert raw_prop_value("x") == _raw_prop_value("x")  # public alias, same behavior
    raw = "电压\t二值逻辑\n(N)_n = \\sum_k K_k×2^k"
    encoded = _raw_prop_value(raw)
    # hOCR 1.2 §2.4 ascii-word: printable ASCII, no space/semicolon
    assert encoded and all(33 <= ord(c) <= 126 for c in encoded)
    assert decode_raw_prop(encoded) == raw
    # plain (unencoded) values decode best-effort to themselves
    assert decode_raw_prop("plain value") == "plain value"
