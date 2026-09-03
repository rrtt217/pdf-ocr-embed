"""API guards: page-update bounds, embed 404, retry status guard."""
from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import ocr_service
from backend.main import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Leaked _JOBS entries must not reach other test files: a stale job
    without the full key set breaks /api/jobs for subsequent tests."""
    yield
    ocr_service._JOBS.pop("guard-1", None)
    ocr_service._STREAMS.pop("guard-1", None)


def _mk_job(tmp_path, monkeypatch, status="stopped", num_pages=2):
    """A real job in the registry with tmp dirs (no filesystem side effects)."""
    monkeypatch.setattr(ocr_service, 'WORK_DIR', tmp_path / 'work')
    monkeypatch.setattr(ocr_service, 'UPLOAD_DIR', tmp_path / 'uploads')
    job = {"job_id": "guard-1", "current": 0,
           "status": status,
           "filename": "guard-1.pdf",
           "hocr_dir": str(tmp_path / "work" / "guard-1" / "hocr"),
           "previews_dir": str(tmp_path / "work" / "guard-1" / "previews"),
           "pdf_path": str(tmp_path / "uploads" / "guard-1.pdf"),
           "num_pages": num_pages, "pages_done": 0, "error": "",
           "embedded_path": "", "created_at": ""}
    ocr_service._JOBS[job["job_id"]] = job
    ocr_service._STREAMS[job["job_id"]] = deque(maxlen=1000)
    return job


def _write_sidecar(job, page_no: int):
    """A block sidecar (as the plugin engine would leave) for one page."""
    import json
    from backend.ocrmypad import parser as parser_mod
    sidecar = (Path(job["hocr_dir"]) / f"{page_no:06d}_ocr_hocr.blocks.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    page = parser_mod.Page(
        page_index=page_no - 1, width=1000, height=2000,
        blocks=[parser_mod.Block(kind="text", bbox=[100, 100, 900, 200],
                                 text="sidecar", lines=["sidecar"])],
    )
    sidecar.write_text(
        json.dumps({"page": page.to_dict(), "dpi": 300.0}, ensure_ascii=False),
        encoding="utf-8")


# --- POST /api/pages/{job}/{i}: index bounds ---------------------------------

def test_update_page_rejects_missing_job(client):
    r = client.post("/api/pages/no-such-job/0", json={"page_index": 0})
    assert r.status_code == 404


def test_update_page_rejects_out_of_range_index(client, monkeypatch, tmp_path):
    _mk_job(tmp_path, monkeypatch, num_pages=2)
    r = client.post("/api/pages/guard-1/2", json={"page_index": 2})
    assert r.status_code == 400
    r = client.post("/api/pages/guard-1/50000000", json={})
    assert r.status_code == 400


def test_update_page_rejects_missing_sidecar(client, monkeypatch, tmp_path):
    _mk_job(tmp_path, monkeypatch, num_pages=2)
    # A page without a block sidecar has no OCR result to edit -> 400.
    r = client.post("/api/pages/guard-1/0",
                    json={"blocks": [{"kind": "text", "bbox": [0, 0, 1, 1],
                                      "text": "x"}]})
    assert r.status_code == 400


def test_update_page_accepts_valid_index(client, monkeypatch, tmp_path):
    job = _mk_job(tmp_path, monkeypatch, num_pages=2)
    _write_sidecar(job, 2)
    r = client.post("/api/pages/guard-1/1",
                    json={"blocks": [{"kind": "text", "bbox": [100, 100, 900, 200],
                                      "text": "edited"}]})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # The edit reached the sidecar.
    stored = json_sidecar_text(job, 2)
    assert "edited" in stored


def json_sidecar_text(job, page_no: int) -> str:
    return (Path(job["hocr_dir"]) /
            f"{page_no:06d}_ocr_hocr.blocks.json").read_text(encoding="utf-8")


# --- POST /api/embed/{job_id}: 404 for a missing job --------------------------

def test_embed_missing_job_returns_404(client):
    # EmbedModel requires job_id; a syntactically valid body with an unknown
    # id must map to 404 like every other job-scoped route.
    r = client.post("/api/embed/no-such-job",
                    json={"job_id": "no-such-job", "pages": None,
                          "embed_font": None})
    assert r.status_code == 404


# --- POST /api/ocr/retry/{job_id}: status guard --------------------------------

def test_retry_refuses_running_job(monkeypatch, tmp_path):
    _mk_job(tmp_path, monkeypatch, status="running")
    started = []

    monkeypatch.setattr(ocr_service.threading, "Thread",
                        lambda **kw: started.append(kw) or
                        type("T", (), {"start": lambda self: None})())
    assert ocr_service.retry_job("guard-1") is False
    assert not started  # no second run_ocr was scheduled


def test_retry_refuses_already_retrying_job(monkeypatch, tmp_path):
    _mk_job(tmp_path, monkeypatch, status="retrying")
    assert ocr_service.retry_job("guard-1") is False


def test_retry_missing_job_returns_false():
    assert ocr_service.retry_job("no-such-job") is False


def test_retry_stopped_job_still_schedules(monkeypatch, tmp_path):
    _mk_job(tmp_path, monkeypatch, status="stopped")
    # Source PDF must exist for the guard to pass.
    pdf = Path(ocr_service._JOBS["guard-1"]["pdf_path"])
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-fake")
    # Stub the worker thread: run_ocr would call the real ocrmypdf pipeline
    # on the fake PDF; we only assert that scheduling happened.
    started = []
    monkeypatch.setattr(ocr_service.threading, "Thread",
                        lambda **kw: started.append(kw) or
                        type("T", (), {"start": lambda self: None})())
    assert ocr_service.retry_job("guard-1") is True
    assert started


# --- POST /api/ocr/retry/{job_id}: precise 409 reasons -----------------------

def test_retry_route_running_job_reports_running_not_missing_file(client,
                                                                  monkeypatch,
                                                                  tmp_path):
    """A running job was previously refused with the misleading "missing file"
    message even though its source PDF is on disk."""
    _mk_job(tmp_path, monkeypatch, status="running")
    r = client.post("/api/ocr/retry/guard-1", data={})
    assert r.status_code == 409
    assert "still running" in r.json()["detail"]
    assert "missing" not in r.json()["detail"]


def test_retry_route_missing_pdf_still_reports_missing_file(client,
                                                            monkeypatch,
                                                            tmp_path):
    _mk_job(tmp_path, monkeypatch, status="stopped")
    # Source PDF does not exist -> the "missing file" refusal remains accurate.
    r = client.post("/api/ocr/retry/guard-1", data={})
    assert r.status_code == 409
    assert "missing source PDF" in r.json()["detail"]
