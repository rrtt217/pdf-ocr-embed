"""Job persistence: work/<job>/job.json survives restarts and resumes cleanly.

The OCR core is OCRmyPDF; per-page OCR results live in the job's hOCR work
folder as block sidecars (``000001_ocr_hocr.blocks.json``), so a restart keeps
every recognized page and the job can be finalized without re-uploading.
"""
from __future__ import annotations

import json
import shutil

import pytest

from backend import ocr_service, page_store
from backend.ocrmypad import parser as parser_mod
from pdf_fixtures import pdf_bytes as make_pdf


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
    return make_pdf(text=text, width=200, height=200)


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
    # Both pages have results on disk, so the restored job is `done`.
    assert revived['status'] == 'done'
    assert [p is not None for p in ocr_service.get_page_dicts(job["job_id"])] \
        == [True, True]
    pages = ocr_service.get_pages(job["job_id"])
    assert pages[0]["blocks"][0]["text"] == "edited text"
    # The regenerated hOCR carries the edited text for finalize.
    hocr = (ocr_service._job_dir(job["job_id"]) / "hocr" /
            "000001_ocr_hocr.hocr").read_text(encoding="utf-8")
    assert "edited text" in hocr


def _write_page_metadata(job, page_no: int):
    """The per-page ``<pageno>_hocr.json`` that the OCR phase leaves behind.

    Finalize keys off it: without this file ocrmypdf's hOCR->PDF pipeline
    treats the page as "no OCR was performed" and grafts nothing, which is
    exactly how a page can look done in the editor yet embed an empty text
    layer.  It points at the hOCR in place, so a regenerated (edited) hOCR is
    picked up without touching this file.
    """
    hdir = ocr_service._job_dir(job["job_id"]) / "hocr"
    hdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "pageno": page_no - 1,
        "pdf_page_from_image": None,
        "hocr": str(hdir / f"{page_no:06d}_ocr_hocr.hocr"),
        "textpdf": None,
        "orientation_correction": 0,
        "ocr_tree": None,
    }
    path = hdir / f"{page_no:06d}_hocr.json"
    path.write_text(json.dumps(meta), encoding="utf-8")
    return path


def _finalize_available() -> bool:
    """The finalize entry point this test exercises: ocrmypdf's hOCR -> PDF
    renderer.  It is a private API (see AGENTS.md), pinned by requirements.txt
    and re-verified after upgrades, so probe for the function itself rather
    than for a class name that moves between versions."""
    try:
        import ocrmypdf.api
    except Exception:  # noqa: BLE001 — ocrmypdf not installed at all
        return False
    return hasattr(ocrmypdf.api, "_hocr_to_ocr_pdf")


@pytest.mark.skipif(not _finalize_available(),
                    reason="ocrmypdf finalize toolchain unavailable")
def test_edited_page_reaches_the_embedded_pdf(monkeypatch, tmp_path):
    """P0-1 acceptance: the WebUI's save call (POST /api/pages/{job}/{i}) writes
    the block sidecar and regenerates that page's hOCR, and finalize renders
    THAT hOCR into the text layer — so a user correction must be extractable
    from the output PDF.

    This pins the SERVER half of the edit path (including the per-page
    `<pageno>_hocr.json` that finalize keys off — without it the page embeds an
    empty text layer even though the editor shows it as done).  The browser
    half — that app.js actually calls this route — is guarded coarsely in
    tests/test_frontend_edit_wiring.py.
    """
    from backend import pdf_processing

    _use_tmp_dirs(monkeypatch, tmp_path)
    # 240x480 pt == exactly 1000x2000 px at the sidecar's 300 dpi, so the
    # rendered text lands inside the page box.
    job = ocr_service.create_job('doc.pdf', make_pdf(pages=1, width=240.0,
                                                    height=480.0))
    _write_sidecar(job, 1, "originalmarker")
    ocr_service._set(job["job_id"], status='done', num_pages=1, pages_done=1)
    hdir = ocr_service._job_dir(job["job_id"]) / "hocr"
    # ocrmypdf's hOCR pipeline stages a copy of the input as
    # <hocr_dir>/origin.pdf; finalize grafts the text layer onto THAT file.
    assert job.get("pdf_path"), "create_job must record the uploaded PDF"
    shutil.copyfile(job["pdf_path"], hdir / "origin.pdf")
    ocr_service._ensure_hocr_files(job["job_id"])
    _write_page_metadata(job, 1)

    first, _stats = ocr_service.embed_job(job["job_id"])
    assert "originalmarker" in pdf_processing.extract_text(first, 0)

    # Exactly what the browser now POSTs before embedding.
    ocr_service.update_page(job["job_id"], 0, {"blocks": [
        {"kind": "text", "bbox": [100, 100, 900, 200],
         "text": "correctedmarker"}]})

    second, _stats2 = ocr_service.embed_job(job["job_id"])
    text = pdf_processing.extract_text(second, 0)
    assert "correctedmarker" in text
    assert "originalmarker" not in text
    ocr_service.clear_job(job["job_id"])


def test_restore_normalizes_crashed_status(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], status='running', num_pages=5)
    _simulate_restart()
    ocr_service.restore_jobs()
    assert ocr_service.get_job(job["job_id"])["status"] == "stopped"


def test_restore_recounts_pages_done_from_disk(monkeypatch, tmp_path):
    """A forced shutdown persisted pages_done=0 at run start; restore must
    recount the pages whose results are really on disk so the recovered job
    shows true progress (and the WebUI's retry-remaining / edit buttons)."""
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], status='running', num_pages=3,
                     pages_done=0)
    ocr_service._persist_if_live(job["job_id"])
    _write_sidecar(job, 1, "page one")
    _write_sidecar(job, 2, "page two")
    _simulate_restart()
    ocr_service.restore_jobs()

    revived = ocr_service.get_job(job["job_id"])
    assert revived["status"] == "stopped"
    assert revived["pages_done"] == 2
    # The WebUI reads `current` = pages_done from /api/jobs.
    entry = {j["job_id"]: j for j in ocr_service.list_jobs()}[job["job_id"]]
    assert entry["current"] == 2 and entry["total"] == 3


def test_restore_marks_complete_job_done(monkeypatch, tmp_path):
    """When every page has a result on disk, a crashed job restores as `done`
    (the OCR phase is effectively finished) instead of `stopped`."""
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], status='running', num_pages=2,
                     pages_done=0)
    ocr_service._persist_if_live(job["job_id"])
    _write_sidecar(job, 1, "page one")
    _write_sidecar(job, 2, "page two")
    _simulate_restart()
    ocr_service.restore_jobs()
    revived = ocr_service.get_job(job["job_id"])
    assert revived["status"] == "done"
    assert revived["pages_done"] == 2


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


def test_restore_normalizes_partial_job_json(monkeypatch, tmp_path):
    """A partial/legacy job.json (missing most keys) must restore into the full
    job shape: it stays visible in /api/jobs instead of 500ing the list."""
    _use_tmp_dirs(monkeypatch, tmp_path)
    legacy = tmp_path / 'work' / 'legacy12345678'
    legacy.mkdir(parents=True)
    (legacy / 'job.json').write_text(
        json.dumps({"job_id": "legacy12345678", "status": "running",
                    "num_pages": 4}), encoding='utf-8')

    _simulate_restart()
    ocr_service.restore_jobs()

    job = ocr_service.get_job('legacy12345678')
    assert job is not None
    # normalized defaults keep every consumer (list_jobs, pages, retry) safe
    assert job["filename"] == "document.pdf"
    assert job["status"] == "stopped"  # running -> interrupted
    assert job.get("pages_done", 0) is not None

    entry = {j["job_id"]: j for j in ocr_service.list_jobs()}['legacy12345678']
    assert entry["filename"] == "document.pdf" and entry["status"] == "stopped"
    # a phantom job without a real upload cannot be run
    assert ocr_service.retry_job('legacy12345678') is False


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


def test_list_jobs_carries_pre_rebuild_aliases(monkeypatch, tmp_path):
    """The WebUI reads `id` / `current` / `total` / `created` from /api/jobs;
    dropping those aliases breaks every job card (stream/clear 404 on
    `undefined`).  Both field-name sets must be present."""
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    try:
        listed = {j["job_id"]: j for j in ocr_service.list_jobs()}
        entry = listed[job["job_id"]]
        for old, new in (("id", "job_id"), ("current", "pages_done"),
                         ("total", "num_pages")):
            assert entry[old] == entry[new]
        assert isinstance(entry["created"], (int, float)) and entry["created"] > 0
        assert entry["status"] == "queued"
        assert entry["has_embedded"] is False
    finally:
        ocr_service.clear_job(job["job_id"])


def test_run_ocr_page_selection_only_runs_remaining(monkeypatch, tmp_path):
    """Retry remaining: run_ocr passes the not-done pages to ocrmypdf as a
    comma-separated `pages` list; an empty selection marks the job done
    without re-running OCR."""
    import json as _json
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], num_pages=3)
    _write_sidecar(job, 1, "page one")
    _write_sidecar(job, 2, "page two")

    captured = {}

    def fake_pipeline(pdf, folder, **kwargs):
        captured["pages"] = kwargs.get("pages")

    import ocrmypdf.api
    monkeypatch.setattr(ocrmypdf.api, "_pdf_to_hocr", fake_pipeline)

    # Selection [3]: page 3 is the only one without a result.
    ocr_service.run_ocr(job["job_id"], {"_page_selection": [3]})
    assert captured["pages"] == "3"
    assert ocr_service.get_job(job["job_id"])["status"] == "done"
    assert ocr_service.get_job(job["job_id"])["pages_done"] == 2

    # Empty selection: everything done -> no OCR run, job marked done.
    captured.clear()
    ocr_service.run_ocr(job["job_id"], {"_page_selection": []})
    assert "pages" not in captured
    assert ocr_service.get_job(job["job_id"])["status"] == "done"
    ocr_service.clear_job(job["job_id"])


def test_embedded_path_named_after_source(monkeypatch, tmp_path):
    """The finalize output is `<source stem>_embedded.pdf` in the job dir —
    never a hardcoded name (same-named uploads must not overwrite each
    other), and hostile characters are sanitized away."""
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('My Report: v2?.pdf', _real_pdf())
    path = ocr_service._embedded_path(job)
    assert path.parent == ocr_service._job_dir(job['job_id'])
    assert path.name == 'My Report_ v2__embedded.pdf'
    # A previous result of the same job is disambiguated, not overwritten.
    path.write_bytes(b'%PDF-old-result')
    path2 = ocr_service._embedded_path(job)
    assert path2.name == 'My Report_ v2__embedded_' + job['job_id'] + '.pdf'
    ocr_service.clear_job(job['job_id'])


def _write_hocr_only(job, page_no: int, text: str = "tesseract line"):
    """Drop ONLY an hOCR file (as ocrmypdf's built-in Tesseract would — no
    block sidecar), with page geometry but no scan_res."""
    hocr = (ocr_service._job_dir(job["job_id"]) / "hocr" /
            f"{page_no:06d}_ocr_hocr.hocr")
    hocr.parent.mkdir(parents=True, exist_ok=True)
    hocr.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<body>\n'
        f"<div class='ocr_page' title='bbox 0 0 1000 2000; ppageno {page_no - 1}'>\n"
        " <p class='ocr_par' title='bbox 100 100 900 200'>\n"
        f"  <span class='ocr_line' title='bbox 100 100 900 200'>"
        f"<span class='ocrx_word' title='bbox 100 100 900 200'>{text}</span></span>\n"
        " </p>\n"
        "</div>\n</body>\n</html>\n",
        encoding="utf-8")
    return hocr


def test_tesseract_hocr_only_page_is_editable_and_embeddable(monkeypatch, tmp_path):
    """An engine that writes only hOCR (ocrmypdf's built-in Tesseract) must
    still give the WebUI editable pages and a passing embed guard: the page
    store derives the sidecar from the hOCR via ocrmypdf's own parser."""
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], num_pages=1)
    _write_hocr_only(job, 1, "recognized by tesseract")

    # The hOCR-only page counts as a result and is editable.
    assert ocr_service._embedded_path(job) is not None
    pages = ocr_service.get_pages(job["job_id"])
    assert len(pages) == 1
    assert pages[0]["blocks"][0]["text"] == "recognized by tesseract"

    # An edit works on the derived page and regenerates the hOCR.
    ocr_service.update_page(job["job_id"], 0, {
        "blocks": [{"kind": "text", "bbox": [100, 100, 900, 200],
                    "text": "edited by user"}]})
    hocr = (ocr_service._job_dir(job["job_id"]) / "hocr" /
            "000001_ocr_hocr.hocr").read_text(encoding="utf-8")
    assert "edited by user" in hocr
    sidecar = (ocr_service._job_dir(job["job_id"]) / "hocr" /
               "000001_ocr_hocr.blocks.json")
    assert sidecar.exists()  # derived sidecar now persisted

    # The embed guard passes for an hOCR-only job.
    assert ocr_service.page_store.has_results(
        ocr_service._job_dir(job["job_id"]) / "hocr") is True
    ocr_service.clear_job(job["job_id"])


def test_retry_remaining_counts_hocr_only_pages(monkeypatch, tmp_path):
    """Pages that only have an hOCR file count as done for the retry-remaining
    selection (engine-agnostic progress)."""
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], num_pages=2)
    _write_hocr_only(job, 1)
    statuses = [bool(p) for p in ocr_service.get_page_dicts(job["job_id"])]
    assert statuses == [True, False]
    from backend.ocr_service import select_pages
    assert select_pages(2, statuses) == [2]
    ocr_service.clear_job(job["job_id"])


def test_run_ocr_selection_preserves_other_engines_hocr(monkeypatch, tmp_path):
    """Retry remaining with pages=2,3 must re-OCR ONLY pages 2 and 3: pages
    1/4/5 keep their existing (tesseract) hOCR files untouched."""
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', _real_pdf())
    ocr_service._set(job["job_id"], num_pages=5)
    # pages 1,4,5 already have tesseract hOCR (no sidecar)
    _write_hocr_only(job, 1, "tess p1")
    _write_hocr_only(job, 4, "tess p4")
    _write_hocr_only(job, 5, "tess p5")

    captured = {}

    def fake_pipeline(pdf, folder, **kwargs):
        captured["pages"] = kwargs.get("pages")
        captured["engine"] = kwargs.get("ocr_engine")

    import ocrmypdf.api
    monkeypatch.setattr(ocrmypdf.api, "_pdf_to_hocr", fake_pipeline)

    ocr_service.run_ocr(job["job_id"], {
        "ocr_engine": "unlimited", "_force": False,
        "_page_range": (2, 3), "_page_selection": [2, 3]})
    assert captured["pages"] == "2,3"
    assert captured["engine"] == "unlimited"
    # The untouched pages keep their hOCR (not deleted by the run).
    hdir = ocr_service._job_dir(job["job_id"]) / "hocr"
    for n in (1, 4, 5):
        assert (hdir / f"{n:06d}_ocr_hocr.hocr").exists()
    ocr_service.clear_job(job["job_id"])


def _write_tesseract_hocr(job, page_no: int, words) -> None:
    """A Tesseract-style hOCR: multi-word lines with per-word boxes and
    ``x_wconf``, page ``scan_res`` present, and NO block sidecar.

    This is the other end of the engine spectrum from the plugin/derived style
    (one full-line-wide ``ocrx_word`` per line, generated by
    ``page_store.blocks_to_hocr``), so a test that mixes both really exercises
    the engine-agnostic seam rather than one code path twice.
    """
    boxes = [[100, 100, 400, 200], [420, 100, 760, 200], [780, 100, 900, 200]]
    spans = "".join(
        f"<span class='ocrx_word' title='bbox {b[0]} {b[1]} {b[2]} {b[3]}; "
        f"x_wconf 93'>{w}</span>" for w, b in zip(words, boxes))
    hocr = (ocr_service._job_dir(job["job_id"]) / "hocr" /
            f"{page_no:06d}_ocr_hocr.hocr")
    hocr.parent.mkdir(parents=True, exist_ok=True)
    hocr.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<body>\n'
        f"<div class='ocr_page' id='page_{page_no}' title='bbox 0 0 1000 2000; "
        f"ppageno {page_no - 1}; scan_res 300 300'>\n"
        " <p class='ocr_par' title='bbox 100 100 900 200'>\n"
        "  <span class='ocr_line' title='bbox 100 100 900 200; baseline 0 -6'>"
        f"{spans}</span>\n"
        " </p>\n</div>\n</body>\n</html>\n",
        encoding="utf-8")


@pytest.mark.skipif(not _finalize_available(),
                    reason="ocrmypdf finalize toolchain unavailable")
def test_mixed_engine_job_embeds_every_page(monkeypatch, tmp_path):
    """A job whose pages came from DIFFERENT engines must still embed a text
    layer on every page.

    Switching the engine does not re-run finished pages (retry-remaining only
    picks pages without a result), so mixed jobs are a normal outcome, not an
    edge case.  Engine identity plays no part at finalize time: it renders each
    page's hOCR as written and keys off that page's ``<pageno>_hocr.json``.

    Page 1: plugin/derived style — block sidecar, hOCR regenerated from it.
    Page 2: Tesseract style — hOCR only (no sidecar), multi-word lines.
    """
    from backend import pdf_processing

    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', make_pdf(pages=2, width=240.0,
                                                    height=480.0))
    hdir = ocr_service._job_dir(job["job_id"]) / "hocr"
    hdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(job["pdf_path"], hdir / "origin.pdf")
    _write_sidecar(job, 1, "alphamarker")               # engine A (sidecar)
    _write_tesseract_hocr(job, 2, ["betamarker", "second"])  # engine B (hOCR)
    ocr_service._ensure_hocr_files(job["job_id"])
    _write_page_metadata(job, 1)
    _write_page_metadata(job, 2)
    ocr_service._set(job["job_id"], status='done', num_pages=2, pages_done=2)

    # The engine-agnostic inventory counts both pages as done.
    assert page_store.page_numbers(hdir) == [1, 2]

    out, _stats = ocr_service.embed_job(job["job_id"])
    page1 = pdf_processing.extract_text(out, 0)
    page2 = pdf_processing.extract_text(out, 1)
    assert "alphamarker" in page1          # the sidecar-derived page
    assert "betamarker" in page2           # the hOCR-only page
    assert "second" in page2               # multi-word line kept both words
    assert "betamarker" not in page1       # no text bled across pages
    assert "alphamarker" not in page2
    ocr_service.clear_job(job["job_id"])


@pytest.mark.skipif(not _finalize_available(),
                    reason="ocrmypdf finalize toolchain unavailable")
def test_sidecar_only_page_without_metadata_embeds_nothing(monkeypatch, tmp_path):
    """Documents a REAL gap, not a desired behaviour.

    ``_ensure_hocr_files`` materializes a missing hOCR from the block sidecar so
    finalize can render it — but finalize does not discover pages from the hOCR
    folder: ocrmypdf's hOCR->PDF pipeline keys off the per-page
    ``<pageno>_hocr.json`` that the OCR phase writes, and skips every page
    without one ("no OCR was performed on this page").  A page left as
    sidecar-only (a crash between the engine writing the sidecar and ocrmypdf
    writing its marker; the inventory deliberately accepts that state) therefore
    counts as done, shows up in the editor, and still embeds NO text layer.

    If this test ever starts failing because the page DID get text, the gap has
    been closed and this test should be rewritten as the positive case.
    """
    from backend import pdf_processing

    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', make_pdf(pages=1, width=240.0,
                                                    height=480.0))
    hdir = ocr_service._job_dir(job["job_id"]) / "hocr"
    hdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(job["pdf_path"], hdir / "origin.pdf")
    _write_sidecar(job, 1, "sidecaronlymarker")     # no hOCR, no _hocr.json
    ocr_service._set(job["job_id"], status='done', num_pages=1, pages_done=1)

    # The inventory counts the page (sidecar-only is a legitimate state) and the
    # hOCR gets materialized for finalize...
    assert page_store.page_numbers(hdir) == [1]
    ocr_service._ensure_hocr_files(job["job_id"])
    assert page_store.hocr_path(hdir, 1).exists()

    out, _stats = ocr_service.embed_job(job["job_id"])
    text = pdf_processing.extract_text(out, 0)
    assert "sidecaronlymarker" not in text, (
        "the gap is closed — turn this into a positive assertion "
        "(the page must embed its text)")
    ocr_service.clear_job(job["job_id"])
