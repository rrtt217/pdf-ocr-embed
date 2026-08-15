# PDF OCR Embed

> English ｜ [中文](README.md)

A cross-platform tool that turns image-only (scanned) PDFs into documents with a
**searchable, selectable, copyable invisible text layer**. Upload a scanned PDF; the
app OCRs every page and embeds the recognized text **invisibly** (PyMuPDF
`render_mode=3`) at the detected coordinates — the text stays hidden visually but is
fully searchable, selectable and copyable. A single-page WebUI lets you edit the
recognized text, tune per-block font sizes, watch live progress (SSE) and download
the `*_embedded.pdf`.

This is a **fully standalone program**: API keys come from external configuration —
nothing is hardcoded in the code.

> **AI / Vibe coding note**: the code, design and other docs of this project were
> generated largely by AI (DeepSeek V4 Flash). Please verify before trusting it;
> when extending, review security, edge cases and dependency versions.
>
> **For AI agents**: `AGENTS.md` is the agent-oriented project guide — architecture,
> hard invariants, and the full steps + checklist for writing a new OCR adapter.
> Read it before changing anything.

---

## Feature Highlights

- **Generic OCR abstraction (Adapter pattern)** — the backend speaks only the
  normalized `OcrPage` interface; each engine is one adapter that converts its raw
  output to `OcrPage` (bboxes unified to **raw pixel coordinates**).
  - `unlimited_ocr_adapter` (full implementation, default): parses
    `<|det|>type [bbox]<|/det|>content` markers and maps the 1000×1000 normalized
    canvas back to real pixels (per-axis scale).
  - `tesseract_adapter` (full implementation): local Tesseract OCR, no API key.
    Word-level TSV is grouped into line blocks (one per line), auto-classified as
    text/heading/equation; Chinese needs a language pack such as `chi_sim`.
  - `generic_openai_adapter` (full implementation): any OpenAI-compatible vision
    model, prompted to return structured JSON with bboxes, mapped back to pixels.
- **Fully externalized OCR settings** — env vars / local `ocr_config.json` / WebUI.
  Priority: **env vars > local config file > WebUI saved values**.
- **PDF pipeline** — PyMuPDF renders each page to an image, OCR runs per page, and
  the text is embedded invisibly with `render_mode=3`; pixel→PDF coordinates are
  flipped correctly (y axis) and scaled by the page rect, saved as `*_embedded.pdf`.
- **Progress streaming** — SSE pushes per-page OCR progress.
- **Parallel OCR** — configurable concurrency: pages are OCR'd concurrently in a
  thread pool, significantly speeding up multi-page documents.
- **Single-page WebUI** — editable text blocks on the left, page preview + bbox
  overlay on the right, settings form, embed button, progress bars, concurrency input.
- **i18n** — English & 中文 built in; switch anytime from the header
  (`frontend/i18n.js`), defaults to the browser language, applies instantly with no
  page refresh.
- **Light / Dark / Auto theme** — switchable from the header; the choice is remembered
  in localStorage; Auto follows the system `prefers-color-scheme` (native controls and
  scrollbars adapt too).
- **WebUI UX polish** — toast notifications, `Ctrl/⌘+Enter` to embed, `←/→` to flip
  pages, remembered preferences (theme/engine/language/zoom/font), visible focus
  styles, `prefers-reduced-motion` support, inline SVG favicon and a theme-aware
  `theme-color`.
- **No CUDA / NVIDIA** dependency.

---

## Installation

```bash
cd /home/david/vibe-arena/pdf-ocr-embed
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## OCR Configuration

Pick one; priority top-down:

### 1) Environment variables

```bash
export OCR_API_KEY="your key"                              # or the USTC_API_KEY alias
export OCR_BASE_URL="https://api.llm.ustc.edu.cn/v1"       # any OpenAI-compatible endpoint
export OCR_MODEL="unlimited-ocr"
export OCR_PROVIDER="ustc"                                 # optional: ustc | openai | custom
```

Any OpenAI-compatible endpoint works — switch engines by changing
`OCR_BASE_URL` + `OCR_MODEL`.

### 2) Local config file (recommend gitignoring it)

Create `ocr_config.json` under `backend/`:

```json
{
  "provider": "ustc",
  "api_key": "your key",
  "base_url": "https://api.llm.ustc.edu.cn/v1",
  "model": "unlimited-ocr"
}
```

Or use `backend/.env` (lines of `KEY=value`).

### 3) WebUI Settings page

Fill in and save via the **Settings** button at the top-right of the page (the key
is stored masked).

> If no key is configured, OCR calls return a clear error; everything else
> (upload, preview) keeps working.

---

## Running

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
# or
python -m backend.main
```

Open <http://localhost:8000> and drag a PDF in.

### Choosing the OCR engine

The upload zone has a dropdown with three engines:

- **Unlimited OCR (API)** (default) — requires an API key / base_url / model (see above).
- **Tesseract (local)** — local OCR, **no API key needed**. Set the language pack in
  "Tesseract language", e.g. `chi_sim` (Chinese), `eng` (English), or `chi_sim+eng`
  (mixed).
- **Generic OpenAI (API)** — any OpenAI-compatible vision model; the key goes through
  Settings.

Command line (tesseract example):

```bash
export OCR_TESS_LANG=chi_sim   # or put "tess_lang" in backend/ocr_config.json
uvicorn backend.main:app --port 8000
```

### API Overview

| Method | Path | Description |
| ---- | ---- | ---- |
| GET | `/` | WebUI page |
| GET | `/api/health` | Health check + available adapters |
| GET/POST | `/api/settings` | Read / save provider config (masked) |
| POST | `/api/ocr/upload` | Upload PDF → background per-page OCR (`concurrency`, `adapter` engine, `lang/psm/oem` for tesseract, `base_url/api_key/model` for API engines) → returns a job id |
| POST | `/api/ocr/retry/{job_id}` | Re-run OCR for failed/interrupted jobs (only missing pages, not from scratch; params same as upload) |
| POST | `/api/ocr/stop/{job_id}` | Stop a running OCR job (completed pages are kept: download or retry the rest) |
| GET | `/api/logs` | Recent backend debug logs |
| GET | `/api/ocr/stream/{job_id}` | SSE progress stream |
| GET | `/api/pages/{job_id}` | All per-page OCR data |
| GET | `/api/pages/{job_id}/{i}/image` | Page preview PNG |
| POST | `/api/pages/{job_id}/{i}` | Update one editable page |
| POST | `/api/embed/{job_id}` | Embed (edited) text → `*_embedded.pdf` |
| GET | `/api/download/{job_id}.pdf` | Download the embedded result |
| GET | `/api/cleanup` | Temp-file cleanup overview (unreferenced work/output/uploads counts + sizes) |
| POST | `/api/cleanup/run` | Run/preview cleanup (`older_than_hours`, `dry_run` preview, `force` to ignore the age limit; in-use job files are never deleted) |

---

## Directory Layout

```
pdf-ocr-embed/
├── backend/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app + all routes
│   ├── config.py               # external setting resolution (env / config file / WebUI)
│   ├── models.py               # normalized OcrPage / OcrBlock schema
│   ├── pdf_processing.py       # page render → PNG + invisible text embedding
│   ├── ocr_service.py          # OCR orchestration, jobs, progress, concurrency
│   └── sources/
│       ├── __init__.py
│       ├── base.py             # OcrSource ABC + coordinate helpers
│       ├── factory.py          # adapter registry + get_adapter
│       ├── unlimited_ocr_adapter.py   # full implementation (<|det|> marker parsing)
│       ├── tesseract_adapter.py       # full implementation (local Tesseract)
│       └── generic_openai_adapter.py  # full implementation (any OpenAI-compatible vision model)
├── frontend/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   └── i18n.js                 # EN + 中文 UI strings
├── requirements.txt
├── .env.example
├── .gitignore
├── AGENTS.md     # agent-oriented project guide (incl. how to write an OCR adapter)
└── DESIGN.md
```

---

## Notes & Limitations

- bboxes are `[x1,y1,x2,y2]` integers; adapters convert normalized canvases back to
  real pixels; the frontend and embedding uniformly use pixel coordinates.
- `max_tokens` is set to 16384 (must stay < 32768 or the API returns HTTP 400).
- Pixel → PDF coordinates are flipped along the y axis (PDF origin is bottom-left,
  pixel origin top-left) and scaled by the page rect / rendered size.
- **Tesseract adapter (local, no key)**:
  - Language is configured via `OCR_TESS_LANG` (or `tess_lang` in `ocr_config.json`,
    or the WebUI upload zone): `chi_sim` for Chinese, combinable as `chi_sim+eng`.
  - Requires the `tesseract` binary + matching language packs (Fedora: `tesseract` +
    `tesseract-langpack-chi_sim`). Use `OCR_TESS_CMD` if the binary is not on PATH,
    and `OCR_TESSDATA_DIR` if tessdata is not in the default location.
  - Each text line is aggregated into one block, auto-classified as
    heading/equation/text, with a confidence score.
- **generic_openai adapter (any OpenAI-compatible vision model)**: same
  key/base_url/model config as unlimited; `OCR_GENERIC_PROMPT` overrides the default
  bbox-JSON prompt.
- **Concurrency**: set it on upload, via the WebUI input or the `concurrency` form
  field of `POST /api/ocr/upload` (1–32). Pages are processed concurrently in a
  thread pool (`concurrency=1` = sequential). Higher concurrency means more load on
  the OCR engine/API — match it to your quota.
- **Smart retry**: after an OCR error or a mid-job stop, the WebUI shows a
  **Retry remaining** button. Retry only re-runs failed/incomplete pages; successful
  pages are kept (fixes the "99% done then restart from scratch" problem). You can
  also call `POST /api/ocr/retry/{job_id}` reusing the uploaded PDF — no re-upload.
- **Mid-job stop**: click **Stop** while OCR is running (or
  `POST /api/ocr/stop/{job_id}`). Completed pages are kept — download them as a
  partial `*_embedded.pdf` via **Download partial**, or finish the rest with
  **Retry remaining**.
- **Debug logs**: full pipeline logging, verbosity controlled by `OCR_LOG_LEVEL`
  (default INFO; DEBUG for detail). The **Logs** button at the top-right of the WebUI
  shows live server logs, or call `GET /api/logs`.
- **Temp file cleanup**: jobs live only in memory — after a server restart,
  `work/<job_id>/` (source PDF + per-page renders) and `output/` (embeds, thumbnails,
  overlays) become orphaned. The backend auto-deletes files that are not referenced by
  any live job and are older than `OCR_CLEANUP_MAX_AGE_HOURS` (default 168h = 7 days),
  at startup and every `OCR_CLEANUP_INTERVAL_HOURS` (default 6h). Files in use are
  **never deleted**. The **Cleanup** button at the top-right of the WebUI shows a
  summary, lets you adjust the retention window and clean manually (Preview first,
  then Clean now); or call `/api/cleanup` and `/api/cleanup/run`.
- Runtime artifacts (`output/`, `work/`, `uploads/`, `ocr_config.json`, `.env`) must
  not be committed to the repository.