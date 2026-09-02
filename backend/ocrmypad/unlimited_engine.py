"""Unlimited-OCR engine as an OCRmyPDF ``OcrEngine`` plugin.

The engine receives ocrmypdf-rasterized page images, calls the unlimited-ocr
OpenAI-compatible API (``backend.ocrmypad.engine_client``), parses the marker
stream into blocks (``backend.ocrmypad.parser``), and writes:

* an hOCR file (what ocrmypdf's fpdf2 renderer turns into the invisible text
  layer, and what ``ocrmypdf._hocr_to_ocr_pdf`` renders after user edits), and
* a plain-text sidecar, and
* a block sidecar JSON (``*_ocr_hocr.blocks.json``) that the WebUI edits.

Decoupling: the engine communicates with the backend ONLY through the job's
work folder on disk (hOCR, sidecars, the ``<job>/cancel`` flag file).  It
derives the job dir from ``options.output_folder`` (the hOCR pipeline's work
folder, a convention documented in ``backend.ocr_service``) and never imports
job/progress state.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.ocrmypad.engine_client import (
    UnlimitedOcrClient,
    image_dpi,
    image_size,
)
from backend.ocrmypad.line_split import (
    split_block_into_lines,
    split_block_text_across_bands,
)
from backend.ocrmypad.parser import (
    blocks_to_hocr,
    hocr_page_sidecar_text,
    parse_response,
)
from backend.page_store import is_cancelled
from ocrmypdf import hookimpl
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence

if TYPE_CHECKING:
    from ocrmypdf._options import OcrOptions

log = logging.getLogger(__name__)


def _per_line_overrides(input_file: Path, page) -> dict:
    """Recover accurate per-line placement for the invisible text layer.

    The model reports paragraph-level bboxes; each renderable line is placed
    on an equal vertical slice today (approximate).  This re-derives the true
    printed line rows from the page image (``backend.ocrmypad.line_split``):

    * blocks whose text already splits into >1 line get one bbox per line,
      aligned to that line's ink;
    * blocks reported as a single long line over a multi-line bbox get their
      text distributed across the detected printed line bands.

    Returns ``{block_index: [(line_text, [x1, y1, x2, y2]), ...]}`` for
    ``blocks_to_hocr``.  Every path falls back to the block's own lines with
    equal-slice bboxes when the image cannot support the split, so the render
    is never worse than today.  Editor semantics are untouched: the block
    sidecar still carries one bbox per block.
    """
    overrides: dict = {}
    try:
        from PIL import Image
        with Image.open(input_file) as img:
            gray = img.convert("L")
            for bi, block in enumerate(page.blocks):
                if block.kind == "image" or not block.lines:
                    continue
                pairs: list = []
                n_lines = len(block.lines)
                if n_lines > 1:
                    boxes = split_block_into_lines(gray, block.bbox, n_lines)
                    if len(boxes) == n_lines:
                        pairs = list(zip(block.lines, boxes))
                elif block.text.strip():
                    split = split_block_text_across_bands(
                        gray, block.bbox, block.text)
                    if split:
                        pairs = list(split)
                if pairs:
                    overrides[bi] = pairs
    except Exception:  # noqa: BLE001  (never degrade the run for placement)
        log.warning("per-line bbox recovery failed for %s; using equal slices",
                    input_file, exc_info=True)
    if overrides:
        log.debug("page %s: recovered per-line placement for %d block(s)",
                  input_file.name, len(overrides))
    return overrides


def _job_dir_from_options(options: "OcrOptions | None") -> Path:
    """Derive the job dir from the hOCR pipeline's work folder.

    Convention (backend.ocr_service): ``_pdf_to_hocr`` is called with
    ``output_folder = work/<job_id>/hocr`` — the parent of that folder is the
    job dir (where the ``cancel`` flag lives).  Unknown conventions return
    the work folder's own parent, which simply never has a cancel flag.
    """
    if options is None:
        return Path()
    folder = getattr(options, "output_folder", None)
    if not folder:
        return Path()
    try:
        return Path(folder).resolve().parent
    except (OSError, ValueError, TypeError):
        return Path()


def _page_index_from_name(name: str) -> int:
    """Parse the 0-based page index from an ocrmypdf page file name.

    ocrmypdf names per-page work files ``000001_ocr_hocr.hocr`` etc.
    Returns 0 when the prefix is unparseable.
    """
    try:
        return max(0, int(name.split("_", 1)[0]) - 1)
    except (ValueError, IndexError):
        return 0


class UnlimitedOcrEngine(OcrEngine):
    """OCRmyPDF engine backed by the unlimited-ocr vision model."""

    @staticmethod
    def version() -> str:
        """Return the engine version (tracks the parser mapping)."""
        from backend.ocrmypad.parser import PARSE_VERSION
        return f"unlimited-ocr (parse v{PARSE_VERSION})"

    @staticmethod
    def creator_tag(options: "OcrOptions") -> str:
        """Return the creator tag for PDF metadata."""
        return "Unlimited-OCR"

    def __str__(self) -> str:
        """Return the human-readable engine name."""
        return "Unlimited-OCR (unlimited)"

    @staticmethod
    def languages(options: "OcrOptions") -> set[str]:
        """Accept any requested language: the engine is language-agnostic.

        Tesseract's internal-use languages (osd/equ) are already rejected by
        OCRmyPDF's global validation before this is consulted.
        """
        if options is not None:
            return set(options.languages) | {"und"}
        return {"und"}

    @staticmethod
    def get_orientation(input_file: Path, options: "OcrOptions") -> OrientationConfidence:
        """No rotation detection: the model OCRs the image as-is."""
        return OrientationConfidence(angle=0, confidence=0.0)

    @staticmethod
    def get_deskew(input_file: Path, options: "OcrOptions") -> float:
        """No deskew detection (deskew preprocessing stays opt-in)."""
        return 0.0

    @staticmethod
    def supports_generate_ocr() -> bool:
        """The editing flow renders from hOCR files, so hOCR is canonical."""
        return False

    @staticmethod
    def generate_pdf(input_file: Path, output_pdf: Path, output_text: Path,
                     options: "OcrOptions") -> None:
        """Not supported: the hOCR renderer produces the text layer."""
        raise NotImplementedError(
            "UnlimitedOcrEngine does not generate PDFs directly; "
            "use pdf_renderer='hocr' (the default 'auto' resolves to it).")

    @staticmethod
    def generate_hocr(input_file: Path, output_hocr: Path, output_text: Path,
                      options: "OcrOptions") -> None:
        """OCR one page image and write hOCR + sidecars.

        Runs inside ocrmypdf's worker threads.  Raises (fails the run) on
        genuine OCR failures; the caller's retry flow re-runs the job.
        """
        job_dir = _job_dir_from_options(options)
        if job_dir != Path() and is_cancelled(job_dir):
            raise RuntimeError("OCR job cancelled by user")

        width, height = image_size(input_file)
        dpi = image_dpi(input_file)
        page_index = _page_index_from_name(output_hocr.name)

        cfg: dict[str, Any] = {}
        try:
            from backend.config import resolve
            cfg = resolve()
        except Exception:  # noqa: BLE001
            log.debug("resolve() failed in engine; using defaults", exc_info=True)
        client = UnlimitedOcrClient(config=cfg)

        raw = client.recognize(input_file)
        page = parse_response(raw, width, height, page_index)

        per_line = _per_line_overrides(input_file, page)
        hocr_text = blocks_to_hocr(width, height, page.blocks, dpi=dpi,
                                   ppageno=page_index,
                                   per_line_overrides=per_line)
        output_hocr.write_text(hocr_text, encoding="utf-8")
        output_text.write_text(
            hocr_page_sidecar_text(page.blocks), encoding="utf-8")

        # Block sidecar: the WebUI's editable representation of this page,
        # written next to the hOCR so a restart can restore progress from disk.
        # ``dpi`` is carried so the hOCR can be regenerated with the same
        # scan_res after user edits (the renderer's px->pt transform needs it).
        try:
            sidecar = output_hocr.with_name(
                output_hocr.stem + ".blocks.json")
            sidecar.write_text(
                json.dumps({"page": page.to_dict(), "dpi": dpi},
                           ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            log.warning("page %d: failed to write block sidecar", page_index)

        log.info("page %d: %d block(s) recognized", page_index + 1,
                 len(page.blocks))


@hookimpl
def get_ocr_engine(options):
    """Return the unlimited engine when ``ocr_engine`` selects it.

    Registered ahead of ocrmypdf's built-in engines, so anything other than
    ``'unlimited'`` falls through to them (e.g. ``'auto'``/``'tesseract'`` use
    ocrmypdf's built-in Tesseract, ``'none'`` disables OCR).
    """
    if options is not None:
        engine_name = getattr(options, "ocr_engine", "auto")
        if engine_name != "unlimited":
            return None
    return UnlimitedOcrEngine()
