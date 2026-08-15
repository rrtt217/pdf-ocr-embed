"""FastAPI REST server for PDF OCR Embed.

Endpoints:
  POST /api/settings            save or read provider config (masked)
  POST /api/ocr/upload          upload PDF -> background OCR -> job id
  GET  /api/ocr/stream/{job_id} SSE stream of progress/status events
  GET  /api/pages/{job_id}/{i}/image   page preview image
  GET  /api/pages/{job_id}      get all page OCR data
  POST /api/pages/{job_id}/{i}  update an editable page (optional)
  POST /api/embed/{job_id}      embed (possibly edited) pages -> *_embedded.pdf
  GET  /api/download/{job_id}.pdf   download embedded result
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import cleanup as cleanup_mod
from backend import config, ocr_cache, ocr_service
from backend.logging_config import recent_logs, setup_logging
from backend.sources.factory import available_adapters

setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Keep orphaned temp files from accumulating: periodically delete
    # unreferenced files older than cleanup_max_age_hours.
    cleanup_mod.start_background_cleanup()
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
    adapter: Optional[str] = None


class EmbedModel(BaseModel):
    job_id: str
    adapter: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    pages: Optional[list] = None
    embed_font: Optional[str] = None   # system font name / path for the text layer
    # --- output optimization (see backend/pdf_processing.optimize_images) ---
    img_mode: Optional[str] = None      # none | jpeg | gray-jpeg
    img_quality: Optional[int] = None   # 1..100 JPEG quality
    img_downscale: Optional[int] = None # 2 (half) | 4 (quarter) raster dims
    linearize: bool = False             # web-first / linearized PDF


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    idx = FRONTEND_DIR / "index.html"
    if idx.exists():
        return idx.read_text(encoding="utf-8")
    raise HTTPException(status_code=404, detail="Frontend not built")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "adapters": available_adapters()}


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




@app.get("/api/cache")
def cache_status() -> dict:
    """Status of the cross-job OCR result cache (entries, hits/misses, TTL)."""
    return ocr_cache.status()


@app.post("/api/cache/clear")
def cache_clear() -> dict:
    """Drop every OCR cache entry (never touches OCR results in job state)."""
    return ocr_cache.clear()


@app.get("/api/settings")
def get_settings() -> dict:
    return config.get_effective_settings()


@app.post("/api/settings")
def save_settings(payload: SettingsModel) -> dict:
    if payload.api_key is not None:
        # Persist via config.save (handles masked-key preservation).
        data = {
            "provider": payload.provider or "",
            "base_url": payload.base_url or "",
            "model": payload.model or "",
            "api_key": payload.api_key,
        }
        return config.save(data)
    # Read-only display mode.
    return config.get_effective_settings()


@app.post("/api/ocr/upload")
async def upload_pdf(
    file: UploadFile = File(...),
    adapter: Optional[str] = Form("unlimited"),
    concurrency: Optional[int] = Form(1),
    base_url: Optional[str] = Form(None),
    api_key: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    # tesseract adapter knobs
    lang: Optional[str] = Form(None),
    psm: Optional[int] = Form(None),
    oem: Optional[int] = Form(None),
    # generic_openai adapter knob
    prompt: Optional[str] = Form(None),
) -> dict:
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file")

    job = ocr_service.create_job(file.filename or "upload.pdf", contents)

    # Per-request overrides for this job only (not persisted).
    ocr_service._set(job, adapter=adapter or "unlimited")
    concurrency = max(1, int(concurrency or 1))
    extra = {}
    for k, v in (("base_url", base_url), ("api_key", api_key),
                 ("model", model), ("lang", lang), ("psm", psm),
                 ("oem", oem), ("prompt", prompt)):
        if v is not None and v != "":
            extra[k] = v

    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        None, ocr_service.run_ocr, job["id"], adapter, extra or None, concurrency)
    log.info("upload job %s: %s, concurrency=%d", job["id"], job["filename"], concurrency)
    return {"job_id": job["id"], "filename": job["filename"], "status": "running",
            "concurrency": concurrency}


@app.post("/api/ocr/retry/{job_id}")
async def retry_ocr(
    job_id: str,
    adapter: Optional[str] = Form("unlimited"),
    concurrency: Optional[int] = Form(1),
    base_url: Optional[str] = Form(None),
    api_key: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    lang: Optional[str] = Form(None),
    psm: Optional[int] = Form(None),
    oem: Optional[int] = Form(None),
    prompt: Optional[str] = Form(None),
) -> dict:
    """Re-run OCR on an already-uploaded job without re-uploading the PDF."""
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    concurrency = max(1, int(concurrency or 1))
    extra = {}
    for k, v in (("base_url", base_url), ("api_key", api_key),
                 ("model", model), ("lang", lang), ("psm", psm),
                 ("oem", oem), ("prompt", prompt)):
        if v is not None and v != "":
            extra[k] = v
    ok = ocr_service.retry_job(
        job_id, adapter_name=adapter or "unlimited",
        extra_cfg=extra or None, concurrency=concurrency)
    if not ok:
        raise HTTPException(status_code=409, detail="Job cannot be retried (missing file)")
    log.info("retry scheduled for job %s (concurrency=%d)", job_id, concurrency)
    return {"job_id": job_id, "filename": job["filename"], "status": "retrying",
            "concurrency": concurrency}


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
            terminal_delivered = False
            for ev in events:
                yield _sse(ev)
                if ev.get("type") == "status" and ev.get("status") in ("done", "stopped"):
                    terminal_delivered = True

            if status in ("done", "embedded"):
                # Guarantee the "done" status reaches the client even if the
                # status flipped before its buffered event was pushed.
                if not terminal_delivered:
                    yield _sse({
                        "type": "status",
                        "status": "done",
                        "message": "OCR complete",
                        "result": [p for p in cur["pages"] if p is not None],
                    })
                break
            if status == "stopped":
                if not terminal_delivered:
                    yield _sse({
                        "type": "status", "status": "stopped",
                        "message": cur.get("error") or "OCR stopped",
                        "result": [p for p in cur["pages"] if p is not None],
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
    pages = ocr_service.update_page(job_id, page_index, payload)
    return {"ok": True, "page_count": len(pages)}


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


class PreviewModel(BaseModel):
    pages: Optional[list] = None   # list of OcrPage dicts (with font_scale)
    embed_font: Optional[str] = None


def _resolve_embed_font(font_name: Optional[str]):
    """Resolve a requested embed font name to a FontSpec (config as fallback)."""
    from backend import fonts
    if font_name:
        return fonts.resolve_font(font_name)
    cfg_font = config.resolve().get("embed_font")
    if cfg_font:
        return fonts.resolve_font(cfg_font)
    return fonts.resolve_font(None)  # default (first registry font)


@app.post("/api/preview/{job_id}/{page_index}")
def preview_overlay(job_id: str, page_index: int, payload: PreviewModel):
    """Render a page's scan with the placed text drawn VISIBLY (debug view).

    The body carries the current (possibly edited) page dicts, so the overlay
    reflects each block's interactive ``font_scale`` without syncing server job
    state on every slider move.  Returns a PNG.
    """
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    pages = payload.pages
    if not pages:
        pages = ocr_service.get_pages(job_id)
    import backend.pdf_processing as pdf_processing
    from backend.models import dict_to_page
    ocr_pages = [dict_to_page(p) for p in pages if isinstance(p, dict)]
    if not ocr_pages:
        raise HTTPException(status_code=400, detail="No page data to preview")
    embed_font = _resolve_embed_font(payload.embed_font)
    try:
        out = pdf_processing.render_overlay(
            job["pdf_path"], ocr_pages, page_index,
            pdf_processing.ensure_output_dir(), for_page=page_index,
            embed_font=embed_font,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    return FileResponse(str(out), media_type="image/png")


@app.post("/api/fontinfo/{job_id}/{page_index}")
def fontinfo(job_id: str, page_index: int, payload: PreviewModel):
    """Return each block's derived / applied font size for the debug UI.

    Body carries current page dicts (with font_scale).  Returns per-block:
    {index, kind, derived_fs, fs (after font_scale), bbox}.
    """
    job = ocr_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    pages = payload.pages or ocr_service.get_pages(job_id)
    import backend.pdf_processing as pdf_processing
    from backend.models import dict_to_page
    ocr_pages = [dict_to_page(p) for p in pages if isinstance(p, dict)]
    cfg = next((p for p in ocr_pages if p.page_index == page_index), None)
    if cfg is None:
        return {"blocks": []}
    embed_font = _resolve_embed_font(payload.embed_font)
    pdf_processing.set_embed_font(embed_font)
    try:
        # Open the source doc to get page geometry for layout.
        with pdf_processing.open_pdf(job["pdf_path"]) as doc:
            target = max(0, min(page_index, doc.page_count - 1))
            pg = doc[target]
            rect = pg.rect
            w_scale = rect.width / cfg.width if cfg.width else 1.0
            h_scale = rect.height / cfg.height if cfg.height else 1.0
            out = []
            for bi, block in enumerate(cfg.blocks):
                if block.kind in ("image", "image_ref") or not block.text.strip():
                    continue
                lay = pdf_processing._compute_block_layout(
                    block, pg, w_scale, h_scale, font_scale=block.font_scale)
                out.append({
                    "index": bi,
                    "kind": block.kind,
                    "bbox": block.bbox,
                    "derived_fs": round(lay["derived"], 2),
                    "fs": round(lay["fontsize"], 2),
                    "font_scale": block.font_scale,
                    "lines": len(lay["lines"]),
                })
        return {"blocks": out}
    finally:
        pdf_processing.set_embed_font(None)


@app.post("/api/embed/{job_id}")
def embed(job_id: str, payload: EmbedModel):
    # Allow either the body pages or the server-side stored pages.
    pages = payload.pages
    if pages is None:
        pages = ocr_service.get_pages(job_id)
    embed_font = _resolve_embed_font(payload.embed_font)
    # embed_job filters out None entries, so partial results work too.
    try:
        out_path, img_stats = ocr_service.embed_job(
            job_id, pages, embed_font=embed_font,
            img_mode=payload.img_mode, img_quality=payload.img_quality,
            img_downscale=payload.img_downscale, linearize=payload.linearize)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "status": "embedded",
        "filename": out_path.name,
        "url": f"/api/download/{job_id}.pdf",
        "font": embed_font.name,
        "images": img_stats,
    }


@app.get("/api/fonts")
def fonts_available() -> dict:
    from backend import fonts
    return {"fonts": fonts.available_fonts()}


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