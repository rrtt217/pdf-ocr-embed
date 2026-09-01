"""Page preview rendering (PyMuPDF) for the WebUI editor.

Since the rebuild, the OCR core and the final text layer live in OCRmyPDF
(rasterization, engine calls, hOCR rendering, grafting, PDF/A, optimization).
What remains here is the WebUI's page preview: rendering the uploaded PDF's
pages to PNG images the editor overlays recognized blocks onto.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

import fitz  # PyMuPDF

log = logging.getLogger(__name__)

# Preview render zoom: the editor scales block overlays by the page's OCR
# pixel width/height, so any consistent zoom aligns correctly.
PREVIEW_DPI = 150.0


def open_pdf(source_path: str) -> fitz.Document:
    """Open a PDF (PyMuPDF)."""
    return fitz.open(source_path)


def page_count(pdf_path: str) -> int:
    """Number of pages in a PDF."""
    with open_pdf(pdf_path) as doc:
        return doc.page_count


def render_page_png(pdf_path: str, page_index: int, out_path: Path,
                    dpi: float = PREVIEW_DPI) -> Tuple[int, int]:
    """Render one page of a PDF to a PNG preview; returns its pixel size.

    Raises for an out-of-range page or an unreadable PDF; the caller surfaces
    the failure.
    """
    with open_pdf(pdf_path) as doc:
        if not 0 <= page_index < doc.page_count:
            raise ValueError(f"page_index out of range: {page_index} "
                             f"(document has {doc.page_count} page(s))")
        page = doc[page_index]
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out_path)
        return int(pix.width), int(pix.height)
