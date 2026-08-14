"""OCR orchestration: upload -> per-page OCR -> editable pages -> embed.

Holds in-memory job state (uploaded PDF path, per-page OcrPage, progress) and
drives the SSE progress stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Deque, Dict, List, Optional

from backend.config import resolve

import fitz  # PyMuPDF

from backend import pdf_processing
from backend.models import OcrPage, dict_to_page, page_to_dict
from backend.sources.factory import get_adapter

log = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
WORK_DIR = Path(__file__).resolve().parent.parent / "work"

# In-memory jobs: job_id -> job dict.
_JOBS: Dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Per-job SSE buffers (deque of messages) indexed by job id.
_STREAMS: Dict[str, Deque[dict]] = {}
_streams_lock = threading.Lock()


def ensure_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    pdf_processing.ensure_output_dir()


def create_job(filename: str, file_bytes: bytes) -> dict:
    ensure_dirs()
    job_id = uuid.uuid4().hex[:12]
    safe_name = Path(filename).name
    src_dir = WORK_DIR / job_id
    src_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = src_dir / (safe_name or "upload.pdf")
    pdf_path.write_bytes(file_bytes)

    with _jobs_lock:
        _JOBS[job_id] = {
            "id": job_id,
            "filename": safe_name,
            "pdf_path": str(pdf_path),
            "img_dir": str(src_dir),
            "pages": [],            # list of OcrPage dicts (normalized), None = not done
            "num_pages": 0,
            "current": 0,
            "status": "uploaded",   # uploaded | running | retrying | stopped | done | error | embedded
            "adapter": "unlimited",
            "concurrency": 1,
            "error": None,
            "embedded_path": None,
            "thumb_path": None,
            "created": time.time(),
            "cancel_event": threading.Event(),
        }
    with _streams_lock:
        _STREAMS[job_id] = deque(maxlen=1000)
    log.info("job %s created: filename=%s, size=%d bytes", job_id, safe_name, len(file_bytes))
    return _JOBS[job_id]


def get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return _JOBS.get(job_id)


def list_jobs() -> List[dict]:
    """Summaries of every job currently held by the server (newest first).

    The frontend fetches this on page load — the server is the single source
    of truth for what tasks exist, so nothing needs to be remembered client-side.
    """
    with _jobs_lock:
        jobs = []
        for j in _JOBS.values():
            jobs.append({
                "id": j["id"],
                "filename": j["filename"],
                "status": j["status"],
                "current": j["current"],
                "total": j["num_pages"],
                "error": j.get("error"),
                "has_embedded": bool(j.get("embedded_path")),
                "created": j["created"],
            })
        jobs.sort(key=lambda j: j["created"], reverse=True)
        return jobs


def clear_job(job_id: str) -> bool:
    """Fully remove a job: drop from memory AND delete its on-disk artifacts.

    Safe to call for any job (running or not) — the cancel event is set first
    so a still-running OCR thread stops respecting future work. Returns False
    if the job does not exist.
    """
    job = get_job(job_id)
    if job is None:
        log.warning("clear: job %s not found", job_id)
        return False
    job["cancel_event"].set()
    with _jobs_lock:
        _JOBS.pop(job_id, None)
    with _streams_lock:
        _STREAMS.pop(job_id, None)
    # Best-effort removal of the job's source PDF / page renders / embedded output.
    try:
        img_dir = job.get("img_dir")
        if img_dir and Path(img_dir).exists():
            shutil.rmtree(img_dir, ignore_errors=True)
        for key in ("embedded_path", "thumb_path"):
            p = job.get(key)
            if p and Path(p).exists():
                Path(p).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log.exception("clear_job %s: on-disk cleanup failed", job_id)
    log.info("job %s cleared (status was %s, %d pages kept)",
             job_id, job.get("status"), sum(1 for p in job.get("pages", []) if p))
    return True


def _set(job: dict, **kw) -> None:
    with _jobs_lock:
        job.update(kw)


def push_event(job_id: str, event: dict) -> None:
    with _streams_lock:
        buf = _STREAMS.get(job_id)
        if buf is not None:
            buf.append(event)


def drain_events(job_id: str) -> List[dict]:
    with _streams_lock:
        buf = _STREAMS.get(job_id)
        if buf is None:
            return []
        out = list(buf)
        buf.clear()
        return out


def page_preview_path(job_id: str, page_index: int) -> Optional[str]:
    job = get_job(job_id)
    if job is None:
        return None
    png = Path(job["img_dir"]) / f"page_{page_index:04d}.png"
    return str(png) if png.exists() else None


def get_pages(job_id: str) -> List[dict]:
    job = get_job(job_id)
    return job["pages"] if job else []


def update_page(job_id: str, page_index: int, page_dict: dict) -> List[dict]:
    job = get_job(job_id)
    if job is None:
        return []
    with _jobs_lock:
        while len(job["pages"]) <= page_index:
            job["pages"].append(None)
        job["pages"][page_index] = page_dict
    return [p for p in job["pages"] if p is not None]


def retry_job(job_id: str, adapter_name: str | None = None,
              extra_cfg: dict | None = None, concurrency: int = 1) -> bool:
    """Re-run OCR for an existing job, **only for pages that are missing/failed**.

    Already-successful pages are preserved (this is the fix for the "99% done
    then retry restarts from 0" bug). Returns True if a retry was scheduled.
    """
    job = get_job(job_id)
    if job is None:
        log.warning("retry: job %s not found", job_id)
        return False
    if not Path(job["pdf_path"]).exists():
        log.warning("retry: job %s source PDF missing", job_id)
        return False

    already_done = sum(1 for p in job["pages"] if p is not None)
    missing = [i for i in range(job.get("num_pages", 0) or len(job["pages"]))
               if i >= len(job["pages"]) or job["pages"][i] is None]
    log.info("retry job %s: %d pages already done, %d to re-run: %s",
             job_id, already_done, len(missing), missing)

    # Reset status/error but KEEP successful page results.
    with _jobs_lock:
        job["status"] = "retrying"
        job["error"] = None
        job["cancel_event"].clear()
    # Reset the SSE buffer.
    with _streams_lock:
        _STREAMS[job_id] = deque(maxlen=1000)
    push_event(job_id, {"type": "status", "status": "retrying",
                        "message": f"Retrying {len(missing)} page(s), "
                                   f"{already_done} already done"})
    # Run OCR in a background thread so this call returns immediately and the
    # SSE progress stream can deliver updates in real time.
    t = threading.Thread(
        target=run_ocr,
        args=(job_id, adapter_name, extra_cfg, concurrency),
        kwargs={"only_missing": True},
        daemon=True,
        name=f"ocr-retry-{job_id}",
    )
    t.start()
    return True


def stop_job(job_id: str) -> bool:
    """Signal a running OCR job to stop immediately.

    Sets the cancel event and marks the job `stopped` right away — we do NOT
    wait for in-flight HTTP requests to finish.  Already-completed pages are
    kept so the user can download a partial result or retry the remaining pages.
    """
    job = get_job(job_id)
    if job is None:
        return False
    log.info("stop requested for job %s (status=%s, current=%d/%d)",
             job_id, job["status"], job["current"], job["num_pages"])
    job["cancel_event"].set()
    # Mark stopped immediately regardless of running state.
    done = sum(1 for p in job["pages"] if p is not None)
    _set(job, status="stopped", current=done)
    push_event(job_id, {"type": "status", "status": "stopped",
                        "message": f"OCR stopped ({done}/{job['num_pages']} pages done)",
                        "result": [p for p in job["pages"] if p is not None]})
    return True


def run_ocr(job_id: str, adapter_name: str | None = None,
            extra_cfg: dict | None = None, concurrency: int = 1,
            only_missing: bool = False) -> None:
    """Run the OCR pipeline for a job.

    Pages are processed concurrently using a thread pool sized by `concurrency`.
    When `only_missing` is True (retry path), pages that already have a result
    are skipped — only missing/failed pages are re-run, so a retry at 99% does
    NOT restart from page 0.
    """
    job = get_job(job_id)
    if job is None:
        return
    adapter_name = adapter_name or job.get("adapter", "unlimited")
    concurrency = max(1, int(concurrency or 1))
    _set(job, concurrency=concurrency)
    cancel = job["cancel_event"]
    cancel.clear()

    # If extra_cfg carries per-request overrides, apply them to the adapter.
    # Allowed override keys differ per adapter; unknown ones are ignored.
    allowed_keys = _ADAPTER_PARAM_KEYS.get(adapter_name or "unlimited",
                                           ("api_key", "base_url", "model"))
    adapter_kwargs = {}
    if extra_cfg:
        adapter_kwargs = {
            k: v for k, v in extra_cfg.items()
            if k in allowed_keys and v
        }

    try:
        base_adapter = _make_adapter(adapter_name, adapter_kwargs)

        _set(job, status="running", adapter=adapter_name, error=None)
        push_event(job_id, {"type": "status", "status": "running",
                            "message": f"OCR started (concurrency={concurrency})"})
        log.info("job %s: run_ocr start, adapter=%s, concurrency=%d, only_missing=%s",
                 job_id, adapter_name, concurrency, only_missing)

        with fitz.open(job["pdf_path"]) as doc:
            num = doc.page_count
            _set(job, num_pages=num)

            # Decide which pages need OCR. On retry, skip pages that already
            # have a result so we don't redo the 99% that succeeded.
            if only_missing:
                page_indices = [i for i in range(num)
                                if i >= len(job["pages"]) or job["pages"][i] is None]
            else:
                page_indices = list(range(num))

            already_done = sum(1 for p in job["pages"] if p is not None)
            _set(job, current=already_done)
            push_event(job_id, {
                "type": "progress",
                "current": already_done,
                "total": num,
                "message": f"OCR {already_done}/{num} already done"
                           + (f", running {len(page_indices)}…" if page_indices else ""),
            })
            log.info("job %s: %d pages to OCR, %d already done, %d total",
                     job_id, len(page_indices), already_done, num)

            if not page_indices:
                # Nothing to do — everything is already complete.
                log.info("job %s: nothing to do, all pages already done", job_id)
                _set(job, status="done")
                push_event(job_id, {"type": "status", "status": "done",
                                    "message": "OCR complete (all pages already done)",
                                    "result": [p for p in job["pages"] if p is not None]})
                return

            # Pre-render only the pages that need OCR (render is not thread-safe).
            page_specs: List[dict] = []
            for i in page_indices:
                if cancel.is_set():
                    log.info("job %s: cancelled during pre-render at page %d", job_id, i)
                    _finish_stopped(job, job_id, num)
                    return
                img_path, w, h = pdf_processing.render_page_to_file(
                    doc[i], Path(job["img_dir"]), i)
                page_specs.append({"page_index": i, "img_path": img_path, "w": w, "h": h})

            if concurrency <= 1 or len(page_specs) <= 1:
                _ocr_pages_sequentially(job, job_id, base_adapter, page_specs,
                                        num, cancel)
            else:
                _ocr_pages_parallel(job, job_id, adapter_kwargs, page_specs,
                                    num, concurrency, already_done, cancel)

        if cancel.is_set():
            _finish_stopped(job, job_id, num)
            return

        _set(job, status="done")
        push_event(job_id, {"type": "status", "status": "done",
                            "message": "OCR complete",
                            "result": [p for p in job["pages"] if p is not None]})
        log.info("job %s: run_ocr done, %d/%d pages",
                 job_id, sum(1 for p in job["pages"] if p is not None), num)
    except Exception as exc:  # noqa: BLE001
        if cancel.is_set():
            # Don't overwrite "stopped" with "error" if we were cancelled.
            _finish_stopped(job, job_id, num)
            return
        log.exception("job %s: run_ocr failed", job_id)
        _set(job, status="error", error=str(exc))
        push_event(job_id, {"type": "error", "message": str(exc)})


def _finish_stopped(job, job_id: str, num: int) -> None:
    done = sum(1 for p in job["pages"] if p is not None)
    _set(job, status="stopped", current=done)
    push_event(job_id, {"type": "status", "status": "stopped",
                        "message": f"OCR stopped ({done}/{num} pages done)",
                        "result": [p for p in job["pages"] if p is not None]})
    log.info("job %s: stopped, %d/%d pages kept", job_id, done, num)


# Per-adapter override keys accepted via the upload/retry API (form fields).
_ADAPTER_PARAM_KEYS: Dict[str, tuple] = {
    "unlimited": ("api_key", "base_url", "model"),
    "generic_openai": ("api_key", "base_url", "model", "prompt"),
    "tesseract": ("lang", "psm", "oem", "config", "tessdata_dir", "tess_cmd"),
}


def _make_adapter(name: str, adapter_kwargs: dict):
    """Build a fresh adapter (one per worker to avoid sharing mutable state).

    If runtime overrides were supplied, reconstruct the adapter with them so
    the change takes effect (the base factory returns a defaults-only instance).
    """
    import backend.sources.factory as factory_mod
    from backend.sources.generic_openai_adapter import GenericOpenAiAdapter
    from backend.sources.tesseract_adapter import TesseractAdapter
    from backend.sources.unlimited_ocr_adapter import UnlimitedOcrAdapter

    _CLS = {
        UnlimitedOcrAdapter.name: UnlimitedOcrAdapter,
        TesseractAdapter.name: TesseractAdapter,
        GenericOpenAiAdapter.name: GenericOpenAiAdapter,
    }
    adapter = factory_mod.get_adapter(name)
    cls = _CLS.get(name, type(adapter))
    if not adapter_kwargs:
        return adapter
    try:
        return cls(**adapter_kwargs)
    except TypeError:
        # Some override not accepted by this adapter's constructor — fall back
        # to the defaults-only instance rather than crash the job.
        log.warning("adapter %s: ignoring unexpected override keys %s",
                    name, sorted(adapter_kwargs))
        return adapter


def _ocr_pages_sequentially(job, job_id, adapter, page_specs, num, cancel):
    for spec in page_specs:
        if cancel.is_set():
            return
        _ocr_one_page_and_report(job, job_id, adapter, spec, num)


def _ocr_one_page_and_report(job, job_id, adapter, spec, num):
    i = spec["page_index"]
    log.debug("job %s: OCR page %d", job_id, i)
    ocr_page = adapter.recognize_pixels(spec["img_path"], spec["w"], spec["h"], i)
    update_page(job_id, i, page_to_dict(ocr_page))
    _bump_progress(job, job_id, num)


def _bump_progress(job, job_id, num):
    with _jobs_lock:
        job["current"] = sum(1 for p in job["pages"] if p is not None)
    push_event(job_id, {
        "type": "progress",
        "current": job["current"],
        "total": num,
        "message": f"OCR page {job['current']}/{num}",
    })


def _ocr_pages_parallel(job, job_id, adapter_kwargs, page_specs,
                        num, concurrency, already_done, cancel):
    """OCR pages concurrently; report progress as each page completes (unordered).

    `done_count` is seeded with `already_done` so that progress reflects the
    true total (e.g. retrying 1 page at 99% shows 99 -> 100, not 0 -> 1).

    Stop semantics: when cancel is set, we immediately abandon the thread pool
    — we do NOT wait for in-flight HTTP requests to finish.  The pool is shut
    down with wait=False so the `with` block doesn't block.
    """
    import concurrent.futures as cf

    done_count = already_done
    done_count_lock = threading.Lock()

    def _work(spec: dict):
        nonlocal done_count
        if cancel.is_set():
            return spec["page_index"], None
        try:
            worker = _make_adapter(job.get("adapter", "unlimited"), adapter_kwargs)
            log.debug("job %s: OCR page %d (parallel)", job_id, spec["page_index"])
            ocr_page = worker.recognize_pixels(spec["img_path"], spec["w"], spec["h"],
                                               spec["page_index"])
            if cancel.is_set():
                return spec["page_index"], None
            update_page(job_id, spec["page_index"], page_to_dict(ocr_page))
            with done_count_lock:
                done_count += 1
                cur = done_count
            _set(job, current=cur)  # sync job["current"] for status queries
            push_event(job_id, {
                "type": "progress",
                "current": cur,
                "total": num,
                "page_index": spec["page_index"],
                "message": f"OCR page {cur}/{num} (page {spec['page_index'] + 1})",
            })
            return spec["page_index"], None
        except Exception as exc:  # noqa: BLE001
            if cancel.is_set():
                return spec["page_index"], None
            log.warning("job %s: page %d failed: %s", job_id, spec["page_index"], exc)
            push_event(job_id, {
                "type": "error_page",
                "page_index": spec["page_index"],
                "message": str(exc),
            })
            return spec["page_index"], exc

    errors = {}
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ocr")
    futures = {pool.submit(_work, spec): spec["page_index"] for spec in page_specs}
    pending = set(futures)
    try:
        while pending:
            if cancel.is_set():
                # Cancel all not-yet-started futures; abandon in-flight ones.
                for f in pending:
                    f.cancel()
                log.info("job %s: cancel during parallel OCR, abandoning %d pending",
                         job_id, len(pending))
                return  # don't wait — stop_job already marked stopped
            done_set, pending = cf.wait(
                pending, timeout=0.3, return_when=cf.FIRST_COMPLETED)
            for f in done_set:
                idx, err = f.result()
                if err is not None:
                    errors[idx] = err
    finally:
        # wait=False: don't block on in-flight HTTP calls if we're cancelling.
        pool.shutdown(wait=not cancel.is_set(), cancel_futures=True)

    if cancel.is_set():
        return

    if errors:
        failed_pages = ", ".join(f"#{i + 1}" for i in sorted(errors))
        if len(errors) == len(page_specs):
            raise RuntimeError(
                f"OCR failed on all {len(errors)} attempted page(s): {failed_pages}. "
                f"Example error: {next(iter(errors.values()))}"
            )
        push_event(job_id, {"type": "warning",
                            "message": f"{len(errors)} page(s) failed: "
                                       f"{failed_pages}. Retry to fill them."})
        for idx, errmsg in errors.items():
            push_event(job_id, {"type": "error_page",
                                "page_index": idx, "message": str(errmsg)})


def embed_job(job_id: str, pages: List[dict], out_dir: Optional[Path] = None,
              embed_font=None) -> Path:
    """Embed the (possibly edited) page list into an embedded copy.

    Pages that are None (not yet OCR'd / failed) are silently skipped so the
    user can always download a partial result from whatever pages succeeded.
    ``embed_font`` is an optional backend.fonts.FontSpec used for the text layer.
    """
    job = get_job(job_id)
    if job is None:
        raise ValueError("Job not found")

    # Filter out None / empty entries — only embed pages that have OCR results.
    valid = [p for p in pages if p is not None and isinstance(p, dict)]
    if not valid:
        raise ValueError("No completed pages to embed")
    ocr_pages = [dict_to_page(p) for p in valid]
    log.info("job %s: embedding %d/%d pages (skipping %d incomplete) font=%s",
             job_id, len(ocr_pages), len(pages), len(pages) - len(valid),
             getattr(embed_font, "name", "builtin"))
    out_file, thumb = pdf_processing.embed_invisible_text(
        job["pdf_path"], ocr_pages, out_dir or pdf_processing.ensure_output_dir(),
        embed_font=embed_font)
    _set(job, status="embedded", embedded_path=str(out_file), thumb_path=str(thumb))
    log.info("job %s: embedded -> %s", job_id, out_file)
    return Path(out_file)