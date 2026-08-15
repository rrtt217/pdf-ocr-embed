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
- **Fully externalized OCR settings** — local TOML config `backend/ocr_config.toml`
  plus the WebUI settings page (the WebUI saves into the same TOML file). Any
  `OCR_*` **environment variable optionally overrides** the corresponding key
  (highest priority: env var > WebUI in-memory value > TOML file); JSON / `.env`
  file config has been removed.
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

All OCR settings live in one TOML config file, `backend/ocr_config.toml`
(already in `.gitignore`). There are two ways to provide them:

### 1) Local TOML config file (recommended)

Copy the repo's example file and edit it:

```bash
cp config.example.toml backend/ocr_config.toml
# then edit backend/ocr_config.toml and fill in your values
```

Minimal config:

```toml
provider = "ustc"
api_key = "your key"
base_url = "https://api.llm.ustc.edu.cn/v1"
model = "unlimited-ocr"
```

Any OpenAI-compatible endpoint works — switch engines by changing
`base_url` + `model`. Every other option (Tesseract, the generic-OpenAI prompt,
the embed font, temp-file cleanup, log level) is documented as a comment inside
`config.example.toml`.

### 2) WebUI Settings page

Fill in and save via the **Settings** button at the top-right of the page (the key
is stored masked). The form is pre-filled from `backend/ocr_config.toml`, and saving
writes the four provider fields back to that file without touching the rest
(tesseract, cleanup, `log_level`, ...); clearing a field before saving resets it.

> If no key is configured, OCR calls return a clear error; everything else
> (upload, preview) keeps working.

### 3) Environment-variable overrides (optional)

Any `OCR_*` environment variable **overrides** the matching key in the TOML file
and the WebUI in-memory value (priority: env var > WebUI saved value > TOML
file). This is handy for temporarily switching key/endpoint/engine without
editing the config file, e.g.:

```bash
OCR_API_KEY=sk-xxx OCR_BASE_URL=https://example.com/v1 python -m backend.main
```

`USTC_API_KEY` acts as an alias for `OCR_API_KEY` (only used when the latter
is not set). The mapping from environment variables to TOML keys is:

| Env var | TOML key |
| ---- | ---- |
| `OCR_API_KEY` / `USTC_API_KEY` | `api_key` |
| `OCR_BASE_URL` | `base_url` |
| `OCR_MODEL` | `model` |
| `OCR_PROVIDER` | `provider` |
| `OCR_TESS_LANG` | `tess_lang` |
| `OCR_TESS_PSM` | `tess_psm` |
| `OCR_TESS_OEM` | `tess_oem` |
| `OCR_TESS_CONFIG` | `tess_config` |
| `OCR_TESSDATA_DIR` | `tessdata_dir` |
| `OCR_TESS_CMD` | `tess_cmd` |
| `OCR_GENERIC_PROMPT` | `generic_prompt` |
| `OCR_EMBED_FONT` | `embed_font` |
| `OCR_CLEANUP_MAX_AGE_HOURS` | `cleanup_max_age_hours` |
| `OCR_CLEANUP_INTERVAL_HOURS` | `cleanup_interval_hours` |
| `OCR_CACHE_ENABLED` | `ocr_cache_enabled` |
| `OCR_CACHE_MAX_AGE_HOURS` | `ocr_cache_max_age_hours` |
| `OCR_LOG_LEVEL` | `log_level` |

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
# put this in backend/ocr_config.toml (or use the WebUI's "Tesseract language" field)
echo 'tess_lang = "chi_sim"' >> backend/ocr_config.toml
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
| GET | `/api/cache` | OCR result-cache status (entries/bytes, hit and miss counts, TTL, enabled flag) |
| POST | `/api/cache/clear` | Drop all cached OCR results (never touches OCR results held in job state) |

---

## Directory Layout

```
pdf-ocr-embed/
├── backend/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app + all routes
│   ├── config.py               # external setting resolution (TOML config file / WebUI)
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
├── tests/                      # pytest suite (coordinate mapping / parsers / cache, ...)
├── requirements-dev.txt        # dev dependencies (pytest)
├── requirements.txt
├── config.example.toml
├── .gitignore
├── AGENTS.md     # agent-oriented project guide (incl. how to write an OCR adapter)
├── DESIGN.md
└── FEATURE_IDEAS.md  # brainstormed candidates for future features (not a schedule)
```

---

## Notes & Limitations

- bboxes are `[x1,y1,x2,y2]` integers; adapters convert normalized canvases back to
  real pixels; the frontend and embedding uniformly use pixel coordinates.
- `max_tokens` is set to 16384 (must stay < 32768 or the API returns HTTP 400).
- Pixel → PDF coordinates are flipped along the y axis (PDF origin is bottom-left,
  pixel origin top-left) and scaled by the page rect / rendered size.
- **Tesseract adapter (local, no key)**:
  - Language is configured via `tess_lang` in `backend/ocr_config.toml` (or the
    WebUI upload zone): `chi_sim` for Chinese, combinable as `chi_sim+eng`.
  - Requires the `tesseract` binary + matching language packs (Fedora: `tesseract` +
    `tesseract-langpack-chi_sim`). Use `tess_cmd` if the binary is not on PATH,
    and `tessdata_dir` if tessdata is not in the default location.
  - Each text line is aggregated into one block, auto-classified as
    heading/equation/text, with a confidence score.
- **generic_openai adapter (any OpenAI-compatible vision model)**: same
  api_key/base_url/model config as unlimited; `generic_prompt` overrides the default
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
- **OCR result cache**: identical work (same PDF content + page + engine + settings)
  is cached by content hash under `cache/ocr/` (key = source-PDF hash + page number +
  render parameters + engine fingerprint; no secrets or page images are ever written).
  Re-OCRing the same document hits the cache instead of re-calling the engine.
  TTL comes from `ocr_cache_max_age_hours` (default 720h); `ocr_cache_enabled = false`
  disables it entirely. The background cleanup loop also expires old entries;
  `GET /api/cache` shows hit/miss stats and `POST /api/cache/clear` wipes the cache.
  Only *pristine* recognition results are cached — your per-page edits are unaffected.
- **Debug logs**: full pipeline logging, verbosity controlled by `log_level` in
  `backend/ocr_config.toml` (default INFO; DEBUG for detail). The **Logs** button at the
  top-right of the WebUI shows live server logs, or call `GET /api/logs`.
- **Temp file cleanup**: jobs live only in memory — after a server restart,
  `work/<job_id>/` (source PDF + per-page renders) and `output/` (embeds, thumbnails,
  overlays) become orphaned. The backend auto-deletes files that are not referenced by
  any live job and are older than `cleanup_max_age_hours` (default 168h = 7 days),
  at startup and every `cleanup_interval_hours` (default 6h); both values come from
  `backend/ocr_config.toml`. Files in use are
  **never deleted**. The **Cleanup** button at the top-right of the WebUI shows a
  summary, lets you adjust the retention window and clean manually (Preview first,
  then Clean now); or call `/api/cleanup` and `/api/cleanup/run`.
- Runtime artifacts (`output/`, `work/`, `uploads/`, `backend/ocr_config.toml`) must
  not be committed to the repository.
