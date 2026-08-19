"""Batch helpers for feature #10: multi-file upload + ZIP packaging.

This module keeps the zip/layout logic as pure(ish) functions so it can be
unit-tested without a running server:

  - ``collect_embedded(job_ids)`` — resolve a list of job ids to the subset
    that actually has an embedded output PDF on disk (skips unknown jobs and
    jobs that are still running / failed / never embedded).
  - ``_unique_arcname(...)`` — pick a collision-free member name inside the
    archive, preferring the job's source filename.
  - ``build_zip(zip_path, entries)`` — write the archive by streaming each
    embedded PDF straight from disk (never loading the whole zip into RAM).

The HTTP endpoint in ``backend/main.py`` builds the archive into a temp file
and serves it with a streaming ``FileResponse``, deleting the temp file in the
background once the response has been sent.
"""
from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from backend import ocr_service

log = logging.getLogger(__name__)

# Stream members in bounded chunks so a 500-page scan is never read as one
# in-memory blob just to be zipped.
_COPY_CHUNK = 1024 * 1024


def collect_embedded(job_ids: List[str]) -> List[dict]:
    """Resolve job ids to entries that can be zipped right now.

    Each returned entry is ``{"job_id", "filename", "embedded_path"}`` where
    ``embedded_path`` is a path that exists on disk.  Unknown job ids, jobs
    without an ``embedded_path`` and jobs whose embedded file has been
    deleted (e.g. by the temp-file cleanup) are skipped — the archive simply
    contains fewer members.
    """
    entries: List[dict] = []
    for job_id in job_ids or []:
        job = ocr_service.get_job(job_id)
        if job is None:
            continue
        path = job.get("embedded_path")
        if not path or not Path(path).exists():
            continue
        entries.append({
            "job_id": job_id,
            "filename": str(job.get("filename") or f"{job_id}.pdf"),
            "embedded_path": str(path),
        })
    return entries


def _unique_arcname(used: set, filename: str) -> str:
    """Collision-free archive member name based on *filename*.

    Falls back to a ``file_<n>`` name when the filename is empty or would
    escape its member dir (e.g. absolute or ``..`` paths).  Existing names get
    a numeric suffix before the extension: ``doc.pdf`` -> ``doc (2).pdf``.
    """
    clean = Path(filename).name if filename else ""
    if not clean or clean in (".", ".."):
        clean = "file.pdf"
    if clean not in used:
        used.add(clean)
        return clean
    stem = Path(clean).stem
    suffix = Path(clean).suffix
    n = 2
    while True:
        candidate = f"{stem} ({n}){suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def build_zip(zip_path: Path, entries: List[dict]) -> dict:
    """Write a ZIP archive containing each entry's embedded PDF from disk.

    Members are named after the job's *source* filename (deduplicated on
    collision) and streamed in chunks from disk directly into the archive —
    neither the members nor the archive itself are buffered in RAM.  Returns a
    small summary: ``{"count", "files", "archive"}`` (``archive`` is the
    resolved ``zip_path`` as a string).
    """
    zip_path = Path(zip_path)
    used: set = set()
    files: List[str] = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in entries:
            src = Path(entry["embedded_path"])
            arc = _unique_arcname(used, str(entry.get("filename") or ""))
            try:
                # ZipFile.open(name, "w") gives us a writable member handle;
                # shutil.copyfileobj streams it from disk in bounded chunks.
                with open(src, "rb") as src_fh, zf.open(arc, "w") as dst_fh:
                    shutil.copyfileobj(src_fh, dst_fh, length=_COPY_CHUNK)
                files.append(arc)
            except OSError:
                log.exception("batch: failed to add %s to archive", src)
    return {"count": len(files), "files": files, "archive": str(zip_path.resolve())}


def default_zip_name() -> str:
    """Filename suggested for the download (a constant, kept here for tests)."""
    return "ocr_results.zip"
