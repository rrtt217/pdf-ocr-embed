"""Output optimization: image recompression / downscale + linearized save."""
from __future__ import annotations

import tempfile
from pathlib import Path

import fitz  # PyMuPDF

from backend.models import OcrBlock, OcrPage
from backend.pdf_processing import (_recompress_image, _visual_to_user,
                                    embed_invisible_text, optimize_images,
                                    render_overlay)


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


def test_embed_writes_image_captions_into_text_layer():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_image_pdf(td)
        page = OcrPage(page_index=0, width=400, height=400, blocks=[
            OcrBlock(kind="text", bbox=[10, 10, 200, 30], text="hello searchable"),
            OcrBlock(kind="image", bbox=[10, 40, 200, 220], text="",
                     caption="Fig. 1 示例",
                     caption_bbox=[10, 230, 200, 250]),
        ])
        out, _thumb, _stats = embed_invisible_text(str(src), [page], td)
        with fitz.open(str(out)) as final:
            text = final[0].get_text()
        assert "hello searchable" in text
        assert "Fig. 1 示例" in text


# ---------------------------------------------------------------------------
# Page-rotation aware embedding: /Rotate 180 scanned books embedded text
# upside-down before _visual_to_user was introduced.
# ---------------------------------------------------------------------------

def _rotated_pdf(tmp_path, rotation=180):
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    page.set_rotation(rotation)
    out = tmp_path / f"rot{rotation}.pdf"
    doc.save(str(out), garbage=4, deflate=True)
    doc.close()
    return out


def test_visual_to_user_identity_for_rotation_0(tmp_path):
    src = _rotated_pdf(tmp_path, rotation=0)
    with fitz.open(str(src)) as doc:
        p = fitz.Point(10, 20)
        assert _visual_to_user(p, doc[0]) == p


def test_visual_to_user_flips_180(tmp_path):
    src = _rotated_pdf(tmp_path, rotation=180)
    with fitz.open(str(src)) as doc:
        page = doc[0]
        mapped = _visual_to_user(fitz.Point(10, 20), page)
        assert abs(mapped.x - (400 - 10)) < 1e-6
        assert abs(mapped.y - (400 - 20)) < 1e-6


def test_embed_respects_page_rotation_180(tmp_path):
    """On a rotation=180 page, a top-left OCR block must display back at the
    top-left: map the extracted word's user-space center through the page's
    rotation matrix and it must land inside the block's visual bbox (with
    font/leading slack).  Exact user-space coordinates are NOT asserted —
    the glyph pre-rotation (morph) legitimately shifts them."""
    src = _rotated_pdf(tmp_path, rotation=180)
    page = OcrPage(page_index=0, width=400, height=400, blocks=[
        OcrBlock(kind="text", bbox=[10, 10, 200, 30], text="rotated hello")])
    out, _thumb, _stats = embed_invisible_text(str(src), [page], tmp_path)
    with fitz.open(str(out)) as final:
        words = final[0].get_text("words")
        assert words, "embedded text missing on rotated page"
        x0, y0, x1, y1, *_ = words[0]
        ux, uy = (x0 + x1) / 2, (y0 + y1) / 2
        vis = fitz.Point(ux, uy) * final[0].rotation_matrix  # user -> visual
        # visual bbox of the block is [10,10,200,30]; text ink sits inside
        # with some centering/baseline slack.
        assert 10 - 30 <= vis.x <= 200 + 30
        assert 0 <= vis.y <= 60
        assert final[0].rotation == 180


def _overlay_png(tmp_path, rotation, text="Rotated Text 123"):
    """render_overlay output for one OCR block on a rotated (or not) page.

    Every page is 400x400 pt and the OcrPage is 800x800 px, so overlay Zoom 2
    produces an 800x800 render for every rotation — the ONLY variable is the
    page's /Rotate and the rotation-aware embedding.
    """
    src = _rotated_pdf(tmp_path, rotation)
    ocr = OcrPage(page_index=0, width=800, height=800, blocks=[
        OcrBlock(kind="text", bbox=[40, 60, 760, 110], text=text)])
    ov = render_overlay(str(src), [ocr], 0, out_dir=tmp_path, for_page=0)
    from PIL import Image as _Im
    return _Im.open(str(ov)).convert("L")


def _pixel_diff(a, b):
    from PIL import ImageChops
    if a.size != b.size:
        a = a.resize(b.size)
    diff = ImageChops.difference(a.convert("L"), b.convert("L"))
    h = diff.histogram()
    return sum(h[64:]) / (a.size[0] * a.size[1])


def test_overlay_pixels_match_rotation_0_across_rotations(tmp_path):
    """The gold standard for the rotation fix: a block embedded on a
    rotated page must render IDENTICALLY to the same block on a rotation=0
    page.  rotation=180 (the real-world scanned-book case) must be pixel-
    exact; 90/270 are exotic and differ only by glyph anti-aliasing at
    sub-pixel level (same position, ~1% of pixels)."""
    base = _overlay_png(tmp_path, 0)
    d180 = _pixel_diff(base, _overlay_png(tmp_path, 180))
    assert d180 < 0.001, f"rotation=180: {d180:.6f} pixel diff vs rotation=0"
    for rot in (90, 270):
        d = _pixel_diff(base, _overlay_png(tmp_path, rot))
        assert d < 0.02, f"rotation={rot}: {d:.4f} pixel diff vs rotation=0 "
