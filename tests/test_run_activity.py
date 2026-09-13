"""Run-activity signals: "is it working, or waiting on the OCR endpoint?".

A job card that only knows ``223 / 224`` cannot answer the question a user
actually asks when a run looks frozen.  Two derived signals fix that:

* how long since the last page got a result — taken from the hOCR files'
  mtimes, so it is real disk state and survives a restart;
* the engine's current HTTP attempt, read from the OPTIONAL
  ``<job_dir>/progress.json`` the plugin writes around every attempt.

Both are additive: a missing file, a stale file from a previous run, or an
engine that writes nothing must degrade to "less detail", never to an error.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import ocr_service, page_store
from backend.main import app

JOB = "activity-1"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    ocr_service._JOBS.pop(JOB, None)
    ocr_service._STREAMS.pop(JOB, None)


def _mk_job(tmp_path, monkeypatch, *, status="running", num_pages=224,
            pages_done=223, run_started_at=None):
    monkeypatch.setattr(ocr_service, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(ocr_service, "UPLOAD_DIR", tmp_path / "uploads")
    hocr_dir = tmp_path / "work" / JOB / "hocr"
    hocr_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "job_id": JOB, "filename": "book.pdf", "status": status,
        "hocr_dir": str(hocr_dir), "previews_dir": str(hocr_dir.parent / "previews"),
        "pdf_path": str(tmp_path / "uploads" / f"{JOB}.pdf"),
        "num_pages": num_pages, "pages_done": pages_done, "error": "",
        "embedded_path": "", "created_at": "",
        "run_started_at": run_started_at,
    }
    ocr_service._JOBS[JOB] = job
    return job


def _write_page_result(job, page_no: int, age_seconds: float = 0.0):
    """A page result (hOCR) — the on-disk definition of "progress"."""
    hocr = Path(job["hocr_dir"]) / f"{page_no:06d}_ocr_hocr.hocr"
    hocr.write_text("<html><body></body></html>", encoding="utf-8")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(hocr, (stamp, stamp))
    return hocr


def _write_progress(job, **fields):
    page_store.progress_path(Path(job["hocr_dir"]).parent).write_text(
        json.dumps(fields), encoding="utf-8")


# --- the signal itself -------------------------------------------------------

def test_activity_reports_time_since_the_last_page(tmp_path, monkeypatch):
    """`seconds_since_progress` is measured from the newest hOCR file, so it is
    exactly "how long has this run produced nothing"."""
    job = _mk_job(tmp_path, monkeypatch, run_started_at=time.time() - 3600)
    _write_page_result(job, 186, age_seconds=400)
    _write_page_result(job, 188, age_seconds=800)   # newest wins

    act = ocr_service.run_activity(JOB)
    assert act["pages_done"] == 223 and act["num_pages"] == 224
    assert 395 <= act["seconds_since_progress"] <= 420
    assert 3595 <= act["running_for"] <= 3610


def test_activity_ignores_a_progress_file_from_a_previous_run(tmp_path,
                                                              monkeypatch):
    """A leftover progress.json (finished run, app restarted) must never make an
    idle job look busy."""
    started = time.time()
    job = _mk_job(tmp_path, monkeypatch, run_started_at=started)
    _write_page_result(job, 187)
    _write_progress(job, page=187, at=started - 120, attempt=3,
                    attempts_total=4)

    assert ocr_service.run_activity(JOB)["progress"] is None

    # A file written by THIS run is reported as-is.
    _write_progress(job, page=187, at=started + 1, event="start", attempt=1,
                    attempts_total=4, elapsed=0.0, timeout=900.0)
    fresh = ocr_service.run_activity(JOB)["progress"]
    assert fresh["attempt"] == 1 and fresh["attempts_total"] == 4
    assert fresh["timeout"] == 900.0


def test_activity_survives_garbage_and_missing_dirs(tmp_path, monkeypatch):
    """Best-effort by contract: no work folder, a corrupt file or a directory
    instead of a file must yield less detail, never an exception."""
    job = _mk_job(tmp_path, monkeypatch)
    page_store.progress_path(Path(job["hocr_dir"]).parent).write_text(
        "{not json", encoding="utf-8")
    act = ocr_service.run_activity(JOB)
    assert act["progress"] is None and act["last_progress_at"] is None

    # Unknown job -> empty dict (the callers index it blindly).
    assert ocr_service.run_activity("no-such-job") == {}


def test_progress_detail_is_only_read_for_live_jobs(tmp_path, monkeypatch):
    """A finished job keeps its timing, but must not advertise an in-flight
    attempt."""
    job = _mk_job(tmp_path, monkeypatch, status="done",
                  run_started_at=time.time() - 60)
    _write_page_result(job, 187, age_seconds=30)
    _write_progress(job, page=187, at=time.time(), attempt=2, attempts_total=4)
    act = ocr_service.run_activity(JOB)
    assert act["progress"] is None
    assert act["seconds_since_progress"] >= 29


# --- exposure to the WebUI ---------------------------------------------------

def test_jobs_list_carries_activity_for_a_live_run(tmp_path, monkeypatch):
    job = _mk_job(tmp_path, monkeypatch, run_started_at=time.time() - 600)
    _write_page_result(job, 186, age_seconds=500)
    _write_progress(job, page=187, at=time.time(), event="start", attempt=2,
                    attempts_total=4, elapsed=30.0, timeout=900.0)

    entry = {j["job_id"]: j for j in ocr_service.list_jobs()}[JOB]
    act = entry["activity"]
    assert act is not None
    assert act["progress"]["attempt"] == 2
    assert act["progress"]["page"] == 187
    assert act["seconds_since_progress"] >= 499

    # Terminal jobs report no activity block at all (nothing to watch).
    ocr_service._JOBS[JOB]["status"] = "done"
    done_entry = {j["job_id"]: j for j in ocr_service.list_jobs()}[JOB]
    assert done_entry["activity"] is None


def test_api_jobs_exposes_activity(client, tmp_path, monkeypatch):
    job = _mk_job(tmp_path, monkeypatch, run_started_at=time.time() - 120)
    _write_page_result(job, 187, age_seconds=100)
    _write_progress(job, page=187, at=time.time(), attempt=1,
                    attempts_total=4)

    body = client.get("/api/jobs").json()
    entry = next(j for j in body["jobs"] if j["job_id"] == JOB)
    assert entry["activity"]["progress"]["attempt"] == 1
    assert entry["activity"]["progress"]["page"] == 187
    assert entry["activity"]["seconds_since_progress"] >= 99
