"""Central path resolution: frozen vs source checkout.

A packaged desktop build cannot write next to its code: PyInstaller unpacks to
a temporary ``sys._MEIPASS`` (deleted on exit in onefile mode) or to a
read-only install directory (``/Applications/…``, ``C:\\Program Files\\…``,
macOS App Translocation), and the config file lives there too.  So paths are
split in two:

* **read-only resources** — ``frontend/``, ``config.example.toml`` — resolved
  from the bundle (``sys._MEIPASS`` / the executable's directory).
* **writable state** — ``uploads/``, ``work/``, ``output/``, the TOML config
  and the log file — resolved to a per-user directory via ``platformdirs``.

In a source checkout (not frozen) every path keeps the historical
repo-relative layout, so development, the CLI and the pytest suite are
unchanged.

Deliberately **no environment-variable override** here: AGENTS.md requires that
``os.environ`` is only read inside ``backend/config.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# platformdirs uses these for the per-user directories.  Not an "org" name:
# it must not change, or a packaged upgrade would look like a fresh install.
APP_NAME = "pdf-ocr-embed"

_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_BACKEND = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle or a Nuitka build."""
    if getattr(sys, "frozen", False):
        return True
    # Nuitka defines ``__compiled__`` in every compiled module's globals.
    return "__compiled__" in globals()


def resource_dir() -> Path:
    """Root of the READ-ONLY bundled assets (``frontend/`` lives below it)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)                       # PyInstaller
    if "__compiled__" in globals():
        return Path(sys.executable).resolve().parent    # Nuitka standalone
    return _SOURCE_ROOT


def _user_dir(kind: str) -> Path:
    """Ask platformdirs for one per-user directory (import kept local)."""
    from platformdirs import (user_config_dir, user_data_dir, user_log_dir)

    if kind == "data":
        # roaming=False keeps large OCR work trees out of the Windows roaming
        # profile; on macOS/Linux this is the standard Application Support /
        # XDG data directory.
        return Path(user_data_dir(APP_NAME, roaming=False))
    if kind == "config":
        return Path(user_config_dir(APP_NAME, roaming=False))
    if kind == "log":
        return Path(user_log_dir(APP_NAME))
    raise ValueError(f"unknown user dir kind: {kind!r}")


# --- writable state ----------------------------------------------------------

def data_dir() -> Path:
    """Writable root for ``uploads/``, ``work/`` and ``output/``."""
    return _user_dir("data") if is_frozen() else _SOURCE_ROOT


def config_dir() -> Path:
    """Writable directory holding ``ocr_config.toml``."""
    return _user_dir("config") if is_frozen() else _SOURCE_BACKEND


def log_dir() -> Path:
    """Writable directory for the log file."""
    return _user_dir("log") if is_frozen() else _SOURCE_ROOT / "logs"


def ensure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) if needed; returns it."""
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


# --- resolved locations (module-level, mirroring the historical names) --------

PROJECT_DIR = data_dir()
UPLOAD_DIR = PROJECT_DIR / "uploads"
WORK_DIR = PROJECT_DIR / "work"
OUTPUT_DIR = PROJECT_DIR / "output"

CONFIG_FILE = config_dir() / "ocr_config.toml"
LOG_DIR = log_dir()
LOG_FILE = LOG_DIR / "app.log"

# Read-only assets.
FRONTEND_DIR = resource_dir() / "frontend"
CONFIG_EXAMPLE = resource_dir() / "config.example.toml"
