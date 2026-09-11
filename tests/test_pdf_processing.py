"""Real PDF rendering through the pypdfium2 seam (``backend.pdf_processing``).

These tests pin the *rendering* half of the seam — page count/size, full-page
previews, clipped crops (including the top-left origin ↔ pypdfium2 "amount cut
off" conversion) and text extraction.  The pure pixel→point mapping
(``clip_rect``) is covered separately in ``test_image_export.py``; here the
PDFs are real so the native renderer actually runs.

The crop-orientation test matters most: pypdfium2's ``crop`` argument cuts
*inward from each edge* with a bottom-left origin for the vertical pair, while
the project's bboxes are top-left origin — a flip would silently crop the wrong
half of the page.
"""
from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from PIL import Image

from backend import image_export, pdf_processing
from pdf_fixtures import pdf_bytes as make_pdf, write_pdf

PREVIEW_DPI = pdf_processing.PREVIEW_DPI


def _quadrant_pdf(path: Path) -> Path:
    """A 200×200pt page whose TOP-LEFT quadrant is filled solid black."""
    from fpdf import FPDF

    pdf = FPDF(unit="pt", format=(200.0, 200.0))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    pdf.add_page()
    pdf.set_fill_color(0, 0, 0)
    pdf.rect(0, 0, 100, 100, style="F")  # fpdf2 shares the top-left origin
    path.write_bytes(bytes(pdf.output()))
    return path


def _is_all_black(image: Image.Image) -> bool:
    return image.convert("L").getextrema() == (0, 0)


def _is_all_white(image: Image.Image) -> bool:
    return image.convert("L").getextrema() == (255, 255)


# --- geometry -----------------------------------------------------------------

def test_page_count_and_size(tmp_path):
    src = write_pdf(tmp_path / "a.pdf", pages=3, width=400, height=300)
    assert pdf_processing.page_count(src) == 3
    assert pdf_processing.page_size_pt(src, 0) == (400.0, 300.0)
    with pdf_processing.open_pdf(src) as doc:
        assert doc.page_count == 3
        assert doc.page_size_pt(2) == (400.0, 300.0)


def test_page_size_reflects_rotation(tmp_path):
    """A /Rotate 90 page reports (and renders) swapped dimensions.

    This is the behavior the old PyMuPDF ``page.rect`` gave; the crop math in
    ``render_clip_png`` relies on it.
    """
    src = write_pdf(tmp_path / "r.pdf", width=200, height=100)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Rotate = 90
        pdf.save(src)

    assert pdf_processing.page_size_pt(src, 0) == (100.0, 200.0)
    width, height = pdf_processing.render_page_png(
        src, 0, tmp_path / "r.png", dpi=72.0)
    assert (width, height) == (100, 200)


# --- full-page preview --------------------------------------------------------

def test_render_page_png_uses_preview_dpi(tmp_path):
    src = write_pdf(tmp_path / "a.pdf", width=200, height=100)
    out = tmp_path / "preview" / "p.png"          # parent dir is created

    width, height = pdf_processing.render_page_png(src, 0, out)

    expected_w = round(200 * PREVIEW_DPI / 72.0)
    expected_h = round(100 * PREVIEW_DPI / 72.0)
    assert abs(width - expected_w) <= 1
    assert abs(height - expected_h) <= 1
    assert Image.open(out).size == (width, height)


def test_render_page_png_out_of_range_raises(tmp_path):
    src = write_pdf(tmp_path / "a.pdf")
    with pytest.raises(IndexError):
        pdf_processing.render_page_png(src, 5, tmp_path / "p.png")
    with pytest.raises(IndexError):
        with pdf_processing.open_pdf(src) as doc:
            doc.page_size_pt(5)


# --- clipped crops (the coordinate conversion) --------------------------------

def test_render_clip_png_keeps_top_left_origin(tmp_path):
    """Crop the black top-left quadrant and the white bottom-right one.

    A y-axis flip in the ``(x0, y0, x1, y1)`` → pypdfium2 crop conversion
    would swap these two results.
    """
    src = _quadrant_pdf(tmp_path / "q.pdf")

    top_left = tmp_path / "tl.png"
    bottom_right = tmp_path / "br.png"
    with pdf_processing.open_pdf(src) as doc:
        doc.render_clip_png(0, (0.0, 0.0, 100.0, 100.0), 1.0, top_left)
        doc.render_clip_png(0, (100.0, 100.0, 200.0, 200.0), 1.0, bottom_right)

    tl, br = Image.open(top_left), Image.open(bottom_right)
    assert tl.size == (100, 100)
    assert br.size == (100, 100)
    assert _is_all_black(tl)
    assert _is_all_white(br)


def test_render_clip_png_scales_to_ocr_pixels(tmp_path):
    """scale is output pixels per point: a 50×20pt region at scale 2 is 100×40."""
    src = _quadrant_pdf(tmp_path / "q.pdf")
    out = tmp_path / "c.png"
    with pdf_processing.open_pdf(src) as doc:
        doc.render_clip_png(0, (0.0, 0.0, 50.0, 20.0), 2.0, out)
    assert Image.open(out).size == (100, 40)


# --- text layer ---------------------------------------------------------------

def test_extract_text_roundtrip(tmp_path):
    src = write_pdf(tmp_path / "t.pdf", text="hello searchable layer",
                    width=400, height=300)
    assert "hello searchable layer" in pdf_processing.extract_text(src, 0)


def test_extract_text_blank_page_is_empty(tmp_path):
    src = write_pdf(tmp_path / "blank.pdf", width=200, height=100)
    assert pdf_processing.extract_text(src, 0).strip() == ""


# --- image_export end to end (real crop from a real PDF) ----------------------

def test_extract_images_crops_only_image_blocks(tmp_path):
    src = _quadrant_pdf(tmp_path / "src.pdf")
    job = {
        "job_id": "img-1",
        "pdf_path": str(src),
        "hocr_dir": str(tmp_path / "work" / "img-1" / "hocr"),
    }
    pages = [{
        "page_index": 0, "width": 200, "height": 200,
        "blocks": [
            {"kind": "image", "bbox": [0, 0, 100, 100]},
            {"kind": "text", "bbox": [100, 100, 200, 200], "text": "not an image"},
        ],
    }]

    image_map = image_export.extract_images(job, pages)

    assert set(image_map) == {(0, 0)}
    out = image_map[(0, 0)]
    assert out.exists()
    assert out.parent.name == image_export._IMAGES_DIRNAME
    crop = Image.open(out)
    assert crop.size == (100, 100)
    assert _is_all_black(crop)          # the black quadrant, at 1:1 OCR pixels


def test_extract_images_requires_a_readable_source(tmp_path):
    job = {"job_id": "img-2", "pdf_path": str(tmp_path / "missing.pdf")}
    with pytest.raises(ValueError):
        image_export.extract_images(job, [])
