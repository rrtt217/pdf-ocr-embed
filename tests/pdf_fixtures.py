"""Tiny PDF builders for tests (fpdf2) — replaces the old PyMuPDF fixtures.

The application itself never *creates* PDFs (OCRmyPDF and pikepdf do the
writing); tests only need real, readable one/multi-page PDFs — blank or with a
real text layer — to feed the read-only pypdfium2 seam in
``backend.pdf_processing``.  ``fpdf2`` is already a mandatory OCRmyPDF
dependency, so this adds nothing to the dependency set.

Page units are PDF points, matching the ``width``/``height`` the old
``fitz.new_page(width=..., height=...)`` calls passed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from fpdf import FPDF

# Where the text baseline sits (points, top-left origin).  Chosen to fit
# comfortably on the small pages the tests build.
TEXT_X = 50.0
TEXT_Y = 50.0
FONT_SIZE = 14

PathLike = Union[str, Path]


def _build(pages: int, text: str, width: float, height: float) -> bytes:
    pdf = FPDF(unit="pt", format=(float(width), float(height)))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    for index in range(max(1, int(pages))):
        pdf.add_page()
        # Only the first page carries the text (the callers that need text use
        # a single page); the rest stay blank/scanned-like.
        if text and index == 0:
            pdf.set_font("helvetica", size=FONT_SIZE)
            pdf.text(TEXT_X, TEXT_Y, text)
    return bytes(pdf.output())


def pdf_bytes(*, pages: int = 1, text: str = "",
              width: float = 200.0, height: float = 200.0) -> bytes:
    """A small PDF as bytes (``text`` lands on page 1 only)."""
    return _build(pages, text, width, height)


def write_pdf(path: PathLike, *, pages: int = 1, text: str = "",
              width: float = 200.0, height: float = 200.0) -> Path:
    """Write a small PDF to ``path`` and return it."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_build(pages, text, width, height))
    return out


def page_count(path: PathLike) -> int:
    """Page count of a PDF on disk (via the app's own read seam)."""
    from backend import pdf_processing

    return pdf_processing.page_count(path)
