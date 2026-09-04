# PDF OCR Embed

> English ｜ [中文](README.md)

A cross-platform tool that turns image-only (scanned) PDFs into documents with a
**searchable, selectable, copyable invisible text layer**. Upload a scanned PDF;
[OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) rasterizes the pages, runs the
selected OCR engine, and grafts the recognized text **invisibly** at the detected
coordinates — the text stays hidden visually but is fully searchable, selectable
and copyable. A single-page WebUI lets you edit the recognized text, watch live
progress (SSE) and download the `*_embedded.pdf`.

This is a **fully standalone program**: API keys come from external configuration —
nothing is hardcoded in the code.

> **AI / Vibe coding note**: the code, design and other docs of this project were
> generated largely by AI (DeepSeek V4 Flash). Please verify before trusting it;
> when extending, review security, edge cases and dependency versions.
>
> **For AI agents**: `AGENTS.md` is the agent-oriented project guide — architecture,
> hard invariants, and the full steps + checklist for writing a new OCR engine
> (`OcrEngine` plugin). Read it before changing anything.

---

- **OCR core = OCRmyPDF** — rasterization, engine scheduling, concurrency, text-layer
  rendering (its built-in fpdf2 renderer), grafting, PDF/A and optimization all live
  in [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) (≥17.11, system deps:
  tesseract + ghostscript, no qpdf). The backend calls it in-process through its
  official edit-round-trip channel: `_pdf_to_hocr` (OCR → per-page hOCR) +
  `_hocr_to_ocr_pdf` (edited hOCR → final PDF).
- **unlimited-ocr as an OCRmyPDF plugin** (`backend/ocrmypad/`) — an `OcrEngine`
  plugin that OCRs each page through an OpenAI-compatible vision API (USTC
  `unlimited-ocr` model), parses the `<|det|>type [bbox]<|/det|>content` markers
  (1000×1000 canvas scaled per-axis back to **raw pixel coordinates**) and writes
  hOCR + a block sidecar JSON (the WebUI's editable representation).
  `ocr_engine = "tesseract"` falls back to ocrmypdf's built-in Tesseract;
  `"none"` disables OCR.
- **Engine-agnostic page store** (`backend/page_store.py`) — the ONLY channel the
  backend uses to talk about pages: block sidecars (the normalized editable form),
  hOCR (the interchange format every engine implements — a Tesseract-only page is
  derived into an editable sidecar via ocrmypdf's own parser), the `<job>/cancel`
  flag file, and the page inventory (hOCR ∪ sidecars). No engine's raw output
  ever leaks past it.
- **Fully externalized OCR settings** — local TOML config `backend/ocr_config.toml`
  plus the WebUI settings page (the WebUI saves into the same TOML file). Any
  `OCR_*` **environment variable optionally overrides** the corresponding key
  (highest priority: env var > WebUI in-memory value > TOML file); JSON / `.env`
  file config has been removed.
- **Frontend stays our own WebUI** (zero-build vanilla JS): editable text blocks on
  the left, page preview + bbox overlay on the right, settings form, embed button,
  SSE progress. OCRmyPDF's `misc/_webservice.py` was NOT adopted (a Streamlit form
  app with no per-page editing / progress; rationale in DESIGN.md).
- **Progress streaming** — SSE pushes per-page OCR progress (derived from the
  page-store file inventory, engine-agnostic).
- **Parallel OCR** — `concurrency` maps to OCRmyPDF's worker count
  (`ocrmypdf_jobs`); `use_threads` is always on (the engines are HTTP/IO-bound,
  and thread-based runs suit file-based progress).
- **Confidence review** — every block shows a confidence badge (green/amber/red at
  85/60), low-confidence blocks get a red outline; a "Low only" filter with an
  adjustable threshold (default 60%) and per-page badge counts on the tabs.
- **Output options** — at finalize, choose **optimization level** (0–3, handled by
  ocrmypdf's optimize stage) and **output type** (PDF / PDF/A).
- **Job persistence** — every job's state is written to `work/<job_id>/job.json` in
  real time; per-page OCR results live as block sidecars under
  `work/<job_id>/hocr/`, and both are restored on server start — recognized pages
  can be finalized without re-uploading.
- **Batch upload + ZIP download** — drop/select several PDFs at once: each file
  becomes its own independent job (parallel cards + SSE progress); once embedded,
  tick any finished jobs and one click packages their embedded PDFs into a single
  ZIP (streamed member by member from disk, never fully loaded into RAM).
- **i18n** — English & 中文 built in; switch anytime from the header
  (`frontend/i18n.js`), defaults to the browser language, applies instantly with no
  page refresh.
- **Light / Dark / Auto theme** — switchable from the header; the choice is remembered
  in localStorage; Auto follows the system `prefers-color-scheme` (native controls and
  scrollbars adapt too).
- **WebUI UX polish** — toast notifications, `Ctrl/⌘+Enter` to embed, `←/→` to flip
  pages, remembered preferences (theme/engine/…), visible focus styles,
  `prefers-reduced-motion` support, inline SVG favicon and a theme-aware `theme-color`.
- **No CUDA / NVIDIA** dependency.

---

## Installation

**System dependencies** (required by OCRmyPDF): tesseract-ocr and ghostscript; no
qpdf requirement.

```bash
# Debian/Ubuntu
sudo apt-get install tesseract-ocr ghostscript
cd /home/david/vibe-arena/pdf-ocr-embed
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # includes ocrmypdf>=17.11
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
`base_url` + `model`. Every other option (engine selection, ocrmypdf pipeline
knobs, retry/rate limits, temp-file cleanup, log level) is documented as a comment
inside `config.example.toml`.

### 2) WebUI Settings page

Fill in and save via the **Settings** button at the top-right of the page (the key
is stored masked). The form is pre-filled from `backend/ocr_config.toml`, and saving
writes the provider fields plus the pipeline knobs back to that file without
touching unrelated keys; clearing a field before saving resets it to the preset.

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
| `OCR_ENGINE` | `ocr_engine` |
| `OCRMYPDF_MODE` | `ocrmypdf_mode` |
| `OCRMYPDF_JOBS` | `ocrmypdf_jobs` |
| `OCRMYPDF_OPTIMIZE` | `ocrmypdf_optimize` |
| `OCRMYPDF_OUTPUT_TYPE` | `ocrmypdf_output_type` |
| `OCRMYPDF_LANGUAGE` | `ocrmypdf_language` |
| `OCRMYPDF_DESKEW` | `ocrmypdf_deskew` |
| `OCRMYPDF_CLEAN` | `ocrmypdf_clean` |
| `OCRMYPDF_ROTATE_PAGES` | `ocrmypdf_rotate_pages` |
| `OCR_MAX_RETRIES` | `max_retries` |
| `OCR_RETRY_BASE_DELAY` | `retry_base_delay` |
| `OCR_RETRY_MAX_DELAY` | `retry_max_delay` |
| `OCR_RATE_LIMIT_RPS` | `rate_limit_rps` |
| `OCR_CLEANUP_MAX_AGE_HOURS` | `cleanup_max_age_hours` |
| `OCR_CLEANUP_INTERVAL_HOURS` | `cleanup_interval_hours` |
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

The upload zone has a dropdown with three engines (`ocr_engine`):

- **Unlimited OCR (API)** (default) — the OCRmyPDF plugin engine; requires an API
  key / base_url / model (see above).
- **Tesseract (local)** — ocrmypdf's built-in Tesseract, **no API key needed**. Set
  the language pack in "OCR language", e.g. `chi_sim` (Chinese), `eng` (English),
  or `chi_sim+eng` (mixed).
- **No OCR** — no OCR at all (ocrmypdf image processing / optimization only).

The Settings dialog also offers pipeline knobs (persisted to the same TOML):
`mode` (force-ocr | skip-text | redo-ocr), `language` (Tesseract), `deskew`,
`clean` (needs unpaper).

Command line (tesseract example):

```bash
# put this in backend/ocr_config.toml (or use the Settings dialog)
echo 'ocrmypdf_language = "chi_sim"' >> backend/ocr_config.toml
uvicorn backend.main:app --port 8000
```

### Batch upload & ZIP download

The upload zone accepts **multiple PDFs** in one drag or file-picker (single-file
uploads still work exactly as before). Every file becomes an **independent job** —
its own card, SSE progress stream and persisted state, running in parallel; there
is no separate queue manager. All uploads fire concurrently, then the job list is
refreshed from the server (`/api/jobs` — the single source of truth) and each
running job subscribes to its own EventSource.

Once a job has been embedded (**Embed invisible text**), its card gains a
**checkbox**: tick any number of finished jobs and click **⬇ Download ZIP** at the
top-right of the job list to download their embedded PDFs as a single ZIP archive
(members are named after the source PDFs, with colliding names auto-suffixed).
The server packages it by streaming each file straight from disk
(`GET /api/ocr/zip?jobs=id1,id2,...`), so the archive is never buffered in RAM;
it returns 404 when none of the requested jobs have embedded results yet — jobs
that do have results are always included.

### Post-embed validation & quality report

After embedding, the backend re-opens the embedded PDF with PyMuPDF, extracts
its text layer and compares it page-by-page against the OCR source
(`backend/validation.py`) — the "can I trust this PDF?" closed loop:

- Per-page **coverage** (tolerant token overlap + character consecutiveness so
  line-wrapping does not hurt), source/embedded character and word counts, and
  confidence stats (min/avg/max, bucketed <60% / 60–80% / ≥80%).
- Summary: average coverage, pages below the threshold (60% by default),
  empty-source pages, total block count.
- CJK / space-less scripts are tokenized per glyph, so coverage is meaningful
  for Chinese and Japanese too.

Usage: the embed response carries a `report`; you can also hit the workspace
**Validate** button to re-run `GET /api/validation/{job_id}` anytime. Validation
only reads artifacts — it never modifies the embedded file and never fails an
embed (a broken report is surfaced as `ok:false` without blocking download).

### Block editing & operations

The block list on the left and the preview pane on the right let you fix
recognition results directly:

- **Merge**: click a block's meta row to select several blocks (or Shift/Ctrl-click
  on the preview), then hit **Merge** — bbox becomes the union, text is joined
  with newlines.
- **Split**: put the text caret in the middle of a block and click its **Split**
  button — the block splits in two along its longer axis, proportionally to text
  length (bboxes split accordingly).
- **Add**: click **Add block**, then drag a rectangle on the preview (Esc cancels)
  to create an empty text block; type its text on the left, then move/resize it.
- **Move / Resize**: drag a block on the preview to move it; a resize handle sits
  on the bottom-right corner of the selected block; with a block editor focused,
  **arrow keys** nudge the bbox (Shift = 10 px).
- **Undo**: every structural edit (add/delete/merge/split/move/resize) takes a
  snapshot; the toolbar **Undo** steps back through them (session-only).
- All bbox adjustments stay **integer pixel coordinates** clamped to the page
  (`x1<=x2`, `y1<=y2`), consistent with the coordinate invariant; edits are baked
  in when the page's hOCR is regenerated for finalize.

### Headless CLI

Run the whole "OCR → finalize" pipeline from the command line without the web
server (reuses the exact `backend.ocr_service` backend logic):

```bash
python -m backend.cli book.pdf --engine tesseract --pages 1-20 --jobs 2
python -m backend.cli book.pdf --engine unlimited --pages 1-5 --sidecar-text  # print per-page text
```

Options: `--engine` (default `unlimited`), `--pages` (1-based; `"1-20"` / `"1,3,5-7"` /
`"1-"` / `"-5"`), `--jobs` (worker count), `--out` (default `output/`),
`--sidecar-text`. Writes `<stem>_embedded_<id>.pdf`. Config (API keys etc.) still
resolves via `resolve()` (TOML / env var), never hardcoded.

### API Overview

| Method | Path | Description |
| ---- | ---- | ---- |
| GET | `/` | WebUI page |
| GET | `/api/health` | Health check + engine map |
| GET/POST | `/api/settings` | Read / save provider config (masked) + pipeline knobs |
| POST | `/api/ocr/upload` | Upload PDF → background OCRmyPDF OCR (`files` multi / `file` single; `ocr_engine` select, `concurrency` → workers, `lang` for tesseract, `base_url/api_key/model` overrides) → returns job id(s) |
| GET | `/api/ocr/zip?jobs=id1,id2` | Package the embedded PDFs of the selected jobs into one ZIP (`jobs` = comma-separated job ids; 404 when none of them have embedded results yet) |
| POST | `/api/ocr/retry/{job_id}` | Re-run OCR for failed/interrupted jobs (default: only missing pages, not from scratch; same params as upload, plus `page_start`/`page_end` range and `force` to re-run already-successful pages) |
| POST | `/api/ocr/stop/{job_id}` | Stop a running OCR job (completed pages are kept on disk; retry the rest) |
| GET | `/api/logs` | Recent backend debug logs |
| GET | `/api/ocr/stream/{job_id}` | SSE progress stream (status + per-page progress events) |
| GET | `/api/pages/{job_id}` | All per-page OCR data (block sidecar JSON) |
| GET | `/api/pages/{job_id}/{i}/image` | Page preview PNG |
| POST | `/api/pages/{job_id}/{i}` | Update one editable page (writes sidecar + regenerates hOCR) |
| POST | `/api/embed/{job_id}` | Finalize (edited) text → `<source>_embedded.pdf` (`optimize`, `output_type`; with `pages`, produces a `<source>_partial.pdf` with exactly those pages) |
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
│   ├── config.py               # external setting resolution (TOML / WebUI / OCR_* env)
│   ├── page_store.py           # engine-agnostic page interchange (sidecar/hOCR/cancel)
│   ├── ocrmypad/               # OCRmyPDF plugin package (unlimited-ocr engine)
│   │   ├── unlimited_engine.py # OcrEngine plugin + get_ocr_engine hook
│   │   ├── engine_client.py    # OpenAI-compatible client (truncation/retry/timeout)
│   │   ├── parser.py           # <|det|> marker parsing + hOCR emission
│   │   └── text_norm.py        # math/table text normalization
│   ├── errors.py               # UnavailableError + 1000-canvas → pixel bbox mapping
│   ├── http_retry.py           # HTTP retry/rate-limit (engine API calls)
│   ├── ocr_service.py          # OCRmyPDF orchestration (_pdf_to_hocr + _hocr_to_ocr_pdf) + job state
│   ├── models.py               # editor page JSON (OcrPage/OcrBlock compatible)
│   ├── pdf_processing.py       # page preview rendering (PyMuPDF)
│   ├── validation.py           # post-embed coverage report
│   ├── batch.py                # ZIP packaging (streamed)
│   ├── cleanup.py              # temp-file cleanup
│   ├── logging_config.py       # logging
│   └── cli.py                  # headless CLI (python -m backend.cli)
├── frontend/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   └── i18n.js                 # EN + 中文 UI strings
├── tests/                      # pytest suite (146 tests)
├── requirements-dev.txt        # dev dependencies (pytest)
├── requirements.txt
├── config.example.toml
├── .gitignore
├── AGENTS.md     # agent-oriented project guide (engine plugins + page store)
└── DESIGN.md     # design doc (OCRmyPDF architecture + frontend rationale)
```

---

## Notes & Limitations

- bboxes are `[x1,y1,x2,y2]` **integers in raw pixel space** (top-left origin). The
  1000×1000 normalized canvas → real-pixel mapping is centralized in
  `backend/errors.normalize_bbox`; the hOCR `scan_res` carries the true DPI (the
  fpdf2 renderer's px→pt transform depends on it).
- `max_tokens` defaults to 16384 (must stay < 32768 or the API returns HTTP 400).
  A truncated response (`finish_reason=length`, or
  `completion_tokens >= max_tokens`) is treated as a page **failure** with a clear
  error — retry re-runs it instead of silently accepting a partial result.
- Coordinate entry into the PDF layer is OCRmyPDF's job: the fpdf2 renderer and
  hOCR share the top-left origin, so no y-flip is needed; page rotation is handled
  by ocrmypdf's rotate/graft flow.
- **Tesseract engine (local, no key)**: ocrmypdf's built-in implementation.
  Language via `ocrmypdf_language` (`chi_sim`, combinable as `chi_sim+eng`);
  requires the `tesseract` binary + matching language packs.
- **unlimited engine result handling**: `table` blocks convert HTML to row/column
  text (no `<tr>/<td>` tags reach the text layer); equation/table-cell spacing from
  the model's tokenization is tightened (`X _ p`→`X_p`, `f (x)`→`f(x)`); adjacent
  single digits are never auto-merged; `image_caption` captions keep their own
  bbox (`caption_bbox`) and are embedded into the text layer.
- **Worker count (concurrency / ocrmypdf_jobs)**: set it on upload (1–32) — maps to
  OCRmyPDF's OCR worker count; `use_threads` is always on. Higher concurrency means
  more load on the OCR engine/API — match it to your quota.
- **Smart retry**: after an OCR error or a mid-job stop, the WebUI shows a
  **Retry remaining** button. Retry only re-runs failed/incomplete pages
  (`force=true` re-runs every selected page). You can also call
  `POST /api/ocr/retry/{job_id}` reusing the uploaded PDF — no re-upload.
- **Mid-job stop**: click **Stop** while OCR is running (or
  `POST /api/ocr/stop/{job_id}`) — the engine polls the `<job>/cancel` flag between
  pages (the unlimited plugin supports it; ocrmypdf's built-in Tesseract has no
  cancel hook, so the run completes and the UI says so). Completed pages are kept
  on disk: **Retry remaining** finishes the rest or finalize downloads the partial
  result.
- **Debug logs**: full pipeline logging, verbosity controlled by `log_level` in
  `backend/ocr_config.toml` (default INFO; DEBUG for detail). The **Logs** button at the
  top-right of the WebUI shows live server logs, or call `GET /api/logs`.
- **Temp file cleanup**: job state is **persisted** in `work/<job_id>/job.json` and
  restored at startup, so a restart no longer loses tasks (a job that crashed
  mid-run comes back as stopped — its hOCR work folder keeps every recognized page
  finalizable). Cleanup only ever removes **unreferenced** files older than
  `cleanup_max_age_hours` (default 168h = 7 days); it runs at startup and every
  `cleanup_interval_hours` (default 6h). Files referenced by a job are
  **never deleted**. The **Cleanup** button at the top-right of the WebUI shows a
  summary and lets you clean manually; or call `/api/cleanup` and
  `/api/cleanup/run`.
- **Batch upload / ZIP packaging**: multi-file uploads each become their own job
  (no queue manager); ZIP members are named after the source PDFs (colliding names
  get a ` (2)` suffix). Only **embedded** jobs whose output file still exists are
  packaged; the rest are skipped, and a 404 is returned when none qualify. The temp
  archive lives in the system temp dir, is served as a streaming `FileResponse` and
  deleted by a background task once the response has been sent.
- Runtime artifacts (`output/`, `work/`, `uploads/`, `backend/ocr_config.toml`) must
  not be committed to the repository.
