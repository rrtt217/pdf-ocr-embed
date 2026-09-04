"""The plugin's OWN work-folder contract — filesystem-only, no shared registry.

The plugin and a host application communicate ONLY through the job's work
folder on disk:

* **hOCR / sidecars** — written by the engine per page into the folder
  ocrmypdf passes as ``options.output_folder`` (``<n>_ocr_hocr.hocr`` /
  ``<n>_ocr_hocr.blocks.json``).
* **Cancel flag** — ``<job_dir>/cancel``, where ``job_dir`` is
  ``output_folder``'s parent.  This is the *exclusive* cancellation capability
  of this plugin: ocrmypdf itself has no mid-run cancel hook (a hard interrupt
  there would lose every completed page), so the plugin polls for the flag
  between pages and stops gracefully, keeping the pages already on disk.

A host that wants to stop a run simply creates ``<job_dir>/cancel`` (see the
host's ``backend.page_store.request_cancel``); a standalone CLI user can
``touch`` it.  The convention here and in any host is deliberately duplicated
on both sides — neither side imports the other.
"""
from __future__ import annotations

from pathlib import Path


def hocr_path(hocr_dir: Path, page_no: int) -> Path:
    """The hOCR file for a 1-based page number (ocrmypdf's naming)."""
    return Path(hocr_dir) / f"{page_no:06d}_ocr_hocr.hocr"


def sidecar_path(hocr_dir: Path, page_no: int) -> Path:
    """The block sidecar JSON for a 1-based page number."""
    return Path(hocr_dir) / f"{page_no:06d}_ocr_hocr.blocks.json"


def cancel_path(job_dir: Path) -> Path:
    """The cancel flag file for a job (``<job_dir>/cancel``)."""
    return Path(job_dir) / "cancel"


def is_cancelled(job_dir: Path) -> bool:
    """True when a stop was requested (polled by the engine per page).

    This is the plugin's exclusive graceful-stop channel: a ``cancel`` file in
    the job dir.  ocrmypdf cannot interrupt a run without losing progress, so
    the plugin owns cancellation semantics for its own pages.
    """
    return cancel_path(job_dir).exists()


def page_index_from_name(name: str) -> int:
    """Parse the 0-based page index from an ocrmypdf page file name.

    ocrmypdf names per-page work files ``000001_ocr_hocr.hocr`` etc.
    Returns 0 when the prefix is unparseable.
    """
    try:
        return max(0, int(name.split("_", 1)[0]) - 1)
    except (ValueError, IndexError):
        return 0
