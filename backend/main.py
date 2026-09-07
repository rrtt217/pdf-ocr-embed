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
  GET  /api/export/{job_id}.md|.tex  export pages as markdown / LaTeX
                                     (?reflow=1 re-wrap, ?llm=1 LLM fix-up)
  GET  /api/export/stream/{job_id}.{ext}  SSE export: progress + done(text)

The OCR core is OCRmyPDF; the unlimited engine ships as the standalone
``ocrmypdf_unlimited`` plugin (``backend/ocrmypad`` is a compat alias).
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
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from backend import batch
from backend import cleanup as cleanup_mod
from backend import config, export as export_mod, export_llm
from backend import image_export
from backend import ocr_service, validation
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
    # Push the effective config into the standalone OCR engine plugin — the
    # plugin never reads backend.config itself; the host initializes its
    # settings store here (and again whenever the settings change, see
    # save_settings).  The plugin is optional: the app runs without it, so
    # wire it up only when it is actually importable.
    _inject_plugin_settings()
    yield
    cleanup_mod.stop_background_cleanup()


def _inject_plugin_settings() -> bool:
    """Push the app's effective config into the plugin's settings store.

    Guards against the standalone ``ocrmypdf_unlimited`` plugin being absent
    ("vice versa" decoupling): without it the app keeps running on the
    built-in engines.  Returns True when the injection happened.
    """
    try:
        from ocrmypdf_unlimited import settings as ocrmypad_settings
    except ImportError:
        log.info("ocrmypdf-unlimited plugin not installed; unlimited engine "
                 "unavailable (built-in tesseract still works)")
        return False
    ocrmypad_settings.configure(config.resolve())
    return True


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
    # Raw generation + export pre-processing knobs (backend/export_llm).  The
    # WebUI sends all of these; without them pydantic silently drops the JSON
    # fields and a save never reaches ocr_config.toml.
    generate_raw: Optional[bool] = None
    export_reflow: Optional[bool] = None
    export_llm: Optional[bool] = None
    export_llm_model: Optional[str] = None
    export_llm_threshold: Optional[str] = None
    export_llm_batch: Optional[str] = None
    export_llm_timeout_s: Optional[str] = None


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
    engines = {
        "tesseract": "ocrmypdf built-in Tesseract",
        "none": "no OCR",
    }
    try:
        from ocrmypdf_unlimited.engine import UnlimitedOcrEngine
        engines["unlimited"] = str(UnlimitedOcrEngine())
    except ImportError:
        engines["unlimited"] = "plugin not installed (ocrmypdf_unlimited)"
    return {
        "status": "ok",
        "adapters": ["unlimited", "tesseract"],
        "engines": engines,
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
    for key in ("ocrmypdf_deskew", "ocrmypdf_clean", "ocrmypdf_rotate_pages",
                "generate_raw", "export_reflow", "export_llm"):
        value = getattr(payload, key, None)
        if value is not None:
            data[key] = value
    for key in ("export_llm_model", "export_llm_threshold",
                "export_llm_batch", "export_llm_timeout_s"):
        value = getattr(payload, key, None)
        if value is not None:
            data[key] = value
    if data:
        # Persist via config.save (handles masked-key preservation, and only
        # writes the fields actually present in the payload).
        result = config.save(data)
        # Keep the standalone engine plugin's injected config in sync with a
        # WebUI save (same host-initializes-plugin contract as above).
        _inject_plugin_settings()
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
    page_start: Optional[int] = Form(None),
    page_end: Optional[int] = Form(None),
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

    ``page_start`` / ``page_end`` (optional, 1-based inclusive) restrict the
    first run to a page range of the document — the WebUI's per-file start
    panel lets the user choose a range before launching the job.  When only
    one bound is given the range is bounded by the document's page count; an
    explicit ``end`` beyond the document length is harmless (OCRmyPDF only
    visits existing pages).
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

    # Page-range sanity for the per-file start flow (bounds are 1-based).
    if (page_start is not None and page_start < 1) or \
       (page_end is not None and page_end < 1):
        raise HTTPException(status_code=400, detail="Invalid page range")
    if page_start is not None and page_end is not None and page_start > page_end:
        raise HTTPException(
            status_code=400, detail="Invalid page range: start > end")
    has_page_range = page_start is not None or page_end is not None

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
        job_options = dict(extra)
        if has_page_range:
            total = int(job.get("num_pages") or 0)
            start = page_start if page_start is not None else 1
            end = page_end if page_end is not None else total
            if start > end:
                # The open bound resolved past the document's length (or an
                # explicit start beyond an explicit end) — this document
                # cannot satisfy the request.  Drop the half-created job.
                ocr_service.clear_job(job["job_id"])
                raise HTTPException(
                    status_code=400,
                    detail=f"Page range {start}..{end} exceeds the "
                           f"document's {total} page(s)")
            job_options["pages"] = f"{start}-{end}"
        loop.run_in_executor(
            None, ocr_service.run_ocr, job["job_id"], job_options or None)
        log.info("upload job %s: %s (engine=%s%s)", job["job_id"],
                 job["filename"], extra.get("ocr_engine") or "unlimited",
                 f", pages={job_options['pages']}"
                 if "pages" in job_options else "")
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
                        # `current` is the COMPLETED-PAGE COUNT (len(done)) — not
                        # the page number.  The job's progress is "how many pages
                        # are done"; page identity lives in `page_index`.  A page
                        # number here makes the WebUI's N/total count jump to the
                        # highest done page when pages complete out of order.
                        "current": len(done),
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


def _llm_flag(value: Optional[str], default: bool) -> bool:
    """Parse a tri-state LLM query flag: absent (None) falls back to the
    legacy master default; otherwise "0"/"false"/"no" is off."""
    if value is None:
        return default
    return str(value).lower() not in ("0", "false", "no")


@app.get("/api/export/{job_id}.{ext}")
def export_document(job_id: str, ext: str, raw: str = "1", llm: str = "0",
                    llm_blocks: Optional[str] = None,
                    llm_outline: Optional[str] = None,
                    reflow: str = "1",
                    images: str = "none"):
    """Export the job's recognized pages as markdown (``.md``) or LaTeX
    (``.tex``).

    ``raw=1`` (default) uses each block's raw (pre-normalization) content when
    the sidecar carries it — written by the unlimited engine when its
    ``generate_raw`` option is on — so the lossy normalization (math spacing,
    LaTeX -> plain, table HTML -> rows) is skipped and tables render as real
    markdown tables / LaTeX ``tabular``.  ``raw=0`` always uses the normalized
    text.  Pages whose sidecar has no raw field fall back to it either way.

    ``reflow=1`` (default) runs the deterministic export pre-processing
    (backend.export_llm): the sidecars' line-split structure exists for the
    PDF text layer, so exports unwrap it (hard-wrapped lines join, hyphens
    merge, ragged tab tables pad, page-boundary paragraphs merge).

    The LLM post-processing steps are export options, independent of each
    other: ``llm_blocks=1`` runs the block fix-up (tables / equations /
    low-confidence), ``llm_outline=1`` the heading refinement (all headings
    plus the detected table of contents).  ``llm=1`` is the legacy master
    switch enabling both.  Any LLM failure falls back to the reflowed text;
    the passes only touch the export copy — sidecars, hOCR and the embedded
    text layer are unaffected.

    ``images`` (markdown only) embeds the image blocks' real figure crops
    (backend.image_export renders them from the source PDF by the block
    bboxes): ``zip`` packages ``<stem>.md`` + an ``images/`` folder (relative
    links, renders everywhere including GitHub) as one archive; ``base64``
    inlines ``data:image/png;base64,…`` URIs for a single self-contained file.
    ``none`` (default) keeps the captioned placeholders.  A job without a
    readable source PDF falls back to placeholders; LaTeX ignores the option.

    404 when the job or its pages are missing; 400 for an unknown extension.
    """
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    pages = export_mod.load_job_pages(job)
    if not pages:
        raise HTTPException(
            status_code=404, detail="No OCR pages to export — run OCR first")
    fmt = {"md": "markdown", "tex": "latex"}.get((ext or "").lower())
    if fmt is None:
        raise HTTPException(
            status_code=400, detail=f"unknown export format: {ext} (.md | .tex)")
    image_mode = (images or "none").strip().lower()
    if image_mode not in ("none", "zip", "base64"):
        raise HTTPException(
            status_code=400,
            detail=f"unknown images mode: {images} (none | zip | base64)")
    use_reflow = str(reflow).lower() not in ("0", "false", "no")
    master = str(llm).lower() not in ("0", "false", "no")
    use_blocks = _llm_flag(llm_blocks, master)
    use_outline = _llm_flag(llm_outline, master)
    if use_reflow or use_blocks or use_outline:
        # The passes rewrite a deep copy; the per-job LLM cache lives next to
        # the hOCR folder (work/<job>/export_llm_cache.json).
        cache_path = (Path(job["hocr_dir"]).parent / "export_llm_cache.json"
                      if job.get("hocr_dir") else None)
        pages = export_llm.preprocess(
            pages, config.resolve(), fmt=fmt,
            enable_blocks=use_blocks, enable_outline=use_outline,
            reflow=use_reflow, cache_path=cache_path)
    # Image embedding (markdown only): extract the figure crops once, then
    # resolve them during rendering.  Any failure (unreadable source PDF, no
    # usable bboxes) falls back to the captioned placeholders — a broken
    # image pipeline never fails the export.
    image_map = None
    if fmt == "markdown" and image_mode != "none":
        try:
            image_map = image_export.extract_images(job, pages)
        except Exception as exc:  # noqa: BLE001
            log.warning("export: image extraction failed (%s); "
                        "falling back to placeholders", exc)
    resolver = (image_export.make_resolver(image_mode, image_map)
                if image_map else None)

    try:
        text = export_mod.export_document(
            fmt, pages, title=Path(job.get("filename") or "document").stem,
            use_raw=str(raw).lower() not in ("0", "false", "no"),
            image_resolver=resolver)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    stem = (Path(job.get("filename") or "document").stem or "document")
    # Non-ASCII filenames must use RFC 5987 filename* (a raw CJK header value
    # breaks the HTTP layer); the ASCII fallback keeps simple names intact.
    from urllib.parse import quote
    ascii_stem = stem.encode("ascii", "ignore").decode() or "export"

    # ZIP mode: <stem>.md + an images/ folder (relative links) as one archive.
    if fmt == "markdown" and image_mode == "zip" and image_map:
        fd, tmp_path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            image_export.build_markdown_zip(
                tmp_path, text, f"{ascii_stem or 'export'}.md", image_map)
        except Exception:  # noqa: BLE001
            os.unlink(tmp_path)
            raise
        return FileResponse(
            tmp_path,
            media_type="application/zip",
            filename=image_export.default_zip_name(stem),
            background=BackgroundTask(os.unlink, tmp_path),
        )

    media = "text/markdown" if fmt == "markdown" else "application/x-tex"
    disposition = (f'attachment; filename="{ascii_stem}.{ext.lower()}"; '
                   f"filename*=UTF-8''{quote(stem)}.{ext.lower()}")
    return Response(
        content=text,
        media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


@app.get("/api/export/stream/{job_id}.{ext}")
def export_document_stream(job_id: str, ext: str, raw: str = "1",
                           llm: str = "0", llm_blocks: Optional[str] = None,
                           llm_outline: Optional[str] = None,
                           reflow: str = "1",
                           images: str = "none"):
    """SSE export: progress events while the pre-processing runs, then one
    ``done`` event carrying the full document text.

    The LLM post-processing steps (``llm_blocks=1`` block fix-up,
    ``llm_outline=1`` heading refinement, legacy ``llm=1`` both) can take
    minutes (one API call per block batch), so the WebUI streams a progress
    bar from the ``progress`` events instead of waiting on a plain GET.

    ``images`` (markdown only) embeds the image crops into the done event's
    text: ``base64`` inlines ``data:image/png;base64,…`` URIs (backend
    .image_export).  ``zip`` is NOT accepted here — a done event carries text
    only, so the WebUI downloads a ZIP export through the plain GET instead.
    Event shapes (``backend.export_llm.preprocess`` progress callback):
      ``{"type":"progress","phase":"reflow"|"llm"|"outline","done":n,"total":m}``
      ``{"type":"done","fmt":"markdown"|"latex","text":"..."}``
      ``{"type":"error","message":"..."}``
    Validation errors (404/400) raise before the stream starts; a failure
    mid-stream is an ``error`` event, never a broken connection.
    """
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    pages = export_mod.load_job_pages(job)
    if not pages:
        raise HTTPException(
            status_code=404, detail="No OCR pages to export — run OCR first")
    fmt = {"md": "markdown", "tex": "latex"}.get((ext or "").lower())
    if fmt is None:
        raise HTTPException(
            status_code=400, detail=f"unknown export format: {ext} (.md | .tex)")

    # base64 embeds into the done event's text; zip cannot (text only) and
    # falls back to placeholders (the WebUI never sends zip over SSE).
    image_mode = (images or "none").strip().lower()
    resolver = None
    if fmt == "markdown" and image_mode == "base64":
        try:
            image_map = image_export.extract_images(job, pages)
            resolver = image_export.make_resolver("base64", image_map)
        except Exception as exc:  # noqa: BLE001
            log.warning("export stream: image extraction failed (%s); "
                        "falling back to placeholders", exc)

    # Snapshot the inputs for the worker thread (the request handler returns
    # before the work finishes — nothing in the closure may touch the request).
    cfg = config.resolve()
    use_reflow = str(reflow).lower() not in ("0", "false", "no")
    master = str(llm).lower() not in ("0", "false", "no")
    use_blocks = _llm_flag(llm_blocks, master)
    use_outline = _llm_flag(llm_outline, master)
    cache_path = (Path(job["hocr_dir"]).parent / "export_llm_cache.json"
                  if job.get("hocr_dir") else None)
    title = Path(job.get("filename") or "document").stem
    use_raw = str(raw).lower() not in ("0", "false", "no")
    ext_out = ext.lower()

    async def gen():
        import queue as _queue
        import threading

        events: _queue.Queue = _queue.Queue()

        def work() -> None:
            try:
                pages2 = export_llm.preprocess(
                    pages, cfg, fmt=fmt, enable_blocks=use_blocks,
                    enable_outline=use_outline, reflow=use_reflow,
                    cache_path=cache_path, progress=events.put)
                text = export_mod.export_document(
                    fmt, pages2, title=title, use_raw=use_raw,
                    image_resolver=resolver)
                events.put({"_result": text})
            except BaseException as exc:  # noqa: BLE001 — an error event, not a dropped connection
                from backend.config import redact_secrets
                events.put({"_error": redact_secrets(str(exc))})

        threading.Thread(target=work, daemon=True, name="export-llm").start()
        while True:
            try:
                # asyncio.to_thread: the blocking queue.get must NOT run on
                # the event loop — it would stall every other request and
                # SSE stream (including the OCR progress streams) for up to
                # the timeout, freezing the whole WebUI while the export
                # LLM steps run.
                ev = await asyncio.to_thread(events.get, True, 15.0)
            except _queue.Empty:
                # SSE comment keepalive: proxies/browsers time a silent
                # stream out long before the LLM batch budget.
                yield ": keepalive\n\n"
                continue
            if "_error" in ev:
                yield _sse({"type": "error", "message": ev["_error"]})
                break
            if "_result" in ev:
                yield _sse({"type": "done", "fmt": fmt, "ext": ext_out,
                            "text": ev["_result"]})
                break
            yield _sse({"type": "progress", **ev})

    return StreamingResponse(gen(), media_type="text/event-stream")


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