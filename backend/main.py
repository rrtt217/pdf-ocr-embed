"""FastAPI REST server for PDF OCR Embed (OCRmyPDF core).

Endpoints:
  POST /api/settings            save or read provider config (masked)
  POST /api/ocr/upload          upload one or more PDFs -> background OCR -> job id(s)
  GET  /api/ocr/zip?jobs=...    download selected jobs' embedded PDFs as a ZIP
  GET  /api/ocr/stream/{job_id} SSE stream of progress/status events
  GET  /api/pages/{job_id}/{i}/image   page preview image
  GET  /api/pages/{job_id}      get all page OCR data
  POST /api/pages/{job_id}/{i}  update an editable page (optional)
  POST /api/embed/{job_id}      finalize (possibly edited) pages -> *_embedded.pdf
  GET  /api/validation/{job_id} compare embedded text with OCR source (report)
  GET  /api/download/{job_id}.pdf   download embedded result

The OCR core is OCRmyPDF; the unlimited engine ships as the
``backend.ocrmypad`` plugin.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from backend import batch
from backend import cleanup as cleanup_mod
from backend import config, ocr_service, validation
from backend.logging_config import recent_logs, setup_logging

setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Bring back jobs persisted in work/<job_id>/job.json so a restart
    # resumes the task list (completed pages / embeds survive for retry).
    ocr_service.restore_jobs()
    # Keep orphaned temp files from accumulating: periodically delete
    # unreferenced files older than cleanup_max_age_hours.
    cleanup_mod.start_background_cleanup()
    # Push the effective config into the OCR engine plugin — the plugin never
    # reads backend.config itself; the host initializes it here (and again
    # whenever the settings change, see save_settings).
    from backend.ocrmypad import settings as ocrmypad_settings
    ocrmypad_settings.configure(config.resolve())
    yield
    cleanup_mod.stop_background_cleanup()


app = FastAPI(title="PDF OCR Embed", version="1.0.0", lifespan=lifespan)

# Served frontend lives in ../frontend relative to this package dir.
BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SettingsModel(BaseModel):
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    # Engine selection + ocrmypdf pipeline knobs.
    ocr_engine: Optional[str] = None
    ocrmypdf_mode: Optional[str] = None
    ocrmypdf_jobs: Optional[str] = None
    ocrmypdf_optimize: Optional[str] = None
    ocrmypdf_output_type: Optional[str] = None
    ocrmypdf_language: Optional[str] = None
    ocrmypdf_deskew: Optional[bool] = None
    ocrmypdf_clean: Optional[bool] = None
    ocrmypdf_rotate_pages: Optional[bool] = None


class EmbedModel(BaseModel):
    job_id: str
    ocr_engine: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    # Output options (applied by ocrmypdf's finalize stage).
    optimize: Optional[int] = None     # 0..3
    output_type: Optional[str] = None  # pdf | pdfa
    # Partial embed: the 0-based page indices to include (None = all done pages).
    pages: Optional[list] = None
    # Partial embed: the 0-based page indices to include (None = all done pages).
    pages: Optional[list] = None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    idx = FRONTEND_DIR / "index.html"
    if idx.exists():
        return idx.read_text(encoding="utf-8")
    raise HTTPException(status_code=404, detail="Frontend not built")


@app.get("/api/health")
def health() -> dict:
    from backend.ocrmypad.unlimited_engine import UnlimitedOcrEngine
    return {
        "status": "ok",
        "adapters": ["unlimited"],
        "engines": {
            "unlimited": str(UnlimitedOcrEngine()),
            "tesseract": "ocrmypdf built-in Tesseract",
            "none": "no OCR",
        },
    }


@app.get("/api/logs")
def get_logs(n: int = 200) -> dict:
    """Return recent backend log lines (for the WebUI debug panel)."""
    return {"lines": recent_logs(min(max(n, 1), 1000))}


@app.get("/api/cleanup")
def cleanup_status() -> dict:
    """Inventory of unreferenced temp files + the current cleanup config.

    Items become "ready" once they are unreferenced AND older than
    ``max_age_hours``; files still used by a live job are always protected.
    """
    data = cleanup_mod.inventory()
    return {
        "config": {
            "max_age_hours": cleanup_mod.max_age_hours(),
            "interval_hours": cleanup_mod.interval_hours(),
            "auto_cleanup_enabled": True,
        },
        **data,
    }


class CleanupModel(BaseModel):
    older_than_hours: Optional[float] = None  # age limit; defaults to config
    force: bool = False   # ignore the age rule (referenced files are still kept)
    dry_run: bool = False  # preview what would be deleted, without deleting


@app.post("/api/cleanup/run")
def run_cleanup(payload: CleanupModel) -> dict:
    """Delete unreferenced temp files (or preview them with ``dry_run``)."""
    return cleanup_mod.cleanup(
        age_limit=payload.older_than_hours,
        force=payload.force,
        dry_run=payload.dry_run,
    )




@app.get("/api/settings")
def get_settings() -> dict:
    return config.get_effective_settings()


@app.post("/api/settings")
def save_settings(payload: SettingsModel) -> dict:
    data = {}
    if payload.api_key is not None:
        data["api_key"] = payload.api_key
    for key in ("provider", "base_url", "model", "ocr_engine",
                "ocrmypdf_mode", "ocrmypdf_jobs", "ocrmypdf_optimize",
                "ocrmypdf_output_type", "ocrmypdf_language"):
        value = getattr(payload, key, None)
        if value is not None:
            data[key] = value
    for key in ("ocrmypdf_deskew", "ocrmypdf_clean", "ocrmypdf_rotate_pages"):
        value = getattr(payload, key, None)
        if value is not None:
            data[key] = value
    if data:
        # Persist via config.save (handles masked-key preservation, and only
        # writes the fields actually present in the payload).
        result = config.save(data)
        # Keep the engine plugin's injected config in sync with a WebUI save
        # (same host-initializes-plugin contract as the startup push above).
        from backend.ocrmypad import settings as ocrmypad_settings
        ocrmypad_settings.configure(config.resolve())
        return result
    # Read-only display mode.
    return config.get_effective_settings()


@app.post("/api/ocr/upload")
async def upload_pdf(
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    ocr_engine: Optional[str] = Form("unlimited"),
    concurrency: Optional[int] = Form(None),
    base_url: Optional[str] = Form(None),
    api_key: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    lang: Optional[str] = Form(None),
) -> dict:
    """Upload one or more PDFs; each file becomes its own OCR job.

    Both a single ``file`` field (legacy clients) and a repeated ``files``
    field (batch upload) are accepted; the two are merged and deduplicated.
    Every file is read into memory, gets its own job (id, card, SSE stream,
    persistence) and is OCR'd by OCRmyPDF with the shared plugin engine —
    there is no separate queue manager.

    ``concurrency`` (optional) maps to the OCRmyPDF worker count for this job.
    ``lang`` (optional) is the OCR language for engines that use one (e.g.
    Tesseract: ``chi_sim+eng``); it overrides the persisted
    ``ocrmypdf_language`` for this run.
    """
    uploads: List[UploadFile] = []
    seen: set = set()
    for uf in list(files or ()) + ([file] if file is not None else []):
        if id(uf) in seen:  # same file supplied via both fields — keep once
            continue
        seen.add(id(uf))
        uploads.append(uf)
    if not uploads:
        raise HTTPException(status_code=400, detail="No file uploaded")

    extra = {}
    for k, v in (("ocr_engine", ocr_engine), ("base_url", base_url),
                 ("api_key", api_key), ("model", model),
                 ("language", lang)):
        if v is not None and v != "":
            extra[k] = v
    if concurrency is not None and int(concurrency) > 0:
        extra["jobs"] = int(concurrency)

    loop = asyncio.get_running_loop()
    jobs = []
    for uf in uploads:
        contents = await uf.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Empty file")
        try:
            job = ocr_service.create_job(uf.filename or "upload.pdf", contents)
        except ValueError as exc:
            # Unreadable / non-PDF bytes: a client error, not a 500.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        loop.run_in_executor(
            None, ocr_service.run_ocr, job["job_id"], extra or None)
        log.info("upload job %s: %s (engine=%s)", job["job_id"],
                 job["filename"], extra.get("ocr_engine") or "unlimited")
        jobs.append({
            "job_id": job["job_id"],
            "filename": job["filename"],
            "status": "running",
        })

    result: dict = {"jobs": jobs,
                    "concurrency": int(concurrency or 0) or None}
    if len(jobs) == 1:
        # Backward-compatible single-file shape for older frontend clients.
        result.update({
            "job_id": jobs[0]["job_id"],
            "filename": jobs[0]["filename"],
            "status": "running",
        })
    else:
        result["count"] = len(jobs)
    return result


@app.get("/api/ocr/zip")
def zip_download(jobs: str):
    """Download the embedded PDFs of selected jobs as one ZIP archive (#10).

    ``jobs`` is a comma-separated list of job ids.  Only jobs that already
    have an embedded output on disk are included; a 404 is returned when none
    of the requested jobs have results.  The archive is built on disk member
    by member (each embedded PDF streamed straight from disk, so the archive
    is never held in RAM) and served back as a streaming ``FileResponse``; the
    temporary archive is deleted in the background once the response is sent.
    """
    job_ids = [j.strip() for j in jobs.split(",") if j.strip()]
    if not job_ids:
        raise HTTPException(status_code=400, detail="No job ids provided")

    entries = batch.collect_embedded(job_ids)
    if not entries:
        raise HTTPException(
            status_code=404,
            detail="None of the requested jobs have an embedded result yet")
    if len(entries) < len(job_ids):
        log.info("zip: %d job(s) requested, %d have embedded results",
                 len(job_ids), len(entries))

    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        batch.build_zip(tmp_path, entries)
    except Exception:  # noqa: BLE001
        os.unlink(tmp_path)
        raise
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=batch.default_zip_name(),
        background=BackgroundTask(os.unlink, tmp_path),
    )


@app.post("/api/ocr/retry/{job_id}")
async def retry_ocr(
    job_id: str,
    ocr_engine: Optional[str] = Form("unlimited"),
    concurrency: Optional[int] = Form(None),
    base_url: Optional[str] = Form(None),
    api_key: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    lang: Optional[str] = Form(None),
    page_start: Optional[int] = Form(None),
    page_end: Optional[int] = Form(None),
    force: Optional[bool] = Form(False),
) -> dict:
    """Re-run OCR on an already-uploaded job without re-uploading the PDF.

    All fields are optional form fields so old clients keep working:
      - ocr_engine / base_url / api_key / model / concurrency / lang — as before.
      - page_start / page_end: a 1-based inclusive page range to run
        (both optional; e.g. page_start=1,page_end=20 runs pages 1..20).
      - force: boolean, default false — when true, already-successful pages in
        the selected range are re-run too (A/B testing after switching
        engine/settings).
    """
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Precise refusals: a retry on a *running* job was previously reported as
    # "missing file" even though the source PDF is on disk (the job's OCR
    # thread simply had not finished).  Surface the real reason.
    if job.get("status") in ("running", "stopping"):
        raise HTTPException(
            status_code=409,
            detail="Job is still running — stop it or wait for it to finish "
                   "before retrying")
    extra = {}
    for k, v in (("ocr_engine", ocr_engine), ("base_url", base_url),
                 ("api_key", api_key), ("model", model),
                 ("language", lang)):
        if v is not None and v != "":
            extra[k] = v
    if concurrency is not None and int(concurrency) > 0:
        extra["jobs"] = int(concurrency)
    page_range = None
    if page_start is not None or page_end is not None:
        page_range = (page_start if page_start is not None else 1,
                      page_end if page_end is not None else job.get("num_pages", 0))
    ok = ocr_service.retry_job(
        job_id, overrides=extra or None,
        page_range=page_range, force=bool(force))
    if not ok:
        # retry_job refuses running/stopping (already caught above) and a
        # missing source PDF; report that specific reason, not a generic one.
        if not job.get("pdf_path") or not Path(job["pdf_path"]).exists():
            raise HTTPException(
                status_code=409,
                detail="Job cannot be retried (missing source PDF file)")
        raise HTTPException(status_code=409, detail="Job cannot be retried")
    log.info("retry scheduled for job %s (range=%s, force=%s)",
             job_id, page_range, force)
    return {"job_id": job_id, "filename": job["filename"], "status": "retrying"}


@app.post("/api/ocr/stop/{job_id}")
def stop_ocr(job_id: str) -> dict:
    """Stop a running OCR job. Completed pages are kept for partial download."""
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    ok = ocr_service.stop_job(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job cannot be stopped")
    return {"job_id": job_id, "status": "stopping",
            "current": job["current"], "total": job["num_pages"]}


@app.post("/api/ocr/clear/{job_id}")
def clear_ocr(job_id: str) -> dict:
    """Fully remove a job: in-memory state, its work dir and embedded output."""
    if not ocr_service.clear_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "job_id": job_id}


@app.get("/api/jobs")
def jobs_list() -> dict:
    """List every OCR job on the server (running or finished), newest first."""
    return {"jobs": ocr_service.list_jobs()}


@app.get("/api/ocr/stream/{job_id}")
async def stream(job_id: str):
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def gen():
        # First flush any already-buffered events.
        for ev in ocr_service.drain_events(job_id):
            yield _sse(ev)
        # Persists across loop iterations: without this, a terminal event read
        # as "running" in one iteration (drained) is re-sent by the fallback
        # branch in the next one — the client gets "done" twice.
        terminal_delivered = False
        delivered_pages: set = set()
        while True:
            cur = ocr_service.get_job(job_id)
            if cur is None:
                break
            status = cur["status"]
            # Drain buffered events BEFORE checking the terminal status.  A
            # terminal "done"/"stopped" event is pushed into the buffer before
            # the job status flips, so it must be delivered — otherwise the
            # stream closes without ever sending it and the browser fires
            # EventSource.onerror ("Connection to server lost") even though the
            # task actually completed successfully.
            events = ocr_service.drain_events(job_id)
            for ev in events:
                yield _sse(ev)
                if ev.get("type") == "status" and ev.get("status") in ("done", "stopped", "embedded"):
                    terminal_delivered = True

            # Per-page progress: pages that already have a result on disk
            # (hOCR/sidecar — engine-agnostic) stream as progress events.
            from backend import page_store
            hdir = cur.get("hocr_dir")
            total = cur.get("num_pages") or 0
            if hdir:
                done = page_store.page_numbers(Path(hdir))
                for page_no in done:
                    if page_no in delivered_pages:
                        continue
                    delivered_pages.add(page_no)
                    yield _sse({
                        "type": "progress",
                        "current": page_no,
                        "total": total,
                        "pages_done": len(done),
                        "page_index": page_no - 1,
                    })

            if status in ("done", "embedded"):
                # Guarantee the "done" status reaches the client even if the
                # status flipped before its buffered event was pushed.
                if not terminal_delivered:
                    yield _sse({
                        "type": "status",
                        "status": "done",
                        "message": "OCR complete",
                        "result": ocr_service.get_pages(job_id),
                    })
                break
            if status == "stopped":
                if not terminal_delivered:
                    yield _sse({
                        "type": "status", "status": "stopped",
                        "message": cur.get("error") or "OCR stopped",
                        "result": ocr_service.get_pages(job_id),
                    })
                break
            if status == "error":
                if not any(e.get("type") == "error" for e in events):
                    yield _sse({"type": "error", "message": cur.get("error", "OCR failed")})
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/api/pages/{job_id}")
def get_pages(job_id: str) -> dict:
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Filter out None (not-yet-done / failed) entries so the frontend gets a
    # compact list of completed pages keyed by their `page_index` field.
    pages = [p for p in ocr_service.get_pages(job_id) if p is not None]
    return {"status": job["status"], "pages": pages, "total": job["num_pages"],
            "has_embedded": bool(job.get("embedded_path"))}


@app.post("/api/pages/{job_id}/{page_index}")
def update_page(job_id: str, page_index: int, payload: dict) -> dict:
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Bound the index: out-of-range would silently pad the pages list
    # (memory growth + persisted garbage) or corrupt pages[-1]; the frontend
    # only ever addresses real pages (0-based page_index from /api/pages).
    max_index = max(int(job.get("num_pages") or 0),
                    len(ocr_service.get_pages(job_id)))
    if not 0 <= page_index < max_index:
        raise HTTPException(
            status_code=400,
            detail=f"page_index out of range: {page_index} (job has "
                   f"{max_index} page(s))")
    try:
        count = ocr_service.update_page(job_id, page_index, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "page_count": count}


@app.get("/api/pages/{job_id}/{page_index}/image")
def page_image(job_id: str, page_index: int):
    # Cache-hit pages skip the pre-render phase; render their preview lazily
    # the first time the WebUI asks for it.
    path = ocr_service.page_preview_path(job_id, page_index)
    if path is None:
        path = ocr_service.ensure_page_image(job_id, page_index)
    if path is None:
        raise HTTPException(status_code=404, detail="Page image not found")
    return FileResponse(path, media_type="image/png")


@app.post("/api/embed/{job_id}")
def embed(job_id: str, payload: EmbedModel):
    if ocr_service.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Finalize options from the payload.  A partial embed (`pages`) renders
    # only the selected pages' text layer: those pages' hOCR files are staged
    # into a temporary work folder, so the job's full hOCR set is untouched
    # for later full embeds.
    overrides = {}
    if payload.optimize is not None:
        overrides["optimize"] = payload.optimize
    if payload.output_type:
        overrides["output_type"] = payload.output_type
    try:
        out_path, stats = ocr_service.embed_job(
            job_id, overrides or None, page_indices=payload.pages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    # Post-embed validation: compare the freshly baked output against the
    # pages that were embedded.  Never fails the embed — a broken report is
    # surfaced as ok:false instead.
    report = _build_embed_report(job_id, Path(out_path), payload.pages)
    return {
        "status": "embedded",
        "filename": Path(out_path).name,
        "url": f"/api/download/{job_id}.pdf",
        "images": stats,
        "report": report,
    }


def _build_embed_report(job_id: str, out_path: Path,
                        page_indices: Optional[list] = None) -> dict:
    """Best-effort report for the POST /api/embed response (never raises)."""
    from backend.models import dict_to_page
    done = [p for p in ocr_service.get_pages(job_id) if isinstance(p, dict)]
    if page_indices is not None:
        # Elements may be bare ints OR whole page dicts (the pre-rebuild
        # frontend sends full page objects).
        wanted = set()
        for item in page_indices:
            idx = item.get("page_index") if isinstance(item, dict) else item
            try:
                wanted.add(int(idx))
            except (TypeError, ValueError):
                continue
        done = [p for p in done if p.get("page_index") in wanted]
    pages = [dict_to_page(p) for p in done]
    if not pages:
        return {"ok": False, "error": "no embeddable pages to validate"}
    try:
        return validation.build_report(str(out_path), pages)
    except Exception as exc:  # noqa: BLE001
        log.exception("validation after embed failed")
        return {"ok": False, "error": str(exc)}


@app.get("/api/download/{job_id}.pdf")
def download(job_id: str):
    job = ocr_service.get_job(job_id)
    if job is None or not job.get("embedded_path"):
        raise HTTPException(status_code=404, detail="No embedded PDF yet")
    path = job["embedded_path"]
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail="File missing")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=Path(path).name,
    )


@app.get("/api/validation/{job_id}")
def validation_report(job_id: str):
    """Re-validate an embedded output on demand: extract its text with
    PyMuPDF and compare it against the stored OCR pages.

    Returns the same report shape as the one baked into the embed response
    (``{"ok", "generated_at", "threshold", "summary", "pages"}``).  404 when
    the job or its embedded output is missing, 409 when the file cannot be
    opened/extracted (extraction failure -> a report with ``ok: false``).

    Accepted limitation: validation compares against the pages STORED in the
    job, so edits the user made in-browser but never re-embedded are not
    reflected here — the embedded output is validated against the source that
    was most recently baked in, which is the honest baseline for "can I trust
    this PDF".
    """
    from backend.models import dict_to_page
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    embedded = job.get("embedded_path")
    if not embedded or not Path(embedded).exists():
        raise HTTPException(status_code=404, detail="No embedded output yet")
    pages = [dict_to_page(p) for p in ocr_service.get_pages(job_id)
             if isinstance(p, dict)]
    if not pages:
        raise HTTPException(status_code=404, detail="No OCR pages to validate against")
    report = validation.build_report(embedded, pages)
    if not report.get("ok"):
        raise HTTPException(status_code=409, detail=report.get("error", "validation failed"))
    return report


# Serve static frontend assets (css/js) if present — with no-cache so JS
# changes are picked up immediately without a hard browser refresh. The root
# HTML gets the same no-cache treatment so markup updates always show up too.
if FRONTEND_DIR.exists():
    from starlette.middleware.base import BaseHTTPMiddleware

    class NoCacheStaticMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.url.path == "/" or request.url.path.startswith("/static"):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return response

    app.add_middleware(NoCacheStaticMiddleware)
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def run() -> None:
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    run()