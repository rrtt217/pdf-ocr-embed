"""Per-job OCR progress registry + cancel flags (shared with the plugin).

The ocrmypad engine runs inside ocrmypdf's worker threads (``use_threads`` is
required for the unlimited engine: HTTP is IO-bound and thread-based runs keep
this registry in the same process).  Each completed page is reported here, and
the FastAPI layer (SSE) reads :func:`snapshot` to stream progress.

Page completions are ALSO persisted as sidecar files next to each page's hOCR
(``*_ocr_hocr.blocks.json``), so a restart can rebuild progress from disk.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

_LOCK = threading.Lock()

# job_id -> state dict. Keys:
#   pages_done: int            completed page count (OCR phase)
#   done_indices: set[int]     1-based page numbers reported complete
#   total: Optional[int]       total page count (hint; may be updated)
#   started_at: float          monotonic start time
#   cancel: bool               user requested a stop
_STATE: Dict[str, dict] = {}


def _state(job_id: str) -> dict:
    with _LOCK:
        st = _STATE.get(job_id)
        if st is None:
            st = {
                "pages_done": 0,
                "done_indices": set(),
                "total": None,
                "started_at": time.monotonic(),
                "cancel": False,
            }
            _STATE[job_id] = st
        return st


def reset(job_id: str, total: Optional[int] = None) -> None:
    """Clear a job's progress state (new OCR pass)."""
    with _LOCK:
        _STATE.pop(job_id, None)
    _state(job_id)["total"] = total


def set_total(job_id: str, total: int) -> None:
    """Record the total page count for progress display."""
    _state(job_id)["total"] = max(0, int(total))


def report_page(job_id: str, page_index: int) -> None:
    """Report one completed page (0-based page index)."""
    st = _state(job_id)
    page_no = int(page_index) + 1
    with _LOCK:
        if page_no not in st["done_indices"]:
            st["done_indices"].add(page_no)
        st["pages_done"] = len(st["done_indices"])


def snapshot(job_id: str) -> dict:
    """Read a job's progress state (defensive copy)."""
    st = _state(job_id)
    with _LOCK:
        return {
            "pages_done": st["pages_done"],
            "done_indices": sorted(st["done_indices"]),
            "total": st["total"],
            "cancel": st["cancel"],
        }


def request_cancel(job_id: str) -> None:
    """Ask the engine to stop processing more pages for this job."""
    _state(job_id)["cancel"] = True


def is_cancelled(job_id: str) -> bool:
    """True when a stop was requested (checked by the engine per page)."""
    return bool(_state(job_id)["cancel"])


def pop(job_id: str) -> None:
    """Drop a job's state entirely (job cleared)."""
    with _LOCK:
        _STATE.pop(job_id, None)


def page_number_from_hocr_name(name: str) -> int:
    """Parse the 0-based page index from an ocrmypdf page file name.

    ocrmypdf names per-page work files ``000001_ocr_hocr.hocr`` etc.
    Returns 0 when the prefix is unparseable.
    """
    try:
        return max(0, int(name.split("_", 1)[0]) - 1)
    except (ValueError, IndexError):
        return 0
