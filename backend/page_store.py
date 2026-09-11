"""Engine-agnostic page interchange between the backend and any OCR engine.

This is the ONLY channel the rest of the backend uses to talk about pages —
no engine's raw output format ever leaks past this module:

* **Block sidecar JSON** (``000001_ocr_hocr.blocks.json``) — the normalized
  page representation (blocks in raw pixel space) the WebUI edits.  Written by
  engines that produce it natively (the unlimited plugin) and derived from
  hOCR for engines that do not (ocrmypdf's built-in Tesseract, and any future
  ``OcrEngine``).
* **hOCR** (``000001_ocr_hocr.hocr``) — ocrmypdf's own interchange format:
  ``generate_hocr`` is the abstract method every engine implements, so parsing
  it here is engine-agnostic by construction (via ocrmypdf's own parser).
* **Cancel flag** (``<job>/cancel``) — a plain file the service creates to ask
  the engine to stop; the engine polls for it.  No shared registry, no imports.

All communication between the engine and the backend goes through the job's
work folder on disk.
"""
from __future__ import annotations

import json
import base64
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from backend import pdf_processing

log = logging.getLogger(__name__)


# --- paths -------------------------------------------------------------------

def hocr_path(hocr_dir: Path, page_no: int) -> Path:
    """The hOCR file for a 1-based page number."""
    return Path(hocr_dir) / f"{page_no:06d}_ocr_hocr.hocr"


def sidecar_path(hocr_dir: Path, page_no: int) -> Path:
    """The block sidecar JSON for a 1-based page number."""
    return Path(hocr_dir) / f"{page_no:06d}_ocr_hocr.blocks.json"


def cancel_path(job_dir: Path) -> Path:
    """The cancel flag file for a job."""
    return Path(job_dir) / "cancel"


# --- cancel flag (service -> engine, via the filesystem) ---------------------

# Cancellation is an EXCLUSIVE capability of the unlimited engine plugin: the
# plugin polls <job_dir>/cancel per page (its own mirror of this contract,
# ocrmypdf_unlimited.files) and stops gracefully, keeping completed pages —
# ocrmypdf itself can only be hard-interrupted.  This module is the host's
# WRITE side; the plugin's files.py is the READ side.  Same path, no imports
# either way (pinned by tests).

def request_cancel(job_dir: Path) -> None:
    """Ask the engine to stop processing more pages for this job.

    The engine polls for this file per page (see ``is_cancelled``); creating
    it is atomic enough — a page already in flight finishes and is kept.
    """
    try:
        cancel_path(job_dir).touch()
    except OSError:
        log.exception("failed to write cancel flag to %s", job_dir)


def is_cancelled(job_dir: Path) -> bool:
    """True when a stop was requested (checked by the engine per page)."""
    return cancel_path(job_dir).exists()


# --- page inventory ----------------------------------------------------------

def _page_no_from_name(path: Path) -> Optional[int]:
    """The 1-based page number encoded in an hOCR/sidecar file name."""
    try:
        return int(path.name.split("_", 1)[0])
    except (ValueError, IndexError):
        return None


def _hocr_complete(hocr_file: Path) -> bool:
    """True when an hOCR file looks *fully written*, not mid-write.

    ocrmypdf's built-in Tesseract writes ``<n>_ocr_hocr.hocr`` progressively
    while it OCRs the page — the closing ``</html>`` only lands when the page
    is done — so a bare file existing in the folder is NOT a finished page
    (parsing a half-written file spams "hOCR parse failed: no element found").

    A page counts as complete when:
      * its hOCR file is non-empty and carries its closing ``</html>`` tag —
        the only reliable content signal.  A stale ``<n>_hocr.json`` from a
        previous run must NOT count: a forced re-run rewrites the hOCR in
        place while the old marker is still on disk, and the marker is only
        refreshed after the page finishes again; or
      * its hOCR is EMPTY and ocrmypdf's per-page marker ``<n>_hocr.json``
        exists — an intentionally empty hOCR (ocrmypdf's null page for a
        timeout / empty page) has no closing tag but is a finished page,
        whereas an empty file with no marker is just tesseract creating it.
    """
    marker = hocr_file.with_name(hocr_file.name.replace(
        "_ocr_hocr.hocr", "_hocr.json"))
    try:
        size = hocr_file.stat().st_size
    except OSError:
        return False
    if size == 0:
        return marker.exists()
    try:
        with open(hocr_file, "rb") as fh:
            fh.seek(max(0, size - 128))
            tail = fh.read()
    except OSError:
        return False
    return b"</html>" in tail.rstrip()


def page_numbers(hocr_dir: Path) -> List[int]:
    """Sorted 1-based page numbers that have a COMPLETE result on disk.

    The union of block sidecars and *fully written* hOCR files: an engine may
    produce either (or both).  This is what "retry remaining", progress
    display and the embed guard count — for every engine.

    The bare hOCR file is only a completion signal once it is fully written
    (see ``_hocr_complete``): engines such as ocrmypdf's built-in Tesseract
    stream the file while they run, and a half-written page otherwise shows up
    as "done" in progress and fails to parse downstream.
    """
    hdir = Path(hocr_dir)
    if not hdir.exists():
        return []
    numbers: set[int] = set()
    # Block sidecars are complete by construction (one JSON blob per page).
    for path in hdir.glob("*_ocr_hocr.blocks.json"):
        page_no = _page_no_from_name(path)
        if page_no is not None:
            numbers.add(page_no)
    for path in hdir.glob("*_ocr_hocr.hocr"):
        page_no = _page_no_from_name(path)
        if page_no is None or page_no in numbers:
            continue
        if _hocr_complete(path):
            numbers.add(page_no)
    return sorted(numbers)


def has_results(hocr_dir: Path) -> bool:
    """True when at least one page has a result (any engine)."""
    return bool(page_numbers(hocr_dir))


# --- hOCR import (the engine-agnostic fallback) -------------------------------

_KIND_MAP = {
    "ocr_header": "heading",
    "ocr_footer": "footnote",
    "ocr_caption": "image_caption",
}


def _dpi_from_origin_pdf(origin_pdf: Optional[Path], page_no: int,
                         width_px: int) -> Optional[float]:
    """Derive the page DPI from the work folder's origin.pdf.

    ocrmypdf's hOCR pipeline copies the input to ``<hocr_dir>/origin.pdf``.
    The hOCR page bbox is in raw pixels, so the true DPI is
    ``width_px / page_width_inches`` — exact for regenerating the hOCR after
    edits when the engine's own hOCR carries no ``scan_res``.
    """
    if not origin_pdf or not Path(origin_pdf).exists():
        return None
    try:
        width_pt, _height_pt = pdf_processing.page_size_pt(
            origin_pdf, max(0, page_no - 1))
        width_inches = width_pt / 72.0
        if width_inches <= 0:
            return None
        return float(width_px) / width_inches
    except Exception:  # noqa: BLE001
        log.debug("origin.pdf DPI derivation failed", exc_info=True)
        return None


def hocr_to_page(hocr_file: Path, origin_pdf: Optional[Path] = None,
                 page_no: Optional[int] = None) -> Tuple[Optional[dict], Optional[float]]:
    """Parse one hOCR file into the normalized page dict (engine-agnostic).

    Uses ocrmypdf's own hOCR parser (the format is the plugin contract every
    engine implements).  Each ``ocr_line`` (or header/footer/caption variant)
    becomes one block; words are joined with spaces; bboxes are the line
    boxes in raw pixel space (integers).  Returns ``(page_dict, dpi)`` —
    ``(None, None)`` when the file cannot be parsed.
    """
    try:
        from ocrmypdf.hocrtransform.hocr_parser import HocrParser
        tree = HocrParser(hocr_file).parse()
    except Exception as exc:  # noqa: BLE001
        # One line, not a full traceback: the underlying parser already
        # reported the concrete reason (usually "no element found" for a
        # truncated file), and a mid-write file must not spam the log.
        log.warning("hOCR parse failed for %s: %s", hocr_file, exc)
        return None, None

    if page_no is None:
        try:
            page_no = int(hocr_file.name.split("_", 1)[0])
        except (ValueError, IndexError):
            page_no = 1

    bbox = tree.bbox
    width = int(round(bbox.right - bbox.left)) if bbox else 0
    height = int(round(bbox.bottom - bbox.top)) if bbox else 0

    blocks: List[dict] = []
    for par in tree.children:
        for line in par.children:
            if line.bbox is None:
                continue
            words = [w.text for w in line.children if w.text]
            text = " ".join(words).strip()
            if not text:
                continue
            confs = [w.confidence for w in line.children
                     if w.confidence is not None]
            # ocrmypdf's HocrParser already converts the hOCR ``x_wconf``
            # (0-100) to a 0.0-1.0 fraction; the block conf must stay in that
            # same scale (the WebUI multiplies by 100 for display).
            conf = round(sum(confs) / len(confs), 2) if confs else None
            lb = line.bbox
            int_bbox = [max(0, int(round(lb.left))), max(0, int(round(lb.top))),
                        max(0, int(round(lb.right))), max(0, int(round(lb.bottom)))]
            blocks.append({
                "kind": _KIND_MAP.get(line.ocr_class, "text"),
                "bbox": int_bbox,
                "text": text,
                "lines": [text],
                **({"conf": conf} if conf is not None else {}),
            })

    page = {
        "page_index": page_no - 1,
        "width": width,
        "height": height,
        "blocks": blocks,
    }

    # DPI: the engine's scan_res when present; otherwise derived from the
    # origin PDF page geometry (needed to regenerate the hOCR after edits).
    dpi = float(tree.dpi) if tree.dpi else None
    if not dpi:
        dpi = _dpi_from_origin_pdf(origin_pdf, page_no, width)

    return page, dpi


def load_page(hocr_dir: Path, page_no: int,
              persist_missing_sidecar: bool = True) -> Optional[dict]:
    """Load one page's normalized dict: sidecar first, else derived from hOCR.

    When only an hOCR file exists (an engine that does not write block
    sidecars), the page is derived from it and the derived sidecar is written
    back, so the page becomes editable exactly like an engine-native one.
    Returns ``None`` when the page has no result.
    """
    hdir = Path(hocr_dir)
    sidecar = sidecar_path(hdir, page_no)
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data.get("page") or data
        except (OSError, ValueError):
            log.warning("unreadable sidecar %s", sidecar, exc_info=True)
            return None

    hocr_file = hocr_path(hdir, page_no)
    if not hocr_file.exists():
        return None

    page, dpi = hocr_to_page(hocr_file, origin_pdf=hdir / "origin.pdf",
                             page_no=page_no)
    if page is None:
        return None
    if persist_missing_sidecar:
        try:
            sidecar.write_text(
                json.dumps({"page": page, "dpi": dpi or 300.0},
                           ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            log.warning("failed to persist derived sidecar %s", sidecar,
                        exc_info=True)
    return page


def read_sidecar_dpi(hocr_dir: Path, page_no: int) -> float:
    """The DPI stored in a page's sidecar (300.0 fallback)."""
    sidecar = sidecar_path(hocr_dir, page_no)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return float(data.get("dpi") or 300.0)
    except (OSError, ValueError, TypeError):
        return 300.0


# --- hOCR emission (the engine-agnostic write direction) ----------------------

def split_block_lines(text: str) -> List[str]:
    """Split a block's text into renderable lines.

    The block text may contain tabs (table cell separators); a tab becomes a
    space-separated gap inside one line so the whole row stays on one baseline.
    """
    return [ln.strip() for ln in text.replace("\t", "  ").split("\n") if ln.strip()]


def _hocr_escape(text: str) -> str:
    """Escape text for embedding in the hOCR (X)HTML document."""
    import html as _html
    return _html.escape(text, quote=True)


def _block_lines(block: dict) -> List[str]:
    """Renderable lines for one block dict (text, falling back to caption)."""
    lines = [ln.strip() for ln in (block.get("lines") or [])
             if isinstance(ln, str) and ln.strip()]
    if lines:
        return lines
    text = (block.get("text") or "").strip()
    if text:
        return split_block_lines(text)
    caption = (block.get("caption") or "").strip()
    return [caption] if caption else []


def _line_bboxes(bbox: List[int], n_lines: int) -> List[List[int]]:
    """Distribute a block's bbox vertically across ``n_lines`` equal rows.

    Only used for the invisible text layer: each renderable line gets an equal
    vertical slice of its block's bbox; a line never extends outside it.
    """
    x1, y1, x2, y2 = bbox
    if n_lines <= 0:
        return []
    height = y2 - y1
    out = []
    for i in range(n_lines):
        top = y1 + int(round(height * i / n_lines))
        bottom = y1 + int(round(height * (i + 1) / n_lines))
        if bottom <= top:
            bottom = top + 1
        out.append([x1, top, x2, min(bottom, y2)])
    return out


def _hocr_line_class(kind: str) -> str:
    """Map a block kind to the closest standard hOCR line class."""
    return {
        "heading": "ocr_header",
        "footnote": "ocr_footer",
        "image_caption": "ocr_caption",
    }.get(kind, "ocr_line")


def _raw_prop_value(text: str) -> str:
    """Encode a block's raw text as an hOCR 1.2 ``ascii-word`` property value.

    Host-side copy of ``ocrmypdf_unlimited.parser._raw_prop_value`` (pinned
    identical by tests) — the backend never imports plugin internals.
    base64url (``A-Za-z0-9_=-``) satisfies the hOCR 1.2 §2.4 ``ascii-word``
    grammar (printable ASCII, no space/semicolon).
    """
    if not text:
        return ""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def blocks_to_hocr(width: int, height: int, blocks: List[dict],
                   dpi: float = 300.0, ppageno: int = 0) -> str:
    """Render normalized block dicts as an hOCR document for ocrmypdf.

    Accepts plain dicts (the interchange representation): ``kind``, ``bbox``,
    ``text`` and optionally ``caption``.  Structure (what
    ``ocrmypdf.hocrtransform`` parses):
      div.ocr_page (title: bbox + ppageno + scan_res)
        p.ocr_par (title: bbox)
          span.ocr_line (title: bbox)  -- one per renderable line
            span.ocrx_word (title: bbox)  -- one full-width word per line

    Gotchas honored:
      * ``scan_res`` MUST be present: the renderer's px->pt transform derives
        from it; a missing value would place text at the wrong scale.
      * an ocr_line with no ocrx_word child is DROPPED by the parser, so every
        line carries exactly one word span.
      * pure image blocks (no text, no caption) contribute nothing.
    """
    dpi_i = max(1, int(round(dpi)))
    body: List[str] = []
    for block in blocks:
        render_lines = _block_lines(block)
        if not render_lines:
            continue
        line_bboxes = _line_bboxes(block.get("bbox") or [0, 0, 0, 0],
                                   len(render_lines))
        line_class = _hocr_line_class(str(block.get("kind") or "text"))
        par_lines: List[str] = []
        for line_text, lb in zip(render_lines, line_bboxes):
            escaped = _hocr_escape(line_text)
            title = f"bbox {lb[0]} {lb[1]} {lb[2]} {lb[3]}"
            par_lines.append(
                f'  <span class="{line_class}" title="{title}">'
                f'<span class="ocrx_word" title="{title}">{escaped}</span>'
                f"</span>"
            )
        if not par_lines:
            continue
        b = block.get("bbox") or [0, 0, 0, 0]
        title = f"bbox {b[0]} {b[1]} {b[2]} {b[3]}"
        # Raw-content blocks (the engine's generate_raw option) carry
        # engine properties on the ocr_par title (hOCR 1.2 extensions,
        # invisible to ocrmypdf's renderer) so a regenerated hOCR keeps them.
        raw_text = str(block.get("raw") or "")
        if raw_text:
            title += f"; x_kind {block.get('kind') or 'text'}"
            raw_value = _raw_prop_value(raw_text)
            if raw_value:
                title += f"; x_raw {raw_value}"
        body.append(
            f' <p class="ocr_par" title="{title}">\n'
            + "\n".join(par_lines)
            + "\n </p>"
        )

    body_text = "\n".join(body)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"\n'
        '    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="und" lang="und">\n'
        "<head>\n"
        "<title>OCR page</title>\n"
        '<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
        "</head>\n"
        "<body>\n"
        f"<div class='ocr_page' id='page_{ppageno + 1}' "
        f"title='bbox 0 0 {width} {height}; ppageno {ppageno}; "
        f"scan_res {dpi_i} {dpi_i}'>\n"
        f"{body_text}\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )
