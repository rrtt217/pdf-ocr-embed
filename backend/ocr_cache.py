"""Cross-job OCR result cache (disk-backed, content-addressed).

Re-OCRing the same document page with the same engine and the same settings
wastes API credits (and minutes of wall time).  This module stores normalized
`OcrPage` dicts (`backend/models.py` round-trip format) keyed by a SHA-256
digest of the full recognition *context* — source PDF content hash, page index,
render parameters and the adapter's output-affecting settings (`fingerprint`).
Entries live under `cache/ocr/<sha256>.json` and expire lazily after
`ocr_cache_max_age_hours` (default 30 days).

Correctness properties:

- Only the digest is persisted — never the context, page image or any secret.
- Writes go through a temp file + atomic `os.replace()`, so concurrent page
  workers (and a "put" racing a "get") never observe a torn file.  Each page
  has a distinct key, so parallel writes cannot collide on the same path
  anyway.
- A corrupted or expired entry is treated as a miss and removed, never served.
- Cached pages are the **pristine** OCR output.  User edits live in job state
  (`ocr_service.update_page`) and never touch this cache.

Settings (via `backend.config.resolve()`):
  ocr_cache_enabled        "true" | "false"   (default true)
  ocr_cache_max_age_hours  entry TTL hours    (default 720)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from backend.config import resolve

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CACHE_ROOT = PROJECT_DIR / "cache"
ENTRY_DIR = CACHE_ROOT / "ocr"

DEFAULT_MAX_AGE_HOURS = 720.0  # 30 days

# Cumulative in-process counters (stats only — not persisted).
_hits = 0
_misses = 0
_stats_lock = threading.Lock()

_MB = 1048576.0


def _num(value, default: float) -> float:
    """Parse a config string as a positive float, falling back on error."""
    try:
        f = float(value)
        return f if f > 0 else default
    except (TypeError, ValueError):
        return default


def _truthy(value) -> bool:
    """Accept "true"/"false"/"1"/"0"/"on"/"off" style config strings."""
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def is_enabled() -> bool:
    """Whether caching is active (config-controlled, fresh each call)."""
    return _truthy(resolve().get("ocr_cache_enabled", "true"))


def max_age_hours() -> float:
    """Entry TTL in hours (config-controlled, fresh each call)."""
    return _num(resolve().get("ocr_cache_max_age_hours"),
                DEFAULT_MAX_AGE_HOURS)


def _sanitize(value: Any) -> Any:
    """Coerce context values to plain JSON scalars (fingerprints may carry
    exotic types; we only ever hash or compare, never serialize them out)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in sorted(value.items())}
    return str(value)


def build_key(context: Dict[str, Any]) -> str:
    """Derive the 64-hex-char cache key from a recognition context dict.

    The digest is a pure function of the context: image-independent w.r.t.
    paths/timestamps, so the same PDF page + adapter settings always collide.
    """
    payload = json.dumps(_sanitize(context), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _entry_path(key: str) -> Path:
    return ENTRY_DIR / f"{key}.json"


def _bump(hit: bool) -> None:
    global _hits, _misses
    with _stats_lock:
        if hit:
            _hits += 1
        else:
            _misses += 1


def get_page(key: str, count_misses: bool = True) -> Optional[Dict[str, Any]]:
    """Fetch a cached OcrPage dict for *key*, or None on miss / expiry / damage.

    ``count_misses=False`` is used by the pre-render cache precheck, whose
    fruitless lookups are pure optimization probes — they must not pollute the
    hit/miss statistics with double counting (the real lookup happens again
    inside ``_recognize_page`` for the pages that go to the engine).
    """
    if not is_enabled():
        if count_misses:
            _bump(False)
        return None
    path = _entry_path(key)
    try:
        if not path.exists():
            if count_misses:
                _bump(False)
            return None
        st = path.stat()
        if time.time() - st.st_mtime > max_age_hours() * 3600.0:
            path.unlink(missing_ok=True)
            if count_misses:
                _bump(False)
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Corrupt entry: treat as a miss and remove so we don't re-fail forever.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        if count_misses:
            _bump(False)
        return None

    page = data.get("page") if isinstance(data, dict) else None
    if not isinstance(page, dict):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        if count_misses:
            _bump(False)
        return None
    _bump(True)
    return page


def put_page(key: str, page: Dict[str, Any]) -> None:
    """Atomically store an OcrPage dict under *key* (no-op when disabled)."""
    if not is_enabled():
        return
    try:
        ENTRY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ENTRY_DIR / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(
            json.dumps({"page": page, "key": key, "created": time.time()},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, _entry_path(key))
    except OSError as exc:
        # Cache is an optimization — logging and dropping is the correct
        # behaviour; it must never fail the OCR pipeline itself.
        try:
            tmp.unlink(missing_ok=True)
        except (OSError, UnboundLocalError, NameError):
            pass
        log.warning("ocr cache: failed to write entry %s: %s", key[:12], exc)


def _entry_stats() -> Dict[str, Any]:
    """Scan the entry dir once; return {entries, bytes}.  Not cached."""
    entries = 0
    total_bytes = 0
    if not ENTRY_DIR.exists():
        return {"entries": 0, "bytes": 0}
    for p in ENTRY_DIR.glob("*.json"):
        try:
            if p.is_file() and not p.is_symlink():
                entries += 1
                total_bytes += p.stat().st_size
        except OSError:
            pass
    return {"entries": entries, "bytes": total_bytes}


def status() -> Dict[str, Any]:
    """Snapshot for the /api/cache endpoint (and UI panels later)."""
    with _stats_lock:
        hits, misses = _hits, _misses
    st = _entry_stats()
    return {
        "enabled": is_enabled(),
        "dir": str(ENTRY_DIR),
        "max_age_hours": max_age_hours(),
        "entries": st["entries"],
        "bytes": st["bytes"],
        "hits": hits,
        "misses": misses,
    }


def purge_expired() -> Dict[str, Any]:
    """Delete expired entries (no age override; uses config TTL)."""
    removed = 0
    freed = 0
    if not ENTRY_DIR.exists():
        return {"removed": 0, "freed_bytes": 0}
    now = time.time()
    for p in ENTRY_DIR.glob("*.json"):
        try:
            if p.is_file() and not p.is_symlink() and                     now - p.stat().st_mtime > max_age_hours() * 3600.0:
                size = p.stat().st_size
                p.unlink(missing_ok=True)
                removed += 1
                freed += size
        except OSError:
            pass
    if removed:
        log.info("ocr cache: purged %d expired entr%s (%.1f MB)",
                 removed, "y" if removed == 1 else "ies", freed / _MB)
    return {"removed": removed, "freed_bytes": freed}


def clear() -> Dict[str, Any]:
    """Remove all cached entries (manual endpoint / housekeeping)."""
    before = _entry_stats()
    if ENTRY_DIR.exists():
        shutil.rmtree(ENTRY_DIR, ignore_errors=True)
    after = _entry_stats()
    freed = before["bytes"] - after["bytes"]
    log.info("ocr cache: cleared %d entr%s (%.1f MB freed)",
             before["entries"], "y" if before["entries"] == 1 else "ies",
             freed / _MB)
    return {"removed": before["entries"], "freed_bytes": freed}
