"""Plugin-local settings store, filled by the host backend.

The plugin package (``backend.ocrmypad``) never imports ``backend.config``:
the host backend resolves the effective config (TOML file + WebUI-saved +
``OCR_*`` env) and pushes a snapshot in here via :func:`configure` at startup
and whenever the settings change.  Engine worker threads (ocrmypdf runs them
concurrently, ``use_threads``) then read a consistent immutable snapshot via
:func:`snapshot` / :func:`get`.

This is the plugin's own state: a plugin loaded by ocrmypdf without a host
backend simply keeps an empty store, and ``UnlimitedOcrClient`` falls back to
its built-in defaults (see ``backend.ocrmypad.engine_client``).
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

_lock = threading.RLock()
_settings: Dict[str, Any] = {}


def configure(settings: Dict[str, Any]) -> None:
    """Replace the whole settings snapshot with a host-provided config dict."""
    global _settings
    with _lock:
        _settings = dict(settings or {})


def snapshot() -> Dict[str, Any]:
    """Return a copy of the current settings (worker-thread safe)."""
    with _lock:
        return dict(_settings)


def get(key: str, default: Any = None) -> Any:
    """Read one setting; ``default`` when the key is absent."""
    with _lock:
        return _settings.get(key, default)
