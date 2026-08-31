"""External OCR settings resolution (TOML config file).

Priority (highest first):
  1. ``OCR_*`` environment variables (override everything below; see
     ``_ENV_ALIASES`` for the full map).
  2. In-memory values saved via the WebUI (`saved` dict, persisted by
     ``save()`` to ``ocr_config.toml``).
  3. Local config file ``ocr_config.toml`` in the backend directory.

Base settings live in the TOML config file; ``OCR_*`` environment variables
can override individual keys for the running process without editing the file.

No hardcoded keys.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR
CONFIG_FILE = BACKEND_DIR / "ocr_config.toml"

# Provider presets (only a *default example* for USTC; any OpenAI-compatible
# endpoint works via base_url + api_key + model).
PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    "ustc": {
        "base_url": "https://api.llm.ustc.edu.cn/v1",
        "model": "unlimited-ocr",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
}

# Keys accepted in ocr_config.toml (core provider fields + per-adapter knobs).
_FILE_KEYS = (
    "api_key", "base_url", "model", "provider",
    # tesseract adapter knobs
    "tess_lang", "tess_psm", "tess_oem", "tess_config",
    "tessdata_dir", "tess_cmd",
    # generic_openai adapter knob
    "generic_prompt",
    # unlimited adapter document-level (multi-page) parsing knobs
    "unlimited_batch_enabled", "unlimited_max_pages_per_batch",
    # guard: cap concurrent pages-in-flight = batch_size x concurrency
    "max_inflight_pages",
    # HTTP adapter retry / rate-limit knobs (see backend/sources/http_utils.py):
    # shared by unlimited_ocr & generic_openai; do NOT affect OCR output.
    "max_retries", "retry_base_delay", "retry_max_delay", "rate_limit_rps",
    # embedded-text font (system font name / path used for the text layer)
    "embed_font",
    # temp-file cleanup (see backend/cleanup.py)
    "cleanup_max_age_hours", "cleanup_interval_hours",
    # OCR result cache (see backend/ocr_cache.py)
    "ocr_cache_enabled", "ocr_cache_max_age_hours",
    # logging verbosity (see backend/logging_config.py)
    "log_level",
    # OCR-input image preprocessing (see backend/pdf_processing.preprocess_image)
    "preprocess_enabled", "preprocess_grayscale", "preprocess_denoise",
    "preprocess_contrast", "preprocess_binarize",
)

# Preprocessing flags (flat keys, mirrored in _FILE_KEYS / _ENV_ALIASES).
# The master switch defaults OFF; when a user enables preprocessing, the common
# trio (grayscale + denoise + contrast) is on by default and binarize stays
# opt-in.  Single source of truth — the render pipeline
# (backend/pdf_processing._preprocess_opts_from_config) reads the same defaults.
_PREPROCESS_KEYS = (
    "preprocess_enabled", "preprocess_grayscale", "preprocess_denoise",
    "preprocess_contrast", "preprocess_binarize",
)
PREPROCESS_DEFAULTS: Dict[str, bool] = {
    "preprocess_enabled": False,
    "preprocess_grayscale": True,
    "preprocess_denoise": True,
    "preprocess_contrast": True,
    "preprocess_binarize": False,
}

# Map environment variables -> resolved config field names.  These restore the
# legacy OCR_* names as highest-priority overrides for the running process.
_ENV_ALIASES = {
    "OCR_TESS_LANG": "tess_lang",
    "OCR_TESS_PSM": "tess_psm",
    "OCR_TESS_OEM": "tess_oem",
    "OCR_TESS_CONFIG": "tess_config",
    "OCR_TESSDATA_DIR": "tessdata_dir",
    "OCR_TESS_CMD": "tess_cmd",
    "OCR_GENERIC_PROMPT": "generic_prompt",
    "OCR_UNLIMITED_BATCH_ENABLED": "unlimited_batch_enabled",
    "OCR_UNLIMITED_MAX_PAGES_PER_BATCH": "unlimited_max_pages_per_batch",
    "OCR_MAX_INFLIGHT_PAGES": "max_inflight_pages",
    "OCR_MAX_RETRIES": "max_retries",
    "OCR_RETRY_BASE_DELAY": "retry_base_delay",
    "OCR_RETRY_MAX_DELAY": "retry_max_delay",
    "OCR_RATE_LIMIT_RPS": "rate_limit_rps",
    "OCR_EMBED_FONT": "embed_font",
    "OCR_CLEANUP_MAX_AGE_HOURS": "cleanup_max_age_hours",
    "OCR_CLEANUP_INTERVAL_HOURS": "cleanup_interval_hours",
    "OCR_CACHE_ENABLED": "ocr_cache_enabled",
    "OCR_CACHE_MAX_AGE_HOURS": "ocr_cache_max_age_hours",
    "OCR_LOG_LEVEL": "log_level",
    "OCR_PREPROCESS_ENABLED": "preprocess_enabled",
    "OCR_PREPROCESS_GRAYSCALE": "preprocess_grayscale",
    "OCR_PREPROCESS_DENOISE": "preprocess_denoise",
    "OCR_PREPROCESS_CONTRAST": "preprocess_contrast",
    "OCR_PREPROCESS_BINARIZE": "preprocess_binarize",
}

# In-memory overrides from the WebUI settings page (applied at runtime).
_saved: Dict[str, str] = {}


def _load_file_config() -> Dict[str, str]:
    """Read the TOML config file (values are normalized to strings)."""
    cfg: Dict[str, str] = {}
    if not CONFIG_FILE.exists():
        return cfg
    try:
        data = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Failed to parse %s: %s", CONFIG_FILE, exc)
        return cfg
    if not isinstance(data, dict):
        log.warning("Ignoring %s: top-level value is not a table", CONFIG_FILE)
        return cfg
    for key in _FILE_KEYS:
        val = data.get(key)
        if val is not None:
            cfg[key] = _as_str(val)
    return cfg


def _as_str(value: Any) -> str:
    """Coerce a TOML scalar to the string form the rest of the backend expects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def as_bool(value: Any) -> bool:
    """Coerce a config value (string from file/env or JSON bool) to a boolean.

    Accepts TOML booleans, JSON booleans, and string forms like "true"/"1"/"yes".
    Anything unrecognized (including None and empty) is falsy.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _load_env() -> Dict[str, str]:
    """Read ``OCR_*`` environment variables (highest-priority overrides)."""
    cfg: Dict[str, str] = {}
    api_key = os.environ.get("OCR_API_KEY") or os.environ.get("USTC_API_KEY")  # alias
    if api_key:
        cfg["api_key"] = api_key
    if os.environ.get("OCR_BASE_URL"):
        cfg["base_url"] = os.environ["OCR_BASE_URL"].rstrip("/")
    if os.environ.get("OCR_MODEL"):
        cfg["model"] = os.environ["OCR_MODEL"]
    if os.environ.get("OCR_PROVIDER"):
        cfg["provider"] = os.environ["OCR_PROVIDER"]
    # Per-adapter knobs and other file keys read straight from env.
    for env_key, field in _ENV_ALIASES.items():
        val = os.environ.get(env_key)
        if val:
            cfg[field] = val
    return cfg


def resolve() -> Dict[str, str]:
    """Merge all sources into a single effective config (env > saved > file)."""
    merged: Dict[str, str] = {}
    merged.update(_load_file_config())
    merged.update(_saved)            # WebUI-saved values win over the file
    merged.update(_load_env())       # environment variables override both

    provider = merged.get("provider") or "ustc"
    preset = PROVIDER_PRESETS.get(provider)
    if preset:
        # An empty string is not an explicit override -- it is what the WebUI
        # writes when a field is cleared.  Treat it as absent so clearing a
        # field really falls back to the provider preset (see README).
        if not merged.get("base_url"):
            merged["base_url"] = preset["base_url"]
        if not merged.get("model"):
            merged["model"] = preset["model"]

    return merged


def get_effective_settings() -> Dict[str, Any]:
    """Return a safe, masked view of the current effective settings for the WebUI."""
    cfg = resolve()
    preprocess = {k: as_bool(cfg.get(k, PREPROCESS_DEFAULTS[k])) for k in _PREPROCESS_KEYS}
    return {
        "provider": cfg.get("provider", "ustc"),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "api_key_masked": _mask_key(cfg.get("api_key", "")),
        "has_api_key": bool(cfg.get("api_key")),
        **preprocess,
    }


def save(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Save settings from the WebUI to ocr_config.toml (masked keys preserved).

    If 'api_key' looks masked (contains '*') it is treated as "unchanged" and
    the previously configured key is kept.  All other file settings (per-adapter
    knobs, cleanup, log level) are preserved and only the editor-relevant
    provider fields are updated.
    """
    prev = _load_file_config()
    api_key = str(settings.get("api_key", "")).strip()
    if api_key and _has_mask(api_key):
        api_key = prev.get("api_key", _saved.get("api_key", ""))

    # Start from the existing file so a WebUI save does not drop unrelated keys.
    # Only fields actually present in the payload are written, so a save that
    # carries just the preprocessing toggles never clears the provider fields
    # (or a previously configured api_key / custom base_url).
    data: Dict[str, str] = dict(prev)
    if "api_key" in settings:
        data["api_key"] = api_key
    if "base_url" in settings:
        data["base_url"] = str(settings.get("base_url", "")).strip().rstrip("/")
    if "model" in settings:
        data["model"] = str(settings.get("model", "")).strip()
    if "provider" in settings:
        data["provider"] = str(settings.get("provider", "ustc")).strip()

    # Persist the preprocessing toggles from the WebUI payload.  Only keys the
    # client actually sent are written; anything absent keeps its previous
    # value (data already starts from the existing file).
    for key in _PREPROCESS_KEYS:
        if settings.get(key) is not None:
            data[key] = "true" if as_bool(settings[key]) else "false"

    CONFIG_FILE.write_text(_dump_toml(data), encoding="utf-8")
    _saved.update(data)
    return get_effective_settings()


def _dump_toml(data: Dict[str, str]) -> str:
    """Serialize the flat settings dict as TOML text."""
    try:
        import tomli_w  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "tomli-w is required to save settings (pip install tomli-w)"
        ) from exc
    return tomli_w.dumps(data)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _has_mask(key: str) -> bool:
    return "*" in key


_REDACT_PLACEHOLDER = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Remove every configured ``api_key`` value from ``text``.

    Frontend-facing surfaces (the debug-log endpoint and job/SSE error messages)
    must never echo the API key, whether it came from the TOML file, a WebUI save
    or an ``OCR_API_KEY`` / ``USTC_API_KEY`` environment variable.
    """
    if not text:
        return text
    secrets: set[str] = set()
    for source in (_load_file_config(), _saved, _load_env()):
        key = str(source.get("api_key", "") or "").strip()
        if key:
            secrets.add(key)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, _REDACT_PLACEHOLDER)
    return text


def apply_runtime_overrides(settings: Dict[str, Any]) -> None:
    """Apply WebUI-only (non-persisted) overrides for the current request/session."""
    for field in ("api_key", "base_url", "model", "provider"):
        val = settings.get(field)
        if val:
            _saved[field] = str(val).rstrip("/") if field == "base_url" else str(val)
        else:
            _saved.pop(field, None)
