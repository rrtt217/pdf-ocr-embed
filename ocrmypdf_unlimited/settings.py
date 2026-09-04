"""Plugin-local settings store + config resolution (fully self-contained).

This is the ``ocrmypdf-unlimited`` plugin's OWN configuration surface.  It
never imports a host application; instead the effective config is resolved
from three independent layers, highest priority first:

1. **Plugin options** — values the user passed on the ocrmypdf command line
   (``--unlimited-*``, registered by ``options.add_options``) or as API
   keyword arguments to ``ocrmypdf.api.ocr()`` / ``_pdf_to_hocr()``
   (mirrored into ``options.unlimited_*`` by OCRmyPDF).  This is what makes
   the plugin usable completely standalone.
2. **Environment variables** — the plugin's own ``OCR_UNLIMITED_*`` surface
   (see ``_ENV_ALIASES``), for a stateless non-interactive setup.
3. **Host-injected snapshot** — a host application (e.g. the ``pdf-ocr-embed``
   backend) may push its effective config with :func:`configure` at startup
   and whenever its settings change.  With no host this stays empty and is
   skipped.

Missing values fall through to the client's built-in defaults (see
``client.UnlimitedOcrClient``).

Thread-safety: engine worker threads (ocrmypdf runs them concurrently,
``use_threads``) read immutable snapshots via :func:`snapshot` /
:func:`effective`; ``configure`` swaps the whole store under a lock.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

_lock = threading.RLock()
_settings: Dict[str, Any] = {}

#: ocrmypdf option destination -> config key.  The option destinations are the
#: ``--unlimited-*`` CLI arguments (and API kwargs) registered in options.py;
#: OCRmyPDF mirrors them onto ``options.unlimited_*``.
_OPTION_KEYS = {
    "unlimited_api_key": "api_key",
    "unlimited_base_url": "base_url",
    "unlimited_model": "model",
    "unlimited_max_tokens": "max_tokens",
    "unlimited_batch_size": "ocr_batch_size",
    "unlimited_batch_timeout_ms": "ocr_batch_timeout_ms",
    "unlimited_batch_per_page_tokens": "ocr_batch_per_page_tokens",
    "unlimited_max_retries": "max_retries",
    "unlimited_retry_base_delay": "retry_base_delay",
    "unlimited_retry_max_delay": "retry_max_delay",
    "unlimited_rate_limit_rps": "rate_limit_rps",
}

#: The plugin's own environment surface (independent of any host's config).
_ENV_ALIASES = {
    "OCR_UNLIMITED_API_KEY": "api_key",
    "OCR_UNLIMITED_BASE_URL": "base_url",
    "OCR_UNLIMITED_MODEL": "model",
    "OCR_UNLIMITED_MAX_TOKENS": "max_tokens",
    "OCR_UNLIMITED_BATCH_SIZE": "ocr_batch_size",
    "OCR_UNLIMITED_BATCH_TIMEOUT_MS": "ocr_batch_timeout_ms",
    "OCR_UNLIMITED_BATCH_PER_PAGE_TOKENS": "ocr_batch_per_page_tokens",
    "OCR_UNLIMITED_MAX_RETRIES": "max_retries",
    "OCR_UNLIMITED_RETRY_BASE_DELAY": "retry_base_delay",
    "OCR_UNLIMITED_RETRY_MAX_DELAY": "retry_max_delay",
    "OCR_UNLIMITED_RATE_LIMIT_RPS": "rate_limit_rps",
}


def configure(settings: Optional[Dict[str, Any]]) -> None:
    """Replace the whole host-injected settings snapshot.

    Called by a host backend to push its effective config; with no host the
    store simply stays empty.  ``None``/empty clears it.
    """
    global _settings
    with _lock:
        _settings = dict(settings or {})


def snapshot() -> Dict[str, Any]:
    """Return a copy of the host-injected snapshot (worker-thread safe)."""
    with _lock:
        return dict(_settings)


def get(key: str, default: Any = None) -> Any:
    """Read one host-injected setting; ``default`` when the key is absent."""
    with _lock:
        return _settings.get(key, default)


def from_options(options) -> Dict[str, Any]:
    """Extract the plugin args explicitly set on an ``OcrOptions``/Namespace.

    Only keys the user actually set (non-``None``) are returned, so absent
    options never shadow lower-priority layers.  Works for both real
    ``OcrOptions`` objects and duck-typed stand-ins (``SimpleNamespace``).
    """
    cfg: Dict[str, Any] = {}
    if options is None:
        return cfg
    for dest, key in _OPTION_KEYS.items():
        value = getattr(options, dest, None)
        if value is not None:
            cfg[key] = value
    return cfg


def from_env() -> Dict[str, Any]:
    """Read the plugin's own ``OCR_UNLIMITED_*`` environment variables."""
    cfg: Dict[str, Any] = {}
    for env_name, key in _ENV_ALIASES.items():
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value
    return cfg


def effective(options=None) -> Dict[str, Any]:
    """Resolve the effective config for a run, highest priority last.

    Layers: host-injected snapshot < ``OCR_UNLIMITED_*`` env < plugin options
    (CLI/API args).  Keys absent from all layers are omitted so the client
    applies its built-in defaults.
    """
    cfg: Dict[str, Any] = {}
    cfg.update(snapshot())
    cfg.update(from_env())
    cfg.update(from_options(options))
    return cfg
