"""Exiting must work: bounded server stop, job cancellation, force-exit.

The failure this pins down is nasty because everything LOOKS fine: uvicorn
logs "Finished server process", and then the process sits there forever.  Two
independent causes, both covered here:

1. ``EmbeddedServer.stop`` waited for open connections with uvicorn's default
   ``timeout_graceful_shutdown=None`` — and the WebUI holds an SSE stream open
   for every running job, so "wait for connections" meant "wait for the OCR
   run to finish".
2. OCR work started on a NON-daemon thread (uvicorn's/anyio's default
   executor) is joined by the interpreter at exit
   (``concurrent.futures.thread._python_exit``), so the process outlives
   ``main`` until that thread ends.

Plus the user-facing guarantee: whatever a third-party thread is doing, a quit
ends the process (``backend.shutdown.exit_soon``).
"""
from __future__ import annotations

import json
import os
import shutil
import signal as signal_mod
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from backend import cleanup as cleanup_mod
from backend import ocr_service, page_store, server, shutdown


# --- 1. the server stops even with an SSE stream open ------------------------

def _stub_lifespan(monkeypatch):
    """Keep the server test offline and repo-clean."""
    monkeypatch.setattr(ocr_service, "restore_jobs", lambda: 0)
    monkeypatch.setattr(cleanup_mod, "start_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "stop_background_cleanup", lambda: None)


def test_stop_is_bounded_while_a_stream_is_open(monkeypatch):
    """A streaming response must not hold the shutdown for its lifetime."""
    import socket

    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    _stub_lifespan(monkeypatch)
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        import asyncio

        async def gen():
            while True:                       # never ends on its own
                yield "data: ping\n\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(gen(), media_type="text/event-stream")

    srv = server.EmbeddedServer(app, graceful_shutdown=1.0)
    srv.start(timeout=30)
    client = socket.create_connection(("127.0.0.1", srv.port))
    try:
        client.sendall(b"GET /stream HTTP/1.1\r\nHost: x\r\n"
                       b"Accept: text/event-stream\r\n\r\n")
        time.sleep(0.5)
        assert client.recv(32).startswith(b"HTTP/1.1 200")

        start = time.monotonic()
        srv.stop(timeout=6.0)
        elapsed = time.monotonic() - start

        assert not srv._thread.is_alive()
        # Bounded by graceful_shutdown (1s) plus teardown — nowhere near the
        # stream's (infinite) remaining lifetime.
        assert elapsed < 5.0, f"stop() took {elapsed:.1f}s with a stream open"
    finally:
        client.close()
        if srv._thread.is_alive():            # never leave a thread behind
            srv._force_exit()
            srv._thread.join(3.0)


def test_stop_is_safe_before_start_and_twice():
    """Teardown runs on paths where startup never happened, and may repeat."""
    from fastapi import FastAPI

    srv = server.EmbeddedServer(FastAPI())
    srv.stop(timeout=2.0)      # never started: must return, not hang
    srv.stop(timeout=2.0)      # idempotent


# --- 2. jobs stop the graceful way, and starting new work is refused ---------

def _fake_job(tmp_path: Path, monkeypatch, name: str = "job-1") -> dict:
    """A minimal running job whose work folder lives under ``tmp_path``."""
    monkeypatch.setattr(ocr_service, "WORK_DIR", tmp_path)
    job_dir = tmp_path / name / "hocr"
    job_dir.mkdir(parents=True)
    job = {
        "job_id": name, "filename": "doc.pdf", "status": "running",
        "pdf_path": "", "hocr_dir": str(job_dir), "previews_dir": "",
        "embedded_path": "", "num_pages": 5, "pages_done": 0, "current": 0,
        "error": "", "created_at": "", "created": 0.0,
    }
    with ocr_service._jobs_lock:
        ocr_service._JOBS[name] = job
    return job


def _clear_jobs():
    with ocr_service._jobs_lock:
        ocr_service._JOBS.clear()


def test_shutdown_jobs_writes_the_cancel_flag_and_marks_the_job_stopped(
        tmp_path, monkeypatch):
    try:
        job = _fake_job(tmp_path, monkeypatch)
        job_dir = Path(job["hocr_dir"]).parent

        assert ocr_service.shutdown_jobs(wait=0.0) is True

        assert page_store.is_cancelled(job_dir) is True
        assert job["status"] == "stopping"
        assert job["error"] == ocr_service.SHUTDOWN_MESSAGE
    finally:
        _clear_jobs()


def test_shutdown_jobs_waits_for_the_worker_to_finish(tmp_path, monkeypatch):
    """The wait is what turns "cancelled" into "stopped" before exit."""
    finished = threading.Event()

    def worker() -> None:
        time.sleep(0.4)          # stands in for the page in flight
        finished.set()

    try:
        _fake_job(tmp_path, monkeypatch, name="job-2")
        thread = threading.Thread(target=worker, daemon=True)
        with ocr_service._workers_cv:
            ocr_service._WORKERS[thread] = "job-2"
        thread.start()

        assert ocr_service.shutdown_jobs(wait=5.0) is True
        assert finished.is_set(), "shutdown_jobs returned before the worker did"
    finally:
        _clear_jobs()


def test_shutdown_jobs_reports_a_straggler_without_blocking(tmp_path,
                                                            monkeypatch):
    """A worker that ignores the cancel flag is left behind, not waited on."""
    straggler = threading.Thread(target=lambda: time.sleep(30), daemon=True)
    with ocr_service._workers_cv:
        ocr_service._WORKERS[straggler] = "job-x"
    straggler.start()
    try:
        start = time.monotonic()
        assert ocr_service.shutdown_jobs(wait=0.3) is False
        assert time.monotonic() - start < 3.0
    finally:
        _clear_jobs()


def test_the_registry_prunes_entries_that_are_not_live_threads():
    """A finished thread (or a stub) must never be waited on.

    ``test_api_guards`` substitutes a fake thread object for a real one; a
    waiter that trusted the registry blindly would raise on it in the middle
    of a shutdown instead of exiting.
    """
    class _NotAThread:
        pass

    finished = threading.Thread(target=lambda: None)
    finished.start()
    finished.join()
    with ocr_service._workers_cv:
        ocr_service._WORKERS[_NotAThread()] = "stub"
        ocr_service._WORKERS[finished] = "finished"
        ocr_service._WORKERS[threading.current_thread()] = "me"

    # The current thread is "live", so this cannot fully drain — the point is
    # that the two bogus entries were pruned rather than waited on.
    assert ocr_service.shutdown_jobs(wait=0.2) is False
    assert ocr_service.active_jobs() == ["me"]
    with ocr_service._workers_cv:
        _WORKERS_KEYS = list(ocr_service._WORKERS)
    assert _WORKERS_KEYS == [threading.current_thread()]


def test_no_new_job_starts_once_shutdown_began(tmp_path, monkeypatch):
    try:
        _fake_job(tmp_path, monkeypatch, name="job-3")
        assert ocr_service.start_job("job-3") is True
        ocr_service.shutdown_jobs(wait=0.0)
        assert ocr_service.is_shutting_down() is True
        assert ocr_service.start_job("job-3") is False
    finally:
        _clear_jobs()
        ocr_service.reset_shutdown_state()


def test_a_worker_that_lost_the_race_does_not_run_ocr(tmp_path, monkeypatch):
    """A job thread scheduled after the quit must not start a doomed run."""
    called: list[bool] = []
    monkeypatch.setattr(ocr_service, "_ocrmypdf_options", lambda **kw: {})
    monkeypatch.setattr(ocr_service, "_run_ocr",
                        lambda *a, **kw: called.append(True))
    try:
        _fake_job(tmp_path, monkeypatch, name="job-4")
        ocr_service.shutdown_jobs(wait=0.0)
        ocr_service.run_ocr("job-4")

        assert called == []
        job = ocr_service.get_job("job-4")
        assert job["status"] == "stopped"
        assert job["error"] == ocr_service.SHUTDOWN_MESSAGE
    finally:
        _clear_jobs()
        ocr_service.reset_shutdown_state()


def test_a_retry_clears_the_cancel_flag(tmp_path, monkeypatch):
    """A new run starts uncancelled: the flag outlives the run that set it."""
    try:
        job = _fake_job(tmp_path, monkeypatch, name="job-5")
        job_dir = Path(job["hocr_dir"]).parent
        page_store.request_cancel(job_dir)
        assert page_store.is_cancelled(job_dir) is True

        page_store.clear_cancel(job_dir)
        assert page_store.is_cancelled(job_dir) is False
    finally:
        _clear_jobs()


# --- 3. the shared shutdown module ------------------------------------------

def test_stop_jobs_is_once_only(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(ocr_service, "shutdown_jobs",
                        lambda wait=0.0: calls.append(wait) or True)

    shutdown.stop_jobs(wait=0.1)
    shutdown.stop_jobs(wait=0.1)

    assert len(calls) == 1


def test_stop_jobs_never_raises(monkeypatch):
    def boom(wait=0.0):
        raise RuntimeError("no")

    monkeypatch.setattr(ocr_service, "shutdown_jobs", boom)
    shutdown.stop_jobs()          # must not propagate


def test_stop_server_tolerates_a_broken_server():
    class Broken:
        def stop(self, timeout=None):       # noqa: ARG002
            raise RuntimeError("no")

    shutdown.stop_server(Broken())
    shutdown.stop_server(None)


def test_the_quit_hook_cancels_first_then_stops_the_server(monkeypatch):
    """Order matters: the engine is told to stop before anything can block.

    The hook is bounded by the caller's deadline, so the cancel sweep has to
    be the first thing it does — a hook cut short after that still left the
    engine stopping, and the teardown continues on the hook's helper thread.
    """
    import desktop

    order: list[str] = []
    monkeypatch.setattr(ocr_service, "request_all_cancels",
                        lambda: order.append("cancel") or [])
    monkeypatch.setattr(shutdown, "stop_jobs",
                        lambda wait=None: order.append("stop_jobs"))
    monkeypatch.setattr(shutdown, "stop_server",
                        lambda server, timeout=None: order.append("stop_server"))

    desktop._quit_hook(object())

    # First action = cancel (immediate); the bounded wait and the server stop
    # come after it.  The sweep may legitimately run twice (stop_jobs starts
    # with one too) — what must not happen is stopping the server first.
    assert order[0] == "cancel"
    assert order.index("cancel") < order.index("stop_server")
    assert order[-1] == "stop_server"
    assert "stop_jobs" in order


def test_request_all_cancels_never_starts_a_wait(tmp_path, monkeypatch):
    """The immediate half of a shutdown must return at once."""
    try:
        job = _fake_job(tmp_path, monkeypatch, name="job-fast")
        start = time.monotonic()
        cancelled = ocr_service.request_all_cancels()
        assert time.monotonic() - start < 1.0
        assert cancelled == ["job-fast"]
        assert page_store.is_cancelled(Path(job["hocr_dir"]).parent) is True
    finally:
        _clear_jobs()


def test_the_exit_deadline_is_armed_by_the_shutdown_not_by_startup():
    """Arming the deadline at launch would kill a healthy, long-running app.

    Starting the entry point must leave no timer behind; the deadline belongs
    to the QUIT (the signal handler and ``stop_server`` arm it).
    """
    assert shutdown._exit_timer is None      # nothing armed by importing

    shutdown.stop_server(None)               # no server: nothing to arm yet
    assert shutdown._exit_timer is None or not shutdown._exit_timer.is_alive()

    class FakeServer:
        def stop(self, timeout=None):        # noqa: ARG002
            pass

    shutdown.stop_server(FakeServer())       # the funnel every quit uses
    assert shutdown._exit_timer is not None
    assert shutdown._exit_timer.is_alive()
    shutdown.reset()


def test_exit_soon_arms_a_deadline_without_killing_the_test():
    timer = shutdown.exit_soon(delay=30.0, force=False)
    assert timer.is_alive()
    # Arming twice replaces the deadline instead of stacking timers.
    second = shutdown.exit_soon(delay=30.0, force=False)
    assert second is not timer
    assert timer.finished.is_set() or not timer.is_alive()
    shutdown.reset()


def test_signal_handler_quits_once_then_forces(monkeypatch):
    quits: list[bool] = []
    forced: list[bool] = []
    handler = shutdown.make_signal_handler(
        lambda: quits.append(True),
        force_exit=lambda: forced.append(True),
        label="the test",
    )

    handler(int(signal_mod.SIGINT), None)
    assert quits == [True] and forced == []

    handler(int(signal_mod.SIGTERM), None)      # second signal: go now
    assert forced == [True]


def test_install_signal_handlers_returns_a_bool():
    original = signal_mod.getsignal(signal_mod.SIGINT)
    try:
        assert shutdown.install_signal_handlers(lambda *_: None) in (True, False)
    finally:
        signal_mod.signal(signal_mod.SIGINT, original)


# --- 4. the real entry points, as child processes ----------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Replace OCRmyPDF's OCR phase with a blocking body: that is the state the
#: app used to hang in.  Injected through the child's PYTHONPATH only.
_BLOCKING_OCR = textwrap.dedent('''
    import time

    def _install():
        try:
            import ocrmypdf.api as api
        except Exception:
            return

        def fake(pdf, out, **kw):
            time.sleep(600)
        api._pdf_to_hocr = fake
    _install()
''')


@pytest.fixture
def blocking_ocr(tmp_path):
    """A directory to put on the child's PYTHONPATH (see ``_BLOCKING_OCR``)."""
    path = tmp_path / "blocking-ocr"
    path.mkdir()
    (path / "blocking_ocr_ph.py").write_text(_BLOCKING_OCR, encoding="utf-8")
    return path


#: A page that "takes an hour": it honours the cancel flag BEFORE the request
#: (exactly like the real engine: `generate_hocr` checks `is_cancelled` on
#: entry) but is uninterruptible once in flight — the worst case the HTTP
#: timeout budget allows (900 s read timeout x 4 attempts with the defaults).
#: The abandoned in-flight page must not cost us the completed pages, and it
#: must never keep the process alive.
_STUCK_PAGE_OCR = textwrap.dedent('''
    import threading
    import time
    from pathlib import Path

    HOUR = 3600.0

    def _install():
        try:
            import ocrmypdf.api as api
        except Exception:
            return

        def fake(pdf, out, **kw):
            out = Path(out)
            job_dir = out.resolve().parent     # the plugin's job-dir convention
            out.mkdir(parents=True, exist_ok=True)

            def page(n):
                if (job_dir / "cancel").exists():   # checked per page, on entry
                    raise RuntimeError("OCR job cancelled by user")
                if n > 1:
                    # The in-flight request: no cancel check inside it, and
                    # its helper threads cannot be interrupted either.
                    helpers = [threading.Thread(target=time.sleep, args=(HOUR,))
                               for _ in range(2)]
                    for helper in helpers:
                        helper.start()
                    for helper in helpers:
                        helper.join()               # the "one hour" page
                (out / f"{n:06d}_ocr_hocr.hocr").write_text("<html/>")

            page(1)                                 # completes almost instantly
            page(2)                                 # hangs for an hour

        api._pdf_to_hocr = fake
    _install()
''')


@pytest.fixture
def stuck_page_ocr(tmp_path):
    """PYTHONPATH dir that makes the FIRST page finish and the NEXT hang."""
    path = tmp_path / "stuck-page"
    path.mkdir()
    (path / "blocking_ocr_ph.py").write_text(_STUCK_PAGE_OCR, encoding="utf-8")
    return path


def _free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_entry_point(argv: list[str], inject: Path, env: dict) -> subprocess.Popen:
    child_env = dict(env)
    # sitecustomize auto-imports for every child process, including -m runs.
    site = inject / "sitecustomize.py"
    site.write_text("import blocking_ocr_ph\n", encoding="utf-8")
    child_env["PYTHONPATH"] = os.pathsep.join(
        [str(inject), str(_REPO_ROOT), child_env.get("PYTHONPATH", "")])
    child_env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(argv, cwd=str(_REPO_ROOT), env=child_env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def _await_http(url: str, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    import urllib.error

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("the server exited during startup:\n"
                                 + proc.stdout.read())
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise AssertionError(f"the server never answered {url}")


def _upload_a_job(base: str, pages: int = 1) -> str:
    """POST a PDF to /api/ocr/upload; return the job id.

    The child process is a REAL entry point, so the job lands in the repo's own
    ``work/`` and ``uploads/`` (the source-checkout paths).  Always pair this
    with :func:`_forget_job` — a leaked job would be restored into the WebUI
    job list by the next start.
    """
    import io

    from tests.pdf_fixtures import pdf_bytes

    body = io.BytesIO(pdf_bytes(pages=pages))
    boundary = "----pytest-exit"
    payload = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="files"; filename="t.pdf"\r\n',
        b"Content-Type: application/pdf\r\n\r\n",
        body.getvalue(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        base + "/api/ocr/upload", data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["job_id"]


def _forget_job(job_id: str) -> None:
    """Remove a test job's work folder and stored upload (keeps work/ clean)."""
    shutil.rmtree(_REPO_ROOT / "work" / job_id, ignore_errors=True)
    (_REPO_ROOT / "uploads" / f"{job_id}.pdf").unlink(missing_ok=True)


def _assert_exits_quickly(proc: subprocess.Popen, *, budget: float) -> str:
    """Wait for the child AND its subprocesses to go; return its log if not.

    Checks descendants too: the hang this guards against leaves the entry
    point process alive, but a helper process left behind would be just as
    wrong.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if proc.poll() is not None and not [p for p in _descendants(proc.pid)
                                            if _alive(p)]:
            return ""
        time.sleep(0.2)
    output = ""
    try:
        proc.kill()
        output = proc.stdout.read()
    except Exception:                       # noqa: BLE001
        pass
    return output or "(no output)"


def _descendants(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as fh:
            return [int(p) for p in fh.read().split()]
    except OSError:
        return []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_desktop_app_exits_when_the_ui_asks_it_to_quit(blocking_ocr):
    """The Quit button, with an OCR job mid-run and a live SSE stream."""
    import socket

    port = _free_port()
    proc = _start_entry_point(
        [sys.executable, "desktop.py", "--no-window", "--port", str(port)],
        blocking_ocr, dict(os.environ))
    base = f"http://127.0.0.1:{port}"
    stream = None
    job_id = None
    try:
        _await_http(base + "/api/health", proc)
        job_id = _upload_a_job(base)

        # Hold an SSE stream open, exactly like the WebUI does.
        stream = socket.create_connection(("127.0.0.1", port), timeout=5)
        stream.sendall(f"GET /api/ocr/stream/{job_id} HTTP/1.1\r\nHost: x\r\n"
                       "Accept: text/event-stream\r\n\r\n".encode())
        time.sleep(1.0)

        req = urllib.request.Request(
            base + "/api/app/quit", data=b"", method="POST",
            headers={"X-PDF-OCR-Embed": "quit"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200

        # Long enough for the bounded job wait plus the server drain, far short
        # of the 600s the fake OCR sleeps for.
        log = _assert_exits_quickly(proc, budget=40.0)
        assert log == "", f"the desktop app did not exit:\n{log}"
    finally:
        if stream is not None:
            stream.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if job_id:
            _forget_job(job_id)


@pytest.mark.parametrize("signame", ["SIGINT", "SIGTERM"])
def test_plain_server_exits_on_a_signal(blocking_ocr, signame):
    """Non-desktop mode: Ctrl-C / SIGTERM must not leave a hung process."""
    port = _free_port()
    proc = _start_entry_point(
        [sys.executable, "-m", "backend.main", "--port", str(port)],
        blocking_ocr, dict(os.environ))
    base = f"http://127.0.0.1:{port}"
    job_id = None
    try:
        _await_http(base + "/api/health", proc)
        job_id = _upload_a_job(base)
        time.sleep(0.5)

        proc.send_signal(getattr(signal_mod, signame))
        log = _assert_exits_quickly(proc, budget=40.0)
        assert log == "", f"the server did not exit on {signame}:\n{log}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if job_id:
            _forget_job(job_id)


def test_a_second_signal_exits_immediately(blocking_ocr):
    """The documented escape hatch: press Ctrl-C twice and it goes NOW.

    The first signal starts the graceful path, which waits a few seconds for
    the in-flight OCR page.  A user who repeats the signal must not have to
    wait that out — the second one exits on the spot (well under the graceful
    budget, and far under the 20s hard deadline).
    """
    port = _free_port()
    proc = _start_entry_point(
        [sys.executable, "-m", "backend.main", "--port", str(port)],
        blocking_ocr, dict(os.environ))
    base = f"http://127.0.0.1:{port}"
    job_id = None
    try:
        _await_http(base + "/api/health", proc)
        job_id = _upload_a_job(base)
        time.sleep(0.5)

        start = time.monotonic()
        proc.send_signal(signal_mod.SIGINT)
        time.sleep(0.3)                     # graceful path is now waiting
        proc.send_signal(signal_mod.SIGINT)
        log = _assert_exits_quickly(proc, budget=10.0)
        elapsed = time.monotonic() - start

        assert log == "", f"the second signal did not force an exit:\n{log}"
        assert elapsed < shutdown.JOB_STOP_WAIT, (
            f"the second signal waited {elapsed:.1f}s for the graceful path")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if job_id:
            _forget_job(job_id)


# --- the page that takes an hour ---------------------------------------------

def test_a_page_that_takes_an_hour_never_delays_the_exit(stuck_page_ocr):
    """The worst case the HTTP budget allows must still exit in seconds.

    A single page can be in flight for ~1 h with the default plugin settings
    (900 s read timeout x 4 attempts), and the engine only checks the cancel
    flag BEFORE a page — so a stop/quit cannot interrupt that page.  What must
    hold anyway:

    * the process exits promptly (page 1 is completed and on disk, the running
      page is abandoned, never awaited);
    * the page that was already finished survives, so the job can be resumed
      ("retry remaining") instead of re-OCR'ing the whole document.
    """
    port = _free_port()
    proc = _start_entry_point(
        [sys.executable, "desktop.py", "--no-window", "--port", str(port)],
        stuck_page_ocr, dict(os.environ))
    base = f"http://127.0.0.1:{port}"
    job_id = None
    work_dir = _REPO_ROOT / "work"
    try:
        _await_http(base + "/api/health", proc)
        job_id = _upload_a_job(base, pages=2)
        job_dir = work_dir / job_id

        # Wait until page 1 is written AND page 2 is the one in flight.
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            results = list((job_dir / "hocr").glob("*_ocr_hocr.hocr"))
            if results:
                break
            time.sleep(0.2)
        assert list((job_dir / "hocr").glob("*_ocr_hocr.hocr")), \
            "page 1 never completed — the fake OCR did not run"
        time.sleep(1.0)                     # let page 2 get in flight

        start = time.monotonic()
        req = urllib.request.Request(
            base + "/api/app/quit", data=b"", method="POST",
            headers={"X-PDF-OCR-Embed": "quit"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200

        log = _assert_exits_quickly(proc, budget=40.0)
        elapsed = time.monotonic() - start

        assert log == "", f"the app did not exit past a stuck page:\n{log}"
        assert elapsed < shutdown.EXIT_DEADLINE, (
            f"exiting took {elapsed:.1f}s — the stuck page delayed the quit")

        # The completed page is still there: nothing was rolled back.
        page_one = job_dir / "hocr" / "000001_ocr_hocr.hocr"
        assert page_one.exists(), "the completed page was lost on exit"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if job_id:
            _forget_job(job_id)


def test_cli_never_starts_daemonless_workers():
    """The CLI runs OCR on the main thread, so nothing to join at exit."""
    source = (_REPO_ROOT / "backend" / "cli.py").read_text(encoding="utf-8")
    assert "run_in_executor" not in source
    assert "ThreadPoolExecutor" not in source


# --- the plain uvicorn command (its own signal handlers, our app) ------------

#: Models what ocrmypdf does internally: its own ThreadPoolExecutor, whose
#: workers are NON-daemon and are therefore joined at interpreter exit.  This
#: is the state the "needs a second Ctrl-C" report came from.
_POOLED_OCR = textwrap.dedent('''
    import concurrent.futures
    import time

    MARKER = "@@MARKER@@"


    def _install():
        try:
            import ocrmypdf.api as api
        except Exception:
            return

        def fake(pdf, out, **kw):
            open(MARKER, "w").write("1")
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            futures = [pool.submit(time.sleep, 600) for _ in range(2)]
            for future in futures:
                future.result()

        api._pdf_to_hocr = fake
    _install()
''')


@pytest.fixture
def pooled_ocr(tmp_path):
    """PYTHONPATH dir whose OCR phase leaves non-daemon pool threads behind."""
    path = tmp_path / "pooled-ocr"
    path.mkdir()
    (path / "blocking_ocr_ph.py").write_text(
        _POOLED_OCR.replace("@@MARKER@@", str(tmp_path / "ocr_started")),
        encoding="utf-8")
    return path


def test_plain_uvicorn_exits_on_the_first_signal(pooled_ocr):
    """`uvicorn backend.main:app` must not need a second Ctrl-C.

    This entry point installs its own signal handlers and never calls
    ``backend.main.run()``, so the app's own quit handling is not active — the
    only guarantees in play are the ones the app wires into its lifespan: a
    signal wrapper that cancels OCR, and an exit watchdog that bounds how long
    the interpreter waits for non-daemon worker threads (ocrmypdf's page pool)
    after the server has already stopped.
    """
    port = _free_port()
    proc = _start_entry_point(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--port", str(port)],
        pooled_ocr, dict(os.environ))
    base = f"http://127.0.0.1:{port}"
    job_id = None
    try:
        _await_http(base + "/api/health", proc)
        job_id = _upload_a_job(base)
        # Let the OCR phase really be inside its pool before the signal.
        # (The fixture bakes the marker path next to its own directory.)
        deadline = time.monotonic() + 30
        marker = pooled_ocr.parent / "ocr_started"
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.2)
        assert marker.exists(), "the OCR phase never started"
        time.sleep(1.0)

        proc.send_signal(signal_mod.SIGINT)
        # ONE signal must be enough: the app's exit watchdog fires a few
        # seconds after uvicorn stops, so anything near the hard deadline
        # means the join was not bounded.
        log = _assert_exits_quickly(proc, budget=shutdown.EXIT_DEADLINE)
        assert log == "", f"uvicorn needed more than one signal:\n{log}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if job_id:
            _forget_job(job_id)


def test_the_exit_watchdog_only_arms_when_a_worker_is_still_alive():
    """A process with no worker threads left exits on its own.

    The watchdog must not turn "finished normally" into "killed at the
    deadline" — it only arms a timer when something is genuinely still
    running.  Daemon threads count too: on Python 3.13+ interpreter shutdown
    waits for their thread state as well, which is exactly how a daemon OCR
    page kept the process alive in the first place.
    """
    import threading

    assert shutdown.watchdog_on_exit(grace=30.0) == []
    assert shutdown._exit_timer is None or not shutdown._exit_timer.is_alive()

    stop = threading.Event()
    helper = threading.Thread(target=stop.wait, daemon=True)
    helper.start()
    try:
        assert [t.name for t in shutdown.watchdog_on_exit(grace=30.0)] == [
            helper.name]
        assert shutdown._exit_timer is not None
    finally:
        stop.set()
        helper.join(5)
        shutdown.reset()
