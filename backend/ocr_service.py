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

from backend import pdf_processing
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
    """Fully remove a job: in-memory state and its work dir."""
    with _jobs_lock:
        job = _JOBS.pop(job_id, None)
    if job is None:
        return False
    from backend.ocrmypad import progress as progress_mod
    progress_mod.pop(job_id)
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    UPLOAD_DIR.joinpath(f"{job_id}.pdf").unlink(missing_ok=True)
    return True


def list_jobs() -> List[dict]:
    """Every job (running or finished), newest first."""
    jobs = sorted(all_jobs(), key=lambda j: j.get("created_at") or "",
                  reverse=True)
    return [{
        "job_id": j["job_id"],
        "filename": j["filename"],
        "status": j["status"],
        "num_pages": j.get("num_pages", 0),
        "pages_done": j.get("pages_done", 0),
        "has_embedded": bool(j.get("embedded_path")),
        "created_at": j.get("created_at", ""),
        "error": j.get("error", ""),
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

    Calls ``ocrmypdf._pdf_to_hocr``: the plugin engine runs per page and leaves
    hOCR + block sidecars under the job's hOCR work folder.  Failures mark the
    job error'd; a user-requested stop marks it stopped with partial pages.
    """
    import ocrmypdf.api
    from backend.ocrmypad import progress as progress_mod

    job = get_job(job_id)
    if job is None:
        return
    overrides = dict(overrides or {})
    options = _ocrmypdf_options(**overrides)
    total = int(job.get("num_pages") or 0)

    progress_mod.reset(job_id, total)
    _set(job_id, status="running", pages_done=0, current=0, error="")
    _push_event(job_id, {"type": "status", "status": "running",
                         "message": "OCR started (OCRmyPDF)"})
    _persist(get_job(job_id) or job)

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
        _persist(get_job(job_id) or job)
        return
    except Exception as exc:  # noqa: BLE001
        cancelled = progress_mod.snapshot(job_id).get("cancel")
        message = "OCR stopped by user" if cancelled else redact_secrets(str(exc))
        status = "stopped" if cancelled else "error"
        log.error("job %s: OCR phase failed: %s", job_id, message)
        _set(job_id, status=status, error=message)
        _push_event(job_id, {"type": "error" if status == "error" else "status",
                             "status": status, "message": message})
        _persist(get_job(job_id) or job)
        return

    progress_mod.reset(job_id, total)
    _set(job_id, status="done", error="")
    _push_event(job_id, {"type": "status", "status": "done",
                         "message": "OCR complete"})
    _persist(get_job(job_id) or job)


def stop_job(job_id: str) -> bool:
    """Ask a running job to stop after its current page."""
    job = get_job(job_id)
    if job is None or job.get("status") != "running":
        return False
    from backend.ocrmypad import progress as progress_mod
    progress_mod.request_cancel(job_id)
    _set(job_id, status="stopping")
    _push_event(job_id, {"type": "status", "status": "stopping",
                         "message": "Stopping after the current page..."})
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

def _block_sidecars(job_id: str) -> List[Path]:
    """The job's block sidecar files (000001_ocr_hocr.blocks.json, sorted)."""
    hdir = Path(get_job(job_id)["hocr_dir"]) if get_job(job_id) else None
    if hdir is None or not hdir.exists():
        return []
    return sorted(hdir.glob("*_ocr_hocr.blocks.json"))


def _hocr_files(job_id: str) -> List[Path]:
    """The job's hOCR files (000001_ocr_hocr.hocr, sorted)."""
    job = get_job(job_id)
    if job is None:
        return []
    hdir = Path(job["hocr_dir"])
    if not hdir.exists():
        return []
    return sorted(hdir.glob("*_ocr_hocr.hocr"))


def _sidecar_path(job_id: str, page_no: int) -> Optional[Path]:
    """The sidecar for a 1-based page number, or None when absent."""
    hdir = get_job(job_id) and Path(get_job(job_id)["hocr_dir"])
    if not hdir:
        return None
    path = hdir / f"{page_no:06d}_ocr_hocr.blocks.json"
    return path if path.exists() else None


def _page_name(page_no: int) -> str:
    return f"{page_no:06d}_ocr_hocr"


def get_page_dicts(job_id: str) -> List[Optional[dict]]:
    """Per-page dicts (None for pages without a result), 1-based indexed."""
    pages: List[Optional[dict]] = []
    job = get_job(job_id)
    total = int((job or {}).get("num_pages") or 0)
    for page_no in range(1, max(total, len(_block_sidecars(job_id))) + 1):
        path = _sidecar_path(job_id, page_no)
        if path is None:
            pages.append(None)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pages.append(data.get("page") or data)
        except (OSError, ValueError):
            pages.append(None)
    return pages


def get_pages(job_id: str) -> List[dict]:
    """Compact list of completed page dicts (the WebUI /api/pages payload)."""
    return [p for p in get_page_dicts(job_id) if p is not None]


def update_page(job_id: str, page_index: int, payload: dict) -> int:
    """Store an edited page (0-based index) and regenerate its hOCR file.

    Edits go back into the block sidecar; the page's hOCR file is regenerated
    from the sidecar blocks so ``embed_job`` renders the edited text.  Returns
    the completed page count.
    """
    job = get_job(job_id)
    if job is None:
        raise ValueError("Job not found")
    page_no = int(page_index) + 1
    path = _sidecar_path(job_id, page_no)
    if path is None:
        raise ValueError(f"Page {page_no} has no OCR result to edit")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read page {page_no}: {exc}") from exc

    page = data.get("page") or data
    blocks = payload.get("blocks")
    if blocks is None:
        raise ValueError("payload must carry 'blocks'")
    # Keep the page geometry: edits only touch blocks.
    page["blocks"] = blocks
    data["page"] = page

    from backend.ocrmypad import parser as parser_mod
    parsed_blocks = [parser_mod.Block.from_dict(b) for b in blocks]
    for block in parsed_blocks:
        if not block.lines and block.text.strip():
            block.lines = parser_mod.split_block_lines(block.text)
    dpi = float(data.get("dpi") or 300.0)

    hdir = Path(job["hocr_dir"])
    sidecar_tmp = path.with_suffix(".json.tmp")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    sidecar_tmp.unlink(missing_ok=True)
    # Regenerate the hOCR from the edited blocks so finalize renders them.
    hocr_path = hdir / f"{_page_name(page_no)}.hocr"
    hocr_path.write_text(
        parser_mod.blocks_to_hocr(int(page.get("width") or 0),
                                  int(page.get("height") or 0),
                                  parsed_blocks, dpi=dpi,
                                  ppageno=page_no),
        encoding="utf-8")
    _persist(job)
    return len([p for p in get_page_dicts(job_id) if p is not None])


# --- finalize (embed) --------------------------------------------------------

def embed_job(job_id: str, overrides: Optional[dict] = None) -> tuple[str, dict]:
    """Run ``ocrmypdf._hocr_to_ocr_pdf`` on the job's hOCR work folder.

    OCRmyPDF renders every page's (possibly edited) hOCR into an invisible
    text layer, grafts it onto the original pages and postprocesses the result
    (metadata, optional PDF/A, optional optimization).  Returns the output
    path and a small stats dict.
    """
    import ocrmypdf.api

    job = get_job(job_id)
    if job is None:
        raise ValueError("Job not found")
    if not _block_sidecars(job_id):
        raise ValueError("No OCR results to embed — run OCR first")

    # Ensure every recognized page has an hOCR file (edits regenerate theirs
    # in update_page; a missing file means a sidecar without a matching hOCR).
    _ensure_hocr_files(job_id)

    overrides = dict(overrides or {})
    cfg = resolve()
    output_path = Path(job["hocr_dir"]).parent / "embedded.pdf"
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
            Path(job["hocr_dir"]),
            output_path,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        message = redact_secrets(str(exc))
        log.error("job %s: finalize failed: %s", job_id, message)
        _push_event(job_id, {"type": "error", "message": message})
        raise

    _set(job_id, embedded_path=str(output_path))
    _persist(get_job(job_id) or job)
    _push_event(job_id, {"type": "status", "status": "embedded",
                         "message": "PDF ready"})
    stats = {"optimize": optimize,
             "output_type": kwargs["output_type"],
             "pages": len(_block_sidecars(job_id))}
    return str(output_path), stats


def _ensure_hocr_files(job_id: str) -> None:
    """Regenerate any missing per-page hOCR from its block sidecar."""
    job = get_job(job_id)
    if job is None:
        return
    from backend.ocrmypad import parser as parser_mod
    hdir = Path(job["hocr_dir"])
    for sidecar in _block_sidecars(job_id):
        page_no = sidecar.name.split("_", 1)[0]
        hocr_path = hdir / f"{page_no}_ocr_hocr.hocr"
        if hocr_path.exists():
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            page = data.get("page") or data
            blocks = [parser_mod.Block.from_dict(b) for b in page.get("blocks", [])]
            for block in blocks:
                if not block.lines and block.text.strip():
                    block.lines = parser_mod.split_block_lines(block.text)
            hocr_path.write_text(
                parser_mod.blocks_to_hocr(int(page.get("width") or 0),
                                          int(page.get("height") or 0),
                                          blocks,
                                          dpi=float(data.get("dpi") or 300.0),
                                          ppageno=int(page_no) - 1),
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
