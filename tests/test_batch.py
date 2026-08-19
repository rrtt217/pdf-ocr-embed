"""Batch upload + ZIP packaging for feature #10.

Two layers are covered:
  - pure helpers in ``backend.batch`` (result assembly, archive member naming,
    ZIP building with files streamed from disk), and
  - API-level behavior via TestClient: ``POST /api/ocr/upload`` accepting
    multiple files (with single-file backward compatibility) and
    ``GET /api/ocr/zip`` returning a real archive (or a 404/400 when there is
    nothing to package).

Style mirrors tests/test_page_selection.py (job injection + lifespan stubs)
and tests/test_job_persistence.py (tmp dirs for work/output/uploads).
"""
from __future__ import annotations

import threading
import zipfile
from collections import deque
from pathlib import Path

import fitz  # PyMuPDF
from fastapi.testclient import TestClient

from backend import batch
from backend import cleanup as cleanup_mod
from backend import ocr_service
from backend import pdf_processing
from backend.main import app


def _stub_lifespan(monkeypatch):
    """Keep API tests offline: no job restore and no cleanup scans."""
    monkeypatch.setattr(ocr_service, "restore_jobs", lambda: 0)
    monkeypatch.setattr(cleanup_mod, "start_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "stop_background_cleanup", lambda: None)


def _use_tmp_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_service, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(ocr_service, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(pdf_processing, "OUTPUT_DIR", tmp_path / "output")


def _make_job(job_id: str, filename: str, embedded_path=None,
              status: str = "embedded") -> dict:
    job = {
        "id": job_id,
        "filename": filename,
        "pdf_path": f"/tmp/{job_id}.pdf",
        "img_dir": f"/tmp/work/{job_id}",
        "pages": [],
        "num_pages": 0,
        "current": 0,
        "status": status,
        "adapter": "unlimited",
        "concurrency": 1,
        "error": None,
        "embedded_path": embedded_path,
        "thumb_path": None,
        "created": 0,
        "cancel_event": threading.Event(),
    }
    with ocr_service._jobs_lock:
        ocr_service._JOBS[job_id] = job
    with ocr_service._streams_lock:
        ocr_service._STREAMS[job_id] = deque(maxlen=100)
    return job


def _drop_job(job_id: str) -> None:
    with ocr_service._jobs_lock:
        ocr_service._JOBS.pop(job_id, None)
    with ocr_service._streams_lock:
        ocr_service._STREAMS.pop(job_id, None)


# ---------------------------------------------------------------------------
# pure helpers: collect_embedded / _unique_arcname / build_zip
# ---------------------------------------------------------------------------

def test_collect_embedded_returns_only_jobs_with_results(tmp_path):
    ok_file = tmp_path / "one_embedded.pdf"
    ok_file.write_bytes(b"%PDF-embedded-1")
    gone = tmp_path / "gone.pdf"
    _make_job("emb-ok", "one.pdf", embedded_path=str(ok_file))
    _make_job("emb-none", "none.pdf", embedded_path=None)
    _make_job("emb-gone", "gone.pdf", embedded_path=str(gone))
    try:
        entries = batch.collect_embedded(["emb-ok", "emb-none", "emb-gone",
                                          "emb-unknown"])
        assert [e["job_id"] for e in entries] == ["emb-ok"]
        e = entries[0]
        assert e["filename"] == "one.pdf"
        assert e["embedded_path"] == str(ok_file)
    finally:
        for jid in ("emb-ok", "emb-none", "emb-gone"):
            _drop_job(jid)


def test_collect_embedded_empty_input():
    assert batch.collect_embedded([]) == []
    assert batch.collect_embedded(None) == []


def test_unique_arcname_sanitizes_paths():
    used: set = set()
    assert batch._unique_arcname(used, "/etc/evil/../doc.pdf") == "doc.pdf"
    assert batch._unique_arcname(used, "../../outside.pdf") == "outside.pdf"
    assert batch._unique_arcname(used, "") == "file.pdf"
    assert batch._unique_arcname(used, "..") == "file (2).pdf"


def test_unique_arcname_deduplicates():
    used: set = set()
    first = batch._unique_arcname(used, "doc.pdf")
    second = batch._unique_arcname(used, "doc.pdf")
    third = batch._unique_arcname(used, "doc.pdf")
    assert first == "doc.pdf"
    assert second == "doc (2).pdf"
    assert third == "doc (3).pdf"


def test_build_zip_streams_members_named_after_source(tmp_path):
    a = tmp_path / "src_a.pdf"
    b = tmp_path / "src_b.pdf"
    a.write_bytes(b"%PDF-A-context")
    b.write_bytes(b"%PDF-B-context")
    entries = [
        {"job_id": "j1", "filename": "report-a.pdf",
         "embedded_path": str(a)},
        {"job_id": "j2", "filename": "report-b.pdf",
         "embedded_path": str(b)},
    ]
    out = tmp_path / "bundle.zip"
    summary = batch.build_zip(out, entries)
    assert summary["count"] == 2
    assert summary["files"] == ["report-a.pdf", "report-b.pdf"]
    assert Path(summary["archive"]).exists()
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == ["report-a.pdf", "report-b.pdf"]
        assert zf.read("report-a.pdf") == b"%PDF-A-context"
        assert zf.read("report-b.pdf") == b"%PDF-B-context"


def test_build_zip_deduplicates_colliding_source_names(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"%PDF-A")
    b.write_bytes(b"%PDF-B")
    entries = [
        {"job_id": "j1", "filename": "doc.pdf", "embedded_path": str(a)},
        {"job_id": "j2", "filename": "doc.pdf", "embedded_path": str(b)},
    ]
    out = tmp_path / "collide.zip"
    summary = batch.build_zip(out, entries)
    assert summary["count"] == 2
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {"doc.pdf", "doc (2).pdf"}
        assert zf.read("doc (2).pdf") == b"%PDF-B"


def test_build_zip_skips_missing_member(tmp_path):
    missing = tmp_path / "nope.pdf"
    ok = tmp_path / "ok.pdf"
    ok.write_bytes(b"%PDF-ok")
    entries = [
        {"job_id": "j1", "filename": "ok.pdf", "embedded_path": str(ok)},
        {"job_id": "j2", "filename": "missing.pdf", "embedded_path": str(missing)},
    ]
    out = tmp_path / "partial.zip"
    summary = batch.build_zip(out, entries)
    assert summary["count"] == 1
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["ok.pdf"]


def test_create_jobs_helper_creates_independent_jobs(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    jobs = ocr_service.create_jobs([("a.pdf", b"%PDF-a"), ("b.pdf", b"%PDF-b")])
    assert len(jobs) == 2
    assert jobs[0]["filename"] == "a.pdf" and jobs[1]["filename"] == "b.pdf"
    assert jobs[0]["id"] != jobs[1]["id"]
    for job in jobs:
        assert ocr_service.get_job(job["id"]) is not None
        _drop_job(job["id"])


# ---------------------------------------------------------------------------
# API: multi-file upload
# ---------------------------------------------------------------------------

def _noop_run_ocr(*_args, **_kwargs):
    return None


def test_upload_accepts_multiple_files(monkeypatch, tmp_path):
    _stub_lifespan(monkeypatch)
    _use_tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(ocr_service, "run_ocr", _noop_run_ocr)
    try:
        with TestClient(app) as client:
            r = client.post("/api/ocr/upload", files=[
                ("files", ("a.pdf", b"%PDF-A", "application/pdf")),
                ("files", ("b.pdf", b"%PDF-B", "application/pdf")),
            ])
        assert r.status_code == 200, r.text
        data = r.json()
        assert [j["filename"] for j in data["jobs"]] == ["a.pdf", "b.pdf"]
        assert all(j["status"] == "running" for j in data["jobs"])
        # Multi-file: no legacy single job_id at the top level, but a count.
        assert "job_id" not in data
        assert data["count"] == 2
        for j in data["jobs"]:
            job = ocr_service.get_job(j["job_id"])
            assert job is not None and job["filename"] == j["filename"]
            assert job["status"] == "uploaded"  # run_ocr stubbed -> still uploaded
    finally:
        for jid in list(ocr_service._JOBS):
            _drop_job(jid)


def test_upload_single_file_backward_compat(monkeypatch, tmp_path):
    """Legacy clients sending field ``file`` still get ``job_id``."""
    _stub_lifespan(monkeypatch)
    _use_tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(ocr_service, "run_ocr", _noop_run_ocr)
    try:
        with TestClient(app) as client:
            r = client.post("/api/ocr/upload",
                            files=[("file", ("legacy.pdf", b"%PDF-legacy",
                                             "application/pdf"))])
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["job_id"] and data["filename"] == "legacy.pdf"
        assert data["status"] == "running"
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["job_id"] == data["job_id"]
    finally:
        for jid in list(ocr_service._JOBS):
            _drop_job(jid)


def test_upload_rejects_empty_batch_and_empty_file(monkeypatch, tmp_path):
    _stub_lifespan(monkeypatch)
    _use_tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(ocr_service, "run_ocr", _noop_run_ocr)
    try:
        with TestClient(app) as client:
            assert client.post("/api/ocr/upload").status_code == 400
            assert client.post("/api/ocr/upload", files=[
                ("files", ("empty.pdf", b"", "application/pdf")),
            ]).status_code == 400
    finally:
        for jid in list(ocr_service._JOBS):
            _drop_job(jid)


# ---------------------------------------------------------------------------
# API: ZIP download
# ---------------------------------------------------------------------------

def _make_embedded(job_id: str, tmp_path: Path, filename: str) -> dict:
    """Write a real tiny PDF on disk as the job's embedded output."""
    doc = fitz.open()
    page = doc.new_page(width=100, height=100)
    page.insert_text(fitz.Point(10, 50), f"job {job_id}")
    out = tmp_path / f"{job_id}_embedded.pdf"
    doc.save(str(out), garbage=4, deflate=True)
    doc.close()
    return _make_job(job_id, filename, embedded_path=str(out))


def test_zip_endpoint_returns_archive_with_source_names(monkeypatch, tmp_path):
    _stub_lifespan(monkeypatch)
    try:
        _make_embedded("zip-aa", tmp_path, "paper-a.pdf")
        _make_embedded("zip-bb", tmp_path, "paper-b.pdf")
        with TestClient(app) as client:
            r = client.get("/api/ocr/zip?jobs=zip-aa,zip-bb")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/zip")
        buf = r.content
        import io
        with zipfile.ZipFile(io.BytesIO(buf)) as zf:
            assert sorted(zf.namelist()) == ["paper-a.pdf", "paper-b.pdf"]
            for name in zf.namelist():
                doc = fitz.open(stream=zf.read(name), filetype="pdf")
                assert doc.page_count == 1
                doc.close()
    finally:
        _drop_job("zip-aa")
        _drop_job("zip-bb")


def test_zip_endpoint_404_when_none_have_results(monkeypatch, tmp_path):
    _stub_lifespan(monkeypatch)
    _make_job("zip-nope", "running.pdf", embedded_path=None, status="running")
    _make_job("zip-dead", "gone.pdf",
              embedded_path=str(tmp_path / "missing.pdf"), status="embedded")
    try:
        with TestClient(app) as client:
            r = client.get("/api/ocr/zip?jobs=zip-nope,zip-dead,zip-unknown")
        assert r.status_code == 404, r.text
        assert "embedded result" in r.json()["detail"]
    finally:
        _drop_job("zip-nope")
        _drop_job("zip-dead")


def test_zip_endpoint_400_without_ids(monkeypatch):
    _stub_lifespan(monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/ocr/zip?jobs=").status_code == 400
        assert client.get("/api/ocr/zip?jobs=,,,").status_code == 400
