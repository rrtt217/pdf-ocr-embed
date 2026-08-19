"""Feature #2 — OCR-input image preprocessing (dimension-invariant checks)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from backend import config
from backend.pdf_processing import (preprocess_image, preprocess_options,
                                    render_page_to_file)

ON = {
    "enabled": True, "grayscale": True, "denoise": True,
    "contrast": True, "binarize": False,
}


def _synth_image(size=(320, 200), mode="RGB"):
    """A noisy greyscale-ish image so filters actually change something."""
    img = Image.new(mode, size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            v = (x * 7 + y * 13) % 256
            if mode == "L":
                px[x, y] = v
            else:
                px[x, y] = (v, v, v)
    return img


def _assert_same_size(a, b):
    assert a.size == b.size, f"size changed: {a.size} != {b.size}"


def test_disabled_returns_identical_image():
    img = _synth_image()
    out = preprocess_image(img, {"enabled": False})
    assert out is img  # no-op path returns the same object


def test_every_combination_preserves_dimensions():
    flags = [{"enabled": True}, {"enabled": True, "grayscale": True},
             {"enabled": True, "denoise": True}, {"enabled": True, "contrast": True},
             {"enabled": True, "binarize": True}, ON]
    for opts in flags:
        out = preprocess_image(_synth_image(), opts)
        _assert_same_size(out, _synth_image())


def test_grayscale_mode():
    out = preprocess_image(_synth_image(), {"enabled": True, "grayscale": True})
    assert out.mode == "L"


def _pixels(img):
    # Pillow>=11 warns on getdata(); compare raw bytes instead (stable API).
    return img.tobytes()


def test_denoise_changes_pixels():
    img = _synth_image()
    out = preprocess_image(img, {"enabled": True, "grayscale": True, "denoise": True})
    assert _pixels(out) != _pixels(img.convert("L"))


def test_contrast_changes_pixels():
    img = _synth_image()
    out = preprocess_image(img, {"enabled": True, "grayscale": True, "contrast": True})
    assert _pixels(out) != _pixels(img.convert("L"))


def test_binarize_is_black_and_white_only():
    out = preprocess_image(_synth_image(), {"enabled": True, "binarize": True})
    vals = set(out.tobytes())
    assert vals <= {0, 255}


def test_render_page_to_file_preprocessed_dimensions_preserved():
    """Integration: the render-to-file path must keep PNG width/height unchanged."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 100), 0)
    for y in range(100):
        for x in range(200):
            v = (x * 3 + y * 5) % 256
            pix.set_pixel(x, y, (v, v, v))
    page.insert_image(fitz.Rect(10, 10, 210, 110), pixmap=pix)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()
        with fitz.open(str(src)) as d:
            pg = d[0]
            # Render with preprocessing explicitly forced on.
            import backend.pdf_processing as pp
            old_opts = pp._preprocess_opts_from_config
            pp._preprocess_opts_from_config = lambda: dict(ON)
            try:
                path, w, h = render_page_to_file(pg, Path(td) / "out", 0)
            finally:
                pp._preprocess_opts_from_config = old_opts
            with Image.open(path) as img:
                assert (img.width, img.height) == (w, h)
                assert img.size == (w, h)
            # Repeat with preprocessing disabled (default) — same size.
            path2, w2, h2 = render_page_to_file(pg, Path(td) / "out2", 0)
            with Image.open(path2) as img:
                assert (img.width, img.height) == (w2, h2)


def test_config_effective_settings_exposes_preprocess(monkeypatch):
    monkeypatch.setattr(config, "_saved", {})
    monkeypatch.setattr(config, "_load_file_config",
                        lambda: {"preprocess_enabled": "true"})
    monkeypatch.setattr(config, "_load_env", lambda: {})
    s = config.get_effective_settings()
    assert s["preprocess_enabled"] is True
    assert s["preprocess_grayscale"] is True   # default
    assert s["preprocess_binarize"] is False   # default


def test_config_save_persists_toggles_and_keeps_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "ocr_config.toml")
    monkeypatch.setattr(config, "_saved", {})
    # Save provider fields first (as the WebUI does).
    config.save({"provider": "ustc", "base_url": "https://api.example.com/v1",
                 "model": "m", "api_key": "sk-secret",
                 "preprocess_enabled": True, "preprocess_binarize": True})
    # A toggles-only save must NOT clear api_key / provider fields.
    out = config.save({"preprocess_enabled": False})
    assert out["has_api_key"] is True
    cfg = config._load_file_config()
    assert cfg["preprocess_enabled"] == "false"
    assert cfg["api_key"] == "sk-secret"
    assert cfg["base_url"] == "https://api.example.com/v1"


def test_config_as_bool_coercion():
    assert config.as_bool(True) is True
    assert config.as_bool(False) is False
    assert config.as_bool(1) is True
    assert config.as_bool(0) is False
    for s in ("true", "1", "yes", "on"):
        assert config.as_bool(s) is True
    for s in ("false", "0", "no", "off", "", "banana", None):
        assert config.as_bool(s) is False


def test_preprocess_options_reads_config(monkeypatch):
    monkeypatch.setattr(config, "_saved", {})
    monkeypatch.setattr(config, "_load_file_config",
                        lambda: {"preprocess_enabled": "true"})
    monkeypatch.setattr(config, "_load_env", lambda: {})
    opts = preprocess_options()
    assert opts["enabled"] is True
    assert opts["grayscale"] is True and opts["denoise"] is True
