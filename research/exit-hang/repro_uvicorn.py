"""Reproduce: does `uvicorn backend.main:app` exit on Ctrl-C?

This is the THIRD entry point (and the one the docs recommend): plain uvicorn,
which installs its own signal handlers and never calls ``backend.main.run()``.
So none of the app's own quit handling is active — and when a running OCR job
has spawned its own pool threads, the interpreter joins them at exit and the
process hangs after "Finished server process" until Ctrl-C is pressed again.

Usage: .venv/bin/python research/exit-hang/repro_uvicorn.py [--signal=SIGINT]
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.path.join(ROOT, "research", "exit-hang", "_tmp")
MARKER = os.path.join(WORK, "ocr_started")
PDF = os.path.join(WORK, "sample3.pdf")

SITECUSTOMIZE = '''\
import concurrent.futures
import time


def _install():
    try:
        import ocrmypdf.api as api
    except Exception:
        return

    def fake(pdf, out, **kw):
        open({marker!r}, "w").write("1")
        # ocrmypdf runs its pages on its OWN ThreadPoolExecutor, whose workers
        # are NON-daemon: interpreter exit joins them, which is what keeps the
        # process alive after uvicorn has already logged "Finished server
        # process".  A request that is in flight (a slow model call) therefore
        # outlives the shutdown.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        futures = [pool.submit(time.sleep, 600) for _ in range(2)]
        for future in futures:
            future.result()

    api._pdf_to_hocr = fake


_install()
'''


def make_pdf(path: str) -> None:
    if os.path.exists(path):
        return
    from fpdf import FPDF
    pdf = FPDF()
    for _ in range(3):
        pdf.add_page()
    pdf.output(path)


def multipart(path: str, boundary: str) -> bytes:
    with open(path, "rb") as fh:
        data = fh.read()
    return b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="files"; filename="sample3.pdf"\r\n',
        b"Content-Type: application/pdf\r\n\r\n",
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ])


def main() -> int:
    signame = "SIGINT"
    for arg in sys.argv[1:]:
        if arg.startswith("--signal="):
            signame = arg.split("=", 1)[1].upper()

    os.makedirs(WORK, exist_ok=True)
    make_pdf(PDF)
    if os.path.exists(MARKER):
        os.unlink(MARKER)

    site = tempfile.mkdtemp(prefix="exit-hang-site-")
    with open(os.path.join(site, "sitecustomize.py"), "w") as fh:
        fh.write(SITECUSTOMIZE.format(marker=MARKER))
    env = dict(os.environ)
    env["PYTHONPATH"] = site + os.pathsep + ROOT
    env["PYTHONUNBUFFERED"] = "1"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # Exactly the documented dev command.
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn",
                             "backend.main:app", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    print(f"=== uvicorn backend.main:app (pid {proc.pid}) ===")

    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            if proc.poll() is not None:
                print("child died early:\n", proc.stdout.read())
                return 2
            time.sleep(0.3)
    else:
        print("server never came up")
        proc.kill()
        return 2
    print(f"server up on {base}")

    req = urllib.request.Request(
        base + "/api/ocr/upload", data=multipart(PDF, "----repro"),
        headers={"Content-Type": "multipart/form-data; boundary=----repro"})
    with urllib.request.urlopen(req, timeout=30) as r:
        job = json.loads(r.read())
    print("job started:", job.get("job_id"))

    deadline = time.time() + 20
    while time.time() < deadline and not os.path.exists(MARKER):
        time.sleep(0.2)
    print("OCR phase running:", os.path.exists(MARKER))
    time.sleep(4)                      # let the run get properly under way

    t0 = time.time()
    print(f"sending {signame}")
    proc.send_signal(getattr(signal, signame))
    try:
        rc = proc.wait(timeout=10)
        print(f"RESULT: exited after {time.time() - t0:.1f}s (rc={rc})")
    except subprocess.TimeoutExpired:
        # The user's report: uvicorn stopped, the process did not.  A second
        # signal is what finally killed it.
        print(f"*** STILL RUNNING {time.time() - t0:.1f}s after one {signame} "
              f"(uvicorn already stopped?) — sending a SECOND one ***")
        try:
            proc.send_signal(getattr(signal, signame))
            rc = proc.wait(timeout=10)
            print(f"RESULT: exited after a SECOND {signame}, "
                  f"{time.time() - t0:.1f}s total (rc={rc})")
        except subprocess.TimeoutExpired:
            print(f"RESULT: *** HUNG even after two signals ***")
            proc.send_signal(signal.SIGKILL)
            proc.wait()
    finally:
        out = proc.stdout.read()
        print("--- child output (tail) ---")
        print("\n".join(out.splitlines()[-14:]))
        shutil.rmtree(site, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
