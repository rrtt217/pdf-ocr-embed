"""Export pre-processing (backend.export_llm): deterministic reflow + LLM fix-up.

Pins the P0 pure functions (line-split unwrap, list marking, table padding,
cross-page merge), the LLM fix-up contract (JSON protocol, per-block guards,
cache, fallbacks) and the regression line: the ``llm`` tag must never leak
into the embedded text layer (page_store.blocks_to_hocr ignores it).
"""
from __future__ import annotations

import json

from backend import export, export_llm
from backend.page_store import blocks_to_hocr


def _page(blocks, page_index=0, width=1000, height=1400):
    return {"page_index": page_index, "width": width, "height": height,
            "blocks": blocks}


def _block(text, kind="text", **extra):
    b = {"kind": kind, "bbox": [0, 0, 100, 20], "text": text,
         "lines": [text]}
    b.update(extra)
    return b


# --- P0: line-split unwrap -----------------------------------------------------

def test_unwrap_joins_cjk_directly():
    assert export_llm.unwrap_lines("数字逻辑概\n论是基础课") == "数字逻辑概论是基础课"


def test_unwrap_joins_latin_with_space():
    assert export_llm.unwrap_lines("the quick\nbrown fox") == "the quick brown fox"


def test_unwrap_merges_hyphen_breaks():
    assert export_llm.unwrap_lines("informa-\ntion systems") == "information systems"


def test_unwrap_keeps_paragraph_breaks():
    assert export_llm.unwrap_lines("第一段\n\n第二段") == "第一段\n\n第二段"


def test_unwrap_keeps_list_lines_separate():
    text = "前言如下\n1. 第一项\n2. 第二项"
    assert export_llm.unwrap_lines(text) == text


def test_unwrap_keeps_table_lines_separate():
    text = "表头如下\n| a | b |\n| 1 | 2 |"
    assert export_llm.unwrap_lines(text) == text


def test_unwrap_keeps_label_lines_separate():
    text = "摘要：\n这是正文"
    assert export_llm.unwrap_lines(text) == "摘要：\n这是正文"


def test_unwrap_single_line_noop():
    assert export_llm.unwrap_lines("单行文本") == "单行文本"
    assert export_llm.unwrap_lines("") == ""


def test_is_list_item():
    assert export_llm.is_list_item("1. 第一项")
    assert export_llm.is_list_item("- 要点")
    assert export_llm.is_list_item("• 要点")
    assert not export_llm.is_list_item("普通段落")
    assert not export_llm.is_list_item("")


def test_mark_list_items_tags_blocks():
    blocks = [_block("1. 甲"), _block("普通"), _block("• 乙")]
    assert export_llm.mark_list_items(blocks) == 2
    assert blocks[0]["llm_list"] is True
    assert "llm_list" not in blocks[1]
    assert blocks[2]["llm_list"] is True


# --- P0: table padding ----------------------------------------------------------

def test_reflow_table_rows_pads_ragged():
    rows = [["a", "b"], ["1"]]
    assert export_llm.reflow_table_rows(rows) == [["a", "b"], ["1", ""]]


def test_reflow_table_rows_single_column_downgrades():
    assert export_llm.reflow_table_rows([["a"], ["b"]]) is None
    assert export_llm.reflow_table_rows([]) is None


def test_reflow_table_block_pads_tab_text():
    block = _block("电压\t逻辑\n3.5~5 V", kind="table")
    assert export_llm.reflow_table_block(block) is True
    assert block["text"] == "电压\t逻辑\n3.5~5 V\t"
    assert block["lines"][-1] == "3.5~5 V\t"


def test_reflow_table_block_downgrades_single_column():
    block = _block("只有\n一列", kind="table")
    assert export_llm.reflow_table_block(block) is True
    assert block["kind"] == "text"


def test_reflow_table_block_leaves_raw_html_alone():
    block = _block("a\tb", kind="table",
                   raw="<table><tr><td>a</td><td>b</td></tr></table>")
    assert export_llm.reflow_table_block(block) is False
    assert block["text"] == "a\tb"


# --- P0: cross-page merge --------------------------------------------------------

def test_merge_cross_page_merges_and_marks():
    tail = _block("这一段还没有写完，下一页", page_index=0)
    head = _block("继续讲述同一件事。", page_index=1)
    pages = [_page([tail], page_index=0), _page([head], page_index=1)]
    assert export_llm.merge_cross_page(pages) == 1
    assert tail["text"] == "这一段还没有写完，下一页继续讲述同一件事。"
    assert tail["llm"]["merged_from"] == [2]
    assert head["text"] == ""


def test_merge_skips_terminal_punctuation():
    tail = _block("这一段已经写完。")
    head = _block("新的一段开始了")
    pages = [_page([tail]), _page([head])]
    assert export_llm.merge_cross_page(pages) == 0
    assert tail["text"] == "这一段已经写完。"


def test_merge_skips_list_heads_and_structured_tails():
    tail = _block("列表如下")
    head = _block("1. 第一项")
    assert export_llm.merge_cross_page([_page([tail]), _page([head])]) == 0
    tail2 = _block("| a | b |")
    head2 = _block("后续内容")
    assert export_llm.merge_cross_page([_page([tail2]), _page([head2])]) == 0


def test_merge_skips_non_text_kinds():
    tail = _block("E = mc^2", kind="equation")
    head = _block("其中 c 是光速")
    assert export_llm.merge_cross_page([_page([tail]), _page([head])]) == 0


def test_merge_skips_uppercase_head():
    tail = _block("the sentence continues")
    head = _block("Next page heading")
    assert export_llm.merge_cross_page([_page([tail]), _page([head])]) == 0


# --- block_source / table rows: the llm tier -------------------------------------

def test_block_source_prefers_llm_then_raw_then_text():
    block = {"kind": "text", "text": "normalized", "raw": "raw text",
             "llm": {"text": "llm text"}}
    assert export.block_source(block) == "llm text"
    assert export.block_source({"kind": "text", "text": "t",
                                "raw": "r"}) == "r"
    assert export.block_source({"kind": "text", "text": "t"}) == "t"


def test_md_table_to_rows_parses_pipe_tables():
    text = "| 电压 | 逻辑 |\n| --- | --- |\n| 3.5~5 V | 1 |"
    rows = export._md_table_to_rows(text)
    assert rows == [["电压", "逻辑"], ["3.5~5 V", "1"]]


def test_md_table_to_rows_unescapes_pipes():
    rows = export._md_table_to_rows("| a \\| b | c |")
    assert rows == [["a | b", "c"]]


def test_table_rows_prefers_llm_pipe_table():
    block = {"kind": "table", "text": "电压\t逻辑",
             "llm": {"text": "| 电压 | 逻辑 |\n| --- | --- |\n| 3.5 V | 1 |"}}
    assert export._table_rows(block, use_raw=True) == \
        [["电压", "逻辑"], ["3.5 V", "1"]]


def test_llm_markdown_table_export():
    block = {"kind": "table", "text": "电压\t逻辑",
             "llm": {"text": "| 电压 | 逻辑 |\n|---|---|\n| 3.5 V | 1 |"}}
    md = export.block_to_markdown(block)
    assert "| 电压 | 逻辑 |" in md
    assert "| 3.5 V | 1 |" in md


def test_llm_latex_equation_export():
    block = {"kind": "equation", "text": "N_k = Σ_k K_k×2^k",
             "llm": {"text": "N_k = \\sum_{k=0}^{\\infty} K_k 2^k"}}
    tex = export.block_to_latex(block)
    assert "\\sum_{k=0}^{\\infty}" in tex


# --- guards ----------------------------------------------------------------------

def test_guard_accepts_reasonable_rewrite():
    assert export_llm.guard_output(
        "text", "some text here with more words",
        "some text here with more words, tidied")
    assert export_llm.guard_output(
        "equation", "N_k = Σ_k K_k×2^k",
        "N_k = \\sum_{k=0}^{\\infty} K_k 2^k")


def test_guard_rejects_empty_and_dropped_tokens():
    assert not export_llm.guard_output("text", "abc 123", "")
    assert not export_llm.guard_output("text", "alpha beta gamma 123 456",
                                       "alpha beta")
    # equations may add LaTeX commands (added tokens unchecked) but may not
    # drop content tokens
    assert not export_llm.guard_output("equation", "E = mc2 + k123",
                                       "E = mc2")


def test_guard_rejects_length_outliers():
    assert not export_llm.guard_output("text", "short", "x" * 200)
    assert not export_llm.guard_output("heading", "a heading text here",
                                       "a")


# --- prompt / response parsing -----------------------------------------------------

def test_user_prompt_numbering_and_rules():
    prompt = export_llm._user_prompt(
        [{"n": 0, "kind": "table", "text": "a\tb"},
         {"n": 1, "kind": "equation", "text": "E=mc2"}], "markdown")
    assert "<<<BLOCK 0>>> kind=table" in prompt
    assert "<<<BLOCK 1>>> kind=equation" in prompt
    assert "markdown pipe tables" in prompt
    assert "Never add, drop or translate" in prompt


def test_parse_blocks_response_handles_fences():
    raw = '```json\n{"blocks":[{"n":0,"ok":true,"text":"x"}]}\n```'
    items = export_llm.parse_blocks_response(raw)
    assert items == [{"n": 0, "ok": True, "text": "x"}]


def test_parse_blocks_response_garbage():
    assert export_llm.parse_blocks_response("") is None
    assert export_llm.parse_blocks_response("no json here") is None
    assert export_llm.parse_blocks_response('{"blocks": "not a list"}') is None


# --- orchestrator ------------------------------------------------------------------

def test_preprocess_reflow_runs_offline_by_default():
    pages = [_page([_block("数字逻辑概\n论是基础课。")])]
    out = export_llm.preprocess(pages, {}, fmt="markdown")
    assert out[0]["blocks"][0]["text"] == "数字逻辑概论是基础课。"
    # raw mirrors the reflowed text (block_source prefers it)
    assert out[0]["blocks"][0]["raw"] == "数字逻辑概论是基础课。"
    # input never mutated
    assert pages[0]["blocks"][0]["text"] == "数字逻辑概\n论是基础课。"


def test_preprocess_both_off_returns_untouched():
    pages = [_page([_block("数字逻辑概\n论是基础课。")])]
    out = export_llm.preprocess(pages, {"export_reflow": "false",
                                        "export_llm": "false"})
    assert out is pages
    assert out[0]["blocks"][0]["text"] == "数字逻辑概\n论是基础课。"


def test_preprocess_no_reflow_flag_keeps_line_split():
    pages = [_page([_block("数字逻辑概\n论是基础课。")])]
    out = export_llm.preprocess(pages, {}, reflow=False, enable_llm=False)
    assert out[0]["blocks"][0]["text"] == "数字逻辑概\n论是基础课。"


class _FakeClient:
    """Test double matching ExportLlmClient's chat_json interface."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat_json(self, system, user, max_tokens):
        self.calls.append({"system": system, "user": user,
                           "max_tokens": max_tokens})
        return self.response


def _llm_json(*items):
    return json.dumps({"blocks": list(items)}, ensure_ascii=False)


def test_preprocess_llm_fixes_eligible_blocks():
    pages = [_page([
        _block("电压\t逻辑\n3.5 V\t1", kind="table"),
        _block("普通段落，不触发。", conf=0.99),
        _block("低置信的段落", conf=0.4),
    ])]
    fake = _FakeClient(_llm_json(
        {"n": 0, "ok": True,
         "text": "| 电压 | 逻辑 |\n|---|---|\n| 3.5 V | 1 |"},
        {"n": 1, "ok": True, "text": "低置信的段落（已校对）"}))
    out = export_llm.preprocess(pages, {}, fmt="markdown", enable_llm=True,
                                client=fake)
    md = export.pages_to_markdown(out)
    assert "| 电压 | 逻辑 |" in md
    # ineligible blocks are never sent (only 2 of the 3 blocks are offered)
    assert "普通段落" not in fake.calls[0]["user"]
    # accepted rewrite lands in the llm tag; the original text stays
    assert out[0]["blocks"][2]["llm"]["text"] == "低置信的段落（已校对）"
    assert out[0]["blocks"][2]["text"] == "低置信的段落"
    assert len(fake.calls) == 1


def test_preprocess_llm_guard_rejects_hallucination():
    pages = [_page([_block("低置信 alpha beta gamma 123", conf=0.4)])]
    fake = _FakeClient(_llm_json(
        {"n": 0, "ok": True, "text": "完全无关的内容"}))
    out = export_llm.preprocess(pages, {}, fmt="markdown", enable_llm=True,
                                client=fake)
    assert "llm" not in out[0]["blocks"][0]
    assert export.pages_to_markdown(out).count("低置信") == 1


def test_preprocess_llm_invalid_json_falls_back():
    pages = [_page([_block("电压\t逻辑\n3.5 V\t1", kind="table")])]
    fake = _FakeClient("not json at all")
    out = export_llm.preprocess(pages, {}, fmt="markdown", enable_llm=True,
                                client=fake)
    assert "llm" not in out[0]["blocks"][0]
    # tab text still exports as a (padded) table
    assert "| 电压 | 逻辑 |" in export.pages_to_markdown(out)


def test_preprocess_llm_skipped_without_api_key():
    pages = [_page([_block("电压\t逻辑\n3.5 V\t1", kind="table")])]
    out = export_llm.preprocess(pages, {"export_llm": "true"},
                                fmt="markdown", enable_llm=True)
    # no api_key in cfg and no injected client: the pass is skipped, P0 kept
    assert "llm" not in out[0]["blocks"][0]
    assert out[0]["blocks"][0]["text"] == "电压\t逻辑\n3.5 V\t1"


def test_preprocess_llm_cache_roundtrip(tmp_path):
    cache_path = tmp_path / "export_llm_cache.json"
    pages = [_page([_block("电压\t逻辑\n3.5 V\t1", kind="table")])]
    fake = _FakeClient(_llm_json(
        {"n": 0, "ok": True,
         "text": "| 电压 | 逻辑 |\n|---|---|\n| 3.5 V | 1 |"}))
    export_llm.preprocess(pages, {}, fmt="markdown", enable_llm=True,
                          client=fake, cache_path=cache_path)
    assert cache_path.exists()
    # second run: served from cache, no client call
    fake2 = _FakeClient(None)
    pages2 = [_page([_block("电压\t逻辑\n3.5 V\t1", kind="table")])]
    out2 = export_llm.preprocess(pages2, {}, fmt="markdown", enable_llm=True,
                                 client=fake2, cache_path=cache_path)
    assert len(fake2.calls) == 0
    assert out2[0]["blocks"][0]["llm"]["text"].startswith("| 电压 |")


def test_preprocess_llm_reflow_runs_before_llm():
    # the reflow rewrites text first; the LLM sees the reflowed content
    pages = [_page([_block("数字逻辑概\n论是基础课。", conf=0.4)])]
    fake = _FakeClient(_llm_json({"n": 0, "ok": True,
                                  "text": "数字逻辑概论是基础课。（校对）"}))
    out = export_llm.preprocess(pages, {}, fmt="markdown", enable_llm=True,
                                client=fake)
    sent = fake.calls[0]["user"]
    assert "数字逻辑概论是基础课。\n<<<" in sent or \
        "数字逻辑概论是基础课。" in sent.split("<<<BLOCK 0>>>", 1)[1]


# --- regression: the llm tag never reaches the text layer ----------------------------

def test_llm_tag_does_not_leak_into_hocr():
    blocks = [_block("内容", llm={"text": "内容", "model": "x", "prompt_v": 1})]
    hocr = blocks_to_hocr(1000, 1400, blocks, dpi=300)
    assert "llm" not in hocr
    assert "内容" in hocr


# --- document-level heading sizing (P0.5) --------------------------------------------

def test_heading_numbering_depth_forms():
    from backend.export import heading_numbering_depth
    assert heading_numbering_depth("1. 数字逻辑概论") == 1
    assert heading_numbering_depth("1.2 二进制") == 2
    assert heading_numbering_depth("1.2.3 编码") == 3
    assert heading_numbering_depth("第一章 绪论") == 1
    assert heading_numbering_depth("第二节 细则") == 2
    assert heading_numbering_depth("一、总则") == 1
    assert heading_numbering_depth("（一）细则") == 2
    assert heading_numbering_depth("Part II Implementation") == 1
    assert heading_numbering_depth("Appendix A") == 1
    assert heading_numbering_depth("I. 前言") == 1
    assert heading_numbering_depth("无编号标题") is None
    assert heading_numbering_depth("") is None


def test_heading_level_extended_forms():
    assert export.heading_level("第一章 绪论") == 1
    assert export.heading_level("（一）细则") == 2
    assert export.heading_level("无编号标题") == 2  # fallback preserved


def test_block_heading_level_prefers_llm_level():
    assert export.block_heading_level({"llm_level": 3}, "1. x") == 3
    assert export.block_heading_level({}, "1.2 x") == 2
    assert export.block_heading_level({"llm_level": 9}, "x") == 2  # out of range: ignored
    assert export.block_heading_level({"llm_level": "2"}, "x") == 2  # not int: fallback


def test_heading_branches_use_llm_level():
    block = {"kind": "heading", "text": "概述", "llm_level": 3}
    assert export.block_to_markdown(block).startswith("### 概述")
    assert "\\subsubsection{概述}" in export.block_to_latex(block)


def test_assign_heading_levels_by_size():
    pages = [_page([
        _block("数字逻辑概论", kind="title", bbox=[100, 60, 900, 120]),   # 60px
        _block("正文一行", kind="text", bbox=[100, 200, 900, 230]),        # 30px
        _block("编码方法", kind="heading", bbox=[100, 300, 900, 350]),     # 50px
        _block("布尔代数", kind="heading", bbox=[100, 400, 900, 430]),     # 30px
    ])]
    assert export_llm.assign_heading_levels(pages) == 3
    blocks = pages[0]["blocks"]
    assert blocks[0]["llm_level"] == 1
    assert blocks[2]["llm_level"] == 2
    assert blocks[3]["llm_level"] == 3  # body-sized heading: one level deeper


def test_assign_numbering_wins_over_size():
    pages = [_page([
        _block("概述", kind="heading", bbox=[100, 60, 900, 120]),          # 60px big
        _block("1.2 具体方法", kind="heading", bbox=[100, 300, 900, 330]),  # 30px small
        _block("正文", kind="text", bbox=[100, 400, 900, 430]),
    ])]
    export_llm.assign_heading_levels(pages)
    blocks = pages[0]["blocks"]
    assert blocks[0]["llm_level"] == 1  # unnumbered → size
    assert blocks[1]["llm_level"] == 2  # numbered → numbering, despite the small size


def test_assign_first_heading_is_title():
    pages = [_page([
        _block("引言", kind="heading", bbox=[100, 300, 900, 330]),
        _block("正文", kind="text", bbox=[100, 400, 900, 430]),
    ])]
    export_llm.assign_heading_levels(pages)
    assert pages[0]["blocks"][0]["llm_level"] == 1


def test_assign_numbered_first_heading_keeps_depth():
    pages = [_page([
        _block("1.1 背景", kind="heading", bbox=[100, 300, 900, 330]),
    ])]
    export_llm.assign_heading_levels(pages)
    assert pages[0]["blocks"][0]["llm_level"] == 2


def test_assign_no_headings_noop():
    pages = [_page([_block("正文而已", kind="text")])]
    assert export_llm.assign_heading_levels(pages) == 0
    assert "llm_level" not in pages[0]["blocks"][0]


def test_assign_uses_per_line_height_before_reflow():
    # a 2-line heading (60px bbox) and a 1-line heading (30px) have the SAME
    # per-line size → same level; computing after the reflow (lines joined)
    # would wrongly put the second heading a level deeper
    pages = [_page([
        _block("概\n述", kind="heading", bbox=[100, 60, 900, 120],
               lines=["概", "述"]),
        _block("布尔代数", kind="heading", bbox=[100, 300, 900, 330]),
        _block("正文", kind="text", bbox=[100, 400, 900, 430]),
    ])]
    out = export_llm.preprocess(pages, {}, fmt="markdown")  # reflow on
    blocks = out[0]["blocks"]
    assert blocks[0]["llm_level"] == 1
    assert blocks[1]["llm_level"] == 1


def test_preprocess_assigns_levels_end_to_end():
    pages = [_page([
        _block("数字逻辑概论", kind="title", bbox=[100, 60, 900, 120]),
        _block("正文一行", kind="text", bbox=[100, 200, 900, 230]),
        _block("编码方法", kind="heading", bbox=[100, 300, 900, 350]),
    ])]
    out = export_llm.preprocess(pages, {}, fmt="markdown")
    md = export.pages_to_markdown(out)
    assert md.startswith("# 数字逻辑概论")
    assert "\n## 编码方法" in md


def test_preprocess_no_reflow_skips_level_assignment():
    pages = [_page([_block("引言", kind="heading", bbox=[100, 300, 900, 330])])]
    out = export_llm.preprocess(pages, {}, reflow=False, enable_llm=False)
    assert "llm_level" not in out[0]["blocks"][0]


# --- LLM outline refinement (B + TOC context) -------------------------------------

def test_detect_toc_entries_parses_dot_leaders():
    pages = [_page([_block("第一章 绪论........1\n第二章 基础……15\n普通段落没有页码",
                           kind="text")])]
    entries = export_llm.detect_toc_entries(pages)
    assert [e["title"] for e in entries] == ["第一章 绪论", "第二章 基础"]
    assert [e["level"] for e in entries] == [1, 1]


def test_detect_toc_entries_depth_from_numbering_and_indent():
    pages = [_page([_block("1.1 二进制....3\n  1.1.1 编码....5\n第一章 概论....1",
                           kind="text")])]
    entries = export_llm.detect_toc_entries(pages)
    assert [e["level"] for e in entries] == [2, 3, 1]


def test_detect_toc_entries_indent_depth_when_unnumbered():
    pages = [_page([_block("背景说明....1\n　　子项说明....2", kind="text")])]
    entries = export_llm.detect_toc_entries(pages)
    assert entries[0]["level"] == 1   # no indent
    assert entries[1]["level"] == 2   # 2 fullwidth spaces → 1 + 2//2 = 2


def test_detect_toc_entries_respects_max_pages():
    toc_page = _page([_block("第一章 绪论....1", kind="text")], page_index=0)
    plain_page = _page([_block("正文而已", kind="text")], page_index=1)
    assert len(export_llm.detect_toc_entries([toc_page, plain_page], max_pages=1)) == 1


def test_outline_prompt_contains_headings_and_toc():
    prompt = export_llm._outline_prompt(
        [{"n": 0, "level": 1, "text": "概述"},
         {"n": 1, "level": 2, "text": "1.2 方法"}],
        [{"title": "第一章 绪论", "level": 1}])
    assert "0|L1|概述" in prompt
    assert "1|L2|1.2 方法" in prompt
    assert "table of contents" in prompt
    assert "L1|第一章 绪论" in prompt


def test_outline_prompt_without_toc():
    prompt = export_llm._outline_prompt([{"n": 0, "level": 2, "text": "x"}], [])
    assert "table of contents" not in prompt


def test_parse_outline_response():
    raw = '```json\n{"headings":[{"n":0,"level":1},{"n":1,"level":2}]}\n```'
    assert export_llm.parse_outline_response(raw) == \
        [{"n": 0, "level": 1}, {"n": 1, "level": 2}]
    assert export_llm.parse_outline_response("garbage") is None
    assert export_llm.parse_outline_response('{"headings": "x"}') is None


def test_refine_outline_applies_levels():
    pages = [_page([
        _block("概述", kind="heading"),
        _block("1.2 具体方法", kind="heading"),
    ])]
    for b in pages[0]["blocks"]:
        b["llm_level"] = 2
    fake = _FakeClient('{"headings":[{"n":0,"level":1},{"n":1,"level":2}]}')
    applied = export_llm._refine_outline(pages, {}, fake, [])
    assert applied == 2
    assert pages[0]["blocks"][0]["llm_level"] == 1
    assert pages[0]["blocks"][1]["llm_level"] == 2


def test_refine_outline_guards():
    pages = [_page([
        _block("概述", kind="heading"),
        _block("细节", kind="heading"),
        _block("更多", kind="heading"),
    ])]
    for b in pages[0]["blocks"]:
        b["llm_level"] = 1
    # n=1 jumps 1 → 3 (rejected), n=2 out of range (rejected)
    fake = _FakeClient('{"headings":[{"n":0,"level":1},{"n":1,"level":3},'
                       '{"n":2,"level":9}]}')
    applied = export_llm._refine_outline(pages, {}, fake, [])
    assert applied == 1
    blocks = pages[0]["blocks"]
    assert blocks[0]["llm_level"] == 1
    assert blocks[1]["llm_level"] == 1  # jump rejected, deterministic level kept
    assert blocks[2]["llm_level"] == 1  # out of range rejected


def test_refine_outline_garbage_falls_back():
    pages = [_page([_block("概述", kind="heading"),
                    _block("1.2 方法", kind="heading")])]
    for b in pages[0]["blocks"]:
        b["llm_level"] = 2
    fake = _FakeClient("no json")
    applied = export_llm._refine_outline(pages, {}, fake, [])
    assert applied == 0
    assert all(b["llm_level"] == 2 for b in pages[0]["blocks"])


class _MultiClient:
    """Dispatches on the system prompt (block fix-up vs outline)."""

    def __init__(self, by_marker):
        self.by_marker = by_marker
        self.calls = []

    def chat_json(self, system, user, max_tokens):
        self.calls.append({"system": system, "user": user})
        for marker, resp in self.by_marker.items():
            if marker in system:
                return resp
        return None


def test_preprocess_llm_runs_blocks_then_outline():
    pages = [_page([
        _block("概述与设计背景", kind="heading", conf=0.4),
        _block("1.2 具体方法", kind="heading"),
    ])]
    for b in pages[0]["blocks"]:
        b["llm_level"] = 1
    fake = _MultiClient({
        "repair OCR-export text blocks": _llm_json(
            {"n": 0, "ok": True, "text": "概述与设计背景（校对）"}),
        "heading hierarchy": '{"headings":[{"n":0,"level":1},{"n":1,"level":2}]}',
    })
    out = export_llm.preprocess(pages, {}, fmt="markdown", enable_llm=True,
                                client=fake)
    assert len(fake.calls) == 2
    # block fix-up landed in the llm tag (only the low-conf block is offered)
    assert out[0]["blocks"][0]["llm"]["text"] == "概述与设计背景（校对）"
    # outline refinement overwrote llm_level
    assert out[0]["blocks"][0]["llm_level"] == 1
    assert out[0]["blocks"][1]["llm_level"] == 2


def test_preprocess_blocks_only_skips_outline():
    pages = [_page([
        _block("概述与设计背景", kind="heading", conf=0.4),
        _block("1.2 具体方法", kind="heading"),
    ])]
    for b in pages[0]["blocks"]:
        b["llm_level"] = 1
    fake = _MultiClient({
        "repair OCR-export text blocks": _llm_json(
            {"n": 0, "ok": True, "text": "概述与设计背景（校对）"}),
    })
    out = export_llm.preprocess(pages, {}, fmt="markdown",
                                enable_blocks=True, client=fake)
    assert len(fake.calls) == 1  # only the block fix-up ran
    assert "repair" in fake.calls[0]["system"]
    assert out[0]["blocks"][0]["llm"]["text"] == "概述与设计背景（校对）"
    # outline skipped: the deterministic sizing stands (1.2 → level 2 by
    # numbering), no LLM refinement on top
    assert out[0]["blocks"][1]["llm_level"] == 2


def test_preprocess_outline_only_skips_blocks():
    pages = [_page([
        _block("概述与设计背景", kind="heading", conf=0.4),
        _block("1.2 具体方法", kind="heading"),
    ])]
    for b in pages[0]["blocks"]:
        b["llm_level"] = 1
    fake = _MultiClient({
        "heading hierarchy": '{"headings":[{"n":0,"level":1},{"n":1,"level":2}]}',
    })
    out = export_llm.preprocess(pages, {}, fmt="markdown",
                                enable_outline=True, client=fake)
    assert len(fake.calls) == 1  # only the outline ran
    assert "hierarchy" in fake.calls[0]["system"]
    # no llm tag (block fix-up skipped), levels refined
    assert "llm" not in out[0]["blocks"][0]
    assert out[0]["blocks"][0]["llm_level"] == 1
    assert out[0]["blocks"][1]["llm_level"] == 2


def test_preprocess_outline_only_no_reflow_assigns_base_levels():
    pages = [_page([
        _block("引言", kind="heading", bbox=[100, 300, 900, 330]),
        _block("正文", kind="text", bbox=[100, 400, 900, 430]),
    ])]
    fake = _MultiClient({})
    out = export_llm.preprocess(pages, {}, fmt="markdown", reflow=False,
                                enable_outline=True, client=fake)
    assert out[0]["blocks"][0]["llm_level"] == 1  # base levels before outline


def test_resolve_settings_granular_keys():
    # granular keys win over the legacy master
    s = export_llm.resolve_settings({"export_llm": "true",
                                     "export_llm_blocks": "false"})
    assert s["blocks"] is False and s["outline"] is True
    # legacy master defaults both when granular keys are absent
    s2 = export_llm.resolve_settings({"export_llm": "true"})
    assert s2["blocks"] is True and s2["outline"] is True
    # everything off by default
    s3 = export_llm.resolve_settings({})
    assert s3["blocks"] is False and s3["outline"] is False


def test_export_route_llm_flags_granular(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import ocr_service
    from backend.main import app
    monkeypatch.setattr(ocr_service, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(ocr_service, "UPLOAD_DIR", tmp_path / "uploads")
    blocks = [
        {"kind": "text", "bbox": [0, 0, 100, 20],
         "text": "数字逻辑概\n论是基础课。", "lines": []},
    ]
    _seed_job(tmp_path, blocks=blocks)
    try:
        with TestClient(app) as tc:
            # llm_blocks=1 alone: reflow ran, no crash, no outline client call
            r = tc.get("/api/export/export-1.md?llm_blocks=1")
            assert r.status_code == 200
            # explicit off overrides the legacy master
            r2 = tc.get("/api/export/export-1.md?llm=1&llm_blocks=0&llm_outline=0")
            assert r2.status_code == 200
            # plain export (no LLM) still works
            r3 = tc.get("/api/export/export-1.md")
            assert r3.status_code == 200
    finally:
        ocr_service._JOBS.pop("export-1", None)


def test_preprocess_llm_no_reflow_still_assigns_base_levels():
    pages = [_page([
        _block("引言", kind="heading", bbox=[100, 300, 900, 330]),
        _block("正文", kind="text", bbox=[100, 400, 900, 430]),
    ])]
    fake = _MultiClient({})
    out = export_llm.preprocess(pages, {}, fmt="markdown", reflow=False,
                                enable_llm=True, client=fake)
    assert out[0]["blocks"][0]["llm_level"] == 1  # first heading = title


def test_preprocess_llm_outline_with_toc_context():
    toc_page = _page([_block("第一章 绪论........1\n第二章 方法........9",
                             kind="text")], page_index=0)
    body_page = _page([
        _block("第一章 绪论", kind="heading", bbox=[100, 60, 900, 120]),
        _block("正文", kind="text", bbox=[100, 200, 900, 230]),
        _block("第二章 方法", kind="heading", bbox=[100, 300, 900, 360]),
    ], page_index=1)
    fake = _MultiClient({"heading hierarchy":
                         '{"headings":[{"n":0,"level":1},{"n":1,"level":1}]}'})
    pages = [toc_page, body_page]
    out = export_llm.preprocess(pages, {}, fmt="markdown", enable_llm=True,
                                client=fake)
    outline_call = fake.calls[-1]
    assert "table of contents" in outline_call["user"]
    assert "第一章 绪论" in outline_call["user"]
    assert out[1]["blocks"][0]["llm_level"] == 1
    assert out[1]["blocks"][2]["llm_level"] == 1


# --- progress callback -----------------------------------------------------------------

def test_preprocess_progress_events_reflow_and_llm():
    pages = [_page([
        _block("电压\t逻辑\n3.5 V\t1", kind="table"),
        _block("低置信的段落", conf=0.4),
    ])]
    events: list = []
    fake = _FakeClient(_llm_json({"n": 0, "ok": True,
                                  "text": "| 电压 | 逻辑 |\n|---|---|\n| 3.5 V | 1 |"}))
    export_llm.preprocess(pages, {}, fmt="markdown", enable_llm=True,
                          client=fake, progress=events.append)
    phases = [ev["phase"] for ev in events]
    assert "reflow" in phases
    assert "llm" in phases
    llm_events = [ev for ev in events if ev["phase"] == "llm"]
    assert llm_events[0]["total"] == 2
    assert llm_events[-1]["done"] == 1  # one accepted rewrite
    assert llm_events[-1]["done"] <= llm_events[-1]["total"]


def test_preprocess_progress_reflow_only():
    pages = [_page([_block("数字逻辑概\n论是基础课。")])]
    events: list = []
    export_llm.preprocess(pages, {}, fmt="markdown", progress=events.append)
    assert events == [{"phase": "reflow", "done": 1, "total": 1}]


def test_preprocess_progress_callback_failure_never_breaks():
    def bad(_ev):
        raise RuntimeError("progress sink down")
    pages = [_page([_block("数字逻辑概\n论是基础课。")])]
    out = export_llm.preprocess(pages, {}, fmt="markdown", progress=bad)
    assert out[0]["blocks"][0]["text"] == "数字逻辑概论是基础课。"


# --- SSE export endpoint (backend.main) -------------------------------------------------

def _seed_job(tmp_path, job_id="export-1", blocks=None):
    """A done job in the registry with one real sidecar page."""
    from backend import ocr_service
    hocr_dir = tmp_path / "work" / job_id / "hocr"
    hocr_dir.mkdir(parents=True, exist_ok=True)
    page = {"page_index": 0, "width": 1000, "height": 1400,
            "blocks": blocks if blocks is not None else [
                {"kind": "text", "bbox": [0, 0, 100, 20],
                 "text": "数字逻辑概\n论是基础课。", "lines": []}]}
    (hocr_dir / "000001_ocr_hocr.blocks.json").write_text(
        json.dumps({"page": page, "dpi": 300.0}, ensure_ascii=False),
        encoding="utf-8")
    job = {"job_id": job_id, "current": 1, "status": "done",
           "filename": f"{job_id}.pdf", "hocr_dir": str(hocr_dir),
           "previews_dir": str(tmp_path / "work" / job_id / "previews"),
           "pdf_path": str(tmp_path / "uploads" / f"{job_id}.pdf"),
           "num_pages": 1, "pages_done": 1, "error": "",
           "embedded_path": "", "created_at": "", "has_embedded": False}
    ocr_service._JOBS[job_id] = job
    return job


def _read_sse(text):
    """Parse an SSE body into (data_events, keepalive_comment_count)."""
    events, comments = [], 0
    for block in text.split("\n\n"):
        if block.startswith(":"):
            comments += 1
        elif block.startswith("data: "):
            events.append(json.loads(block[len("data: "):]))
    return events, comments


def test_export_stream_progress_and_done(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import ocr_service
    from backend.main import app
    monkeypatch.setattr(ocr_service, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(ocr_service, "UPLOAD_DIR", tmp_path / "uploads")
    _seed_job(tmp_path)
    try:
        with TestClient(app) as tc:
            with tc.stream("GET", "/api/export/stream/export-1.md?llm=0") as r:
                assert r.status_code == 200
                body = "".join(chunk for chunk in r.iter_text())
    finally:
        ocr_service._JOBS.pop("export-1", None)
    events, _ = _read_sse(body)
    kinds = [ev["type"] for ev in events]
    assert "progress" in kinds and "done" in kinds
    done = events[-1]
    assert done["fmt"] == "markdown"
    assert "数字逻辑概论是基础课。" in done["text"]


def test_export_stream_validation_before_stream(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import ocr_service
    from backend.main import app
    monkeypatch.setattr(ocr_service, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(ocr_service, "UPLOAD_DIR", tmp_path / "uploads")
    _seed_job(tmp_path)
    try:
        with TestClient(app) as tc:
            r = tc.get("/api/export/stream/export-1.html?llm=0")
            assert r.status_code == 400
            r2 = tc.get("/api/export/stream/no-such-job.md?llm=0")
            assert r2.status_code == 404
    finally:
        ocr_service._JOBS.pop("export-1", None)


def test_export_stream_error_event_not_broken_connection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import ocr_service
    from backend.main import app
    monkeypatch.setattr(ocr_service, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(ocr_service, "UPLOAD_DIR", tmp_path / "uploads")
    _seed_job(tmp_path)
    try:
        with TestClient(app) as tc:
            with tc.stream("GET", "/api/export/stream/export-1.md?llm=0&reflow=1") as r:
                body = "".join(chunk for chunk in r.iter_text())
    finally:
        ocr_service._JOBS.pop("export-1", None)
    # a mid-stream failure would be an SSE error event; here the happy path
    # must NOT carry one
    events, _ = _read_sse(body)
    assert all(ev["type"] != "error" for ev in events)
