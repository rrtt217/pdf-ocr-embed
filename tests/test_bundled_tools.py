"""Bundled-tool wiring: how a packaged app points OCRmyPDF at its own Tesseract.

The real thing is only exercised end to end by the packaging smoke test; here
the layout and the environment manipulation are pinned with a fake bundle.
"""
from __future__ import annotations

import os

import pytest

from backend import bundled_tools

_ENV_VARS = ("PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "TESSDATA_PREFIX")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Restore the real environment and the activation flag after each test."""
    saved = {name: os.environ.get(name) for name in _ENV_VARS}
    bundled_tools.reset()
    yield
    for name, value in saved.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    bundled_tools.reset()


@pytest.fixture
def fake_bundle(monkeypatch, tmp_path):
    """A staged tesseract tree, presented as the app's resource directory."""
    root = tmp_path / "resources"
    (root / "tesseract" / "bin").mkdir(parents=True)
    (root / "tesseract" / "lib").mkdir(parents=True)
    (root / "tesseract" / "tessdata").mkdir(parents=True)
    exe = root / "tesseract" / "bin" / bundled_tools._EXE_NAMES[0]
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (root / "tesseract" / "lib" / "libtesseract.so.5").write_bytes(b"\x7fELF")
    for lang in ("eng", "chi_sim"):
        (root / "tesseract" / "tessdata" / f"{lang}.traineddata").write_bytes(b"x")
    monkeypatch.setattr(bundled_tools.paths, "resource_dir", lambda: root)
    return root


# --- layout ------------------------------------------------------------------

def test_source_checkout_ships_no_tesseract(monkeypatch, tmp_path):
    monkeypatch.setattr(bundled_tools.paths, "resource_dir", lambda: tmp_path)
    assert bundled_tools.is_bundled() is False
    assert bundled_tools.tesseract_path() is None
    assert bundled_tools.bundled_languages() == []


def test_bundle_layout_is_detected(fake_bundle):
    assert bundled_tools.is_bundled() is True
    assert bundled_tools.tesseract_path().parent == fake_bundle / "tesseract" / "bin"
    assert bundled_tools.lib_dir() == fake_bundle / "tesseract" / "lib"
    assert bundled_tools.tessdata_dir() == fake_bundle / "tesseract" / "tessdata"


def test_bundled_languages_are_listed(fake_bundle):
    assert bundled_tools.bundled_languages() == ["chi_sim", "eng"]


# --- activation --------------------------------------------------------------

def test_activate_is_a_noop_without_a_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(bundled_tools.paths, "resource_dir", lambda: tmp_path)
    before = os.environ.get("PATH")
    assert bundled_tools.activate() is False
    assert os.environ.get("PATH") == before
    assert "TESSDATA_PREFIX" not in os.environ


def test_activate_wires_path_libraries_and_tessdata(fake_bundle, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("DYLD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

    assert bundled_tools.activate() is True

    # The bundled bin comes first so it wins over a system tesseract.
    assert os.environ["PATH"].split(os.pathsep)[0] == str(fake_bundle / "tesseract" / "bin")
    assert "/usr/bin" in os.environ["PATH"].split(os.pathsep)
    # …and the loader finds the bundled shared libraries.
    loader_var = "DYLD_LIBRARY_PATH" if os.uname().sysname == "Darwin" else "LD_LIBRARY_PATH"
    assert os.environ[loader_var].split(os.pathsep)[0] == str(fake_bundle / "tesseract" / "lib")
    # TESSDATA_PREFIX is the tessdata directory ITSELF (tesseract 5 semantics).
    assert os.environ["TESSDATA_PREFIX"] == str(fake_bundle / "tesseract" / "tessdata")


def test_activate_is_idempotent(fake_bundle, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    bundled_tools.activate()
    once = os.environ["PATH"]
    bundled_tools.activate()
    assert os.environ["PATH"] == once
    assert once.count(str(fake_bundle / "tesseract" / "bin")) == 1


def test_describe_reports_the_bundled_source(fake_bundle, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    bundled_tools.activate()
    info = bundled_tools.describe()
    assert info["bundled"] is True
    assert info["source"] == "bundled"
    assert info["languages"] == ["chi_sim", "eng"]
    assert info["path"].endswith(bundled_tools._EXE_NAMES[0])


def test_describe_reports_a_system_tesseract(monkeypatch, tmp_path):
    monkeypatch.setattr(bundled_tools.paths, "resource_dir", lambda: tmp_path)
    info = bundled_tools.describe()
    assert info["bundled"] is False
    assert info["bundled_path"] is None
    assert info["source"] in ("system", "missing")
