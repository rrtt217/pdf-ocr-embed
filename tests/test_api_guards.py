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
    job = {"id": "guard-1", "current": 0, "pages": [],
           "status": status, "adapter": "unlimited",
           "filename": "guard-1.pdf",
           "img_dir": str(tmp_path / "work" / "guard-1"),
           "pdf_path": str(tmp_path / "uploads" / "guard-1.pdf"),
           "num_pages": num_pages, "error": None,
           "cancel_event": __import__("threading").Event()}
    ocr_service._JOBS[job["id"]] = job
    ocr_service._STREAMS[job["id"]] = deque(maxlen=1000)
    return job


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


def test_update_page_rejects_negative_index(client, monkeypatch, tmp_path):
    job = _mk_job(tmp_path, monkeypatch, num_pages=2)
    ocr_service.update_page(job["id"], 0,
                            {"page_index": 0, "width": 10, "height": 10,
                             "blocks": []})
    r = client.post("/api/pages/guard-1/-1", json={"page_index": -1})
    assert r.status_code == 400
    # pages[-1] must not have been overwritten by the request
    assert job["pages"][-1]["page_index"] == 0


def test_update_page_accepts_valid_index(client, monkeypatch, tmp_path):
    _mk_job(tmp_path, monkeypatch, num_pages=2)
    r = client.post("/api/pages/guard-1/1",
                    json={"page_index": 1, "width": 10, "height": 10,
                          "blocks": []})
    assert r.status_code == 200
    assert r.json()["ok"] is True


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
    assert ocr_service.retry_job("guard-1") is True
