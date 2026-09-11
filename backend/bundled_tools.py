"""Expose binaries shipped *inside* the app to OCRmyPDF (currently Tesseract).

A packaged build wants to work on a machine with nothing installed.  OCRmyPDF
finds ``tesseract`` by name on ``PATH`` and its language data via
``TESSDATA_PREFIX``, so this module points both at the copy staged by
``packaging/bundle_tesseract.py`` and shipped as ``_internal/tesseract/``.

Two details that are easy to get wrong:

* **``PATH`` is not enough** — the staged ``tesseract`` links against
  ``libtesseract``/``libleptonica``/… from ``lib/``, so that directory has to be
  on the loader path (``LD_LIBRARY_PATH`` / ``DYLD_LIBRARY_PATH``).  On Windows
  the DLLs sit next to the executable, which ``PATH`` already covers.
* **``TESSDATA_PREFIX`` points at the tessdata DIRECTORY itself.**  Tesseract
  4.1+ is often documented as wanting the *parent*, but 5.x resolves
  ``<TESSDATA_PREFIX>/<lang>.traineddata`` — pointing at the parent makes it
  report bogus languages like ``tessdata/eng``.  Verified against 5.5.3.

This is the only module besides ``backend/config.py`` that reads/writes
``os.environ`` (see AGENTS.md).
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from backend import paths

log = logging.getLogger(__name__)

BUNDLE_DIRNAME = "tesseract"
_EXE_NAMES = ("tesseract.exe", "tesseract") if os.name == "nt" else ("tesseract",)

_lock = threading.Lock()
_activated = False


# --- layout ------------------------------------------------------------------

def bundle_dir() -> Path:
    """Where the packaging step staged Tesseract (``_internal/tesseract``)."""
    return paths.resource_dir() / BUNDLE_DIRNAME


def bin_dir() -> Path:
    return bundle_dir() / "bin"


def lib_dir() -> Path:
    return bundle_dir() / "lib"


def tessdata_dir() -> Path:
    return bundle_dir() / "tessdata"


def tesseract_path() -> Optional[Path]:
    """The bundled executable, or None when this build ships no Tesseract."""
    for name in _EXE_NAMES:
        candidate = bin_dir() / name
        if candidate.is_file():
            return candidate
    return None


def is_bundled() -> bool:
    """True when the app carries its own Tesseract (no install needed)."""
    return tesseract_path() is not None


def bundled_languages() -> List[str]:
    """Language codes present in the bundled tessdata."""
    directory = tessdata_dir()
    if not directory.is_dir():
        return []
    return sorted(f.stem for f in directory.glob("*.traineddata"))


# --- activation --------------------------------------------------------------

def _prepend_env(name: str, value: str) -> None:
    current = os.environ.get(name, "")
    os.environ[name] = value + os.pathsep + current if current else value


def activate() -> bool:
    """Put the bundled Tesseract ahead of anything on the system.

    Idempotent and safe to call from any entry point (server, desktop, CLI).
    Returns True when a bundled Tesseract was found and wired up.
    """
    global _activated
    exe = tesseract_path()
    if exe is None:
        return False
    with _lock:
        if _activated:
            return True
        _prepend_env("PATH", str(exe.parent))
        # The loader path matters on POSIX only; Windows resolves DLLs from the
        # executable's own directory, which PATH already covers.
        libs = lib_dir()
        if libs.is_dir():
            _prepend_env("DYLD_LIBRARY_PATH" if sys.platform == "darwin"
                         else "LD_LIBRARY_PATH", str(libs))
        data = tessdata_dir()
        if data.is_dir():
            # See the module docstring: the tessdata directory itself, not its
            # parent (tesseract 5.x semantics).
            os.environ["TESSDATA_PREFIX"] = str(data)
        _activated = True
        log.info("using bundled tesseract: %s (languages: %s)", exe,
                 ", ".join(bundled_languages()) or "none found")
        return True


def reset() -> None:
    """Forget that activation happened (tests only; does not undo the env)."""
    global _activated
    with _lock:
        _activated = False


# --- diagnostics -------------------------------------------------------------

def resolve_tesseract() -> Optional[str]:
    """The executable OCRmyPDF will actually run, whichever one that is."""
    bundled = tesseract_path()
    if _activated and bundled is not None:
        return str(bundled)
    return shutil.which("tesseract")


def describe() -> Dict[str, object]:
    """A JSON-friendly summary for ``/api/health`` and the log."""
    bundled = tesseract_path()
    return {
        "bundled": bundled is not None,
        "bundled_path": str(bundled) if bundled else None,
        "path": resolve_tesseract(),
        "languages": bundled_languages() if bundled is not None else [],
        "source": ("bundled" if (bundled is not None and _activated)
                   else "system" if shutil.which("tesseract") else "missing"),
    }
