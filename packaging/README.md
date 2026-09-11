# Packaging pdf-ocr-embed as a desktop app

This directory holds everything needed to turn the app into a double-clickable
desktop build with [PyInstaller](https://pyinstaller.org/).

## Quick start

```bash
# from the repo root, with the venv active
pip install -r requirements.txt -r requirements-desktop.txt
python packaging/build.py --clean

# run it (opens a native window, or the browser if pywebview is absent)
./dist/pdf-ocr-embed/pdf-ocr-embed

# headless smoke test
./dist/pdf-ocr-embed/pdf-ocr-embed --no-window
```

`--browser` forces the system browser; `--port N` pins the HTTP port (default:
a kernel-assigned free port on `127.0.0.1`).

**PyInstaller is not a cross-compiler** — build on each target OS separately
(GitHub Actions matrix: `windows-latest` / `macos-latest` / `ubuntu-latest`).
Use **onedir** (the default here), not onefile: onefile re-extracts the whole
bundle to a temp dir on every launch, and PyInstaller 6.13+ deprecates onefile
for macOS `.app` bundles.

## The window and how to quit

`desktop.py` renders the UI in the OS webview through
[pywebview](https://pywebview.flowrl.com/) (WebView2 / WKWebView /
WebKit2GTK). Closing the window quits the app.

If pywebview is missing or its backend cannot start, the app **falls back to
the system browser** rather than failing. To cover that case — and to give the
windowed mode a visible exit — the WebUI shows a **Quit** button whenever it
was launched by `desktop.py` (`/api/health` reports `desktop: true`). It calls
`POST /api/app/quit`, which requires an `X-PDF-OCR-Embed: quit` header; since
CORS only allows loopback origins, a random web page cannot pass the preflight
needed to send it.

Whichever way you exit, running OCR jobs are stopped through the project's own
`cancel`-flag contract first, so no completed pages are lost.

On Linux the native window needs system GTK/WebKit at runtime — for the
*packaged* build these are bundled, but a source checkout needs
`python3-gobject` + `webkit2gtk4.1` (Fedora) / `python3-gi` +
`gir1.2-webkit2-4.1` (Debian).

## What the build produces

```
dist/pdf-ocr-embed/
├── pdf-ocr-embed          # the launcher (entry: desktop.py)
└── _internal/
    ├── frontend/          # the WebUI, served by FastAPI (bundled data)
    ├── config.example.toml
    ├── libpdfium.so       # pypdfium2 (rendering/geometry/text)
    ├── pikepdf.libs/…     # libqpdf (page deletion)
    ├── libgtk-3.so.0 …    # GTK/WebKit for the native window
    └── …                  # Python runtime + the rest of the deps
```

~142 MB on Linux.  Ship it inside an installer (Inno Setup / MSI on Windows,
`.dmg` on macOS, AppImage / `.deb` on Linux) for the "double-click an app"
experience.

## Where a packaged build keeps its files

A frozen app must not write next to its code, so `backend/paths.py` splits
read-only resources from writable state:

| Kind | Source checkout | Packaged build (`platformdirs`) |
| --- | --- | --- |
| Frontend / example config | `./frontend`, `./config.example.toml` | `_internal/` (from `sys._MEIPASS`) |
| uploads / work / output | repo root | `user_data_dir("pdf-ocr-embed")` |
| `ocr_config.toml` | `backend/ocr_config.toml` | `user_config_dir("pdf-ocr-embed")` |
| log file | not written | `user_log_dir("pdf-ocr-embed")/app.log` |

Concretely: `%LOCALAPPDATA%\pdf-ocr-embed` (Windows),
`~/Library/Application Support/pdf-ocr-embed` (macOS),
`~/.local/share|~/.config|~/.local/state/pdf-ocr-embed` (Linux).

The packaged app therefore **does not read the repo's `backend/ocr_config.toml`** —
configure it through the WebUI Settings dialog, or set `OCR_*` environment
variables (they take priority).  `XDG_DATA_HOME` / `XDG_CONFIG_HOME` /
`XDG_STATE_HOME` relocate the Linux directories at runtime.

## Two deliberate choices in the spec

- **The frontend is bundled as data** and resolved from `sys._MEIPASS` at
  runtime.  If it is missing the app logs
  `frontend assets missing at … — the UI will not load` instead of serving a
  bare 404.
- **`ocrmypdf_unlimited` is collected as a module but its metadata is NOT
  copied.**  Copying the dist-info would make OCRmyPDF's entry-point scan
  auto-load the plugin *and* the app request it by dotted name, and pluggy
  rejects the same module registered twice.  `ocr_service.plugin_auto_loaded()`
  returns `False` when frozen so the dotted-module path is always used.

## Tesseract: bundled, so nothing has to be installed

The app can ship its own Tesseract — program, shared libraries and language
data — which is what makes the *Tesseract (local)* engine work on a machine
with nothing installed. `packaging/bundle_tesseract.py` stages it and the spec
ships it as `_internal/tesseract/`:

```bash
python packaging/build.py --clean --with-tesseract          # eng + chi_sim
python packaging/bundle_tesseract.py --langs eng,deu        # or stage directly
```

The bundler copies the host program and, on Linux/macOS, walks `ldd`/`otool`
for its shared-library closure (skipping glibc and the loader), plus `tessdata`
— the requested languages, `configs/` and `tessconfigs/`. The last two are
**mandatory**: OCRmyPDF runs tesseract with the `hocr`/`txt`/`pdf` configs.

Two things are easy to get wrong, and both are handled in
`backend/bundled_tools.py:activate()`:

- **`PATH` alone is not enough.** The staged `tesseract` links against
  `libtesseract`/`libleptonica`/… from `tesseract/lib/`, so that directory must
  be prepended to `LD_LIBRARY_PATH` (or `DYLD_LIBRARY_PATH`). On Windows the
  DLLs sit next to the executable and `PATH` covers them.
- **`TESSDATA_PREFIX` points at the tessdata directory ITSELF**, not its parent.
  Tesseract 4.1+ is widely documented as wanting the parent, but 5.x resolves
  `<TESSDATA_PREFIX>/<lang>.traineddata`; pointing at the parent makes it list
  bogus entries like `tessdata/eng`. Verified against **5.5.3**.

The command output it produces, and the fact that it is actually used, are
visible at runtime:

```bash
curl -s http://127.0.0.1:<port>/api/health | python -m json.tool
# "tesseract": {"bundled": true, "source": "bundled", "languages": ["chi_sim", "eng"], …}
```

Unbundled builds still work — the app just requires a system `tesseract` and
`source` reads `system`.

- **The Unlimited API engine needs no local binary at all** — just an API key.
- **Ghostscript is optional** since OCRmyPDF 17.0 — only PDF/A output needs it
  (the default `output_type="pdf"` rasterizes with pypdfium2).

One PyInstaller-specific trap: a frozen process rewrites the library search
path (`LD_LIBRARY_PATH`, `SetDllDirectoryW`, `DYLD_*`) and children inherit it,
which can make a bundled/system `tesseract` load an incompatible `.so`.  Reset
it before spawning external programs
([docs](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)).
On macOS, an app launched from Finder has a minimal `PATH`, so call bundled
binaries by absolute path.

## Bundle size

**~199 MB** on Linux without Tesseract, **~242 MB** with it (`--with-tesseract`:
~35 MB of staged program + libraries + `eng`/`chi_sim`, plus PyInstaller's own
copies).  PyInstaller's PyGObject hook collects the **entire** GTK data tree;
the spec prunes `share/icons/` (the Adwaita icon theme alone was 238 MB
uncompressed) and `share/locale/` because the UI is drawn inside the webview and
GTK only renders the window frame.  That one filter takes the bundle from
**458 MB to 199 MB** with the window still working.  If size matters, the next
candidates are `uvloop` (15 MB) and the GTK theme/fontconfig data.

## Verified

On Linux (`PyInstaller 6.22.2`, Python 3.14) the built bundle was verified end to
end:

- a **native GTK/WebKit window** opens (no browser fallback) with a clean log —
  no tracebacks, no backend-probe noise;
- the frontend loads and `ocrmypdf` + `ocrmypdf_unlimited` import
  (`/api/health` reports the `unlimited` adapter);
- page-preview rendering (PNG 1241×1786, identical to the source-mode output);
- a **full local pipeline** — upload → Tesseract OCR → embed → download —
  producing an output PDF with a real invisible text layer (995 chars,
  validation coverage 0.9962);
- the Quit path: `POST /api/app/quit` is **403** without its header, and quits
  the app cleanly (~2 s, exit code 0) with it;
- writable state lands in the per-user directories, not the bundle.

**Self-containment with Tesseract bundled** (the strongest check): the app was
launched with `PATH` pointing at an *empty directory*, so the system `tesseract`
was unreachable. It still reported

```
"tesseract": {"bundled": true, "source": "bundled",
              "path": ".../_internal/tesseract/bin/tesseract",
              "languages": ["chi_sim", "eng"]}
```

and completed a full OCR run through the *Tesseract (local)* engine → embed →
download, producing the same 995-character text layer. `smoke_test.py
--expect-tesseract` asserts this in CI (and was checked to fail when the
bundled directory is removed, so the assertion is not vacuous).

## Continuous integration

[`.github/workflows/desktop-build.yml`](../.github/workflows/desktop-build.yml)
runs on every push to `main`, on pull requests, and on demand:

1. **test** — the pytest suite on Linux/Python 3.14 (a fast gate);
2. **build** — a matrix over `ubuntu-latest` / `windows-latest` / `macos-latest`
   that runs `packaging/build.py`, then `packaging/smoke_test.py`, then uploads
   `dist/pdf-ocr-embed` as a 14-day artifact.

`packaging/smoke_test.py` is the part that matters: it launches the *frozen*
binary headless, asserts the frontend and `/api/health` answer, that the
`unlimited` plugin imported, that `POST /api/app/quit` is **403** without its
confirmation header, and that the process then exits 0 — so a bundle that
merely built but cannot run still fails the job.

The Linux job installs the GTK stack as a `continue-on-error` step: if
PyGObject/WebKit cannot be set up on the runner, the bundle is still produced and
the app falls back to the system browser. On `ubuntu-latest` it succeeds — the
step pulls **PyGObject 3.50.0** through `pywebview[gtk]` (the pin that matches
the runner's `girepository-1.0`), so the Linux artifact does carry the native
window. Windows (WebView2) and macOS (WKWebView) need no extra system packages.

### Verified on GitHub Actions

Every push builds all three platforms, and each bundle's **own** Tesseract is
executed with only the libraries and language data inside that bundle:

| Job | Result | Tesseract staged | Artifact |
| --- | --- | --- | --- |
| Test suite | ✅ ~40 s | — | pytest on clean `ubuntu-latest` + Python 3.14 |
| Build linux-x86_64 | ✅ ~2 m | 54 files | **152 MB** zipped (GTK + Tesseract) |
| Build windows-x86_64 | ✅ ~2.5 m | 73 files | **136 MB** (Tesseract, system WebView2) |
| Build macos-arm64 | ✅ ~1.5 m | 15 files | **69 MB** (Tesseract, system WKWebView) |

On all three, CI reports `bundled tesseract runs; languages: ['chi_sim', 'eng']`.
Each also runs `smoke_test.py`, which additionally asserts the frontend and
`/api/health` answer, that the `unlimited` plugin imported, that
`POST /api/app/quit` is **403** without its confirmation header, and that the
process then exits 0 — so a bundle that merely built but cannot run still fails
the job. (The Tesseract *install* and the GTK step are `continue-on-error`: a
runner that cannot provide them still produces a working bundle, just one that
needs a system Tesseract / falls back to the browser.)

The Windows/macOS **windows** are not exercised in CI (no interactive session),
only the headless app, and a full OCR run through OCRmyPDF was verified on
Linux. What CI does prove on every platform is that the shipped Tesseract
executes and finds its language data.

Inspect a run with the GitHub CLI:

```bash
gh run list --workflow=desktop-build.yml
gh run watch                       # live-tail the newest run
gh run view --log-failed           # only the failing steps
gh run view --job=<job-id> --log   # one job's full log
gh run download <run-id>           # fetch the artifacts
```

> If `gh` reports `open ~/.cache/gh/...: read-only file system` (sandboxed
> shells), point its cache somewhere writable: `XDG_CACHE_HOME=$PWD/.ghcache`.

Not yet done: installers (Inno Setup / `.dmg` / AppImage), code signing +
macOS notarization, and a full OCR pass through OCRmyPDF on Windows/macOS
(CI proves the bundled Tesseract runs there, but only Linux runs the whole
upload → OCR → embed pipeline end to end).
