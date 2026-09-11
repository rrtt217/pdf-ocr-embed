# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the pdf-ocr-embed desktop app (onedir).

Build:   .venv/bin/pyinstaller packaging/pdf_ocr_embed.spec --noconfirm
Output:  dist/pdf-ocr-embed/  (launch ``pdf-ocr-embed`` in that folder)

Why onedir and not onefile: onefile re-extracts the whole bundle (Python +
PyMuPDF-free but still ~200MB of libs) to a temp directory on every launch, is
slow to start, and PyInstaller 6.13+ deprecates onefile for macOS ``.app``
bundles.  Ship onedir inside an installer instead.

Two things this spec is deliberate about:

* the frontend is bundled as DATA (``_internal/frontend``); ``backend.paths``
  resolves it from ``sys._MEIPASS`` at runtime.
* ``ocrmypdf_unlimited`` is collected as an importable MODULE but its
  distribution metadata is NOT copied, so OCRmyPDF's entry-point scan does not
  auto-load it.  ``ocr_service.plugin_auto_loaded()`` returns False when frozen
  and the app requests the plugin by dotted name instead — collecting both
  would make pluggy register the same module twice.
"""
from pathlib import Path

from PyInstaller.utils.hooks import (collect_data_files, collect_submodules,
                                     copy_metadata)

ROOT = Path(SPECPATH).resolve().parent

# --- data files --------------------------------------------------------------
datas = [
    # The WebUI (read-only runtime asset).
    (str(ROOT / "frontend"), "frontend"),
    # A template users can copy to the per-user config location.
    (str(ROOT / "config.example.toml"), "."),
]
# OCRmyPDF ships small runtime data; harmless when empty.
datas += collect_data_files("ocrmypdf")

# --- optional bundled Tesseract ----------------------------------------------
# Staged by `python packaging/bundle_tesseract.py`, which the build runs with
# --with-tesseract.  Installing the program + its shared libraries + language
# data is what lets the Tesseract engine work on a machine with nothing
# installed; `backend.bundled_tools` wires PATH/LD_LIBRARY_PATH/TESSDATA_PREFIX
# to this directory.  Executables and libraries go through `binaries` so their
# permission bits and (on macOS) their install names are handled properly.
TESSERACT_STAGING = ROOT / "packaging" / "tesseract-staging"
binaries = []
if (TESSERACT_STAGING / "bin").is_dir():
    binaries += [(str(p), "tesseract/bin")
                 for p in sorted((TESSERACT_STAGING / "bin").iterdir())
                 if p.is_file()]
    if (TESSERACT_STAGING / "lib").is_dir():
        binaries += [(str(p), "tesseract/lib")
                     for p in sorted((TESSERACT_STAGING / "lib").iterdir())
                     if p.is_file()]
    if (TESSERACT_STAGING / "tessdata").is_dir():
        datas += [(str(TESSERACT_STAGING / "tessdata"), "tesseract/tessdata")]
    print(f"[spec] bundling tesseract ({len(binaries)} binary/lib file(s))")
else:
    print("[spec] no tesseract staged — the app will need a system tesseract")

# --- metadata ----------------------------------------------------------------
# Some libraries read their own version at runtime via importlib.metadata.
# NOTE: deliberately NOT copy_metadata("ocrmypdf-unlimited") — see the module
# docstring (entry-point auto-load vs explicit dotted-module load).
for dist in ("ocrmypdf", "pikepdf", "pypdfium2", "Pillow"):
    try:
        datas += copy_metadata(dist)
    except Exception as exc:  # pragma: no cover - dist renamed upstream
        print(f"[spec] could not copy metadata for {dist}: {exc}")

# --- hidden imports ----------------------------------------------------------
hiddenimports = [
    # Our own packages (uvicorn's dynamic protocol/lifespan lookups included).
    "ocrmypdf_unlimited",
    "backend.ocrmypad",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]
hiddenimports += collect_submodules("backend")
hiddenimports += collect_submodules("ocrmypdf_unlimited")
hiddenimports += collect_submodules("ocrmypdf")
# pywebview is optional (the app falls back to the system browser).  Only pull
# it in when it is actually installed.
try:
    import webview  # noqa: F401

    hiddenimports += collect_submodules("webview")
except ImportError:
    pass

# --- exclusions --------------------------------------------------------------
# Nothing in this app needs a GUI toolkit, a test runner or a plotting stack;
# dropping them keeps the bundle from ballooning.
excludes = [
    "tkinter", "matplotlib", "numpy", "scipy", "pandas",
    "pytest", "IPython", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "streamlit", "notebook", "setuptools",
]

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# --- prune GTK/WebKit data files --------------------------------------------
# PyInstaller's PyGObject hook collects the whole GTK data tree.  The UI is
# rendered inside the webview and GTK only draws the window frame, so the
# Adwaita icon theme (~238 MB uncompressed!) and the gettext catalogues are
# dead weight.  Dropping them takes the bundle from ~458 MB to ~200 MB; the
# window still opens (GTK falls back to built-in defaults).
_DROP_DATA_PREFIXES = ("share/icons/", "share/locale/")


def _prune_gtk_data(toc):
    kept, dropped = [], 0
    for entry in toc:
        dest = str(entry[0]).replace("\\", "/")
        if dest.startswith(_DROP_DATA_PREFIXES):
            dropped += 1
            continue
        kept.append(entry)
    print(f"[spec] pruned {dropped} GTK data file(s) "
          f"({', '.join(_DROP_DATA_PREFIXES)})")
    return kept


a.datas = _prune_gtk_data(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pdf-ocr-embed",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed: no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="pdf-ocr-embed",
)
