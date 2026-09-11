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

Tech: Python 3 + FastAPI backend, ocrmypdf (plugin: the standalone
[`ocrmypdf_unlimited`](ocrmypdf_unlimited/README.md) package, with
`backend/ocrmypad/` kept as a compatibility alias), vanilla-JS frontend,
pypdfium2 + pikepdf (the only two PDF libraries — see
`backend/pdf_processing.py`), httpx. No PyMuPDF (AGPL), no CUDA/NVIDIA.
**All coordinates are integers in raw pixel space** (top-left origin), which is
the single most important invariant to preserve.

## Hard invariants (do not break)

- Block bboxes are `[x1, y1, x2, y2]` **integers in raw pixel space** of the page
  image. The plugin converts the unlimited model's 1000×1000 normalized canvas
  back to raw pixels *before* anything else sees it — via
  `ocrmypdf_unlimited.geometry.normalize_bbox` (the single source of truth for
  that mapping; `backend.errors` keeps an identical host-side copy, pinned by
  `tests/test_ocrmypdf_unlimited_plugin.py::test_coordinate_mapping_matches_host_copy`).
- hOCR written by the plugin MUST carry `scan_res <dpi> <dpi>` (the fpdf2
  renderer's px→pt transform derives from it) and every `ocr_line` MUST contain
  at least one `ocrx_word` child (the hocrtransform parser drops empty lines).
- Engine-agnostic boundary: the raw `<|det|>` marker stream never leaves
  `ocrmypdf_unlimited`; the rest of the backend reads the block sidecar JSON only.
- API keys/providers come only from external config — never hardcode. Base
  config is TOML: `backend/config.py::resolve()` reads `backend/ocr_config.toml`
  (the WebUI also persists there). `OCR_*` environment variables are read by
  `resolve()` as **highest-priority overrides** (`_ENV_ALIASES` maps names), so
  they can override file/WebUI values for the running process. Do not
  reintroduce JSON / `.env` file config, and never read `os.environ` outside
  `backend/config.py`. Two sanctioned exceptions: the STANDALONE plugin has its
  own `OCR_UNLIMITED_*` env surface in `ocrmypdf_unlimited/settings.py`, and
  `backend/bundled_tools.py` sets `PATH`/`LD_LIBRARY_PATH`/`TESSDATA_PREFIX` so
  a packaged build can use the Tesseract it ships. Add new env coupling THERE
  or in `config.py` — not scattered through the code.
- **Tesseract is optional but, when bundled, must actually be used.** A
  packaged build may ship its own `tesseract` under `_internal/tesseract/`;
  `backend.bundled_tools.activate()` puts it ahead of any system copy and is
  called from `run_ocr`, the server lifespan and the CLI. Locating it relies on
  OCRmyPDF resolving `tesseract` by name on `PATH`, so keep that call before
  any ocrmypdf invocation. `TESSDATA_PREFIX` points at the tessdata DIRECTORY
  (tesseract 5.x semantics — the parent makes it list bogus `tessdata/eng`).
- Config reaches the plugin three ways, highest first:
  `--unlimited-*` CLI/API args (`options.unlimited_*`) > `OCR_UNLIMITED_*` env >
  host-injected snapshot. The host pushes `config.resolve()` into the plugin's
  own store at startup / on settings save / in the CLI:
  `ocrmypdf_unlimited.settings.configure(...)` (guarded — the app must run
  without the plugin). The plugin never imports `backend.config`.
- **Cancellation is an EXCLUSIVE capability of the unlimited plugin**:
  ocrmypdf itself has no mid-run cancel hook (a hard interrupt would lose every
  completed page). The plugin polls `<job_dir>/cancel` per page
  (`ocrmypdf_unlimited.files.is_cancelled`); the host requests a stop by
  creating that file (`backend.page_store.request_cancel`). No shared registry,
  no imports.
- `max_tokens` must stay `< 32768` (enforced by the plugin's own CLI arg too).
- ocrmypdf runs must use `use_threads=True` (the engines are HTTP/IO-bound;
  a forked child could not report progress through the work files).
- **Never derive a writable location from `Path(__file__)`.** In a packaged
  build `__file__` points inside a read-only or temporary bundle
  (`sys._MEIPASS`). Writable state comes from `backend/paths.py`
  (`UPLOAD_DIR` / `WORK_DIR` / `OUTPUT_DIR` / `CONFIG_FILE` / `LOG_FILE`) and
  read-only bundled assets from `paths.resource_dir()`. Add new locations
  THERE, not ad hoc — and never write next to the code.
- **Never start uvicorn with `reload=True`, `workers>1`, or the string form
  `"pkg.mod:app"`.** The reloader/multi-process paths re-execute
  `sys.executable`, which in a frozen app is the application itself. Use
  `backend.server.EmbeddedServer` (background thread, `127.0.0.1`,
  kernel-assigned port) or `backend.main.run()`, and call
  `multiprocessing.freeze_support()` in every new entry point before heavy
  imports.
- Runtime artifacts (`output/`, `work/`, `uploads/`, `logs/`, `build/`,
  `dist/`, `.venv/`, `backend/ocr_config.toml`) are gitignored. Never commit
  keys or large sample PDFs.

## Layout

```
ocrmypdf_unlimited/        # THE standalone OCRmyPDF plugin (pip-installable)
  __init__.py              # package + hookimpl re-exports (loads as a plugin)
  options.py               # initialize/add_options/check_options hooks (--unlimited-*)
  settings.py              # config resolution: options > OCR_UNLIMITED_* env > snapshot
  engine.py                # OcrEngine plugin (unlimited) + get_ocr_engine
  client.py                # OpenAI-compatible client (retry/timeout/truncation)
  parser.py                # <|det|> markers -> blocks -> hOCR
  batching.py              # multi-page batcher (leader-follower)
  line_split.py            # per-line bbox recovery from page images
  text_norm.py             # math/table/LaTeX text normalization
  geometry.py              # 1000-canvas -> pixel bbox mapping (canonical)
  http_retry.py            # HTTP retry/backoff/rate-limit
  files.py                 # work-folder + cancel-flag file contract
  errors.py                # UnavailableError (plugin's own)
  pyproject.toml           # entry point "ocrmypdf": unlimited = ocrmypdf_unlimited
  README.md                # usage straight from the plugin docs
backend/
  main.py                 # FastAPI app + all routes
  config.py               # external setting resolution (resolve())
  page_store.py           # engine-agnostic page interchange (sidecar/hOCR/cancel)
  ocrmypad/               # COMPATIBILITY ALIAS -> ocrmypdf_unlimited (deprecated)
  errors.py               # UnavailableError (aliased to plugin's) + coordinate copy
  ocr_service.py          # job flow on _pdf_to_hocr + _hocr_to_ocr_pdf
  models.py               # editor page JSON (OcrPage/OcrBlock compatible)
  export.py               # other-format export: markdown + LaTeX (pure builders)
  paths.py                # path resolution: source checkout vs frozen bundle
  server.py               # EmbeddedServer: uvicorn on a thread, 127.0.0.1, free port
  lifecycle.py            # desktop mode flag + quit state (the WebUI Quit button)
  bundled_tools.py        # put the bundled Tesseract on PATH/LD_LIBRARY_PATH/TESSDATA_PREFIX
  pdf_processing.py       # the ONLY PDF-library seam (pypdfium2: previews/geometry/text)
  validation.py           # post-embed coverage report
  batch.py                # ZIP packaging (streamed)
  image_export.py         # markdown image embedding: crop image blocks -> zip/base64
  cleanup.py              # temp-file cleanup
  cli.py                  # headless CLI (python -m backend.cli)
desktop.py                # desktop entry point (freeze_support, window, exit cleanup)
packaging/                # desktop packaging: PyInstaller spec, build.py, bundle_tesseract.py, smoke_test.py
frontend/                 # index.html / style.css / app.js / i18n.js (no build)
tests/                    # pytest suite (incl. tests/test_ocrmypdf_unlimited_plugin.py)
requirements.txt          # server / CLI / test dependencies
requirements-desktop.txt  # optional: pywebview + PyGObject + pyinstaller
AGENTS.md  README.md  DESIGN.md  config.example.toml  .gitignore
```

## The engine-agnostic page interchange (backend/page_store.py)

This is the ONLY channel the rest of the backend uses to talk about pages —
no engine's raw output format ever leaks past it, and the backend never
imports any `ocrmypdf_unlimited` internals (engine/client/parser). The plugin
likewise never imports `backend.*`; the two sides meet ONLY on the filesystem
(paths pinned by `tests/test_ocrmypdf_unlimited_plugin.py`), plus the guarded
config-injection seam (`ocrmypdf_unlimited.settings.configure(...)`):

- **Block sidecar** (`work/<job>/hocr/000001_ocr_hocr.blocks.json`) — the
  normalized page representation (blocks in raw pixel space) the WebUI edits.
  Written natively by engines that produce it (the unlimited plugin) and
  **derived from hOCR** for engines that do not (ocrmypdf's built-in
  Tesseract, and any future engine) — via ocrmypdf's own hOCR parser.
- **hOCR** (`000001_ocr_hocr.hocr`) — the interchange format every engine
  implements (`generate_hocr` is the abstract contract). This is also what
  ocrmypdf's finalize stage renders into the text layer.
- **Cancel flag** (`work/<job>/cancel`) — the host writes it
  (`page_store.request_cancel`); the PLUGIN polls it per page
  (`ocrmypdf_unlimited.files.is_cancelled`). This graceful stop is an
  exclusive capability of the unlimited engine — ocrmypdf itself can only be
  hard-interrupted, losing completed pages. No shared registry, no imports.
- **Page inventory** (`page_store.page_numbers`) — the union of hOCR files
  and sidecars: what "retry remaining", progress counts and the embed guard
  use (for every engine).

## How to write a new OCR engine (the main extension point)

An engine packages one OCR engine behind OCRmyPDF's `OcrEngine` interface so
the rest of the backend is engine-agnostic. ocrmypdf already ships Tesseract
and a null engine. The unlimited engine is a standalone plugin package
(`ocrmypdf_unlimited/`); add a NEW engine either the same way (preferred — a
self-contained package that follows the plugin docs) or as a module inside an
existing plugin package.

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

1. **Create `<pkg>/<engine>_engine.py`** (or a `<pkg>/<engine>.py` module)
   implementing the contract. Config resolution follows the plugin's own
   `settings.effective(options)` pattern: plugin CLI/API args >
   `OCR_<ENGINE>_*` env > host-injected snapshot. The app pushes its config
   with `ocrmypdf_unlimited.settings.configure(resolve())`.
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
   `<job_dir>/cancel` (`ocrmypdf_unlimited.files.is_cancelled`; the job dir is
   `options.output_folder`'s parent) and raise to stop; no registry, no
   backend imports. (A host engine SHOULD NOT reuse the plugin's cancel
   semantics blindly, but the file contract is shared.)
5. **Register it**: `get_ocr_engine` hook + (for the WebUI's engine list)
   `backend/main.py::health` + `frontend` engine selects. Expose it so the app
   loads it like the unlimited plugin:
   `ocr_service.plugin_path()` (dotted module name, entry-point aware).
6. **Error semantics**: missing dependency / bad setup → raise an
   `UnavailableError` (the app's `backend.errors.UnavailableError` aliases the
   plugin's class, so the friendly message surfaces); genuine OCR failures →
   raise `RuntimeError` (fails the run; the retry flow re-runs it).

### Editing checklist

- [ ] `get_ocr_engine` hook returns the engine only for its own `ocr_engine` name.
- [ ] Every hOCR/`ocrx_word` bbox is integer raw-pixel `[x1,y1,x2,y2]`,
      `x1<=x2`, `y1<=y2`; `scan_res` present.
- [ ] Block sidecar JSON written next to the hOCR (WebUI edits it).
- [ ] Missing optional dependency raises `UnavailableError`, not a traceback.
- [ ] No hardcoded keys/URLs; settings come from plugin options / env / the
      host-injected snapshot (`ocrmypdf_unlimited.settings`), never
      `backend.config` directly.
- [ ] Works via `ocrmypdf.api._pdf_to_hocr(..., ocr_engine='my_engine')` with
      the plugin package loaded (see `ocr_service.plugin_path()`).

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
- **Plugin selection**: the app requests the standalone plugin by dotted
  module name — `plugins=['ocrmypdf_unlimited']` (see `ocr_service.plugin_path()`);
  when it is pip-installed (its `ocrmypdf` entry point active),
  `plugin_path()` returns `None` so OCRmyPDF auto-loads it and pluggy never
  sees the same module twice. Loading the OLD path shim
  (`backend/ocrmypad/__init__.py`) still works but is deprecated.
- **Testing**: a pytest suite lives in `tests/` (run `.venv/bin/python -m pytest`).
  The standalone-plugin contract is pinned by
  `tests/test_ocrmypdf_unlimited_plugin.py` (independence, options hooks,
  entry-point discovery, exclusive cancel, filesystem contract, and a real
  `_pdf_to_hocr` end-to-end run). When adding logic (especially coordinate
  mapping, hOCR generation and parsing), prefer pure functions and extend the
  suite.
- **Vibe-coding notice**: this project was generated largely by AI. Re-verify
  correctness rather than assuming prior code is bug-free; prefer small,
  reviewable diffs.

## Useful commands

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
# system deps (once): tesseract-ocr (only for the Tesseract engine);
# ghostscript is OPTIONAL since OCRmyPDF 17.0 (needed only for PDF/A output)
uvicorn backend.main:app --host 0.0.0.0 --port 8000   # or: python -m backend.main
# health + engines:
curl http://localhost:8000/api/health
# headless:
python -m backend.cli input.pdf -o output/out.pdf --pages 1-3
# desktop app: run in source, or build a frozen bundle (see packaging/README.md)
python desktop.py --no-window
python packaging/build.py --clean && ./dist/pdf-ocr-embed/pdf-ocr-embed
```
