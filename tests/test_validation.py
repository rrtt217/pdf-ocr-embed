"""Feature #17 — post-embed validation: pure comparison math + API report."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from backend import ocr_service, pdf_processing, validation
from backend.main import app
from backend.models import OcrBlock, OcrPage, dict_to_page
from pdf_fixtures import pdf_bytes as make_pdf, write_pdf


# ---------------------------------------------------------------------------
# Pure comparison math
# ---------------------------------------------------------------------------

def _page(blocks, index=0):
    return OcrPage(page_index=index, width=100, height=100, blocks=blocks)


def _text_block(text, conf=None):
    return OcrBlock(kind="text", bbox=[1, 1, 50, 10], text=text, conf=conf)


def test_normalize_text_handles_case_punct_whitespace():
    assert validation.normalize_text("Hello,\nworld!") == "hello world"
    assert validation.normalize_text("  多  行  text  ") == "多 行 text"
    assert validation.normalize_text("") == ""


def test_tokenize_splits_cjk_per_glyph_and_keeps_latin_words():
    assert validation.tokenize("hello world") == ["hello", "world"]
    assert validation.tokenize("你好世界") == ["你", "好", "世", "界"]
    assert validation.tokenize("abc你好def") == ["abc", "你", "好", "def"]


def test_token_coverage_exact_partial_and_reordered():
    assert validation.token_coverage("hello world", "world hello") == 1.0
    assert validation.token_coverage("hello world", "hello") == 0.5
    assert validation.token_coverage("hello world", "completely different words") == 0.0
    assert validation.token_coverage("", "anything") == 1.0  # vacuous
    assert validation.token_coverage("你好世界", "你好") == 0.5


def test_coverage_is_tolerant_to_wrapping_and_garbled():
    # Same words, different wrapping/punctuation -> essentially 1.0.
    assert validation.coverage(
        "The quick brown fox jumps over the lazy dog",
        "The quick\nbrown fox, jumps - over the lazy dog!") > 0.95
    # Reversed / garbled text -> near zero on both axes.
    assert validation.coverage("abc def ghi", "ihg fed cba") < 0.1
    # Empty source -> 1.0; empty embedded against real source -> 0.0.
    assert validation.coverage("", "") == 1.0
    assert validation.coverage("some real text", "") == 0.0


def test_text_metrics_counts_chars_and_words():
    m = validation.text_metrics("Hello, 世界 world!")
    assert m["chars"] == 12  # hello(5) + 世界(2 glyphs) + world(5)
    assert m["words"] == 4   # hello / 世 / 界 / world


def test_conf_stats_normalizes_units_and_buckets():
    blocks = [_text_block("a", conf=0.9), _text_block("b", conf=70),  # 0.7
              _text_block("c", conf=None), _text_block("d", conf=0.1)]
    s = validation.conf_stats(blocks)
    assert s["count"] == 3
    assert s["min"] == 0.1 and s["max"] == 0.9
    assert abs(s["avg"] - (0.9 + 0.7 + 0.1) / 3) < 0.001  # rounded to 4 dp
    assert s["buckets"] == {"low": 1, "medium": 1, "high": 1}
    assert s["block_count"] == 4


def test_page_source_text_matches_what_gets_embedded():
    page = OcrPage(page_index=0, width=100, height=100, blocks=[
        OcrBlock(kind="text", bbox=[1, 1, 5, 5], text="alpha"),
        # image kind embeds its caption (not the empty text)
        OcrBlock(kind="image", bbox=[1, 1, 5, 5], text="", caption="fig"),
        OcrBlock(kind="table", bbox=[1, 1, 5, 5], text="beta"),
        OcrBlock(kind="text", bbox=[1, 1, 5, 5], text="   "),
    ])
    assert validation.page_source_text(page) == "alpha\nfig\nbeta"


def test_page_report_flags():
    good = validation.page_report(_page([_text_block("faithful text", 0.9)]),
                                  "faithful text")
    assert good["coverage"] == 1.0
    assert good["flags"]["low_coverage"] is False
    assert good["flags"]["empty_source"] is False

    bad = validation.page_report(_page([_text_block("source words here")]),
                                 "utterly different garbage")
    assert bad["flags"]["low_coverage"] is True
    assert bad["coverage"] < 0.1

    empty = validation.page_report(_page([]), "")
    assert empty["flags"]["empty_source"] is True
    assert empty["flags"]["empty_embedded"] is True


def test_summarize_report_aggregates():
    reports = [
        validation.page_report(_page([_text_block("good text")], 0), "good text"),
        validation.page_report(_page([_text_block("second page")], 1),
                               "junk junk junk"),
        validation.page_report(_page([], 2), ""),
    ]
    s = validation.summarize_report(reports)
    assert s["pages"] == 3
    assert s["avg_coverage"] < 1.0 and s["avg_coverage"] > 0.0
    assert s["low_coverage_pages"] == [1]
    assert s["empty_source_pages"] == [2]
    assert s["threshold"] == validation.LOW_COVERAGE_THRESHOLD
    assert s["total_blocks"] == 2


# ---------------------------------------------------------------------------
# PDF-backed build_report
# ---------------------------------------------------------------------------

def _make_text_pdf(path: Path, text: str) -> Path:
    return write_pdf(path, text=text, width=400, height=300)


def test_build_report_missing_file_returns_error():
    r = validation.build_report("/nonexistent/nope.pdf", [])
    assert r == {"ok": False, "error": validation._error_report("x")["error"]} or r["ok"] is False
    assert "not found" in r["error"]


def test_build_report_high_coverage_on_faithful_embed():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_text_pdf(td / "src.pdf", "the faithful red fox")
        page = OcrPage(page_index=0, width=400, height=300, blocks=[
            OcrBlock(kind="text", bbox=[50, 40, 350, 60], text="the faithful red fox")])
        r = validation.build_report(str(src), [page])
        assert r["ok"] is True
        assert r["summary"]["pages"] == 1
        assert r["pages"][0]["coverage"] > 0.9
        assert r["pages"][0]["flags"]["low_coverage"] is False


def test_build_report_low_coverage_on_mismatch():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_text_pdf(td / "src.pdf", "completely unrelated words")
        page = OcrPage(page_index=0, width=400, height=300, blocks=[
            OcrBlock(kind="text", bbox=[50, 40, 350, 60], text="the faithful red fox")])
        r = validation.build_report(str(src), [page])
        assert r["ok"] is True
        assert r["pages"][0]["flags"]["low_coverage"] is True


def test_build_report_corrupt_file_returns_error():
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.pdf"
        bad.write_bytes(b"%PDF-1.7\nthis is not a real pdf at all 12345")
        r = validation.build_report(str(bad), [_page([])])
        assert r["ok"] is False and "error" in r


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

def _stub_lifespan(monkeypatch):
    monkeypatch.setattr(ocr_service, "restore_jobs", lambda: 0)
    from backend import cleanup as cleanup_mod
    monkeypatch.setattr(cleanup_mod, "start_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "stop_background_cleanup", lambda: None)


def test_validation_api_endpoint(monkeypatch, tmp_path):
    _stub_lifespan(monkeypatch)
    monkeypatch.setattr(ocr_service, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(ocr_service, "UPLOAD_DIR", tmp_path / "uploads")

    pdf_bytes = make_pdf(text="api validation roundtrip", width=400, height=300)

    job = ocr_service.create_job("doc.pdf", pdf_bytes)
    p0 = {"page_index": 0, "width": 400, "height": 300, "blocks": [
        {"kind": "text", "bbox": [50, 40, 350, 60],
         "text": "api validation roundtrip", "caption": "", "conf": 0.95}]}
    # A block sidecar for the page (as the plugin engine leaves it) —
    # update_page edits pages that already have an OCR result.
    import json as _json
    from backend.ocrmypad import parser as parser_mod
    sidecar = (ocr_service._job_dir(job["job_id"]) / "hocr" /
               "000001_ocr_hocr.blocks.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    parsed = parser_mod.Page(
        page_index=0, width=400, height=300,
        blocks=[parser_mod.Block(kind="text", bbox=[50, 40, 350, 60],
                                 text="seed", lines=["seed"])])
    sidecar.write_text(
        _json.dumps({"page": parsed.to_dict(), "dpi": 300.0},
                    ensure_ascii=False),
        encoding="utf-8")
    ocr_service.update_page(job["job_id"], 0, p0)
    ocr_service._set(job["job_id"], num_pages=1)

    # A sidecar + hOCR for the page (as the plugin engine leaves them), then a
    # real finalize output the validation endpoint can extract text from.
    out_path = tmp_path / "output" / "embedded.pdf"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(out_path, text="api validation roundtrip", width=400, height=300)
    ocr_service._set(job["job_id"], embedded_path=str(out_path))

    with TestClient(app) as client:
        resp = client.get(f"/api/validation/{job['job_id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["summary"]["pages"] == 1
    assert data["pages"][0]["coverage"] > 0.9

    # No embedded output -> 404.
    other = ocr_service.create_job("other.pdf", pdf_bytes)
    with TestClient(app) as client:
        resp = client.get(f"/api/validation/{other['job_id']}")
    assert resp.status_code == 404
