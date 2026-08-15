"""Output optimization: image recompression / downscale + linearized save."""
from __future__ import annotations

import tempfile
from pathlib import Path

import fitz  # PyMuPDF

from backend.models import OcrBlock, OcrPage
from backend.pdf_processing import (_recompress_image, embed_invisible_text,
                                    optimize_images)


def _gradient_pixmap(size=256, alpha=False):
    # Noisy gradient: compresses decently at q=95, much better at q=60.
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, size, size), int(alpha))
    for y in range(size):
        for x in range(size):
            v = (x * 37 + y * 91) % 256
            if alpha:
                pix.set_pixel(x, y, (v, (v * 3) % 256, (255 - v) % 256,
                                    (x + y) % 256))
            else:
                pix.set_pixel(x, y, (v, (v * 3) % 256, (255 - v) % 256))
    return pix


def _make_image_pdf(tmp_path, alpha=False):
    """One-page PDF carrying a 256x256 (optionally alpha) image."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    pix = _gradient_pixmap(alpha=alpha)
    if alpha:
        page.insert_image(fitz.Rect(10, 10, 266, 266), pixmap=pix)
    else:
        jpg = pix.tobytes("jpeg", jpg_quality=95)
        page.insert_image(fitz.Rect(10, 10, 266, 266), stream=jpg)
    out = tmp_path / "src.pdf"
    doc.save(str(out), garbage=4, deflate=True)
    doc.close()
    return out


def _first_image_bytes(doc):
    xref = doc.get_page_images(0, full=True)[0][0]
    return doc.extract_image(xref)["image"]


def test_jpeg_recompress_shrinks_and_replaces():
    with tempfile.TemporaryDirectory() as td:
        src = _make_image_pdf(Path(td))
        doc = fitz.open(str(src))
        before = len(_first_image_bytes(doc))
        stats = optimize_images(doc, "jpeg", 60, None)
        assert stats["attempted"] == 1 and stats["replaced"] == 1
        assert stats["after_bytes"] < before
        out = Path(td) / "out.pdf"
        doc.save(str(out), garbage=4, deflate=True)
        doc.close()
        with fitz.open(str(out)) as check:
            assert check.get_page_images(0, full=True)[0][0] > 0


def test_gray_jpeg_mode_produces_grayscale():
    with tempfile.TemporaryDirectory() as td:
        src = _make_image_pdf(Path(td))
        doc = fitz.open(str(src))
        stats = optimize_images(doc, "gray-jpeg", 55, None)
        assert stats["replaced"] == 1
        pix = fitz.Pixmap(_first_image_bytes(doc))
        assert pix.n == 1                     # single-component = gray
        doc.close()


def test_downscale_quarters_raster():
    with tempfile.TemporaryDirectory() as td:
        src = _make_image_pdf(Path(td))
        doc = fitz.open(str(src))
        stats = optimize_images(doc, "jpeg", 70, 4)
        assert stats["replaced"] == 1
        pix = fitz.Pixmap(_first_image_bytes(doc))
        assert (pix.width, pix.height) == (64, 64)   # 256 / 4
        doc.close()


def test_alpha_image_is_skipped_not_flattened():
    with tempfile.TemporaryDirectory() as td:
        src = _make_image_pdf(Path(td), alpha=True)
        doc = fitz.open(str(src))
        _xref, smask = doc.get_page_images(0, full=True)[0][0:2]
        assert smask > 0
        stats = optimize_images(doc, "jpeg", 60, None)
        assert stats["skipped"] >= 1 and stats["replaced"] == 0
        doc.close()


def test_none_mode_is_a_noop():
    with tempfile.TemporaryDirectory() as td:
        src = _make_image_pdf(Path(td))
        doc = fitz.open(str(src))
        assert optimize_images(doc, "none", 60, None) == {
            "attempted": 0, "replaced": 0, "skipped": 0,
            "before_bytes": 0, "after_bytes": 0, "saved_bytes": 0}
        doc.close()


def test_recompress_pure_function_steps():
    data = _gradient_pixmap().tobytes("jpeg", jpg_quality=95)
    small = _recompress_image(data, "jpeg", 50, 0)
    assert small and len(small) < len(data)
    quarter = _recompress_image(data, "jpeg", 50, 4)   # /4 per dimension
    qp = fitz.Pixmap(quarter)
    assert (qp.width, qp.height) == (64, 64)
    half = _recompress_image(data, "jpeg", 50, 2)         # /2 per dimension
    hp = fitz.Pixmap(half)
    assert (hp.width, hp.height) == (128, 128)
    assert _recompress_image(b"not an image", "jpeg", 50, 0) is None


def test_embed_with_optimize_and_linearize_returns_stats():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_image_pdf(td)
        page = OcrPage(page_index=0, width=400, height=400, blocks=[
            OcrBlock(kind="text", bbox=[10, 10, 200, 30], text="hello searchable")])
        out, thumb, stats = embed_invisible_text(
            str(src), [page], td, None,
            img_mode="jpeg", img_quality=60, img_downscale=None,
            linearize=True)
        assert stats["replaced"] >= 1 and stats["saved_bytes"] > 0
        assert Path(out).exists() and Path(thumb).exists()
        with fitz.open(str(out)) as final:
            assert "hello searchable" in final[0].get_text()
        # Linearization may be unavailable in the bundled MuPDF; the save
        # must never fail over it — the flag records what actually happened.
        assert isinstance(stats.get("linearized"), bool)
