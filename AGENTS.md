# AGENTS.md — Guidance for AI coding agents

> This file is written primarily for **AI coding agents** (Claude Code, Cursor,
> GitHub Copilot, DSH, etc.) that read the repo before editing. Humans may find
> it useful too, but the concise human-facing guide is `README.md`.

## What this project is

`pdf-ocr-embed` turns a pure-image (scanned) PDF into one with a searchable /
selectable / copyable **invisible text layer**. The OCR core is
**[OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF)** (≥17.11); the
`unlimited-ocr` engine ships as an **OCRmyPDF plugin**. Pipeline per job:

1. `POST /api/ocr/upload` → FastAPI stores the PDF, opens a job.
2. **OCR phase** — `ocrmypdf.api._pdf_to_hocr(pdf, work/<job>/hocr, plugins=[...])`:
   OCRmyPDF rasterizes pages, runs the selected engine per page (`jobs`-way
   concurrency), leaving per-page `000001_ocr_hocr.hocr` (the interchange
   format every engine implements). The unlimited plugin additionally writes a
   block sidecar `000001_ocr_hocr.blocks.json`.
3. **Edit phase** — `POST /api/pages/{job}/{i}` writes edits back into the
   block sidecar (derived from the hOCR when the engine wrote none) and
   regenerates that page's hOCR from it.
4. **Finalize** — `ocrmypdf.api._hocr_to_ocr_pdf(work/<job>/hocr,
   <source stem>_embedded.pdf)`: OCRmyPDF renders the (possibly edited) hOCR
   into an invisible text layer via its fpdf2 renderer, grafts it onto the
   original pages, postprocesses.
5. A single-page WebUI (native JS, no build step) drives all of it over
   `/api/*` + SSE progress.

Tech: Python 3 + FastAPI backend, ocrmypdf (plugin: `backend/ocrmypad`),
vanilla-JS frontend, PyMuPDF (page previews), httpx. No CUDA/NVIDIA.
**All coordinates are integers in raw pixel space** (top-left origin), which is
the single most important invariant to preserve.

## Hard invariants (do not break)

- Block bboxes are `[x1, y1, x2, y2]` **integers in raw pixel space** of the page
  image. The plugin converts the unlimited model's 1000×1000 normalized canvas
  back to raw pixels *before* anything else sees it — via
  `backend.errors.normalize_bbox` (the single source of truth for that mapping).
- hOCR written by the plugin MUST carry `scan_res <dpi> <dpi>` (the fpdf2
  renderer's px→pt transform derives from it) and every `ocr_line` MUST contain
  at least one `ocrx_word` child (the hocrtransform parser drops empty lines).
- Engine-agnostic boundary: the raw `<|det|>` marker stream never leaves
  `backend/ocrmypad`; the rest of the backend reads the block sidecar JSON only.
- API keys/providers come only from external config — never hardcode. Base
  config is TOML: `backend/config.py::resolve()` reads `backend/ocr_config.toml`
  (the WebUI also persists there). `OCR_*` environment variables are read by
  `resolve()` as **highest-priority overrides** (`_ENV_ALIASES` maps names), so
  they can override file/WebUI values for the running process. Do not
  reintroduce JSON / `.env` file config, and never read `os.environ` outside
  `backend/config.py`.
- `max_tokens` must stay `< 32768`.
- ocrmypdf runs must use `use_threads=True` (the engines are HTTP/IO-bound;
  a forked child could not report progress through the work files).
- Runtime artifacts (`output/`, `work/`, `uploads/`, `.venv/`,
  `backend/ocr_config.toml`) are gitignored. Never commit keys or large sample PDFs.

## Layout

```
backend/
  main.py                 # FastAPI app + all routes
  config.py               # external setting resolution (resolve())
  page_store.py           # engine-agnostic page interchange (sidecar/hOCR/cancel)
  ocrmypad/               # OCRmyPDF plugin package (unlimited engine)
    unlimited_engine.py   # OcrEngine plugin + get_ocr_engine hook
    engine_client.py      # OpenAI-compatible client (retry/timeout/truncation)
    parser.py             # <|det|> markers -> blocks -> hOCR
    text_norm.py          # math/table/LaTeX text normalization
  errors.py               # UnavailableError + 1000-canvas -> pixel bbox mapping
  http_retry.py           # HTTP retry/backoff/rate-limit (engine API calls)
  ocr_service.py          # job flow on _pdf_to_hocr + _hocr_to_ocr_pdf
  models.py               # editor page JSON (OcrPage/OcrBlock compatible)
  pdf_processing.py       # page preview rendering (PyMuPDF)
  validation.py           # post-embed coverage report
  batch.py                # ZIP packaging (streamed)
  cleanup.py              # temp-file cleanup
  cli.py                  # headless CLI (python -m backend.cli)
frontend/                 # index.html / style.css / app.js / i18n.js (no build)
requirements.txt
AGENTS.md  README.md  DESIGN.md  config.example.toml  .gitignore
```

## The engine-agnostic page interchange (backend/page_store.py)

This is the ONLY channel the rest of the backend uses to talk about pages —
no engine's raw output format ever leaks past it, and the backend never
imports `backend.ocrmypad`:

- **Block sidecar** (`work/<job>/hocr/000001_ocr_hocr.blocks.json`) — the
  normalized page representation (blocks in raw pixel space) the WebUI edits.
  Written natively by engines that produce it (the unlimited plugin) and
  **derived from hOCR** for engines that do not (ocrmypdf's built-in
  Tesseract, and any future engine) — via ocrmypdf's own hOCR parser.
- **hOCR** (`000001_ocr_hocr.hocr`) — the interchange format every engine
  implements (`generate_hocr` is the abstract contract). This is also what
  ocrmypdf's finalize stage renders into the text layer.
- **Cancel flag** (`work/<job>/cancel`) — a plain file the service creates to
  ask the engine to stop; the engine polls for it per page. No shared
  registry, no imports: the engine and the backend communicate ONLY through
  the job's work folder on disk.
- **Page inventory** (`page_store.page_numbers`) — the union of hOCR files
  and sidecars: what "retry remaining", progress counts and the embed guard
  use (for every engine).

## How to write a new OCR engine (the main extension point)

An engine packages one OCR engine behind OCRmyPDF's `OcrEngine` interface so
the rest of the backend is engine-agnostic. ocrmypdf already ships Tesseract
and a null engine; add yours as a module in `backend/ocrmypad/`.

### The contract

ocrmypdf calls the engine **per page** in its own worker threads
(`use_threads`), with the page already rasterized to an image:

```python
from ocrmypdf import hookimpl
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence

class MyEngine(OcrEngine):
    @staticmethod
    def version() -> str: ...
    @staticmethod
    def creator_tag(options) -> str: ...
    def __str__(self) -> str: ...
    @staticmethod
    def languages(options) -> set[str]: ...   # accepted languages
    @staticmethod
    def get_orientation(input_file, options) -> OrientationConfidence: ...
    @staticmethod
    def generate_hocr(input_file, output_hocr, output_text, options) -> None:
        """OCR the image at input_file; write hOCR + sidecar text."""
    @staticmethod
    def get_deskew(input_file, options) -> float: return 0.0  # optional
    @staticmethod
    def supports_generate_ocr() -> bool: return False          # optional

@hookimpl
def get_ocr_engine(options):
    # Return your engine only when options.ocr_engine selects it;
    # otherwise return None so ocrmypdf's built-ins handle it.
    if options is not None and getattr(options, "ocr_engine", "auto") != "my_engine":
        return None
    return MyEngine()
```

### Steps

1. **Create `backend/ocrmypad/<engine>_engine.py`** implementing the contract.
   Read settings via `backend.config.resolve()` (never `os.environ`), resolve
   defaults in the client constructor, not per call.
2. **Emit hOCR** matching what `ocrmypdf.hocrtransform` parses:
   `div.ocr_page` (title: `bbox 0 0 W H; ppageno N; scan_res DPI DPI`) →
   `p.ocr_par` → `span.ocr_line` → `span.ocrx_word` (bbox in **raw pixels**,
   top-left origin). Missing `scan_res` = wrong text scale; empty lines are
   dropped by the parser.
3. **Write a block sidecar JSON** next to the hOCR
   (`output_hocr.with_name(output_hocr.stem + ".blocks.json")`) with
   `{"page": {page_index, width, height, blocks: [...]}, "dpi": ...}` — this
   is what the WebUI edits and what regenerates the hOCR after edits.
   OPTIONAL: an engine that writes only hOCR (like ocrmypdf's built-in
   Tesseract) is fully supported — the page store derives the sidecar from
   the hOCR via ocrmypdf's own parser.
4. **Report progress by writing files**: the engine's output files ARE the
   progress. Honor the cancel flag per page — check
   `backend.page_store.is_cancelled(job_dir)` (the job dir is
   `options.output_folder`'s parent) and raise to stop; no registry, no
   backend imports.
5. **Register it** in `backend/ocrmypad/__init__.py` and (for the WebUI's
   engine list) in `backend/main.py::health` + `frontend` engine selects.
6. **Error semantics**: missing dependency / bad setup → raise
   `backend.errors.UnavailableError` (surfaces a friendly message); genuine OCR
   failures → raise `RuntimeError` (fails the run; the retry flow re-runs it).

### Editing checklist

- [ ] `get_ocr_engine` hook returns the engine only for its own `ocr_engine` name.
- [ ] Every hOCR/`ocrx_word` bbox is integer raw-pixel `[x1,y1,x2,y2]`,
      `x1<=x2`, `y1<=y2`; `scan_res` present.
- [ ] Block sidecar JSON written next to the hOCR (WebUI edits it).
- [ ] Missing optional dependency raises `UnavailableError`, not a traceback.
- [ ] No hardcoded keys/URLs; settings come from `resolve()`.
- [ ] Works via `ocrmypdf.api._pdf_to_hocr(..., plugins=[backend/ocrmypad/__init__.py])`.

## Conventions & gotchas

- **Python**: use `from __future__ import annotations` in new modules; type hints;
  dataclasses for data; `logging` not `print`. Keep it dependency-light.
- **Frontend has no build step** — edit `frontend/index.html`, `style.css`,
  `app.js` directly; no bundler to run. i18n keys live in `frontend/i18n.js`
  (both `en` and `zh` dicts).
- **ocrmypdf private APIs**: the editing flow uses
  `ocrmypdf.api._pdf_to_hocr` / `ocrmypdf.api._hocr_to_ocr_pdf` (import from
  `ocrmypdf.api`, NOT the top-level package — they are not re-exported).
  These are marked experimental upstream; pin `ocrmypdf>=17.11` in
  requirements.txt and re-verify after upgrades.
- **Plugin selection**: ocrmypdf's plugin loader reads `plugins=[path]`; we pass
  `backend/ocrmypad/__init__.py` (see `ocr_service.plugin_path()`).
- **Testing**: a pytest suite lives in `tests/` (run `.venv/bin/python -m pytest`).
  When adding logic (especially coordinate mapping, hOCR generation and
  parsing), prefer pure functions and extend the suite.
- **Vibe-coding notice**: this project was generated largely by AI. Re-verify
  correctness rather than assuming prior code is bug-free; prefer small,
  reviewable diffs.

## Useful commands

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
# system deps (once): sudo apt-get install tesseract-ocr ghostscript
uvicorn backend.main:app --host 0.0.0.0 --port 8000   # or: python -m backend.main
# health + engines:
curl http://localhost:8000/api/health
# headless:
python -m backend.cli input.pdf -o output/out.pdf --pages 1-3
```
