# AGENTS.md — Guidance for AI coding agents

> This file is written primarily for **AI coding agents** (Claude Code, Cursor,
> GitHub Copilot, DSH, etc.) that read the repo before editing. Humans may find
> it useful too, but the concise human-facing guide is `README.md`.

## What this project is

`pdf-ocr-embed` turns a pure-image (scanned) PDF into one with a searchable /
selectable / copyable **invisible text layer**. Pipeline per page:

1. PyMuPDF renders the page to a PNG image.
2. An **OCR adapter** (`backend/sources/*`) recognizes text and returns a
   normalized `OcrPage` with block bounding boxes in **original pixel space**.
3. PyMuPDF embeds the text invisibly (`render_mode=3`) so PDF geometry matches
   the pixel bboxes after a y-axis flip.
4. A single-page WebUI (native JS, no build step) lets users edit the recognized
   text, set OCR settings, watch SSE progress, and download the `*_embedded.pdf`.

Tech: Python 3 + FastAPI backend, vanilla-JS single-page frontend, PyMuPDF,
httpx. No CUDA/NVIDIA. **All coordinates are integers in raw pixel space**
(top-left origin), which is the single most important invariant to preserve.

## Hard invariants (do not break)

- `OcrBlock.bbox` is `[x1, y1, x2, y2]` **integers in original pixel space** of
  the page image. Adapters must convert any engine-specific coordinate system
  (e.g. a 1000×1000 normalized canvas) back to raw pixels *before* returning.
- Adapters return a normalized `backend.models.OcrPage`; the rest of the backend
  must **never** depend on one engine's raw output format.
- API keys/providers come only from external config — never hardcode. Base
  config is TOML: `backend/config.py::resolve()` reads `backend/ocr_config.toml`
  (the WebUI also persists there). `OCR_*` environment variables are read by
  `resolve()` as **highest-priority overrides** (`_ENV_ALIASES` maps names), so
  they can override file/WebUI values for the running process. Do not reintroduce
  JSON / `.env` file config, and never read `os.environ` outside
  `backend/config.py` — env-sourced values should reach the rest of the code
  through `resolve()`.
- `max_tokens` must stay `< 32768`.
- Runtime artifacts (`output/`, `work/`, `uploads/`, `.venv/`,
  `backend/ocr_config.toml`) are gitignored. Never commit keys or large sample PDFs.

## Layout

```
backend/
  main.py                 # FastAPI app + all routes
  config.py               # external setting resolution (resolve())
  models.py               # OcrPage / OcrBlock (normalized schema)
  pdf_processing.py       # render page->PNG + invisible text embedding
  ocr_service.py          # jobs, per-page progress, concurrency (thread pool)
  sources/
    base.py               # OcrSource ABC + coordinate helpers + UnavailableError
    factory.py            # adapter registry + get_adapter(name)
    unlimited_ocr_adapter.py     # reference implementation (marker format)
    tesseract_adapter.py         # local OCR via pytesseract
    generic_openai_adapter.py    # generic OpenAI-compatible vision model
frontend/                 # index.html / style.css / app.js (no build)
requirements.txt
AGENTS.md  README.md  DESIGN.md  config.example.toml  .gitignore
```

## How to write a new OCR adapter (the main extension point)

An adapter packages one OCR engine behind the `OcrSource` interface so the rest
of the backend is engine-agnostic. Follow the pattern exactly; the
`unlimited_ocr_adapter.py` is the reference.

### Result schema (what you must produce)

Every block is a `backend.models.OcrBlock`:

```python
OcrBlock(
    kind=...,      # "text" | "heading" | "equation" | "table" | "image"
                   # | "image_caption" | "footnote"  (free string, lowercase)
    bbox=[x1, y1, x2, y2],  # INTEGERS, raw pixel space, top-left origin
    text=...,               # recognized text ("" for pure image blocks)
    caption=...,            # for image blocks, else ""
    conf=...,               # Optional[float] 0..1 (may be None)
)
```

Return one `OcrPage(page_index, width, height, blocks=[...])` per page.

### Steps

1. **Create `backend/sources/<engine>_adapter.py`** defining a subclass:

   ```python
   from backend.sources.base import OcrSource
   from backend.models import OcrBlock, OcrPage

   class MyAdapter(OcrSource):
       name = "my_engine"  # lowercase; used by factory.get_adapter(name)

       def __init__(self, ...):   # optional; resolve() for defaults
           cfg = resolve()        # -> dict of external settings
           ...

       def recognize_pixels(self, image_path, width, height, page_index) -> OcrPage:
           """Run OCR on the PNG at image_path; return normalized OcrPage."""
           ...  # engine call + parse
           return OcrPage(page_index=page_index, width=width, height=height,
                          blocks=blocks)

       def close(self):
           """Release any held resources (client, subprocess). Optional."""
   ```

2. **Register it** in `backend/sources/factory.py` `_REGISTRY`:
   ```python
   from backend.sources.my_adapter import MyAdapter
   _REGISTRY = { ... , MyAdapter.name: MyAdapter }
   ```
   `get_adapter("my_engine")` then resolves it, and it appears in
   `/api/health`'s available-adapters list automatically.

3. **Coordinate conversion — the critical part.** If your engine returns
   coordinates in anything other than raw pixels, convert them:
   - 1000×1000 normalized canvas → use `backend.sources.base.normalize_bbox(bbox, width, height)`
     (per-axis linear scale; returns integer pixel bbox).
   - If the engine already outputs raw pixels (like Tesseract's TSV), use them
     directly — do **not** normalize.
   - Clamp/round to integers; ensure `x1<=x2`, `y1<=y2`.

4. **Handle missing deps / unavailable engine** by raising
   `backend.sources.base.UnavailableError` with a clear setup message (e.g. "pip
   install X" or "set `tess_cmd` in backend/ocr_config.toml") — the backend
   surfaces this to the user gracefully. Raise `RuntimeError` for genuine OCR
   failures, and log via `logging.getLogger(__name__)`.

### Config convention

Pull engine settings from `backend.config.resolve()` (a dict) and/or explicit
constructor args, and mirror them into `backend/ocr_config.toml` keys. Follow
the existing flat naming: `<engine>_<setting>` config key (e.g. `tess_lang`,
`tess_cmd`, `generic_prompt`). The legacy `OCR_<ENGINE>_<SETTING>` environment
variables are handled centrally by `resolve()` (see `_ENV_ALIASES`) and
override the TOML/WebUI values — adapters must go through `resolve()`, never
`os.environ`. Resolve defaults in `__init__`, not in `recognize_pixels`.

### Editing checklist

- [ ] `name` registered in `factory._REGISTRY`; class import added there.
- [ ] Every returned bbox is integer raw-pixel `[x1,y1,x2,y2]`, `x1<=x2`,`y1<=y2`.
- [ ] Missing optional dependency raises `UnavailableError`, not a traceback.
- [ ] No hardcoded keys/URLs; settings come from `resolve()` / args (TOML config).
- [ ] `close()` releases long-lived resources (httpx client, subprocess).
- [ ] Works via `get_adapter("<name>")` and shows in `/api/health`.

## Conventions & gotchas

- **Python**: use `from __future__ import annotations` in new modules; type hints;
  dataclasses for data; `logging` not `print`. Keep it dependency-light.
- **Frontend has no build step** — edit `frontend/index.html`, `style.css`,
  `app.js` directly; no bundler to run.
- **Testing**: there is no test suite yet. When adding logic (especially
  coordinate mapping and parsing), prefer pure functions and add a test if one
  exists to extend; otherwise keep parsing in isolated static methods.
- **Vibe-coding notice**: this project was generated largely by AI
  (DeepSeek V4 Flash). Re-verify correctness rather than assuming prior code is
  bug-free; prefer small, reviewable diffs.

## Useful commands

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000   # or: python -m backend.main
# health + available adapters:
curl http://localhost:8000/api/health
```