"""SSE progress payload semantics: `current` is the COMPLETED-PAGE COUNT.

Pages finish out of order (jobs-way concurrency), so the per-page progress
event must report how many pages are done — never the last completed page
number.  A page number in `current` made the WebUI's N/total count jump to the
highest done page (e.g. "9/10" when only pages 1,2,8,9 — four pages — exist).
"""
from __future__ import annotations

import json
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
    yield
    ocr_service._JOBS.pop("stream-1", None)
    ocr_service._STREAMS.pop("stream-1", None)


def _mk_job(tmp_path, monkeypatch, num_pages=10, status="done"):
    """A terminal job in the registry with a real (empty) hOCR work folder.

    The SSE stream closes for a terminal job, so TestClient can read the whole
    event stream to completion.
    """
    monkeypatch.setattr(ocr_service, 'WORK_DIR', tmp_path / 'work')
    monkeypatch.setattr(ocr_service, 'UPLOAD_DIR', tmp_path / 'uploads')
    job = {"job_id": "stream-1", "current": 0,
           "status": status,
           "filename": "stream-1.pdf",
           "hocr_dir": str(tmp_path / "work" / "stream-1" / "hocr"),
           "previews_dir": str(tmp_path / "work" / "stream-1" / "previews"),
           "pdf_path": str(tmp_path / "uploads" / "stream-1.pdf"),
           "num_pages": num_pages, "pages_done": 0, "error": "",
           "embedded_path": "", "created_at": ""}
    Path(job["hocr_dir"]).mkdir(parents=True, exist_ok=True)
    ocr_service._JOBS[job["job_id"]] = job
    ocr_service._STREAMS[job["job_id"]] = deque(maxlen=1000)
    return job


def _write_sidecar(job, page_no: int):
    """A block sidecar (engine-native completion signal) for one page."""
    sidecar = Path(job["hocr_dir"]) / f"{page_no:06d}_ocr_hocr.blocks.json"
    sidecar.write_text(json.dumps({"page": {"page_index": page_no - 1,
                                            "width": 1000, "height": 2000,
                                            "blocks": []},
                                   "dpi": 300.0}),
                       encoding="utf-8")


def _read_progress(client) -> list:
    """Read the whole SSE stream; return every `progress` event."""
    out = []
    with client.stream("GET", "/api/ocr/stream/stream-1") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            msg = json.loads(line[len("data: "):])
            if msg.get("type") == "progress":
                out.append(msg)
    return out


def test_progress_current_is_done_count_not_page_number(client, monkeypatch,
                                                        tmp_path):
    """Pages 1,2,8,9 done — four pages, highest page number 9.  `current` must
    be 4 for every progress event, never 9."""
    job = _mk_job(tmp_path, monkeypatch, num_pages=10)
    for n in (1, 2, 8, 9):
        _write_sidecar(job, n)

    progress = _read_progress(client)
    assert [m["page_index"] for m in progress] == [0, 1, 7, 8]  # all four pages
    for msg in progress:
        assert msg["current"] == 4        # count of done pages, not page 9
        assert msg["pages_done"] == 4
        assert msg["total"] == 10


def test_progress_count_grows_as_more_pages_finish(client, monkeypatch,
                                                   tmp_path):
    """Pages finishing later must raise `current` to the new COUNT (the old bug
    stuck at the highest completed page number regardless of new pages)."""
    job = _mk_job(tmp_path, monkeypatch, num_pages=10)
    for n in (1, 8, 9):
        _write_sidecar(job, n)
    first = _read_progress(client)
    assert len(first) == 3
    assert all(m["current"] == 3 for m in first)

    # Pages 2..7 finish: the count climbs to 9 (every page done), while the
    # highest page number was already 9 in the first burst.
    for n in (2, 3, 4, 5, 6, 7):
        _write_sidecar(job, n)
    later = _read_progress(client)
    assert len(later) == 9
    assert all(m["current"] == 9 for m in later)


def test_stream_emits_activity_for_a_live_run(client, monkeypatch, tmp_path):
    """A running job must stream *why* it looks frozen: how long since the last
    page finished, plus the engine's current HTTP attempt.  Without this the
    card sits at "223 / 224" for an hour and the user cannot tell a slow model
    from an endpoint that accepted the request and stopped answering.

    The job is flipped to a terminal status as soon as the real activity is
    computed, so the stream ends by itself (an endless stream cannot be read to
    completion through TestClient).
    """
    import os
    import time

    from backend import page_store

    job = _mk_job(tmp_path, monkeypatch, num_pages=224, status="running")
    job["run_started_at"] = time.time() - 3600
    # One page finished 400 s ago: that is the last progress on disk.
    hocr = Path(job["hocr_dir"]) / "000186_ocr_hocr.hocr"
    hocr.write_text("<html><body></body></html>", encoding="utf-8")
    stamp = time.time() - 400
    os.utime(hocr, (stamp, stamp))
    # The plugin's per-attempt progress file (written by the engine).
    page_store.progress_path(Path(job["hocr_dir"]).parent).write_text(
        json.dumps({"page": 187, "at": time.time(), "event": "start",
                    "attempt": 2, "attempts_total": 4, "elapsed": 12.0,
                    "timeout": 900.0}),
        encoding="utf-8")

    real_activity = ocr_service.run_activity

    def activity_then_finish(job_id):
        activity = real_activity(job_id)
        ocr_service._JOBS[job_id]["status"] = "done"   # end the stream
        return activity

    monkeypatch.setattr(ocr_service, "run_activity", activity_then_finish)

    events = []
    with client.stream("GET", "/api/ocr/stream/stream-1") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))

    activity = [e for e in events if e.get("type") == "activity"]
    assert activity, "the stream must carry activity events for a live run"
    got = activity[0]
    assert got["progress"]["page"] == 187
    assert got["progress"]["attempt"] == 2
    assert got["progress"]["attempts_total"] == 4
    assert got["progress"]["timeout"] == 900.0
    assert 395 <= got["seconds_since_progress"] <= 420
    assert 3595 <= got["running_for"] <= 3610
