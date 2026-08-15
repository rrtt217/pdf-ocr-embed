"""Generic OpenAI adapter: model-agnostic JSON parsing + bbox coercion."""
from __future__ import annotations

from backend.sources.generic_openai_adapter import (GenericOpenAiAdapter,
                                                    _extract_json)


def test_extract_json_plain():
    assert _extract_json('{"blocks": []}') == {"blocks": []}


def test_extract_json_fenced():
    raw = """Sure! Here you go:
```json
{"blocks": []}
```
Done."""
    assert _extract_json(raw) == {"blocks": []}


def test_extract_json_prose_around():
    raw = 'Here is the result: {"blocks": []} hope it helps'
    assert _extract_json(raw) == {"blocks": []}


def test_extract_json_invalid():
    assert _extract_json("") is None
    assert _extract_json("not json at all") is None
    assert _extract_json("{bad json") is None


def test_coerce_bbox():
    c = GenericOpenAiAdapter._coerce_bbox
    assert c([1, 2, 3, 4]) == [1.0, 2.0, 3.0, 4.0]
    assert c((1, 2, 3, 4)) == [1.0, 2.0, 3.0, 4.0]
    assert c(None) is None and c([1, 2]) is None
    assert c('1,2,3,4') is None and c([1, 2, 3, 'x']) is None


def test_parse_json_blocks_remap_and_defaults():
    data = {
        "blocks": [
            {"kind": "TEXT", "bbox": [0, 0, 500, 250], "text": "hi"},
            {"bbox": [0, 500, 500, 1000]},          # missing kind/text
            {"kind": "image", "bbox": [10, 10, 20, 20]},
            {"kind": "heading", "bbox": [1, 2, 3], "text": "skipped"},
        ],
    }
    page = GenericOpenAiAdapter()._parse_json_blocks(data, 2000, 1000, 0)
    assert len(page.blocks) == 3
    b0, b1, b2 = page.blocks
    assert b0.kind == "text" and b0.text == "hi"
    assert b0.bbox == [0, 0, 1000, 250]          # x*2000/1000, y*1000/1000
    assert b1.kind == "text" and b1.text == ""
    assert b1.bbox == [0, 500, 1000, 1000]
    assert b2.kind == "image" and b2.text == ""


def test_parse_json_blocks_garbage_data():
    page = GenericOpenAiAdapter()._parse_json_blocks(None, 100, 100, 0)
    assert page.blocks == []
    page = GenericOpenAiAdapter()._parse_json_blocks({"blocks": "nope"},
                                                       100, 100, 0)
    assert page.blocks == []
