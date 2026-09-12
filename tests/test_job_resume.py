"""Resuming a stopped job must continue where it left off — for real.

The exit work guarantees that quitting keeps completed pages on disk.  This
module pins the other half of that promise: what happens on the NEXT start.

Everything here drives a REAL entry point in a child process with a fake OCR
phase that behaves like the plugin (writes ``<n>_ocr_hocr.hocr`` +
``<n>_ocr_hocr.blocks.json``, checks the cancel flag once per page).  Using
real data rather than stubs is the point: it exercises the actual inventory
(``page_store.page_numbers``), the restore path (``ocr_service.restore_jobs``)
and the retry selection (``select_pages``) end to end.

Covered interruptions:

* **graceful stop** — the cancel flag stops the run at a page boundary;
* **hard crash** — SIGKILL mid-write, leaving a page whose hOCR is on disk but
  whose sidecar is truncated;
* **restart** — the job comes back from ``work/<id>/job.json`` with progress
  recounted from disk;
* **resume** — "retry remaining" re-runs ONLY the pages without a result.
"""
from __future__ import annotations

import json
import os
import shutil
import signal as signal_mod
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

from backend import ocr_service, page_store
from tests.test_exit_shutdown import (_REPO_ROOT, _await_http, _free_port,
                                      _forget_job, _start_entry_point,
                                      _upload_a_job)

#: A plugin-shaped fake.  Data-driven via environment variables (read by the
#: child, which is a real process):
#:
#:   OCR_TEST_SPEED   seconds each page takes (default 0)
#:   OCR_TEST_SKIP    pages this run must NOT process (default none)
#:   OCR_TEST_PARTIAL pages whose sidecar is written TRUNCATED (the hOCR is
#:                    complete) — models a crash between the two writes
#:   OCR_TEST_BROKEN  pages whose hOCR itself is TRUNCATED (models a crash
#:                    while the engine was streaming it)
#:   OCR_TEST_TOTAL   page count of the document (when `pages` is not given,
#:                    exactly like the real engine OCRs the whole file)
_FAKE_ENGINE = textwrap.dedent('''
    import json
    import os
    import time
    from pathlib import Path


    def _pages_env(name):
        raw = os.environ.get(name, "").strip()
        if not raw:
            return set()
        out = set()
        for part in raw.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = part.split("-", 1)
                out.update(range(int(lo), int(hi) + 1))
            elif part:
                out.add(int(part))
        return out


    def _install():
        try:
            import ocrmypdf.api as api
        except Exception:
            return

        def fake(pdf, out, **kw):
            out = Path(out)
            job_dir = out.resolve().parent      # the plugin's job-dir convention
            out.mkdir(parents=True, exist_ok=True)
            speed = float(os.environ.get("OCR_TEST_SPEED", "0") or 0)
            skip = _pages_env("OCR_TEST_SKIP")
            partial = _pages_env("OCR_TEST_PARTIAL")
            broken = _pages_env("OCR_TEST_BROKEN")
            # Record the page selection ocrmypdf handed us: this is how the
            # tests see exactly which pages a resume asked for ("3,4,5", or
            # "" = the whole document).
            pages = []
            raw = str(kw.get("pages") or "").strip()
            (out / "_fake.log").write_text(raw, encoding="utf-8")
            for part in (raw.split(",") if raw else []):
                part = part.strip()
                if "-" in part:
                    lo, hi = part.split("-", 1)
                    pages.extend(range(int(lo), int(hi) + 1))
                elif part:
                    pages.append(int(part))
            if not pages:
                pages = list(range(1, int(os.environ.get("OCR_TEST_TOTAL",
                                                         "1")) + 1))

            for n in sorted(set(pages)):
                if (job_dir / "cancel").exists():   # checked once per page
                    raise RuntimeError("OCR job cancelled by user")
                if n in skip:
                    continue
                if speed:
                    time.sleep(speed)
                hocr = out / f"{n:06d}_ocr_hocr.hocr"
                # A real, parseable hOCR document (the page store derives
                # blocks from it when a sidecar is unreadable) — with a word
                # per sentence so the derived page is not empty.
                html = "".join([
                    "<html><body>",
                    f"<div class='ocr_page' title='bbox 0 0 100 100; "
                    f"ppageno {n - 1}; scan_res 300 300'>",
                    "<p class='ocr_par' title='bbox 10 10 90 30'>",
                    "<span class='ocr_line' title='bbox 10 10 90 30'>",
                    "<span class='ocrx_word' title='bbox 10 10 60 30'>",
                    f"page{n}</span></span></p></div>",
                    "</body></html>",
                ])
                if n in broken:
                    html = html[: len(html) // 2]   # killed mid-stream
                hocr.write_text(html, encoding="utf-8")
                sidecar = out / f"{n:06d}_ocr_hocr.blocks.json"
                blob = json.dumps({"page": {"page_index": n - 1, "width": 100,
                                            "height": 100, "blocks": []},
                                   "dpi": 300})
                if n in partial:
                    sidecar.write_text(blob[: len(blob) // 2], encoding="utf-8")
                else:
                    sidecar.write_text(blob, encoding="utf-8")

        api._pdf_to_hocr = fake
    _install()
''')


@pytest.fixture
def resumable_ocr(tmp_path):
    """PYTHONPATH dir with the plugin-shaped fake engine."""
    path = tmp_path / "resumable-ocr"
    path.mkdir()
    (path / "blocking_ocr_ph.py").write_text(_FAKE_ENGINE, encoding="utf-8")
    _sweep_test_jobs()
    yield path
    # Safety net: these tests drive real entry points, so every job they create
    # lives in the repo's work/ until it is cleared.  A test that dies mid-way
    # (assertion, timeout, CI kill) must not leave one behind for the next
    # start to restore into the WebUI.
    _sweep_test_jobs()


def _sweep_test_jobs() -> None:
    """Delete work/<id> folders whose job.json names this module's upload."""
    work = _REPO_ROOT / "work"
    if not work.exists():
        return
    for jdir in work.iterdir():
        state = jdir / "job.json"
        if not jdir.is_dir() or not state.exists():
            continue
        try:
            filename = str(json.loads(state.read_text(encoding="utf-8"))
                           .get("filename") or "")
        except (OSError, ValueError):
            continue
        if filename == "resume.pdf":
            shutil.rmtree(jdir, ignore_errors=True)
            (_REPO_ROOT / "uploads" / f"{jdir.name}.pdf").unlink(
                missing_ok=True)


class _App:
    """A running entry point plus the repo paths its jobs live in."""

    def __init__(self, inject: Path, env_extra: dict | None = None):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = _start_entry_point(
            [sys.executable, "-m", "backend.main", "--port", str(self.port)],
            inject, dict(os.environ, **(env_extra or {})))
        self.job_ids: list[str] = []

    def __enter__(self) -> "_App":
        _await_http(self.base + "/api/health", self.proc)
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- lifecycle ---------------------------------------------------------
    def stop(self, signame: str = "SIGTERM", timeout: float = 40.0) -> None:
        """Quit gracefully and wait for the process to be gone."""
        if self.proc.poll() is None:
            self.proc.send_signal(getattr(signal_mod, signame))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.proc.poll() is None:
            time.sleep(0.2)
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError("the server did not exit:\n"
                                 + self.proc.stdout.read())

    def kill(self) -> None:
        """Hard crash: no teardown, no flush, mid-write is possible."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal_mod.SIGKILL)
            self.proc.wait(20)

    def forget_jobs(self) -> None:
        for job_id in self.job_ids:
            _forget_job(job_id)

    # -- job API -----------------------------------------------------------
    def upload(self, pages: int, name: str = "resume.pdf") -> str:
        import io

        from tests.pdf_fixtures import pdf_bytes

        body = io.BytesIO(pdf_bytes(pages=pages))
        boundary = "----pytest-resume"
        payload = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="files"; '
            f'filename="{name}"\r\n'.encode(),
            b"Content-Type: application/pdf\r\n\r\n",
            body.getvalue(),
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(
            self.base + "/api/ocr/upload", data=payload,
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            job_id = json.loads(resp.read())["job_id"]
        self.job_ids.append(job_id)
        return job_id

    def job(self, job_id: str) -> dict:
        payload = json.loads(
            urllib.request.urlopen(self.base + "/api/jobs", timeout=20).read())
        for entry in payload["jobs"]:
            if entry["job_id"] == job_id:
                return entry
        raise AssertionError(f"job {job_id} is not in /api/jobs")

    def pages(self, job_id: str) -> dict:
        return json.loads(urllib.request.urlopen(
            self.base + f"/api/pages/{job_id}", timeout=60).read())

    def stop_job(self, job_id: str) -> None:
        req = urllib.request.Request(
            self.base + f"/api/ocr/stop/{job_id}", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=20).read()

    def retry(self, job_id: str, force: bool = False) -> dict:
        req = urllib.request.Request(
            self.base + f"/api/ocr/retry/{job_id}",
            data={"force": "true"} if force else {}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def wait_status(self, job_id: str, wanted: set[str],
                    timeout: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout
        job = {}
        while time.monotonic() < deadline:
            job = self.job(job_id)
            if job["status"] in wanted:
                return job
            time.sleep(0.3)
        raise AssertionError(
            f"job {job_id} stayed {job.get('status')!r} (wanted {wanted})")


def _fake_log(job_id: str) -> str:
    """What the fake engine was asked to process in its LAST call."""
    path = _REPO_ROOT / "work" / job_id / "hocr" / "_fake.log"
    return path.read_text(encoding="utf-8") if path.exists() else "(no log)"


def _write_job_state(job_id: str, **fields) -> None:
    """Rewrite a job's persisted state (models a stale/crashed job.json)."""
    state = _REPO_ROOT / "work" / job_id / "job.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data.update(fields)
    state.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")


# --- 1. graceful stop -> restart -> resume -----------------------------------

def test_a_stopped_job_restores_and_resumes_only_the_missing_pages(
        resumable_ocr):
    """The whole promise, in one flow: stop, restart, finish.

    A 5-page job that stops after page 2, then survives a server restart, must
    come back as ``stopped`` with its two completed pages, and "retry
    remaining" must send ONLY pages 3-5 — never the whole document, and never
    touching the pages already on disk.
    """
    env = {"OCR_TEST_SPEED": "1.0", "OCR_TEST_TOTAL": "5"}
    job_id = None
    with _App(resumable_ocr, env) as app:
        job_id = app.upload(pages=5)
        app.wait_status(job_id, {"running"})
        # Let pages 1-2 land, then stop: the engine sees the flag before page 3.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if len(page_store.page_numbers(
                    _REPO_ROOT / "work" / job_id / "hocr")) >= 2:
                break
            time.sleep(0.2)
        app.stop_job(job_id)
        job = app.wait_status(job_id, {"stopped", "done"})
        assert job["status"] == "stopped", job
        done_before = job["pages_done"]
        assert done_before >= 1, "the stop lost every completed page"
        staged = set(page_store.page_numbers(
            _REPO_ROOT / "work" / job_id / "hocr"))
        app.stop()
    try:
        # --- restart: the job must come back, with progress recounted -------
        with _App(resumable_ocr, {"OCR_TEST_SPEED": "0",
                                 "OCR_TEST_TOTAL": "5"}) as app2:
            restored = app2.job(job_id)
            assert restored["status"] == "stopped", restored
            assert restored["pages_done"] == done_before, (
                f"restore lost progress: {restored['pages_done']} != "
                f"{done_before}")
            pages = app2.pages(job_id)["pages"]
            assert {p["page_index"] + 1 for p in pages} == staged, (
                "restored pages do not match the results on disk")

            # --- resume: only the pages without a result --------------------
            app2.retry(job_id)
            job = app2.wait_status(job_id, {"done", "error"}, timeout=90)
            assert job["status"] == "done", job
            assert job["pages_done"] == 5, job

            # What did the engine actually get asked to do?  A page that was
            # in flight when the stop landed still finishes, so the
            # expectation is "everything not on disk" — computed from the
            # staged set rather than assumed to be exactly 3-5.
            missing = {1, 2, 3, 4, 5} - staged
            asked = {int(p) for p in _fake_log(job_id).split(",") if p}
            if missing == {1, 2, 3, 4, 5}:
                asked = set()              # whole document: no `pages` option
            assert asked == missing, (
                f"resume asked for {sorted(asked)}, missing was "
                f"{sorted(missing)}")
            assert asked.isdisjoint(staged), (
                f"resume re-ran completed pages: {asked & staged}")
            # ...and the completed pages were not rewritten.
            now = set(page_store.page_numbers(
                _REPO_ROOT / "work" / job_id / "hocr"))
            assert now == {1, 2, 3, 4, 5}
            assert staged <= now
    finally:
        if job_id:
            _forget_job(job_id)


# --- 2. hard crash mid-write -> restart --------------------------------------

def test_a_crashed_job_restores_without_double_counting_a_half_written_page(
        resumable_ocr):
    """SIGKILL mid-write leaves a page with a good hOCR and a truncated sidecar.

    The page must survive as a usable, COMPLETE page: hOCR is the reproducible
    source, so a damaged sidecar is rebuilt from it (``load_page``) instead of
    costing a re-OCR.  And the page must not be lost either way — whatever the
    inventory decides, "loadable" and "counted as done" have to agree, or the
    page is invisible in the editor AND skipped by every retry.
    """
    job_id = None
    with _App(resumable_ocr, {"OCR_TEST_SPEED": "0.4", "OCR_TEST_TOTAL": "5",
                             "OCR_TEST_PARTIAL": "3"}) as app:
        job_id = app.upload(pages=5)
        app.wait_status(job_id, {"running"})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (_REPO_ROOT / "work" / job_id / "hocr" /
                    "000003_ocr_hocr.blocks.json").exists():
                break
            time.sleep(0.1)
        app.kill()          # crash: no cancel flag, no teardown
        # The crash happened while the job was still "running" in job.json.
        _write_job_state(job_id, status="running", pages_done=0)
    try:
        hdir = _REPO_ROOT / "work" / job_id / "hocr"
        sidecar = page_store.sidecar_path(hdir, 3)
        assert sidecar.exists()
        with pytest.raises(ValueError):
            json.loads(sidecar.read_text(encoding="utf-8"))

        with _App(resumable_ocr, {"OCR_TEST_SPEED": "0",
                                  "OCR_TEST_TOTAL": "5"}) as app2:
            restored = app2.job(job_id)
            assert restored["status"] == "stopped", restored
            assert restored["pages_done"] >= 1

            # The damaged page is still a real page: readable through the page
            # store (derived from its hOCR) and therefore offered to the editor.
            page = page_store.load_page(hdir, 3)
            assert page is not None, "the page lost its data to a bad sidecar"
            assert [b["text"] for b in page["blocks"]] == ["page3"]
            assert any(p["page_index"] == 2 for p in app2.pages(job_id)["pages"])
            assert 3 in page_store.page_numbers(hdir)

            # Retry still goes after whatever is genuinely missing — and never
            # re-runs the pages that are already on disk.
            done_before = set(page_store.page_numbers(hdir))
            assert len(done_before) >= 1
            app2.retry(job_id)
            job = app2.wait_status(job_id, {"done", "error"}, timeout=90)
            assert job["status"] == "done", job
            assert job["pages_done"] == 5, job
            asked = {int(p) for p in _fake_log(job_id).split(",") if p}
            if done_before == {1, 2, 3, 4, 5}:
                asked = set()             # everything had landed already
            assert asked.isdisjoint(done_before), (
                f"retry re-ran already-complete pages: {asked & done_before}")
            assert asked == {1, 2, 3, 4, 5} - done_before
    finally:
        if job_id:
            _forget_job(job_id)


def test_a_page_whose_hocr_was_cut_mid_write_is_re_ocred(resumable_ocr):
    """The other crash window: page 3's hOCR is truncated, its sidecar fine.

    An hOCR without its closing tag is NOT a finished page (that is exactly
    how ocrmypdf's Tesseract streams it), and the sidecar alone must not
    promote it: the page has to stay out of the inventory so "retry remaining"
    re-runs it.  A truncated hOCR that still counted would be a page the user
    can never open and never re-run.
    """
    job_id = None
    with _App(resumable_ocr, {"OCR_TEST_SPEED": "0.4", "OCR_TEST_TOTAL": "5",
                             "OCR_TEST_BROKEN": "3"}) as app:
        job_id = app.upload(pages=5)
        app.wait_status(job_id, {"running"})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (_REPO_ROOT / "work" / job_id / "hocr" /
                    "000003_ocr_hocr.hocr").exists():
                break
            time.sleep(0.1)
        app.kill()
        _write_job_state(job_id, status="running", pages_done=0)
    try:
        hdir = _REPO_ROOT / "work" / job_id / "hocr"
        hocr_file = page_store.hocr_path(hdir, 3)
        assert hocr_file.exists() and b"</html>" not in hocr_file.read_bytes()
        # The sidecar for that page is intact — it must not count on its own.
        assert page_store.sidecar_path(hdir, 3).exists()
        assert 3 not in page_store.page_numbers(hdir)

        with _App(resumable_ocr, {"OCR_TEST_SPEED": "0",
                                  "OCR_TEST_TOTAL": "5"}) as app2:
            restored = app2.job(job_id)
            assert restored["status"] == "stopped", restored
            # Every page counted as done must be readable: one that is
            # counted but unreadable could never be repaired.  (The reverse
            # is allowed — a page may still be readable from its sidecar
            # while it waits to be re-OCR'd.)
            counted = set(page_store.page_numbers(hdir))
            unreadable = {n for n in range(1, 6)
                          if page_store.load_page(hdir, n) is None}
            assert counted.isdisjoint(unreadable), (
                f"counted as done but unreadable: {sorted(counted & unreadable)}")
            assert 3 not in counted

            app2.retry(job_id)
            job = app2.wait_status(job_id, {"done", "error"}, timeout=90)
            assert job["status"] == "done", job
            assert job["pages_done"] == 5, job
            asked = {int(p) for p in _fake_log(job_id).split(",") if p}
            assert asked == {1, 2, 3, 4, 5} - counted
            assert 3 in asked, "the truncated page was never re-OCR'd"
            # The re-run healed it.
            assert page_store.load_page(hdir, 3) is not None
            assert page_store.page_numbers(hdir) == [1, 2, 3, 4, 5]
    finally:
        if job_id:
            _forget_job(job_id)


# --- 3. stale "running" state must not survive a restart ---------------------

def test_a_job_with_no_results_left_by_a_crash_restores_as_resumable(
        resumable_ocr):
    """A crash before the first page leaves an empty work folder behind."""
    job_id = None
    with _App(resumable_ocr, {"OCR_TEST_SPEED": "5.0",
                             "OCR_TEST_TOTAL": "3"}) as app:
        job_id = app.upload(pages=3)
        app.wait_status(job_id, {"running"})
        app.kill()                       # nothing has been written yet
        _write_job_state(job_id, status="running", pages_done=0)
    try:
        with _App(resumable_ocr, {"OCR_TEST_SPEED": "0",
                                 "OCR_TEST_TOTAL": "3"}) as app2:
            restored = app2.job(job_id)
            assert restored["status"] == "stopped", restored
            assert restored["pages_done"] == 0, restored
            # The job is still actionable: a retry runs all three pages.
            app2.retry(job_id)
            job = app2.wait_status(job_id, {"done", "error"}, timeout=60)
            assert job["status"] == "done", job
            assert job["pages_done"] == 3, job
            # All three pages were missing, so ocrmypdf is handed the whole
            # document ("" == no `pages` option).
            assert _fake_log(job_id) == "", _fake_log(job_id)
            hdir = _REPO_ROOT / "work" / job_id / "hocr"
            assert page_store.page_numbers(hdir) == [1, 2, 3]
    finally:
        if job_id:
            _forget_job(job_id)
