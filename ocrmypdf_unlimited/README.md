# ocrmypdf-unlimited

A standalone **[OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) plugin** (≥
17.11) that adds the `unlimited` OCR engine: an OpenAI-compatible vision model
that recognises text, tables, equations and captions and outputs `<|det|>`
marker streams, which the plugin parses into hOCR (an invisible, searchable
text layer) plus an editable block-sidecar JSON.

It is packaged strictly per the
[OCRmyPDF plugin documentation](https://ocrmypdf.readthedocs.io/en/latest/plugins.html)
and is **completely independent** of `pdf-ocr-embed` (the app it was born
out of) — and the app is likewise independent of it.

## Install

```bash
pip install .            # from this directory; installs into the current venv
```

This registers the `ocrmypdf` entry point `unlimited`, so OCRmyPDF finds the
plugin automatically whenever they share a venv.

## Usage

```bash
# Command line (script or packaged plugin both work):
ocrmypdf --plugin ocrmypdf_unlimited --ocr-engine unlimited \
         --unlimited-api-key "$KEY" \
         --unlimited-model unlimited-ocr \
         input.pdf output.pdf
```

```python
import ocrmypdf

ocrmypdf.ocr(
    "input.pdf", "output.pdf",
    plugins=["ocrmypdf_unlimited"],
    ocr_engine="unlimited",
    unlimited_api_key=...,
    unlimited_model=...,
)
```

The plugin also supports the two-phase editing workflow:

```python
import ocrmypdf.api
ocrmypdf.api._pdf_to_hocr("input.pdf", "hocr_work", plugins=["ocrmypdf_unlimited"],
                          ocr_engine="unlimited", unlimited_api_key=...)
# ... edit hocr_work/*.blocks.json ...
ocrmypdf.api._hocr_to_ocr_pdf("hocr_work", "output.pdf", use_threads=True)
```

> Note: the docs warn that plugins should not be auto-loaded for all files —
> the entry point does make it load by default, so pass `--ocr-engine` /
> `ocr_engine=` explicitly (anything other than `unlimited` falls through to
> the built-in engines), or load explicitly with `--plugin`.

## Configuration

Priority (highest first):

1. **Plugin options** — `--unlimited-api-key`, `--unlimited-base-url`,
   `--unlimited-model`, `--unlimited-max-tokens`, `--unlimited-batch-size`,
   `--unlimited-batch-timeout-ms`, `--unlimited-batch-per-page-tokens`,
   `--unlimited-max-retries`, `--unlimited-retry-base-delay`,
   `--unlimited-retry-max-delay`, `--unlimited-rate-limit-rps`
   (also valid as `ocr()` / `_pdf_to_hocr()` keyword arguments).
2. **Environment** — `OCR_UNLIMITED_API_KEY`, `OCR_UNLIMITED_BASE_URL`,
   `OCR_UNLIMITED_MODEL`, `OCR_UNLIMITED_MAX_TOKENS`, `OCR_UNLIMITED_BATCH_SIZE`,
   `OCR_UNLIMITED_BATCH_TIMEOUT_MS`, `OCR_UNLIMITED_BATCH_PER_PAGE_TOKENS`,
   `OCR_UNLIMITED_MAX_RETRIES`, `OCR_UNLIMITED_RETRY_BASE_DELAY`,
   `OCR_UNLIMITED_RETRY_MAX_DELAY`, `OCR_UNLIMITED_RATE_LIMIT_RPS`.
3. **Host-injected snapshot** — a host app can push its config with
   `ocrmypdf_unlimited.settings.configure({...})`; with no host this is empty.
4. **Defaults** — endpoint `https://api.llm.ustc.edu.cn/v1`, model
   `unlimited-ocr`, `max_tokens=16384` (hard cap `< 32768`).

## Cancellation (this plugin's exclusive capability)

Bare OCRmyPDF can only be hard-interrupted (losing already-completed pages).
This plugin adds **graceful per-page cancellation**: it polls
`<job_dir>/cancel` between pages (job dir = `options.output_folder`'s parent,
e.g. the `work/<job>/hocr/..` convention) and stops cleanly, keeping every
page already written to disk. A host requests a stop simply by creating the
`cancel` file; anything else it does with the work folder is via hOCR /
sidecar files only — no shared registry, no imports. The file contract lives
in `ocrmypdf_unlimited/files.py`.
