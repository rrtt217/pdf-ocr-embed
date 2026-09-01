"""Pure selection logic for page-range / force-rerun.

``ocr_service.select_pages`` decides which 1-based page numbers to run for a
job given its per-page statuses, an optional 1-based inclusive page range and
a force flag.  These tests pin down the semantics without any I/O or engines.
A small TestClient smoke test also verifies the retry route accepts and threads
the ``page_start``/``page_end``/``force`` fields.
"""
from __future__ import annotations

from collections import deque

from fastapi.testclient import TestClient

from backend import cleanup as cleanup_mod
from backend import ocr_service
from backend.main import app
from backend.ocr_service import select_pages


def test_default_selects_all_pages():
    # No range, no force -> every page, regardless of prior results (fresh run).
    assert select_pages(5, [False, False, True, False, True], force=True) == [1, 2, 3, 4, 5]
    # Fresh upload (nothing done yet) picks every page even with force=False
    # (there is nothing to skip).
    assert select_pages(5, [None, None, None, None, None]) == [1, 2, 3, 4, 5]


def test_default_skips_done_pages():
    statuses = [True, None, True, False, None]  # pages 1,3 done; 4 has entry
    assert select_pages(5, statuses) == [2, 4, 5]


def test_force_includes_done_pages():
    statuses = [True, None, True, False, None]
    assert select_pages(5, statuses, force=True) == [1, 2, 3, 4, 5]


def test_range_limits_selection():
    # 1-based inclusive range (2,4) -> page numbers 2,3,4.
    statuses = [None] * 6
    assert select_pages(6, statuses, page_range=(2, 4)) == [2, 3, 4]


def test_range_with_skip_still_respects_done():
    # Pages 1-5 selected, but 1 and 3 are already done -> skipped.
    statuses = [True, None, True, None, None]
    assert select_pages(6, statuses, page_range=(1, 5)) == [2, 4, 5]


def test_range_with_force_reruns_done():
    statuses = [True, None, True, None, None]
    assert select_pages(6, statuses, page_range=(1, 5), force=True) == [1, 2, 3, 4, 5]


def test_single_page_range():
    statuses = [None] * 5
    assert select_pages(5, statuses, page_range=(3, 3)) == [3]


def test_range_beyond_page_count_clamped():
    # end beyond doc -> clamped to last page.
    statuses = [None] * 5
    assert select_pages(5, statuses, page_range=(1, 100)) == [1, 2, 3, 4, 5]
    # start beyond page count entirely -> empty.
    assert select_pages(5, statuses, page_range=(20, 30)) == []


def test_reversed_range_is_empty():
    statuses = [None] * 5
    assert select_pages(5, statuses, page_range=(4, 2)) == []


def test_start_only_and_end_only_defaults():
    # page_range with an end beyond the doc extends to the last page.
    statuses = [None] * 5
    assert select_pages(5, statuses, page_range=(2, 5)) == [2, 3, 4, 5]


def test_shorter_statuses_list_counts_rest_as_missing():
    # statuses shorter than num_pages -> trailing pages are "not done".
    statuses = [True, True]  # only 2 entries, num_pages=5
    assert select_pages(5, statuses) == [3, 4, 5]


def test_zero_page_count():
    assert select_pages(0, []) == []
    assert select_pages(0, [], page_range=(1, 10)) == []


def test_negative_or_zero_range_bounds_clamp():
    statuses = [None] * 4
    # start=0 (user typed 0) -> clamps to page 1.
    assert select_pages(4, statuses, page_range=(0, 2)) == [1, 2]
    # end=0 -> reversed, empty.
    assert select_pages(4, statuses, page_range=(1, 0)) == []


# --- API smoke test: the retry route accepts & threads the fields ---------
API_JOB_ID = "page-range-smoke-job"


def _stub_lifespan(monkeypatch):
    monkeypatch.setattr(ocr_service, "restore_jobs", lambda: 0)
    monkeypatch.setattr(cleanup_mod, "start_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "stop_background_cleanup", lambda: None)


def _inject_job():
    job = {
        "job_id": API_JOB_ID,
        "filename": "doc.pdf",
        "pdf_path": "/tmp/doc.pdf",
        "hocr_dir": "/tmp/work/" + API_JOB_ID + "/hocr",
        "previews_dir": "/tmp/work/" + API_JOB_ID + "/previews",
        "num_pages": 3,
        "pages_done": 0,
        "current": 0,
        "status": "done",
        "error": "",
        "embedded_path": "",
        "created_at": "",
    }
    with ocr_service._jobs_lock:
        ocr_service._JOBS[API_JOB_ID] = job
    with ocr_service._streams_lock:
        ocr_service._STREAMS[API_JOB_ID] = deque(maxlen=100)
    return job


def test_retry_route_accepts_page_range_and_force(monkeypatch):
    _stub_lifespan(monkeypatch)
    _inject_job()

    captured = {}

    def fake_retry(job_id, overrides=None, page_range=None, force=False):
        captured.update(job_id=job_id, overrides=overrides,
                        page_range=page_range, force=force)
        return True

    monkeypatch.setattr(ocr_service, "retry_job", fake_retry)

    try:
        with TestClient(app) as client:
            # Old-client shape (no new fields) must keep working.
            r = client.post(f"/api/ocr/retry/{API_JOB_ID}",
                            data={"ocr_engine": "tesseract"})
            assert r.status_code == 200, r.text
            assert captured["page_range"] is None
            assert captured["force"] is False

            # A 1-based inclusive range + force=true.
            r = client.post(f"/api/ocr/retry/{API_JOB_ID}",
                            data={"ocr_engine": "unlimited",
                                  "page_start": "1", "page_end": "3",
                                  "force": "true"})
            assert r.status_code == 200, r.text
            assert captured["page_range"] == (1, 3)
            assert captured["force"] is True
    finally:
        with ocr_service._jobs_lock:
            ocr_service._JOBS.pop(API_JOB_ID, None)
        with ocr_service._streams_lock:
            ocr_service._STREAMS.pop(API_JOB_ID, None)
