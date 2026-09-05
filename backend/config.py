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

# Keys accepted in ocr_config.toml.
_FILE_KEYS = (
    # unlimited engine (ocrmypdf_unlimited plugin) provider fields
    "api_key", "base_url", "model", "provider",
    # engine selection: 'unlimited' (plugin), 'tesseract' (ocrmypdf built-in),
    # 'none' (no OCR).  Default 'unlimited'.
    "ocr_engine",
    # unlimited engine HTTP retry / rate-limit knobs (ocrmypdf_unlimited.http_retry):
    # do NOT affect OCR output.
    "max_retries", "retry_base_delay", "retry_max_delay", "rate_limit_rps",
    # unlimited engine multi-page batching (ocrmypdf_unlimited.batching):
    # 0=disabled (default).  >1 groups concurrent pages into one request.
    "ocr_batch_size",            # pages per multi-image request
    "ocr_batch_timeout_ms",      # batch window flush timeout (backstop, ms)
    "ocr_batch_per_page_tokens", # per-page output token budget inside a batch
    # unlimited engine raw generation (ocrmypdf_unlimited.parser): keep each
    # block's raw (pre-normalization) content in the block sidecar ('raw'
    # field) and the hOCR (x_kind/x_raw ocr_par title properties) so
    # other-format export (markdown/LaTeX) can skip the lossy normalization.
    # Default off; the embedded text layer is unaffected.
    "generate_raw",
    # ocrmypdf pipeline knobs (backend.ocr_service builds OcrOptions from these)
    "ocrmypdf_mode",        # force | skip | redo | default (default: force)
    "ocrmypdf_jobs",        # 0 = auto (cpu count)
    "ocrmypdf_optimize",    # 0..3, applied at finalize (default 0)
    "ocrmypdf_output_type", # pdf | pdfa (default pdf)
    "ocrmypdf_language",    # tesseract needs it; the unlimited engine ignores it
    "tess_lang",            # legacy name for ocrmypdf_language (auto-mapped)
    "ocrmypdf_deskew",      # boolean, default off
    "ocrmypdf_clean",       # boolean, default off (requires unpaper)
    "ocrmypdf_rotate_pages",  # boolean, default off (needs an OSD-capable engine)
    # temp-file cleanup (see backend/cleanup.py)
    "cleanup_max_age_hours", "cleanup_interval_hours",
    # logging verbosity (see backend/logging_config.py)
    "log_level",
)

# Map environment variables -> resolved config field names.  These restore the
# legacy OCR_* names as highest-priority overrides for the running process.
_ENV_ALIASES = {
    "OCR_ENGINE": "ocr_engine",
    "OCR_MAX_RETRIES": "max_retries",
    "OCR_RETRY_BASE_DELAY": "retry_base_delay",
    "OCR_RETRY_MAX_DELAY": "retry_max_delay",
    "OCR_RATE_LIMIT_RPS": "rate_limit_rps",
    "OCR_BATCH_SIZE": "ocr_batch_size",
    "OCR_BATCH_TIMEOUT_MS": "ocr_batch_timeout_ms",
    "OCR_BATCH_PER_PAGE_TOKENS": "ocr_batch_per_page_tokens",
    "OCR_GENERATE_RAW": "generate_raw",
    "OCRMYPDF_MODE": "ocrmypdf_mode",
    "OCRMYPDF_JOBS": "ocrmypdf_jobs",
    "OCRMYPDF_OPTIMIZE": "ocrmypdf_optimize",
    "OCRMYPDF_OUTPUT_TYPE": "ocrmypdf_output_type",
    "OCRMYPDF_LANGUAGE": "ocrmypdf_language",
    "OCRMYPDF_DESKEW": "ocrmypdf_deskew",
    "OCRMYPDF_CLEAN": "ocrmypdf_clean",
    "OCRMYPDF_ROTATE_PAGES": "ocrmypdf_rotate_pages",
    "OCR_CLEANUP_MAX_AGE_HOURS": "cleanup_max_age_hours",
    "OCR_CLEANUP_INTERVAL_HOURS": "cleanup_interval_hours",
    "OCR_LOG_LEVEL": "log_level",
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
    # Legacy ``tess_lang`` (pre-rebuild name) maps onto ``ocrmypdf_language``
    # so an existing config keeps working; ``ocrmypdf_language`` wins if both
    # are present.
    if cfg.get("tess_lang") and not cfg.get("ocrmypdf_language"):
        cfg["ocrmypdf_language"] = cfg["tess_lang"]
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
    # Per-key overrides read straight from env.
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
    return {
        "provider": cfg.get("provider", "ustc"),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "ocr_engine": cfg.get("ocr_engine", "unlimited"),
        "ocrmypdf_mode": cfg.get("ocrmypdf_mode", "force"),
        "ocrmypdf_jobs": cfg.get("ocrmypdf_jobs", "0"),
        "ocrmypdf_optimize": cfg.get("ocrmypdf_optimize", "0"),
        "ocrmypdf_output_type": cfg.get("ocrmypdf_output_type", "pdf"),
        "ocrmypdf_language": cfg.get("ocrmypdf_language", ""),
        "ocrmypdf_deskew": as_bool(cfg.get("ocrmypdf_deskew", "false")),
        "ocrmypdf_clean": as_bool(cfg.get("ocrmypdf_clean", "false")),
        "api_key_masked": _mask_key(cfg.get("api_key", "")),
        "has_api_key": bool(cfg.get("api_key")),
        "ocr_batch_size": cfg.get("ocr_batch_size", "0"),
        "ocr_batch_timeout_ms": cfg.get("ocr_batch_timeout_ms", "3000"),
        "ocr_batch_per_page_tokens": cfg.get("ocr_batch_per_page_tokens", "2048"),
        "generate_raw": as_bool(cfg.get("generate_raw", "false")),
    }


def save(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Save settings from the WebUI to ocr_config.toml (masked keys preserved).

    If 'api_key' looks masked (contains '*') it is treated as "unchanged" and
    the previously configured key is kept.  All other file settings are
    preserved and only the editor-relevant fields are updated.
    """
    prev = _load_file_config()
    api_key = str(settings.get("api_key", "")).strip()
    if api_key and _has_mask(api_key):
        api_key = prev.get("api_key", _saved.get("api_key", ""))

    # Start from the existing file so a WebUI save does not drop unrelated keys.
    data: Dict[str, str] = dict(prev)
    if "api_key" in settings:
        data["api_key"] = api_key
    if "base_url" in settings:
        data["base_url"] = str(settings.get("base_url", "")).strip().rstrip("/")
    if "model" in settings:
        data["model"] = str(settings.get("model", "")).strip()
    if "provider" in settings:
        data["provider"] = str(settings.get("provider", "ustc")).strip()
    if "ocr_engine" in settings:
        data["ocr_engine"] = str(settings.get("ocr_engine", "unlimited")).strip()

    # Persist the ocrmypdf pipeline toggles from the WebUI payload.  Only keys
    # the client actually sent are written; anything absent keeps its value.
    for key in ("ocrmypdf_mode", "ocrmypdf_jobs", "ocrmypdf_optimize",
                "ocrmypdf_output_type", "ocrmypdf_language"):
        if settings.get(key) is not None:
            data[key] = str(settings[key]).strip()
    for key in ("ocrmypdf_deskew", "ocrmypdf_clean", "ocrmypdf_rotate_pages"):
        if settings.get(key) is not None:
            data[key] = "true" if as_bool(settings[key]) else "false"
    for key in ("ocr_batch_size", "ocr_batch_timeout_ms", "ocr_batch_per_page_tokens"):
        if settings.get(key) is not None:
            data[key] = str(settings[key]).strip()
    for key in ("generate_raw",):
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
