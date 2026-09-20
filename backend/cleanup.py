"""Temporary-file cleanup for `work/`, `output/` and `uploads/`.

Job state lives in `work/<job_id>/job.json` and is restored at startup
(see ``ocr_service.restore_jobs``), so every restored job's files stay
protected.  Orphaned files only arise when a state file is deleted or
corrupt (or an upload/embed produced files no job references), and would
otherwise stay on disk forever — a single 500-page scan can leave
hundreds of MB of page renders behind.

This module:
  - inventories unreferenced temp files (age + size),
  - deletes those older than a configurable age — NEVER touching anything a
    live or restored job still references,
  - re-runs automatically on a background daemon thread.

Settings (via ``backend.config.resolve()``, from ``backend/ocr_config.toml``):
  cleanup_max_age_hours   default 168 (7d)
  cleanup_interval_hours  default 6

Deletion safety:
  - Every path referenced by a live job (`pdf_path`, `hocr_dir`,
    `previews_dir`, `embedded_path`) is excluded up front.
  - A job that exists ON DISK also protects itself, even when this process does
    not know it: `uploads/<id>.pdf` and `work/<id>/` reference each other.  Two
    instances sharing the runtime directories (or any process with a different
    registry view) must not collect each other's jobs — losing the upload breaks
    retry/finalize, and losing the work dir throws away the OCR results.  A job
    is dropped explicitly, by ``clear_job`` removing both halves.
  - Symlinks are never followed or removed.
  - ``force`` only relaxes the *age* rule — referenced files are still kept.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from backend import paths
from backend.config import resolve

log = logging.getLogger(__name__)

# The three writable areas this module sweeps: repo-relative in a source
# checkout, a per-user data directory in a packaged build (backend/paths.py).
PROJECT_DIR = paths.PROJECT_DIR
WORK_DIR = paths.WORK_DIR
OUTPUT_DIR = paths.OUTPUT_DIR
UPLOAD_DIR = paths.UPLOAD_DIR

# Work dirs hold per-job folders; the other two areas are scanned recursively.
AREAS = ("work", "output", "uploads")


def _area_dirs() -> Dict[str, Path]:
    """The scan roots, resolved at CALL time.

    Deliberately not a module-level dict: ``WORK_DIR``/``OUTPUT_DIR``/
    ``UPLOAD_DIR`` get patched — by tests, and by anything that runs this app
    against a different runtime directory — and an import-time snapshot kept
    scanning the ORIGINAL locations no matter what.  That is how a second
    process once collected the first one's uploads.
    """
    return {"work": WORK_DIR, "output": OUTPUT_DIR, "uploads": UPLOAD_DIR}

DEFAULT_MAX_AGE_HOURS = 168.0   # 7 days
DEFAULT_INTERVAL_HOURS = 6.0

_MB = 1048576.0


def _num(value, default: float) -> float:
    try:
        f = float(value)
        return f if f > 0 else default
    except (TypeError, ValueError):
        return default


def max_age_hours() -> float:
    """Unreferenced temp files older than this many hours are cleanable."""
    return _num(resolve().get("cleanup_max_age_hours"), DEFAULT_MAX_AGE_HOURS)


def interval_hours() -> float:
    """How often the background cleanup loop re-runs."""
    return _num(resolve().get("cleanup_interval_hours"), DEFAULT_INTERVAL_HOURS)


def on_disk_job_paths() -> set:
    """Paths of jobs that exist ON DISK, whether or not this process knows them.

    ``uploads/<id>.pdf`` and ``work/<id>/`` reference each other: the UI restores
    jobs from disk, "retry"/"finalize" need the upload, and the work dir holds
    the OCR results.  A process whose registry does not contain the job (a second
    instance, or one started against different work dirs) would otherwise see
    both halves as garbage and delete them once they are older than the age
    limit.  Only a pair counts: a lone half is what an interrupted ``clear_job``
    or a failed upload leaves behind, and that is real garbage.
    """
    refs: set = set()
    try:
        if not WORK_DIR.exists() or not UPLOAD_DIR.exists():
            return refs
        work_ids = {p.name for p in WORK_DIR.iterdir() if p.is_dir()}
        uploads = {p.stem: p for p in UPLOAD_DIR.glob("*.pdf")}
        for job_id in work_ids & set(uploads):
            refs.add(str((WORK_DIR / job_id).resolve()))
            refs.add(str(uploads[job_id].resolve()))
    except OSError:
        log.debug("could not enumerate the on-disk jobs", exc_info=True)
    return refs


def referenced_paths() -> set:
    """Absolute paths of everything live jobs still need (never cleaned)."""
    from backend import ocr_service

    refs = set()
    for job in ocr_service.all_jobs():
        for key in ("pdf_path", "hocr_dir", "previews_dir", "embedded_path"):
            p = job.get(key)
            if p:
                refs.add(str(Path(p).resolve()))
    return refs | on_disk_job_paths()


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _item_info(path: Path) -> Optional[dict]:
    """Size / age / kind of one temp item (None if un-statable or a symlink)."""
    if path.is_symlink():
        return None  # never touch symlinks
    try:
        st = path.stat()
    except OSError:
        return None
    is_dir = path.is_dir()
    size = _dir_size(path) if is_dir else st.st_size
    age = max(0.0, (time.time() - st.st_mtime) / 3600.0)
    return {
        "path": str(path),
        "name": path.name,
        "is_dir": is_dir,
        "size": size,
        "age_hours": round(age, 2),
        "mtime": int(st.st_mtime),
    }


def _children(base: Path, recursive: bool) -> List[Path]:
    """Non-symlink entries under *base* (top-level for work, recursive else)."""
    if not base.exists():
        return []
    it = base.rglob("*") if recursive else base.glob("*")
    return [p for p in it if not p.is_symlink()]


def _scan() -> Dict[str, dict]:
    """Split every temp area into referenced (kept) and unreferenced items."""
    refs = referenced_paths()
    areas: Dict[str, dict] = {}
    for area in AREAS:
        base = _area_dirs()[area]
        recursive = area != "work"
        referenced_items, unreferenced = [], []
        for child in _children(base, recursive):
            if str(child.resolve()) in refs:
                referenced_items.append(child)
                continue
            info = _item_info(child)
            if info is not None:
                unreferenced.append(info)
        areas[area] = {
            "unreferenced": unreferenced,
            "referenced_count": len(referenced_items),
        }
    return areas


def inventory(age_limit: Optional[float] = None) -> dict:
    """Inventory of unreferenced temp files, grouped by area.

    ``ready_*`` counts only items old enough to be cleaned at *age_limit*.
    """
    age_limit = age_limit if age_limit is not None else max_age_hours()
    areas = {}
    total_unref = {"count": 0, "bytes": 0}
    total_ready = {"count": 0, "bytes": 0}
    for area, data in _scan().items():
        unref = data["unreferenced"]
        unref.sort(key=lambda i: i["age_hours"], reverse=True)
        ready = [i for i in unref if i["age_hours"] >= age_limit]
        a_unref = {"count": len(unref), "bytes": sum(i["size"] for i in unref)}
        a_ready = {"count": len(ready), "bytes": sum(i["size"] for i in ready)}
        areas[area] = {
            "items": unref,
            "unreferenced_count": a_unref["count"],
            "unreferenced_bytes": a_unref["bytes"],
            "ready_count": a_ready["count"],
            "ready_bytes": a_ready["bytes"],
            "referenced_count": data["referenced_count"],
        }
        total_unref["count"] += a_unref["count"]
        total_unref["bytes"] += a_unref["bytes"]
        total_ready["count"] += a_ready["count"]
        total_ready["bytes"] += a_ready["bytes"]
    return {
        "areas": areas,
        "totals": {
            "unreferenced_count": total_unref["count"],
            "unreferenced_bytes": total_unref["bytes"],
            "ready_count": total_ready["count"],
            "ready_bytes": total_ready["bytes"],
        },
        "age_limit_hours": round(age_limit, 2),
    }


def cleanup(age_limit: Optional[float] = None, dry_run: bool = False,
            force: bool = False) -> dict:
    """Delete unreferenced temp items older than *age_limit* hours.

    Never touches paths referenced by live jobs.  ``force`` also removes
    unreferenced items younger than the limit (referenced paths are still
    kept).  ``dry_run`` reports what *would* be removed without deleting.
    Returns a summary of what was deleted / kept / freed.
    """
    age_limit = age_limit if age_limit is not None else max_age_hours()
    deleted: List[dict] = []
    kept = {"referenced": 0, "too_fresh": 0, "errors": 0}
    freed_bytes = 0
    for area, data in _scan().items():
        kept["referenced"] += data["referenced_count"]
        for info in data["unreferenced"]:
            if not (force or info["age_hours"] >= age_limit):
                kept["too_fresh"] += 1
                continue
            deleted.append(info)
            freed_bytes += info["size"]
            if dry_run:
                continue
            path = Path(info["path"])
            try:
                if info["is_dir"]:
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                kept["errors"] += 1
                log.exception("cleanup: failed to remove %s", path)

    freed_mb = freed_bytes / _MB
    log.info("cleanup(age_limit=%.1fh, dry_run=%s, force=%s): %d item(s), "
             "%.1f MB freed, %s kept",
             age_limit, dry_run, force, len(deleted), freed_mb, kept)
    return {
        "dry_run": bool(dry_run),
        "force": bool(force),
        "age_limit_hours": round(age_limit, 2),
        "deleted": deleted,
        "deleted_count": len(deleted),
        "freed_bytes": freed_bytes,
        "kept": kept,
    }


# --- background automatic cleanup -------------------------------------------
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def start_background_cleanup() -> threading.Thread:
    """Start (or return the existing) daemon thread that auto-cleans files.

    The loop runs one pass shortly after startup, then re-runs every
    ``cleanup_interval_hours``.  Deletion only ever targets unreferenced files
    older than ``cleanup_max_age_hours``, so a restart is safe.
    """
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_cleanup_loop, daemon=True,
                               name="tmp-cleanup")
    _thread.start()
    log.info("background temp-file cleanup started "
             "(interval=%.1fh, max_age=%.1fh)", interval_hours(), max_age_hours())
    return _thread


def stop_background_cleanup() -> None:
    """Signal the background loop to exit (used on server shutdown)."""
    _stop.set()


def _cleanup_loop() -> None:
    # A short initial delay avoids racing a *just-started* server / upload.
    if not _stop.wait(5.0):
        try:
            result = cleanup()
            if result["deleted_count"]:
                log.info("auto-cleanup: removed %d item(s), %.1f MB",
                         result["deleted_count"], result["freed_bytes"] / _MB)
        except Exception:  # noqa: BLE001
            log.exception("auto-cleanup: initial pass failed")
    while not _stop.is_set():
        if _stop.wait(interval_hours() * 3600.0):
            break
        try:
            result = cleanup()
            if result["deleted_count"]:
                log.info("auto-cleanup: removed %d item(s), %.1f MB",
                         result["deleted_count"], result["freed_bytes"] / _MB)
        except Exception:  # noqa: BLE001
            log.exception("auto-cleanup: pass failed")