"""generate_hocr integration: batch routing, single-page fallback, output files."""
from __future__ import annotations

import json
import types
from pathlib import Path

import PIL.Image
import pytest

from backend.ocrmypad import settings as ocrmypad_settings
from backend.ocrmypad.unlimited_engine import (
    _BATCHERS,
    _batchers_lock,
    UnlimitedOcrEngine,
)

RAW = "<|det|>text [0,0,100,100]<|/det|>hello page"


def _options(tmp_path):
    hocr = tmp_path / "job" / "hocr"
    hocr.mkdir(parents=True)
    return types.SimpleNamespace(output_folder=str(hocr), jobs=2)


def _page(tmp_path, name="p.png"):
    p = tmp_path / name
    PIL.Image.new("RGB", (100, 100), "white").save(p)
    return p


@pytest.fixture(autouse=True)
def _clean_batchers():
    with _batchers_lock:
        _BATCHERS.clear()
    yield
    with _batchers_lock:
        _BATCHERS.clear()


@pytest.fixture(autouse=True)
def _reset_settings():
    ocrmypad_settings.configure({})
    yield
    ocrmypad_settings.configure({})


def test_generate_hocr_batch_mode_uses_recognize_multi(monkeypatch, tmp_path):
    ocrmypad_settings.configure({"ocr_batch_size": "2", "ocr_batch_timeout_ms": "3000"})
    calls = []

    def fake_multi(paths):
        calls.append(("multi", len(paths), Path(str(paths[0])).parent))
        return [RAW] * len(paths)

    def fake_single(path):
        calls.append(("single", path))
        return RAW

    monkeypatch.setattr(
        "backend.ocrmypad.unlimited_engine.UnlimitedOcrClient.recognize_multi",
        lambda self, paths: fake_multi(paths))
    monkeypatch.setattr(
        "backend.ocrmypad.unlimited_engine.UnlimitedOcrClient.recognize",
        lambda self, path: fake_single(path))

    opts = _options(tmp_path)
    img = _page(tmp_path)
    hocr_out = tmp_path / "job" / "hocr" / "000001_ocr_hocr.hocr"
    txt_out = tmp_path / "job" / "hocr" / "000001_ocr_hocr.txt"
    UnlimitedOcrEngine.generate_hocr(img, hocr_out, txt_out, opts)

    # batch mode routes through recognize_multi (a window of 1 flushes at once)
    assert any(kind == "multi" for kind, *_ in calls)
    assert not any(kind == "single" for kind, *_ in calls)
    assert hocr_out.exists() and txt_out.exists()
    assert "hello page" in hocr_out.read_text(encoding="utf-8")
    sidecar = tmp_path / "job" / "hocr" / "000001_ocr_hocr.blocks.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["page"]["blocks"][0]["text"] == "hello page"
    assert data["page"]["width"] == 100 and data["page"]["height"] == 100


def test_generate_hocr_batch_mode_falls_back_to_single_page(monkeypatch, tmp_path):
    ocrmypad_settings.configure({"ocr_batch_size": "2", "ocr_batch_timeout_ms": "3000"})
    calls = []

    def fake_multi(paths):
        raise ConnectionError("endpoint down")

    def fake_single(path):
        calls.append(("single", path))
        return RAW

    monkeypatch.setattr(
        "backend.ocrmypad.unlimited_engine.UnlimitedOcrClient.recognize_multi",
        lambda self, paths: fake_multi(paths))
    monkeypatch.setattr(
        "backend.ocrmypad.unlimited_engine.UnlimitedOcrClient.recognize",
        lambda self, path: fake_single(path))

    opts = _options(tmp_path)
    img = _page(tmp_path)
    hocr_out = tmp_path / "job" / "hocr" / "000001_ocr_hocr.hocr"
    UnlimitedOcrEngine.generate_hocr(img, hocr_out, tmp_path / "x.txt", opts)

    # Batch failure degrades to a per-page request for this page only.
    assert [kind for kind, *_ in calls] == ["single"]
    assert "hello page" in hocr_out.read_text(encoding="utf-8")


def test_generate_hocr_batch_disabled_uses_single(monkeypatch, tmp_path):
    ocrmypad_settings.configure({"ocr_batch_size": "0"})
    calls = []

    def fake_multi(paths):
        calls.append(("multi", paths))
        return [RAW] * len(paths)

    def fake_single(path):
        calls.append(("single", path))
        return RAW

    monkeypatch.setattr(
        "backend.ocrmypad.unlimited_engine.UnlimitedOcrClient.recognize_multi",
        lambda self, paths: fake_multi(paths))
    monkeypatch.setattr(
        "backend.ocrmypad.unlimited_engine.UnlimitedOcrClient.recognize",
        lambda self, path: fake_single(path))

    opts = _options(tmp_path)
    img = _page(tmp_path)
    hocr_out = tmp_path / "job" / "hocr" / "000001_ocr_hocr.hocr"
    UnlimitedOcrEngine.generate_hocr(img, hocr_out, tmp_path / "x.txt", opts)

    assert [kind for kind, *_ in calls] == ["single"]


def test_generate_hocr_serial_jobs_bypass_batching(monkeypatch, tmp_path):
    ocrmypdf_settings = {"ocr_batch_size": "4"}
    ocrmypad_settings.configure(ocrmypdf_settings)
    calls = []

    def fake_multi(paths):
        calls.append(("multi", paths))
        return [RAW] * len(paths)

    monkeypatch.setattr(
        "backend.ocrmypad.unlimited_engine.UnlimitedOcrClient.recognize_multi",
        lambda self, paths: fake_multi(paths))
    monkeypatch.setattr(
        "backend.ocrmypad.unlimited_engine.UnlimitedOcrClient.recognize",
        lambda self, path: (calls.append(("single", path)), RAW)[1])

    hocr = tmp_path / "job" / "hocr"
    hocr.mkdir(parents=True)
    opts = types.SimpleNamespace(output_folder=str(hocr), jobs=1)  # serial
    img = _page(tmp_path)
    UnlimitedOcrEngine.generate_hocr(img, hocr / "000001_ocr_hocr.hocr",
                                     tmp_path / "x.txt", opts)

    assert [kind for kind, *_ in calls] == ["single"]
