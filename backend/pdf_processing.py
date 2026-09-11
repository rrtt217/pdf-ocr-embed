"""PDF reading / rendering seam for the whole backend (pypdfium2).

This module is the **single place** the backend talks to a PDF library.  Every
other module (previews, validation, image export, job creation) goes through
the small read-only API here, so the dependency set stays minimal:

* **pypdfium2** — rendering, page geometry, page count and text extraction.
  It is already a mandatory dependency of OCRmyPDF (17.x rasterizes with it by
  default), so bundling it adds nothing new.
* **pikepdf** — the one *editing* operation the backend needs (dropping pages
  for a partial embed, see ``backend.ocr_service._drop_pages_except``).

There is deliberately no PyMuPDF: it is AGPL-3.0/commercial and its rendering
job is fully covered by pypdfium2.

Coordinate note (project invariant): page geometry returned by
:meth:`PdfReader.page_size_pt` is in PDF points and, like PyMuPDF's
``page.rect``, already reflects the page's ``/Rotate`` — pypdfium2's
``get_size()`` and its renderer both apply rotation.  Block bboxes remain
integer raw pixels in the OCR raster's top-left space; callers map between the
two (see ``backend.image_export.clip_rect``).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple, Union

import pypdfium2 as pdfium

log = logging.getLogger(__name__)

# Preview render zoom: the editor scales block overlays by the page's OCR
# pixel width/height, so any consistent zoom aligns correctly.
PREVIEW_DPI = 150.0

# A PDF path, or raw bytes (uploads are validated before they hit disk).
Source = Union[str, Path, bytes, bytearray]


def _as_input(source: Source):
    """pypdfium2 accepts a path str or a ``bytes`` document; not a bytearray."""
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    return str(source)


class PdfReader:
    """Read-only PDF access via pypdfium2.

    Use as a context manager; every method takes a **0-based** page index and
    raises ``IndexError`` for an out-of-range one (callers that need a soft
    failure catch it).
    """

    def __init__(self, source: Source) -> None:
        self._pdf = pdfium.PdfDocument(_as_input(source))

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "PdfReader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._pdf.close()
        except Exception:  # noqa: BLE001 - closing twice is not an error
            log.debug("closing PDF failed", exc_info=True)

    # -- geometry ----------------------------------------------------------
    @property
    def page_count(self) -> int:
        """Number of pages."""
        return len(self._pdf)

    def _page(self, page_index: int) -> pdfium.PdfPage:
        if not 0 <= page_index < len(self._pdf):
            raise IndexError(f"page_index out of range: {page_index} "
                             f"(document has {len(self._pdf)} page(s))")
        return self._pdf[page_index]

    def page_size_pt(self, page_index: int) -> Tuple[float, float]:
        """Page size in PDF points, reflecting the page's rotation."""
        width, height = self._page(page_index).get_size()
        return float(width), float(height)

    # -- rendering ---------------------------------------------------------
    def render_page_png(self, page_index: int, out_path: Path,
                        dpi: float = PREVIEW_DPI) -> Tuple[int, int]:
        """Render one whole page to a PNG; returns its pixel size."""
        page = self._page(page_index)
        bitmap = page.render(scale=float(dpi) / 72.0)
        return _write_png(bitmap, out_path)

    def render_clip_png(self, page_index: int,
                        clip_pt: Tuple[float, float, float, float],
                        scale: float, out_path: Path) -> Tuple[int, int]:
        """Render a sub-rectangle of a page to a PNG.

        ``clip_pt`` is ``(x0, y0, x1, y1)`` in PDF points with a **top-left**
        origin (the shape ``backend.image_export.clip_rect`` returns), while
        pypdfium2's ``crop`` argument is the amount to *cut off* from
        ``(left, bottom, right, top)`` — hence the conversion below.
        ``scale`` is output pixels per PDF point.
        """
        page = self._page(page_index)
        width_pt, height_pt = page.get_size()
        x0, y0, x1, y1 = (float(v) for v in clip_pt)
        crop = (
            max(0.0, x0),                 # cut from the left
            max(0.0, height_pt - y1),     # cut from the bottom (top-left y1)
            max(0.0, width_pt - x1),      # cut from the right
            max(0.0, y0),                 # cut from the top
        )
        bitmap = page.render(scale=float(scale), crop=crop)
        return _write_png(bitmap, out_path)

    # -- text --------------------------------------------------------------
    def extract_text(self, page_index: int) -> str:
        """The page's text layer as a string ("" when it has none)."""
        textpage = self._page(page_index).get_textpage()
        try:
            return textpage.get_text_range()
        finally:
            try:
                textpage.close()
            except Exception:  # noqa: BLE001 - best effort
                log.debug("closing text page failed", exc_info=True)


def _write_png(bitmap: pdfium.PdfBitmap, out_path: Path) -> Tuple[int, int]:
    """Save a rendered bitmap as a PNG; returns its pixel size."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image = bitmap.to_pil()
    try:
        image.save(out_path, format="PNG")
        return int(image.width), int(image.height)
    finally:
        image.close()


# --- module-level convenience wrappers (open/close per call) -----------------

def open_pdf(source: Source) -> PdfReader:
    """Open a PDF for reading."""
    return PdfReader(source)


def page_count(source: Source) -> int:
    """Number of pages in a PDF."""
    with open_pdf(source) as doc:
        return doc.page_count


def page_size_pt(source: Source, page_index: int) -> Tuple[float, float]:
    """One page's size in PDF points (rotation-aware)."""
    with open_pdf(source) as doc:
        return doc.page_size_pt(page_index)


def render_page_png(source: Source, page_index: int, out_path: Path,
                    dpi: float = PREVIEW_DPI) -> Tuple[int, int]:
    """Render one page of a PDF to a PNG preview; returns its pixel size.

    Raises for an out-of-range page or an unreadable PDF; the caller surfaces
    the failure.
    """
    with open_pdf(source) as doc:
        return doc.render_page_png(page_index, out_path, dpi)


def extract_text(source: Source, page_index: int) -> str:
    """One page's text layer."""
    with open_pdf(source) as doc:
        return doc.extract_text(page_index)
