"""Temp-file cleanup: it must never collect a job that still exists.

Two failure modes are pinned here, both learned the hard way on a real machine:

1. **Scan roots must be resolved at call time.**  They used to be captured in a
   module-level dict at import, so patching ``cleanup.WORK_DIR`` (tests, an
   embedded host, a second instance) silently kept sweeping the ORIGINAL
   directories — a verification harness with isolated dirs deleted a user's
   32 MB upload from the real ``uploads/``.

2. **A job that exists on disk protects itself.**  ``referenced_paths()`` only
   consulted the in-memory registry, so any process that did not have the job in
   its registry treated a complete job (upload + work dir) as garbage.
"""
from __future__ import annotations

import os
import time

import pytest

from backend import cleanup, ocr_service


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    """Isolated work/output/uploads dirs, and an empty job registry."""
    work = tmp_path / "work"
    output = tmp_path / "output"
    uploads = tmp_path / "uploads"
    for d in (work, output, uploads):
        d.mkdir()
    monkeypatch.setattr(cleanup, "WORK_DIR", work)
    monkeypatch.setattr(cleanup, "OUTPUT_DIR", output)
    monkeypatch.setattr(cleanup, "UPLOAD_DIR", uploads)
    saved = dict(ocr_service._JOBS)
    ocr_service._JOBS.clear()
    yield {"work": work, "output": output, "uploads": uploads}
    ocr_service._JOBS.clear()
    ocr_service._JOBS.update(saved)


def _old(path, hours: float, *, content: bytes = b"x"):
    """Age a temp entry, creating it first when it is not there yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)
    stamp = time.time() - hours * 3600.0
    os.utime(path, (stamp, stamp))
    return path


def _job_pair(dirs, job_id: str, hours: float = 400.0):
    """A complete on-disk job: `uploads/<id>.pdf` + `work/<id>/hocr/`."""
    pdf = dirs["uploads"] / f"{job_id}.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    hdir = dirs["work"] / job_id / "hocr"
    hdir.mkdir(parents=True)
    (hdir / "000001_ocr_hocr.hocr").write_text("<html></html>", encoding="utf-8")
    _old(pdf, hours)
    _old(hdir / "000001_ocr_hocr.hocr", hours)
    _old(hdir, hours)
    _old(hdir.parent, hours)
    return pdf, hdir.parent


# --- 1. the scan roots ------------------------------------------------------

def test_scan_uses_the_patched_directories(runtime):
    """An isolated harness must sweep ITS dirs — never the real ones."""
    stray = _old(runtime["uploads"] / "stray.pdf", 400)
    assert cleanup._area_dirs()["uploads"] == runtime["uploads"]

    cleanup.cleanup(dry_run=True)
    listed = {i["path"] for a in cleanup.inventory()["areas"].values()
              for i in a["items"]}
    assert str(stray) in listed, "the patched uploads dir was not scanned"


def test_a_job_on_disk_survives_a_process_that_does_not_know_it(runtime):
    """THE incident: an empty registry must not make a real job collectable."""
    pdf, jdir = _job_pair(runtime, "925ff65c70e9")

    refs = cleanup.referenced_paths()
    assert str(pdf.resolve()) in refs
    assert str(jdir.resolve()) in refs

    cleanup.cleanup(force=True)          # age rule deliberately bypassed
    assert pdf.exists(), "the upload was collected — retry/finalize would break"
    assert jdir.exists(), "the work dir was collected — the OCR results are gone"


def test_half_a_job_is_still_garbage(runtime):
    """Cleanup must keep working: an interrupted clear leaves a collectable half."""
    lone_upload = _old(runtime["uploads"] / "orphan.pdf", 400)
    lone_dir = runtime["work"] / "orphanwork"
    lone_dir.mkdir()
    _old(lone_dir, 400)

    cleanup.cleanup(force=True)
    assert not lone_upload.exists()
    assert not lone_dir.exists()


def test_age_still_gates_deletion(runtime):
    """Without force, a fresh orphan is kept (the age rule is what makes this
    feature safe to run automatically)."""
    fresh = runtime["uploads"] / "fresh.pdf"
    fresh.write_bytes(b"%PDF-1.4")
    report = cleanup.cleanup()
    assert fresh.exists()
    assert report["kept"]["too_fresh"] >= 1


def test_live_job_paths_are_still_protected(runtime):
    """The original rule stays: anything a live job references is untouchable,
    even for a lone upload with no work dir."""
    pdf = _old(runtime["uploads"] / "live.pdf", 400)
    ocr_service._JOBS["live"] = {
        "job_id": "live", "status": "done", "pdf_path": str(pdf),
        "hocr_dir": str(runtime["work"] / "live" / "hocr"),
        "previews_dir": str(runtime["work"] / "live" / "previews"),
        "embedded_path": "",
    }
    cleanup.cleanup(force=True)
    assert pdf.exists()


def test_symlinks_are_never_touched(runtime, tmp_path):
    """A symlinked temp entry is neither removed nor followed.

    The target lives OUTSIDE the scanned areas on purpose: if the sweeper ever
    followed the link, the target would vanish even though nothing in the
    scanned tree points at it legitimately.
    """
    outside = tmp_path / "outside-the-areas.bin"
    outside.write_bytes(b"precious")
    link = runtime["output"] / "link.bin"
    link.symlink_to(outside)

    cleanup.cleanup(force=True)
    assert link.is_symlink(), "the symlink itself was removed"
    assert outside.exists(), "a symlink was followed and its target removed"
