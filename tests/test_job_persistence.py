"""Job persistence: work/<job>/job.json survives restarts and resumes cleanly.

The OCR core is OCRmyPDF; per-page OCR results live in the job's hOCR work
folder as block sidecars (``000001_ocr_hocr.blocks.json``), so a restart keeps
every recognized page and the job can be finalized without re-uploading.
"""
from __future__ import annotations

import json

import fitz

from backend import ocr_service
from backend.ocrmypad import parser as parser_mod


def _use_tmp_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_service, 'WORK_DIR', tmp_path / 'work')
    monkeypatch.setattr(ocr_service, 'UPLOAD_DIR', tmp_path / 'uploads')


def _simulate_restart():
    """Drop all in-memory state as if the process just restarted."""
    ocr_service._JOBS.clear()
    ocr_service._STREAMS.clear()


def _state_path(job):
    return ocr_service._job_dir(job["job_id"]) / "job.json"


def _real_pdf(text: str = "hello page") -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text(fitz.Point(20, 100), text)
    data = doc.tobytes()
    doc.close()
    return data


def _write_sidecar(job, page_no: int, text: str = "block text"):
    """Drop a block sidecar (as the plugin engine would) into the job's hOCR."""
    sidecar = (ocr_service._job_dir(job["job_id"]) / "hocr" /
               f"{page_no:06d}_ocr_hocr.blocks.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    page = parser_mod.Page(
        page_index=page_no - 1, width=1000, height=2000,
        blocks=[parser_mod.Block(kind="text", bbox=[100, 100, 900, 200],
                                 text=text, lines=[text])],
    )
    sidecar.write_text(
        json.dumps({"page": page.to_dict(), "dpi": 300.0},
                   ensure_ascii=False),
        encoding="utf-8")
    return sidecar


def test_create_job_persists_state(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    data = json.loads(_state_path(job).read_text(encoding='utf-8'))
    assert data['job_id'] == job['job_id'] and data['filename'] == 'doc.pdf'
    assert data['num_pages'] == 1 and data['status'] == 'queued'
    assert (_state_path(job)).exists()


def test_update_page_persists_and_restore_roundtrip(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    _write_sidecar(job, 1, "original")
    _write_sidecar(job, 2, "page two")
    ocr_service._set(job["job_id"], status='stopped', num_pages=2)

    # An edit goes into the sidecar AND regenerates the page's hOCR.
    edited = {"blocks": [{"kind": "text", "bbox": [100, 100, 900, 200],
                          "text": "edited text"}]}
    ocr_service.update_page(job["job_id"], 0, edited)

    _simulate_restart()
    assert ocr_service.restore_jobs() == 1

    revived = ocr_service.get_job(job["job_id"])
    assert revived is not None
    assert revived['status'] == 'stopped'
    assert [p is not None for p in ocr_service.get_page_dicts(job["job_id"])] \
        == [True, True]
    pages = ocr_service.get_pages(job["job_id"])
    assert pages[0]["blocks"][0]["text"] == "edited text"
    # The regenerated hOCR carries the edited text for finalize.
    hocr = (ocr_service._job_dir(job["job_id"]) / "hocr" /
            "000001_ocr_hocr.hocr").read_text(encoding="utf-8")
    assert "edited text" in hocr


def test_restore_normalizes_crashed_status(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], status='running', num_pages=5)
    _simulate_restart()
    ocr_service.restore_jobs()
    assert ocr_service.get_job(job["job_id"])["status"] == "stopped"


def test_restore_skips_corrupt_state(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    ok = ocr_service.create_job('ok.pdf', _real_pdf())

    bad_dir = tmp_path / 'work' / 'bad000000000'
    bad_dir.mkdir(parents=True)
    (bad_dir / 'job.json').write_text('{not valid json', encoding='utf-8')

    _simulate_restart()
    assert ocr_service.restore_jobs() == 1
    assert ocr_service.get_job(ok['job_id']) is not None
    assert ocr_service.get_job('bad000000000') is None


def test_clear_job_removes_state_file(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    state = _state_path(job)
    assert state.exists()
    assert ocr_service.clear_job(job['job_id']) is True
    assert not state.exists() and not state.parent.exists()
    assert ocr_service.get_job(job['job_id']) is None


def test_missing_sidecars_read_as_not_done(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], num_pages=3)
    _write_sidecar(job, 2)
    pages = ocr_service.get_page_dicts(job["job_id"])
    assert [p is not None for p in pages] == [False, True, False]
    assert ocr_service.get_pages(job["job_id"])[0]["page_index"] == 1


def test_ensure_hocr_files_regenerates_missing_hocr(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    _write_sidecar(job, 1, "sidecar only")
    hdir = ocr_service._job_dir(job["job_id"]) / "hocr"
    assert not (hdir / "000001_ocr_hocr.hocr").exists()
    ocr_service._ensure_hocr_files(job["job_id"])
    hocr = (hdir / "000001_ocr_hocr.hocr").read_text(encoding="utf-8")
    assert "sidecar only" in hocr
