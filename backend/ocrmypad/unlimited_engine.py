"""Unlimited-OCR engine as an OCRmyPDF ``OcrEngine`` plugin.

The engine receives ocrmypdf-rasterized page images, calls the unlimited-ocr
OpenAI-compatible API (``backend.ocrmypad.engine_client``), parses the marker
stream into blocks (``backend.ocrmypad.parser``), and writes:

* an hOCR file (what ocrmypdf's fpdf2 renderer turns into the invisible text
  layer, and what ``ocrmypdf._hocr_to_ocr_pdf`` renders after user edits), and
* a plain-text sidecar, and
* a block sidecar JSON (``*_ocr_hocr.blocks.json``) that the WebUI edits and
  that feeds progress reporting.

Page attribution: the engine derives the job id from ``options.output_folder``
(the hOCR pipeline's work folder sits directly under the job dir, a convention
documented in ``backend.ocr_service``) and reports progress into
``backend.ocrmypad.progress``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.ocrmypad import progress as progress_mod
from backend.ocrmypad.engine_client import (
    UnlimitedOcrClient,
    image_dpi,
    image_size,
)
from backend.ocrmypad.parser import (
    blocks_to_hocr,
    hocr_page_sidecar_text,
    parse_response,
)
from ocrmypdf import hookimpl
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence

if TYPE_CHECKING:
    from ocrmypdf._options import OcrOptions

log = logging.getLogger(__name__)


def _job_id_from_options(options: "OcrOptions | None") -> str:
    """Derive the job id from the hOCR pipeline's work folder.

    Convention (backend.ocr_service): ``_pdf_to_hocr`` is called with
    ``output_folder = work/<job_id>/hocr`` — the parent of that folder is the
    job dir.  Unknown conventions return "" and progress reporting is skipped.
    """
    if options is None:
        return ""
    folder = getattr(options, "output_folder", None)
    if not folder:
        return ""
    try:
        return Path(folder).resolve().parent.name
    except (OSError, ValueError, TypeError):
        return ""


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
        job_id = _job_id_from_options(options)
        if job_id and progress_mod.is_cancelled(job_id):
            raise RuntimeError("OCR job cancelled by user")

        width, height = image_size(input_file)
        dpi = image_dpi(input_file)
        page_index = progress_mod.page_number_from_hocr_name(output_hocr.name)

        cfg: dict[str, Any] = {}
        try:
            from backend.config import resolve
            cfg = resolve()
        except Exception:  # noqa: BLE001
            log.debug("resolve() failed in engine; using defaults", exc_info=True)
        client = UnlimitedOcrClient(config=cfg)

        raw = client.recognize(input_file)
        page = parse_response(raw, width, height, page_index)

        hocr_text = blocks_to_hocr(width, height, page.blocks, dpi=dpi,
                                   ppageno=page_index)
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

        if job_id:
            progress_mod.report_page(job_id, page_index)
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
