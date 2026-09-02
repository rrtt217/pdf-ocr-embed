"""OCR orchestration on OCRmyPDF: upload -> hOCR -> editable pages -> finalize.

The OCR core is OCRmyPDF (https://github.com/ocrmypdf/OCRmyPDF); the
``unlimited`` engine ships as the ``backend.ocrmypad`` plugin.  Job flow:

1. ``create_job`` stores the upload and opens a job.
2. ``run_ocr`` calls ``ocrmypdf._pdf_to_hocr`` in a worker thread: OCRmyPDF
   rasterizes the PDF, runs the plugin engine per page (with ``jobs``-way
   concurrency), and leaves per-page hOCR + block sidecars under
   ``work/<job_id>/hocr/``.
3. The WebUI edits pages (``update_page``): edits go back into the block
   sidecar and the page's hOCR file is regenerated from them.
4. ``embed_job`` calls ``ocrmypdf._hocr_to_ocr_pdf``: OCRmyPDF renders the
   (possibly edited) hOCR files into an invisible text layer, grafts it onto
   the original pages, and runs postprocessing (PDF/A, optimization).

In-memory job state is persisted to ``work/<job_id>/job.json`` and restored at
startup, so a restart keeps every job's hOCR work folder usable.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

import fitz  # PyMuPDF

from backend import page_store, pdf_processing
from backend.config import as_bool, redact_secrets, resolve
from backend.errors import UnavailableError

log = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
WORK_DIR = Path(__file__).resolve().parent.parent / "work"

# In-memory jobs: job_id -> job dict.
_JOBS: Dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Per-job SSE buffers (deque of messages) indexed by job id.
_STREAMS: Dict[str, Deque[dict]] = {}
_streams_lock = threading.Lock()

# The OCRmyPDF plugin package (the unlimited engine).
PLUGIN_PATH = Path(__file__).resolve().parent / "ocrmypad" / "__init__.py"


def plugin_path() -> str:
    """The ocrmypad plugin path passed to ocrmypdf ``plugins=``."""
    return str(PLUGIN_PATH)


def select_pages(num_pages: int, statuses, page_range=None, force: bool = False) -> list:
    """Return the 1-based page numbers to run for a job/pass.

    Pure helper (unit-testable, no I/O). ``statuses`` is a list where a truthy
    entry at index i means page i+1 already has a result.  ``page_range`` is a
    ``(start, end)`` pair, 1-based and inclusive; ``None`` means all pages.
    ``force=False`` skips already-done pages; ``force=True`` includes them.
    """
    num_pages = max(0, int(num_pages))
    if page_range is None:
        numbers = list(range(1, num_pages + 1))
    else:
        start = max(1, int(page_range[0]))
        end = min(num_pages, int(page_range[1]))
        if start > end:
            return []
        numbers = list(range(start, end + 1))
    if not force:
        numbers = [n for n in numbers if n - 1 >= len(statuses) or not statuses[n - 1]]
    return numbers


# --- job state ---------------------------------------------------------------

def _new_job(filename: str) -> dict:
    job_id = uuid.uuid4().hex[:12]
    return {
        "job_id": job_id,
        "filename": filename,
        "status": "queued",       # queued | running | done | stopped | error
        "pdf_path": "",
        "hocr_dir": "",
        "previews_dir": "",
        "embedded_path": "",
        "num_pages": 0,
        "pages_done": 0,
        "current": 0,
        "error": "",
        "created_at": "",
    }


def _job_dir(job_id: str) -> Path:
    return WORK_DIR / job_id


def create_job(filename: str, contents: bytes) -> dict:
    """Store an uploaded PDF and open a new job for it."""
    job = _new_job(filename or "upload.pdf")
    jdir = _job_dir(job["job_id"])
    (jdir / "hocr").mkdir(parents=True, exist_ok=True)
    pdf_path = UPLOAD_DIR / f"{job['job_id']}.pdf"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(contents)
    job["pdf_path"] = str(pdf_path)
    job["hocr_dir"] = str(jdir / "hocr")
    job["previews_dir"] = str(jdir / "previews")
    try:
        with fitz.open(pdf_path) as doc:
            job["num_pages"] = doc.page_count
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Cannot open uploaded PDF: {exc}") from exc
    import time
    job["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # Epoch seconds: the WebUI sorts the job list by this numeric field.
    job["created"] = time.time()
    with _jobs_lock:
        _JOBS[job["job_id"]] = job
    _push_event(job["job_id"], {"type": "status", "status": "queued",
                                "message": "Job created"})
    _persist(job)
    return job


def get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return _JOBS.get(job_id)


def all_jobs() -> List[dict]:
    with _jobs_lock:
        return list(_JOBS.values())


def _set(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _JOBS.get(job_id)
    if job is None:
        return
    for key, value in fields.items():
        job[key] = value


def _push_event(job_id: str, event: dict) -> None:
    with _streams_lock:
        _STREAMS.setdefault(job_id, deque(maxlen=200)).append(event)


def drain_events(job_id: str) -> list:
    with _streams_lock:
        buf = _STREAMS.get(job_id)
        if not buf:
            return []
        events = list(buf)
        buf.clear()
    for ev in events:
        if ev.get("message"):
            ev["message"] = redact_secrets(str(ev["message"]))
    return events


def _persist(job: dict) -> None:
    jdir = _job_dir(job["job_id"])
    try:
        jdir.mkdir(parents=True, exist_ok=True)
        (jdir / "job.json").write_text(
            json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.exception("persist job %s failed", job.get("job_id"))


def _persist_if_live(job_id: str) -> None:
    """Persist a job's state only while it is still in the registry.

    A clear during a running job must win over the worker thread: without
    this guard, ``run_ocr``/``embed_job`` re-persist the stale in-memory dict
    after ``clear_job`` popped it, recreating the work dir (the cleared task
    reappears in the WebUI job list on next restore).
    """
    fresh = get_job(job_id)
    if fresh is None:
        return
    _persist(fresh)


def restore_jobs() -> int:
    """Restore jobs persisted in work/<job_id>/job.json (called at startup).

    A job that was mid-OCR keeps its hOCR work folder: already-recognized
    pages survive and can be finalized without re-uploading.
    """
    restored = 0
    if not WORK_DIR.exists():
        return 0
    for jdir in sorted(WORK_DIR.iterdir()):
        state = jdir / "job.json"
        if not jdir.is_dir() or not state.exists():
            continue
        try:
            job = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("restore_jobs: skipping corrupt %s", state)
            continue
        job_id = job.get("job_id") or jdir.name
        job["job_id"] = job_id
        # A job interrupted mid-run shows as stopped, not running.
        if job.get("status") in ("running", "queued", "stopping"):
            job["status"] = "stopped"
            job.setdefault("error", "Interrupted by a server restart")
        with _jobs_lock:
            _JOBS[job_id] = job
        restored += 1
    if restored:
        log.info("restored %d job(s) from work/", restored)
    return restored


def clear_job(job_id: str) -> bool:
    """Fully remove a job: in-memory state and its work dir.

    The whole work dir (hOCR, sidecars, cancel flag) goes with it — the
    engine's filesystem channel is the only state to clean.
    """
    with _jobs_lock:
        job = _JOBS.pop(job_id, None)
    if job is None:
        return False
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    UPLOAD_DIR.joinpath(f"{job_id}.pdf").unlink(missing_ok=True)
    return True


def list_jobs() -> List[dict]:
    """Every job (running or finished), newest first.

    Each summary carries BOTH field-name sets: the pre-rebuild names the
    WebUI reads (`id`, `current`, `total`, `created`) and the current internal
    names (`job_id`, `pages_done`, `num_pages`, `created_at`).  Dropping the
    old aliases breaks every job card in the browser (`/api/ocr/stream/undefined`
    404s, clears fail) — the frontend is the compatibility contract here.
    """
    jobs = sorted(all_jobs(), key=lambda j: j.get("created") or 0,
                  reverse=True)
    return [{
        # current names
        "job_id": j["job_id"],
        "filename": j["filename"],
        "status": j["status"],
        "num_pages": j.get("num_pages", 0),
        "pages_done": j.get("pages_done", 0),
        "has_embedded": bool(j.get("embedded_path")),
        "created_at": j.get("created_at", ""),
        "error": j.get("error", ""),
        # pre-rebuild aliases the WebUI depends on
        "id": j["job_id"],
        "current": j.get("pages_done", 0),
        "total": j.get("num_pages", 0),
        "created": j.get("created") or 0,
    } for j in jobs]


# --- the OCRmyPDF pipeline ---------------------------------------------------

def _ocrmypdf_jobs() -> int:
    """Worker count for ocrmypdf (0 = auto -> ocrmypdf picks cpu count)."""
    try:
        return max(0, int(resolve().get("ocrmypdf_jobs") or 0))
    except (TypeError, ValueError):
        return 0


def _ocrmypdf_options(**overrides):
    """Build ocrmypdf keyword options from config + per-request overrides."""
    cfg = resolve()
    mode = str(overrides.get("mode") or cfg.get("ocrmypdf_mode") or "force")
    kwargs: dict = {
        "mode": mode,
        # use_threads is REQUIRED for the unlimited engine: the engine is
        # HTTP-bound (IO) and thread-based runs keep the progress registry in
        # this process.  Process-based runs would fork it away.
        "use_threads": True,
        "output_type": str(overrides.get("output_type")
                           or cfg.get("ocrmypdf_output_type") or "pdf"),
    }
    jobs = int(overrides.get("jobs") or 0) or _ocrmypdf_jobs()
    if jobs > 0:
        kwargs["jobs"] = jobs
    if as_bool(cfg.get("ocrmypdf_deskew")):
        kwargs["deskew"] = True
    if as_bool(cfg.get("ocrmypdf_clean")):
        kwargs["clean"] = True
    if as_bool(cfg.get("ocrmypdf_rotate_pages")):
        kwargs["rotate_pages"] = True
    language = overrides.get("language") or cfg.get("ocrmypdf_language")
    if language:
        kwargs["language"] = str(language)
    pages = overrides.get("pages")
    if pages:
        kwargs["pages"] = str(pages)
    return kwargs


def run_ocr(job_id: str, overrides: Optional[dict] = None) -> None:
    """Run the OCR phase for one job (worker-thread entry point).

    Calls ``ocrmypdf._pdf_to_hocr``: the engine (unlimited plugin or ocrmypdf's
    built-in Tesseract) runs per page and leaves hOCR files under the job's
    hOCR work folder.  Failures mark the job error'd; a user-requested stop
    marks it stopped with partial pages.

    "Retry remaining" semantics: when ``overrides['_page_selection']`` is set
    (computed by ``retry_job`` from the pages that don't have a result yet),
    only those pages are run — ``pages`` is passed to ocrmypdf as a
    comma-separated list.  A page selection that is empty means every selected
    page already has a result: the job is marked done without re-running OCR.
    """
    import ocrmypdf.api

    job = get_job(job_id)
    if job is None:
        return
    overrides = dict(overrides or {})
    options = _ocrmypdf_options(**overrides)
    total = int(job.get("num_pages") or 0)

    # Page selection (retry remaining): the selection wins over a raw pages
    # range — it already accounts for the range AND the pages that are done.
    selection = overrides.get("_page_selection")
    if selection is not None:
        if not selection:
            # Nothing to run: every selected page already has a result.
            _set(job_id, status="done",
                 pages_done=len(page_store.page_numbers(Path(job["hocr_dir"]))),
                 error="")
            _push_event(job_id, {"type": "status", "status": "done",
                                 "message": "All selected pages already done"})
            _persist_if_live(job_id)
            return
        if len(selection) < total:
            options["pages"] = ",".join(str(n) for n in sorted(selection))

    _set(job_id, status="running", current=0, error="")
    _push_event(job_id, {"type": "status", "status": "running",
                         "message": "OCR started (OCRmyPDF)"})
    _persist_if_live(job_id)

    try:
        ocrmypdf.api._pdf_to_hocr(
            Path(job["pdf_path"]),
            Path(job["hocr_dir"]),
            plugins=[plugin_path()],
            ocr_engine=overrides.get("ocr_engine")
            or resolve().get("ocr_engine") or "unlimited",
            **options,
        )
    except UnavailableError as exc:
        _set(job_id, status="error", error=str(exc))
        _push_event(job_id, {"type": "error", "message": str(exc)})
        _persist_if_live(job_id)
        return
    except Exception as exc:  # noqa: BLE001
        cancelled = page_store.is_cancelled(_job_dir(job_id))
        message = "OCR stopped by user" if cancelled else redact_secrets(str(exc))
        status = "stopped" if cancelled else "error"
        log.error("job %s: OCR phase failed: %s", job_id, message)
        # pages_done reflects the pages that actually have results on disk
        # (a partially completed run keeps its completed pages).
        _set(job_id, status=status, error=message,
             pages_done=len(page_store.page_numbers(Path(job["hocr_dir"]))))
        _push_event(job_id, {"type": "error" if status == "error" else "status",
                             "status": status, "message": message})
        _persist_if_live(job_id)
        return

    _set(job_id, status="done",
         pages_done=len(page_store.page_numbers(Path(job["hocr_dir"]))),
         error="")
    _push_event(job_id, {"type": "status", "status": "done",
                         "message": "OCR complete"})
    _persist_if_live(job_id)


def stop_job(job_id: str) -> bool:
    """Ask a running job to stop after its current page.

    The cancel flag is a plain file in the job dir; the engine polls for it
    per page (the only engine <-> service channel besides the work files).

    Engine support: the unlimited plugin checks the flag between pages, so a
    stop lands after the page in flight.  ocrmypdf's built-in Tesseract has
    no cancel hook — its run completes and the job ends up `done` with every
    page recognized; the SSE stream notes this in the terminal event so the
    WebUI does not promise a stop the engine cannot deliver.
    """
    job = get_job(job_id)
    if job is None or job.get("status") != "running":
        return False
    page_store.request_cancel(_job_dir(job_id))
    engine = "unlimited"
    try:
        engine = resolve().get("ocr_engine") or "unlimited"
    except Exception:  # noqa: BLE001
        pass
    cancellable = engine == "unlimited"
    _set(job_id, status="stopping")
    _push_event(job_id, {"type": "status", "status": "stopping",
                         "message": "Stopping after the current page..."
                         if cancellable else
                         "Stop requested, but the tesseract engine cannot "
                         "cancel mid-run — the run will complete."})
    return True


def retry_job(job_id: str, overrides: Optional[dict] = None,
              page_range=None, force: bool = False) -> bool:
    """Re-run OCR for a job without re-uploading (optionally a page range)."""
    job = get_job(job_id)
    if job is None:
        return False
    if job.get("status") in ("running", "stopping"):
        return False
    if not job.get("pdf_path") or not Path(job["pdf_path"]).exists():
        return False
    overrides = dict(overrides or {})
    if page_range is not None:
        start, end = int(page_range[0]), int(page_range[1])
        overrides["pages"] = f"{start}-{end}"
    overrides["_force"] = bool(force)
    overrides["_page_range"] = page_range
    overrides["_page_selection"] = select_pages(
        int(job.get("num_pages") or 0),
        _page_status_list(job_id),
        page_range=page_range, force=force)
    threading.Thread(target=run_ocr, args=(job_id, overrides),
                     daemon=True, name=f"ocr-{job_id}").start()
    return True


def _page_status_list(job_id: str) -> list:
    """Truthy-per-page list of pages that already have a block sidecar."""
    return [bool(p) for p in get_page_dicts(job_id)]


# --- editable pages ----------------------------------------------------------

def _embedded_path(job: dict) -> Path:
    """The finalize output path for a job: `<stem>_embedded.pdf`.

    Named after the SOURCE file (never hardcoded): re-running the same book
    keeps its previous result distinguishable, and the download filename
    (`GET /api/download/{job_id}.pdf` serves this file) carries the real
    document name.  When a previous result of the SAME job is still on disk
    under that name (e.g. an earlier finalize of the same upload), the job id
    disambiguates instead of silently overwriting it.
    """
    stem = (Path(job.get("filename") or "document.pdf").stem or "document")
    # Filesystem-hostile characters never reach the name.
    for ch in "/\\:*?\"<>|":
        stem = stem.replace(ch, "_")
    stem = stem.strip().rstrip(".") or "document"
    out = _job_dir(job["job_id"]) / f"{stem}_embedded.pdf"
    if out.exists():
        out = _job_dir(job["job_id"]) / f"{stem}_embedded_{job['job_id']}.pdf"
    return out


def get_page_dicts(job_id: str) -> List[Optional[dict]]:
    """Per-page dicts (None for pages without a result), 1-based indexed.

    Reads through the engine-agnostic page store: a page with only an hOCR
    file (e.g. ocrmypdf's built-in Tesseract, which writes no block sidecar)
    is derived from it and becomes editable exactly like an engine-native one.
    """
    pages: List[Optional[dict]] = []
    job = get_job(job_id)
    total = int((job or {}).get("num_pages") or 0)
    hdir = Path(job["hocr_dir"]) if job else None
    done = sorted(page_store.page_numbers(hdir)) if hdir else []
    for page_no in range(1, max(total, max(done, default=0)) + 1):
        if hdir is None or page_no not in done:
            pages.append(None)
            continue
        pages.append(page_store.load_page(hdir, page_no))
    return pages


def get_pages(job_id: str) -> List[dict]:
    """Compact list of completed page dicts (the WebUI /api/pages payload)."""
    return [p for p in get_page_dicts(job_id) if p is not None]


def update_page(job_id: str, page_index: int, payload: dict) -> int:
    """Store an edited page (0-based index) and regenerate its hOCR file.

    Edits go back into the block sidecar; the page's hOCR file is regenerated
    from the edited blocks so ``embed_job`` renders the edited text.  Works
    for EVERY engine: a page that only had an hOCR file gets its sidecar
    created here first (via ``page_store.load_page``), then edited.  Returns
    the completed page count.
    """
    job = get_job(job_id)
    if job is None:
        raise ValueError("Job not found")
    page_no = int(page_index) + 1
    hdir = Path(job["hocr_dir"])

    blocks = payload.get("blocks")
    if blocks is None:
        raise ValueError("payload must carry 'blocks'")

    # Load through the interchange layer (derives from hOCR when the engine
    # wrote no sidecar) so the page geometry / dpi survive the edit.
    page = page_store.load_page(hdir, page_no)
    if page is None:
        raise ValueError(f"Page {page_no} has no OCR result to edit")
    # Keep the page geometry: edits only touch blocks.
    page["blocks"] = blocks

    dpi = page_store.read_sidecar_dpi(hdir, page_no)
    hocr_w = int(page.get("width") or 0)
    hocr_h = int(page.get("height") or 0)
    if not hocr_w or not hocr_h:
        # A derived page without geometry: fall back to the source PDF page.
        try:
            with pdf_processing.open_pdf(job["pdf_path"]) as doc:
                pg = doc[max(0, page_no - 1)]
                zoom = 300.0 / 72.0
                hocr_w = int(round(pg.rect.width * zoom))
                hocr_h = int(round(pg.rect.height * zoom))
            page["width"], page["height"] = hocr_w, hocr_h
        except Exception:  # noqa: BLE001
            log.debug("geometry fallback failed for page %s", page_no,
                      exc_info=True)

    # Persist the edited sidecar, then regenerate the hOCR from the same
    # blocks (finalize renders the hOCR, so it must carry the edit).
    sidecar = page_store.sidecar_path(hdir, page_no)
    sidecar.write_text(
        json.dumps({"page": page, "dpi": dpi}, ensure_ascii=False),
        encoding="utf-8")
    hocr_file = page_store.hocr_path(hdir, page_no)
    hocr_file.write_text(
        page_store.blocks_to_hocr(hocr_w, hocr_h, blocks, dpi=dpi,
                                  ppageno=page_index),
        encoding="utf-8")
    _persist(job)
    return len([p for p in get_page_dicts(job_id) if p is not None])


# --- finalize (embed) --------------------------------------------------------

def embed_job(job_id: str, overrides: Optional[dict] = None,
              page_indices: Optional[list] = None) -> tuple[str, dict]:
    """Run ``ocrmypdf._hocr_to_ocr_pdf`` on the job's hOCR work folder.

    OCRmyPDF renders every page's (possibly edited) hOCR into an invisible
    text layer, grafts it onto the original pages and postprocesses the result
    (metadata, optional PDF/A, optional optimization).  Returns the output
    path and a small stats dict.  Works for EVERY engine: pages that only
    have an hOCR file (no sidecar) are used as-is.

    ``page_indices`` (partial embed): 0-based indices of the pages to include.
    The text layer is rendered ONLY for those pages (ocrmypdf ``pages``
    option); the remaining body pages arrive textless from the origin PDF and
    are dropped afterwards (pikepdf), so the partial document contains exactly
    the selected pages.  A partial result is named `<stem>_partial.pdf` and
    does NOT replace the job's full embedded output.
    """
    import ocrmypdf.api

    job = get_job(job_id)
    if job is None:
        raise ValueError("Job not found")
    if not page_store.has_results(Path(job["hocr_dir"])):
        raise ValueError("No OCR results to embed — run OCR first")

    partial = page_indices is not None
    wanted: list = []
    if partial:
        # Elements may be bare ints OR whole page dicts (the pre-rebuild
        # frontend sends the full page objects it got from /api/pages).
        def _index_of(item) -> int:
            if isinstance(item, dict):
                return int(item.get("page_index", -1))
            return int(item)
        wanted = sorted({_index_of(i) + 1 for i in page_indices})
        if not wanted or -1 in wanted:
            raise ValueError("Partial embed selected no valid pages")
        # A partial document only makes sense for pages that have results.
        done = set(page_store.page_numbers(Path(job["hocr_dir"])))
        missing = [n for n in wanted if n not in done]
        if missing:
            raise ValueError(
                f"Page(s) {','.join(str(n) for n in missing)} have no OCR "
                "result — run OCR first")

    # Ensure every recognized page has an hOCR file (edits regenerate theirs
    # in update_page; a sidecar without a matching hOCR — e.g. written by an
    # engine that emits no hOCR — is materialized from the sidecar blocks).
    _ensure_hocr_files(job_id)

    overrides = dict(overrides or {})
    cfg = resolve()
    output_path = _embedded_path(job)
    if partial:
        output_path = output_path.with_name(
            output_path.stem.replace("_embedded", "") + "_partial.pdf")

    embed_dir = Path(job["hocr_dir"])
    try:
        optimize = max(0, min(3, int(overrides.get("optimize")
                                    or cfg.get("ocrmypdf_optimize") or 0)))
    except (TypeError, ValueError):
        optimize = 0

    kwargs: dict = {
        "use_threads": True,
        "optimize": optimize,
        "output_type": str(overrides.get("output_type")
                           or cfg.get("ocrmypdf_output_type") or "pdf"),
    }
    jobs = int(overrides.get("jobs") or 0) or _ocrmypdf_jobs()
    if jobs > 0:
        kwargs["jobs"] = jobs

    _push_event(job_id, {"type": "status", "status": "embedding",
                         "message": "Embedding text layer (OCRmyPDF)..."})
    try:
        ocrmypdf.api._hocr_to_ocr_pdf(
            embed_dir,
            output_path,
            **({"pages": ",".join(str(n) for n in wanted)} if partial else {}),
            **kwargs,
        )
        if partial:
            # The pipeline keeps every origin-PDF body page (textless for
            # pages outside `pages`); a partial document drops them.
            _drop_pages_except(output_path, {n - 1 for n in wanted})
    except Exception as exc:  # noqa: BLE001
        message = redact_secrets(str(exc))
        log.error("job %s: finalize failed: %s", job_id, message)
        _push_event(job_id, {"type": "error", "message": message})
        raise

    if not partial:
        # A partial result is NOT the job's embedded output: the download
        # endpoint and /api/jobs keep pointing at the latest FULL embed.
        _set(job_id, embedded_path=str(output_path))
        _persist_if_live(job_id)
    _push_event(job_id, {"type": "status", "status": "embedded",
                         "message": "PDF ready"})
    stats = {"optimize": optimize,
             "output_type": kwargs["output_type"],
             "pages": len(wanted) if partial
             else len(page_store.page_numbers(Path(job["hocr_dir"]))),
             "partial": partial}
    return str(output_path), stats


def _ensure_hocr_files(job_id: str) -> None:
    """Materialize any missing per-page hOCR from its block sidecar.

    Engines that write hOCR natively (Tesseract, the unlimited plugin) never
    hit this; a sidecar without a matching hOCR (an engine that emits only
    the normalized sidecar) gets its hOCR generated from the sidecar blocks
    so finalize can render it.
    """
    job = get_job(job_id)
    if job is None:
        return
    hdir = Path(job["hocr_dir"])
    for page_no in page_store.page_numbers(hdir):
        if page_store.hocr_path(hdir, page_no).exists():
            continue
        page = page_store.load_page(hdir, page_no)
        if page is None:
            continue
        try:
            dpi = page_store.read_sidecar_dpi(hdir, page_no)
            page_store.hocr_path(hdir, page_no).write_text(
                page_store.blocks_to_hocr(int(page.get("width") or 0),
                                          int(page.get("height") or 0),
                                          page.get("blocks", []),
                                          dpi=dpi, ppageno=page_no - 1),
                encoding="utf-8")
        except (OSError, ValueError):
            log.exception("ensure_hocr_files: page %s failed", page_no)


# --- page previews -----------------------------------------------------------

def page_preview_path(job_id: str, page_index: int) -> Optional[Path]:
    """The rendered preview PNG for a page, when it already exists."""
    job = get_job(job_id)
    if job is None:
        return None
    path = Path(job["previews_dir"]) / f"{page_index + 1:06d}.png"
    return path if path.exists() else None


def ensure_page_image(job_id: str, page_index: int) -> Optional[Path]:
    """Render a page preview from the uploaded PDF (lazily, on first ask)."""
    job = get_job(job_id)
    if job is None:
        return None
    pdf_path = job.get("pdf_path")
    if not pdf_path or not Path(pdf_path).exists():
        return None
    previews_dir = Path(job["previews_dir"])
    previews_dir.mkdir(parents=True, exist_ok=True)
    out = previews_dir / f"{page_index + 1:06d}.png"
    try:
        pdf_processing.render_page_png(pdf_path, page_index, out)
    except Exception as exc:  # noqa: BLE001
        log.error("page preview render failed for %s/%s: %s",
                  job_id, page_index, exc)
        return None
    return out


def _drop_pages_except(pdf_path: Path, keep_0based: set) -> None:
    """Delete every page except ``keep_0based`` from a PDF (in place).

    Used by the partial embed: ocrmypdf's hOCR-to-PDF pipeline renders text
    only for the pages in ``pages`` but keeps every origin-PDF body page, so
    a partial document needs the un-selected (textless) pages removed.
    """
    import pikepdf

    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        for i in reversed(range(len(pdf.pages))):
            if i not in keep_0based:
                del pdf.pages[i]
        pdf.save(pdf_path)
