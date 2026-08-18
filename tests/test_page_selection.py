"""Pure selection logic for page-range / force-rerun (#12).

``ocr_service.select_pages`` decides which zero-based page indices to run for
a job given its per-page statuses, an optional 1-based inclusive page range and
a force flag.  These tests pin down the semantics without any I/O or engines.
A small TestClient smoke test also verifies the retry route accepts and threads
the new ``page_start``/``page_end``/``force`` fields.
"""
from __future__ import annotations

import threading
from collections import deque

from fastapi.testclient import TestClient

from backend import cleanup as cleanup_mod
from backend import ocr_service
from backend.main import app
from backend.ocr_service import select_pages


def test_default_selects_all_pages():
    # No range, no force -> every page, regardless of prior results (fresh run).
    assert select_pages(5, [False, False, True, False, True], force=True) == [0, 1, 2, 3, 4]
    # Fresh upload (nothing done yet) picks every page even with force=False
    # (there is nothing to skip).
    assert select_pages(5, [None, None, None, None, None]) == [0, 1, 2, 3, 4]


def test_default_skips_done_pages():
    statuses = [True, None, True, False, None]  # 0,2 done; 3 has entry
    assert select_pages(5, statuses) == [1, 3, 4]


def test_force_includes_done_pages():
    statuses = [True, None, True, False, None]
    assert select_pages(5, statuses, force=True) == [0, 1, 2, 3, 4]


def test_range_limits_selection():
    # 1-based inclusive range (2,4) -> zero-based indices 1,2,3.
    statuses = [None] * 6
    assert select_pages(6, statuses, page_range=(2, 4)) == [1, 2, 3]


def test_range_with_skip_still_respects_done():
    # Pages 1-6 selected, but 3 (zero-based 2) is already done -> skipped.
    statuses = [True, True, True, None, None]
    # range (1,5) covers 0..4
    assert select_pages(6, statuses, page_range=(1, 5)) == [3, 4]


def test_range_with_force_reruns_done():
    statuses = [True, True, True, None, None]
    assert select_pages(6, statuses, page_range=(1, 5), force=True) == [0, 1, 2, 3, 4]


def test_single_page_range():
    statuses = [None] * 5
    assert select_pages(5, statuses, page_range=(3, 3)) == [2]


def test_range_beyond_page_count_clamped():
    # end beyond doc -> clamped to last page (zero-based 4).
    statuses = [None] * 5
    assert select_pages(5, statuses, page_range=(1, 100)) == [0, 1, 2, 3, 4]
    # start beyond page count entirely -> empty.
    assert select_pages(5, statuses, page_range=(20, 30)) == []


def test_reversed_range_is_empty():
    statuses = [None] * 5
    assert select_pages(5, statuses, page_range=(4, 2)) == []


def test_start_only_and_end_only_defaults():
    # page_range with a None end is treated as extending to the last page.
    statuses = [None] * 5
    assert select_pages(5, statuses, page_range=(2, 5)) == [1, 2, 3, 4]


def test_shorter_statuses_list_counts_rest_as_missing():
    # statuses shorter than num_pages -> trailing pages are "not done".
    statuses = [True, True]  # only 2 entries, num_pages=5
    assert select_pages(5, statuses) == [2, 3, 4]


def test_zero_page_count():
    assert select_pages(0, []) == []
    assert select_pages(0, [], page_range=(1, 10)) == []


def test_negative_or_zero_range_bounds_clamp():
    statuses = [None] * 4
    # start=0 (user typed 0) -> clamps to zero-based 0.
    assert select_pages(4, statuses, page_range=(0, 2)) == [0, 1]
    # end=0 -> reversed, empty.
    assert select_pages(4, statuses, page_range=(1, 0)) == []


# --- API smoke test: the retry route accepts & threads the new fields -----
API_JOB_ID = "page-range-smoke-job"


def _stub_lifespan(monkeypatch):
    monkeypatch.setattr(ocr_service, "restore_jobs", lambda: 0)
    monkeypatch.setattr(cleanup_mod, "start_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "stop_background_cleanup", lambda: None)


def _inject_job():
    job = {
        "id": API_JOB_ID,
        "filename": "doc.pdf",
        "pdf_path": "/tmp/doc.pdf",
        "img_dir": "/tmp/work/" + API_JOB_ID,
        "pages": [None, None, None],
        "num_pages": 3,
        "current": 0,
        "status": "done",
        "adapter": "unlimited",
        "concurrency": 1,
        "error": None,
        "embedded_path": None,
        "thumb_path": None,
        "created": 0,
        "cancel_event": threading.Event(),
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

    def fake_retry(job_id, adapter_name=None, extra_cfg=None, concurrency=1,
                   page_range=None, force=False):
        captured.update(job_id=job_id, adapter_name=adapter_name,
                        extra_cfg=extra_cfg, concurrency=concurrency,
                        page_range=page_range, force=force)
        return True

    monkeypatch.setattr(ocr_service, "retry_job", fake_retry)

    try:
        with TestClient(app) as client:
            # Old-client shape (no new fields) must keep working.
            r = client.post(f"/api/ocr/retry/{API_JOB_ID}",
                            data={"adapter": "tesseract", "concurrency": "2"})
            assert r.status_code == 200, r.text
            assert captured["page_range"] is None
            assert captured["force"] is False

            # New fields: a 1-based inclusive range + force=true.
            r = client.post(f"/api/ocr/retry/{API_JOB_ID}",
                            data={"adapter": "unlimited", "concurrency": "4",
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
