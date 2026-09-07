"""Image extraction for markdown export (backend.image_export).

Pins the pure geometry (raw-pixel bbox → PDF point clip), the deterministic
crop filenames, the two resolver delivery modes (relative ZIP links vs base64
data URIs) and the archive layout (``<stem>.md`` + ``images/``).  The crop
rendering itself needs PyMuPDF + a real PDF and is exercised by the route
smoke path, not here.
"""
from __future__ import annotations

import base64
import zipfile

import pytest

from backend.image_export import (
    clip_rect,
    data_uri,
    default_zip_name,
    image_filename,
    make_resolver,
    relative_url,
)


# --- clip_rect: the pixel → point mapping -----------------------------------------

def test_clip_rect_maps_bbox_to_points():
    # OCR raster 1224x1584 px over a 612x792 pt page ⇒ scale 0.5.
    rect = clip_rect([200, 200, 600, 500], 1224, 1584, 612.0, 792.0)
    assert rect == pytest.approx((100.0, 100.0, 300.0, 250.0))


def test_clip_rect_clamps_to_page():
    # A bbox slightly outside the raster never leaves the page.
    rect = clip_rect([-50, -50, 2000, 2000], 1224, 1584, 612.0, 792.0)
    x0, y0, x1, y1 = rect
    assert x0 == 0.0 and y0 == 0.0
    assert x1 <= 612.0 and y1 <= 792.0


def test_clip_rect_rejects_degenerate_input():
    assert clip_rect([10, 10, 10, 20], 100, 100, 50.0, 50.0) is None   # zero width
    assert clip_rect([10, 20, 10, 10], 100, 100, 50.0, 50.0) is None   # inverted
    assert clip_rect([10, 10, 20, 20], 0, 100, 50.0, 50.0) is None     # zero scale
    assert clip_rect(None, 100, 100, 50.0, 50.0) is None               # no bbox
    assert clip_rect([1, 2], 100, 100, 50.0, 50.0) is None             # too short


# --- naming -----------------------------------------------------------------------

def test_image_filename_is_deterministic():
    assert image_filename(0, 1) == "page-001-img-1.png"
    assert image_filename(11, 0) == "page-012-img-0.png"


def test_relative_url_uses_images_folder():
    assert relative_url("page-001-img-1.png") == "images/page-001-img-1.png"


def test_default_zip_name():
    assert default_zip_name("doc") == "doc_markdown.zip"
    assert default_zip_name("") == "export_markdown.zip"


# --- resolvers ----------------------------------------------------------------------

def test_make_resolver_zip_mode(tmp_path):
    crop = tmp_path / "page-001-img-0.png"
    crop.write_bytes(b"png-bytes")
    resolver = make_resolver("zip", {(0, 0): crop})
    assert resolver({}, 0, 0) == "images/page-001-img-0.png"
    assert resolver({}, 1, 1) is None  # no such crop


def test_make_resolver_base64_mode(tmp_path):
    crop = tmp_path / "page-001-img-0.png"
    crop.write_bytes(b"png-bytes")
    resolver = make_resolver("base64", {(0, 0): crop})
    uri = resolver({}, 0, 0)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == b"png-bytes"


def test_data_uri_roundtrip(tmp_path):
    crop = tmp_path / "x.png"
    crop.write_bytes(b"\x89PNG\r\n\x1a\n")
    uri = data_uri(crop)
    assert base64.b64decode(uri.split(",", 1)[1]) == b"\x89PNG\r\n\x1a\n"


# --- archive ------------------------------------------------------------------------

def test_build_markdown_zip_layout(tmp_path):
    from backend.image_export import build_markdown_zip

    crop = tmp_path / "page-001-img-1.png"
    crop.write_bytes(b"png-bytes")
    zip_path = tmp_path / "doc_markdown.zip"
    n = build_markdown_zip(str(zip_path), "# Doc\n\nhello", "doc.md",
                           {(0, 1): crop})
    assert n == 2
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["doc.md", "images/page-001-img-1.png"]
        assert zf.read("doc.md") == b"# Doc\n\nhello"
        assert zf.read("images/page-001-img-1.png") == b"png-bytes"


def test_build_markdown_zip_skips_missing_crops(tmp_path):
    from backend.image_export import build_markdown_zip

    missing = tmp_path / "gone.png"
    zip_path = tmp_path / "doc_markdown.zip"
    n = build_markdown_zip(str(zip_path), "# Doc", "doc.md", {(0, 0): missing})
    assert n == 1  # only the .md
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["doc.md"]
