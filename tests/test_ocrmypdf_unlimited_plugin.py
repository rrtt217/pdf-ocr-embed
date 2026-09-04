"""Standalone ``ocrmypdf_unlimited`` plugin — the "real plugin" contract.

Pins the OCRmyPDF plugin behavior from
https://ocrmypdf.readthedocs.io/en/latest/plugins.html:

* the package is self-contained (never imports ``backend.*``),
* ``add_options`` registers the engine's own ``--unlimited-*`` CLI/API args,
* ``check_options`` validates them before a run,
* ``get_ocr_engine`` routes by ``options.ocr_engine``,
* it loads via its ``ocrmypdf`` setuptools entry point OR
  ``plugins=['ocrmypdf_unlimited']``,
* it drives a real ``ocrmypdf.api._pdf_to_hocr`` run for a host that passes
  it (through the app's own ``ocr_service.plugin_path()`` seam),
* cancellation is the plugin's EXCLUSIVE graceful-stop channel (per-page
  ``cancel`` flag polling; ocrmypdf itself cannot do this),
* the plugin's work-folder paths match the host ``backend.page_store``
  convention (the filesystem-only contract between them).
"""
from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest

from backend import ocr_service, page_store

PKG_DIR = Path(__file__).resolve().parents[1] / "ocrmypdf_unlimited"

RAW = "<|det|>text [0,0,100,100]<|/det|> Standalone plugin works page"


# --- independence ------------------------------------------------------------

def test_package_has_no_backend_imports():
    """The plugin must run without pdf-ocr-embed (and vice versa).

    No real ``import backend...`` / ``from backend...`` statements anywhere in
    the package.  (Prose that merely *mentions* the host is fine.)
    """
    import_re = re.compile(
        r"^\s*(?:import backend|from backend\b)", re.MULTILINE)
    offenders = []
    for py in sorted(PKG_DIR.glob("*.py")):
        if import_re.search(py.read_text(encoding="utf-8")):
            offenders.append(py.name)
    assert not offenders, (
        f"ocrmypdf_unlimited must not import a host app; found in: {offenders}")


def test_entry_point_declared():
    """pyproject.toml declares the packaged-plugin entry point (namespace
    'ocrmypdf', name 'unlimited') so ocrmypdf auto-loads it when installed."""
    import tomllib
    data = tomllib.loads((PKG_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    eps = data["project"]["entry-points"]["ocrmypdf"]
    assert eps["unlimited"] == "ocrmypdf_unlimited"


# --- configuration resolution ------------------------------------------------

def _fake_options(**kwargs):
    opts = types.SimpleNamespace(
        ocr_engine="unlimited",
        output_folder="/tmp/x/hocr",
        jobs=1,
    )
    for key, value in kwargs.items():
        setattr(opts, key, value)
    return opts


def test_effective_priority_options_over_env_over_snapshot(monkeypatch):
    from ocrmypdf_unlimited import settings as s
    s.configure({"api_key": "snapshot-key", "model": "snap-model",
                 "base_url": "https://snap/v1"})
    monkeypatch.setenv("OCR_UNLIMITED_MODEL", "env-model")
    try:
        cfg = s.effective(_fake_options(unlimited_api_key="opt-key"))
        # options beat env beat snapshot; snapshot's base_url survives
        assert cfg["api_key"] == "opt-key"
        assert cfg["model"] == "env-model"
        assert cfg["base_url"] == "https://snap/v1"
    finally:
        s.configure({})
        monkeypatch.delenv("OCR_UNLIMITED_MODEL", raising=False)


def test_effective_ignores_options_with_default_none(monkeypatch):
    from ocrmypdf_unlimited import settings as s
    s.configure({"model": "from-snapshot"})
    try:
        # No explicit unlimited_* on a namespace -> snapshot wins; a real
        # OcrOptions-like object with defaults None behaves the same.
        cfg = s.effective(_fake_options())
        assert cfg["model"] == "from-snapshot"
    finally:
        s.configure({})


def test_from_options_only_explicit_values():
    from ocrmypdf_unlimited.settings import from_options
    opts = _fake_options(unlimited_max_tokens=1024)
    cfg = from_options(opts)
    assert cfg == {"max_tokens": 1024}
    assert from_options(None) == {}
    # an absent attr (duck-typed namespace) contributes nothing
    assert "api_key" not in cfg


# --- the plugin hooks (add_options / check_options / get_ocr_engine) ---------

def test_add_options_registers_unlimited_args():
    import argparse

    from ocrmypdf_unlimited.options import add_options
    parser = argparse.ArgumentParser()
    add_options(parser)
    ns = parser.parse_args([])
    for dest in ("unlimited_api_key", "unlimited_base_url", "unlimited_model",
                 "unlimited_max_tokens", "unlimited_batch_size",
                 "unlimited_batch_timeout_ms", "unlimited_batch_per_page_tokens",
                 "unlimited_max_retries", "unlimited_retry_base_delay",
                 "unlimited_retry_max_delay", "unlimited_rate_limit_rps"):
        assert getattr(ns, dest) is None, dest


@pytest.mark.parametrize("attrs,message", [
    ({"unlimited_max_tokens": 33000}, "max-tokens must be"),
    ({"unlimited_max_tokens": 0}, "max-tokens must be"),
    ({"unlimited_batch_size": -1}, "batch-size must be"),
    ({"unlimited_retry_base_delay": -2.0}, "retry-base-delay must be"),
    ({"unlimited_rate_limit_rps": "abc"}, "rate-limit-rps must be"),
])
def test_check_options_rejects_bad_values(attrs, message):
    from ocrmypdf.exceptions import ExitCodeException

    from ocrmypdf_unlimited.options import check_options
    with pytest.raises(ExitCodeException, match=message):
        check_options(_fake_options(**attrs))


def test_check_options_accepts_good_values():
    from ocrmypdf_unlimited.options import check_options
    check_options(_fake_options(
        unlimited_max_tokens=2048, unlimited_batch_size=3,
        unlimited_batch_timeout_ms=1000, unlimited_batch_per_page_tokens=1024,
        unlimited_max_retries=1, unlimited_retry_base_delay=0.2,
        unlimited_retry_max_delay=5.0, unlimited_rate_limit_rps=2.0))
    check_options(_fake_options())  # nothing set -> no validation


def test_get_ocr_engine_routing():
    from ocrmypdf_unlimited.engine import get_ocr_engine
    assert get_ocr_engine(_fake_options(ocr_engine="unlimited")) is not None
    assert get_ocr_engine(_fake_options(ocr_engine="tesseract")) is None
    assert get_ocr_engine(_fake_options(ocr_engine="auto")) is None
    assert get_ocr_engine(None) is not None  # backward-compat (no options)


# --- discovered by ocrmypdf (entry point OR plugins=) -------------------------

def test_ocrmypdf_discovers_plugin_hooks():
    """A plugin manager finds the engine + hooks through ocrmypdf itself.

    Uses the app's own seam (ocr_service.plugin_path) so it stays correct in
    both layouts: explicit `plugins=['ocrmypdf_unlimited']` when the plugin is
    not installed, or entry-point auto-load when it is.
    """
    from ocrmypdf._plugin_manager import OcrmypdfPluginManager
    from ocrmypdf_unlimited.engine import UnlimitedOcrEngine

    plugins = [ocr_service.plugin_path()] if ocr_service.plugin_path() else []
    pm = OcrmypdfPluginManager("ocrmypdf", plugins=plugins, builtins=True)
    opts = types.SimpleNamespace(ocr_engine="unlimited", languages=[],
                                 output_folder="/tmp/x/hocr")
    engine = pm.get_ocr_engine(options=opts)
    assert engine is not None and "Unlimited" in str(engine)
    # unchanged routing: a non-unlimited engine falls through to the built-ins
    builtin = pm.get_ocr_engine(options=types.SimpleNamespace(
        ocr_engine="tesseract", languages=[]))
    assert builtin is not None and not isinstance(builtin, UnlimitedOcrEngine)


# --- exclusive cancellation ---------------------------------------------------

def test_cancel_flag_check_is_plugin_exclusive(tmp_path):
    """The plugin polls <job_dir>/cancel per page; ocrmypdf has no such hook.

    Verified at the engine level so no network is needed: a cancel flag in the
    job dir fails the page BEFORE the client is ever constructed/used.
    """
    import PIL.Image

    from ocrmypdf_unlimited import files
    from ocrmypdf_unlimited.engine import UnlimitedOcrEngine
    from ocrmypdf_unlimited.settings import configure

    job_dir = tmp_path / "job"
    (job_dir / "hocr").mkdir(parents=True)
    files.cancel_path(job_dir).touch()  # host writes <job_dir>/cancel; plugin reads it
    img = tmp_path / "page.png"
    PIL.Image.new("RGB", (100, 100), "white").save(img)
    opts = types.SimpleNamespace(output_folder=str(job_dir / "hocr"), jobs=1)
    configure({})
    with pytest.raises(RuntimeError, match="cancelled"):
        UnlimitedOcrEngine.generate_hocr(
            img, job_dir / "hocr" / "000001_ocr_hocr.hocr",
            tmp_path / "t.txt", opts)


def test_files_paths_match_host_page_store():
    """The filesystem-only contract between plugin and host: identical paths.

    Neither side imports the other; this test pins that the documented
    convention ('<n>_ocr_hocr.hocr', '<n>_ocr_hocr.blocks.json', 'cancel')
    never drifts apart.
    """
    from ocrmypdf_unlimited import files
    hdir = Path("/j/hocr")
    assert files.hocr_path(hdir, 7) == page_store.hocr_path(hdir, 7)
    assert files.sidecar_path(hdir, 7) == page_store.sidecar_path(hdir, 7)
    assert files.cancel_path(Path("/j")) == page_store.cancel_path(Path("/j"))
    assert files.page_index_from_name("000003_ocr_hocr.hocr") == 2
    assert files.page_index_from_name("garbage") == 0


def test_coordinate_mapping_matches_host_copy():
    """The 1000-canvas->pixel mapping in backend.errors (host copy) equals the
    plugin's canonical implementation (ocrmypdf_unlimited.geometry), so the
    'integers in raw pixel space' invariant can never drift between the two."""
    from backend.errors import normalize_bbox as host_norm
    from ocrmypdf_unlimited.geometry import normalize_bbox as plugin_norm
    for bbox in ([0, 0, 1000, 1000], [200, 300, 100, 60], [-3, 0, 1005, 900],
                 [12.4, 55.9, 880.1, 940.2]):
        for w, h in ((100, 200), (1000, 1000), (612, 792)):
            assert plugin_norm(bbox, w, h) == host_norm(bbox, w, h)


# --- end-to-end through ocrmypdf ---------------------------------------------

def _stub_client(monkeypatch, raw=RAW):
    class _FakeClient:
        max_tokens = 16384
        batch_per_page_tokens = 2048
        READ_TIMEOUT_MIN = 900.0
        READ_TIMEOUT_PER_TOKEN = 0.08

        def __init__(self, config=None):
            self.api_key = config.get("api_key") or "k"

        def recognize(self, path):
            return raw

        def recognize_multi(self, paths):
            return [raw] * len(paths)

    import ocrmypdf_unlimited.engine as engine_mod
    monkeypatch.setattr(engine_mod, "UnlimitedOcrClient", _FakeClient)
    return _FakeClient


def _tiny_pdf(path: Path):
    """A blank (scanned-like) page PDF: no text layer, no prior OCR."""
    import fitz
    doc = fitz.open()
    doc.new_page(width=200, height=100)  # pure image page
    doc.save(path)
    doc.close()


@pytest.mark.skipif(not ocr_service.plugin_available(),
                    reason="ocrmypdf_unlimited not importable")
def test_pdf_to_hocr_end_to_end(monkeypatch, tmp_path):
    """A REAL ocrmypdf hOCR run driven by the plugin through the app seam.

    Rasterizes a real PDF, runs the plugin engine per page (client stubbed —
    no network), and leaves the hOCR + block sidecar the app edits.
    """
    import ocrmypdf.api

    _stub_client(monkeypatch)
    pdf = tmp_path / "doc.pdf"
    _tiny_pdf(pdf)
    hocr_dir = tmp_path / "hocr"
    hocr_dir.mkdir()

    ocrmypdf.api._pdf_to_hocr(
        pdf, hocr_dir,
        plugins=[ocr_service.plugin_path()] if ocr_service.plugin_path() else [],
        ocr_engine="unlimited",
        use_threads=True, jobs=1,
    )

    hocr_files = sorted(hocr_dir.glob("*_ocr_hocr.hocr"))
    sidecars = sorted(hocr_dir.glob("*.blocks.json"))
    assert hocr_files and sidecars
    text = hocr_files[0].read_text(encoding="utf-8")
    assert "scan_res" in text          # renderer's px->pt transform depends on it
    assert "ocrx_word" in text         # parser drops lines without words
    data = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert data["page"]["blocks"][0]["text"] == "Standalone plugin works page"
