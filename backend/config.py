"""External OCR settings resolution.

Priority (highest first):
  1. CLI / environment variables (OCR_API_KEY, OCR_BASE_URL, OCR_MODEL,
     optional OCR_PROVIDER).  USTC_API_KEY is accepted as an alias for
     OCR_API_KEY when OCR_API_KEY is not set.
  2. Local config file `ocr_config.json` / `.env` in the backend directory.
  3. WebUI-saved default (the in-memory `saved` dict, persisted to
     ocr_config.json when the user saves via the WebUI).

No hardcoded keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR
CONFIG_FILE = BACKEND_DIR / "ocr_config.json"

# Provider presets (only a *default example* for USTC; any OpenAI-compatible
# endpoint works via base_url + api_key + model).
PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    "ustc": {
        "base_url": "https://api.llm.ustc.edu.cn/v1",
        "model": "glm-4v-flash",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
}

# Keys accepted in ocr_config.json (core provider fields + per-adapter knobs).
_FILE_KEYS = (
    "api_key", "base_url", "model", "provider",
    # tesseract adapter knobs
    "tess_lang", "tess_psm", "tess_oem", "tess_config",
    "tessdata_dir", "tess_cmd",
    # generic_openai adapter knob
    "generic_prompt",
    # embedded-text font (system font name / path used for the text layer)
    "embed_font",
    # temp-file cleanup (see backend/cleanup.py)
    "cleanup_max_age_hours", "cleanup_interval_hours",
)

# Map environment variables -> resolved config field names.
_ENV_ALIASES = {
    "OCR_TESS_LANG": "tess_lang",
    "OCR_TESS_PSM": "tess_psm",
    "OCR_TESS_OEM": "tess_oem",
    "OCR_TESS_CONFIG": "tess_config",
    "OCR_TESSDATA_DIR": "tessdata_dir",
    "OCR_TESS_CMD": "tess_cmd",
    "OCR_GENERIC_PROMPT": "generic_prompt",
    "OCR_EMBED_FONT": "embed_font",
    "OCR_CLEANUP_MAX_AGE_HOURS": "cleanup_max_age_hours",
    "OCR_CLEANUP_INTERVAL_HOURS": "cleanup_interval_hours",
}

# In-memory overrides from the WebUI settings page (applied at runtime).
_saved: Dict[str, str] = {}


def _load_file_config() -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in _FILE_KEYS:
                    val = data.get(key)
                    if val is not None:
                        cfg[str(key)] = str(val)
        except Exception:
            pass
    # Also read a conventional .env next to the config file.
    env_file = BACKEND_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key == "OCR_API_KEY":
                cfg["api_key"] = cfg.get("api_key") or value
            elif key == "OCR_BASE_URL":
                cfg["base_url"] = cfg.get("base_url") or value
            elif key == "OCR_MODEL":
                cfg["model"] = cfg.get("model") or value
            elif key == "OCR_PROVIDER":
                cfg["provider"] = cfg.get("provider") or value
            elif key == "USTC_API_KEY":
                cfg["api_key"] = cfg.get("api_key") or value
            elif key in _ENV_ALIASES:
                cfg[_ENV_ALIASES[key]] = cfg.get(_ENV_ALIASES[key]) or value
    return cfg


def _load_env() -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    api_key = os.environ.get("OCR_API_KEY")
    if not api_key:
        api_key = os.environ.get("USTC_API_KEY")  # alias
    if api_key:
        cfg["api_key"] = api_key
    if os.environ.get("OCR_BASE_URL"):
        cfg["base_url"] = os.environ["OCR_BASE_URL"].rstrip("/")
    if os.environ.get("OCR_MODEL"):
        cfg["model"] = os.environ["OCR_MODEL"]
    if os.environ.get("OCR_PROVIDER"):
        cfg["provider"] = os.environ["OCR_PROVIDER"]
    # Extra per-adapter knobs (tesseract / generic_openai) read straight from env.
    for env_key, field in _ENV_ALIASES.items():
        val = os.environ.get(env_key)
        if val:
            cfg[field] = cfg.get(field) or val
    return cfg


def resolve() -> Dict[str, str]:
    """Merge all sources into a single effective config (env > file > saved)."""
    merged: Dict[str, str] = {}
    merged.update(_saved)            # lowest: WebUI saved values
    merged.update(_load_file_config())  # middle: local config file
    merged.update(_load_env())       # highest: environment variables

    provider = merged.get("provider", "ustc")
    preset = PROVIDER_PRESETS.get(provider)
    if preset:
        merged.setdefault("base_url", preset["base_url"])
        merged.setdefault("model", preset["model"])

    return merged


def get_effective_settings() -> Dict[str, Any]:
    """Return a safe, masked view of the current effective settings for the WebUI."""
    cfg = resolve()
    return {
        "provider": cfg.get("provider", "ustc"),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "api_key_masked": _mask_key(cfg.get("api_key", "")),
        "has_api_key": bool(cfg.get("api_key")),
    }


def save(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Save settings from the WebUI to ocr_config.json (masked keys preserved).

    If 'api_key' looks masked (contains '*') it is treated as "unchanged" and
    the previously configured key is kept.
    """
    prev = _load_file_config()
    new: Dict[str, str] = {}
    api_key = str(settings.get("api_key", "")).strip()
    if api_key and _has_mask(api_key):
        new["api_key"] = prev.get("api_key", _saved.get("api_key", ""))
    else:
        new["api_key"] = api_key

    new["base_url"] = str(settings.get("base_url", "")).strip().rstrip("/")
    new["model"] = str(settings.get("model", "")).strip()
    new["provider"] = str(settings.get("provider", "ustc")).strip()

    # Persist to local file.
    data = {
        "api_key": new["api_key"],
        "base_url": new["base_url"],
        "model": new["model"],
        "provider": new["provider"],
    }
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _saved.update(new)
    return get_effective_settings()


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _has_mask(key: str) -> bool:
    return "*" in key


def apply_runtime_overrides(settings: Dict[str, Any]) -> None:
    """Apply WebUI-only (non-persisted) overrides for the current request/session."""
    for field in ("api_key", "base_url", "model", "provider"):
        val = settings.get(field)
        if val:
            _saved[field] = str(val).rstrip("/") if field == "base_url" else str(val)
        else:
            _saved.pop(field, None)
