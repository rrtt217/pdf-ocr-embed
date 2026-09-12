"""Reproduce: can the desktop app exit while an OCR job is running?

Runs the REAL desktop entry point (`desktop.py --no-window`) in a child
process, patches OCRmyPDF inside that child so the OCR phase blocks like a long
real run, starts a job over HTTP, then POSTs /api/app/quit (exactly what the
WebUI Quit button does) and measures how long the process takes to exit — or
whether it exits at all.

Expected today (see tests/test_exit_shutdown.py for the automated version):
"RESULT: exited after ~5s (rc=0)".  Before the fix this printed
"*** STILL RUNNING after 30.0s — HANG ***": the job's worker thread was
non-daemon, so the interpreter joined it at exit while it slept.

Usage:
    .venv/bin/python research/exit-hang/repro_quit.py [--with-sse] [--signal]

    --with-sse   also hold an EventSource stream open (the WebUI always has one)
    --signal     send SIGTERM instead of using the Quit endpoint
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
QUIT_HEADERS = {"X-PDF-OCR-Embed": "quit"}

SITECUSTOMIZE = '''\
import time
def _install():
    try:
        import ocrmypdf.api as api
    except Exception:
        return
    def fake(pdf, out, **kw):
        open({marker!r}, "w").write("1")
        time.sleep(600)          # stand in for a long OCR run
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


def wait_for_server(base: str, proc: subprocess.Popen, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            if proc.poll() is not None:
                print("child died early:\n", proc.stdout.read())
                return False
            time.sleep(0.3)
    return False


def main() -> int:
    with_sse = "--with-sse" in sys.argv
    use_signal = "--signal" in sys.argv

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    make_pdf(PDF)

    # Patch OCRmyPDF only inside the child, via its own PYTHONPATH.
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

    argv = [sys.executable, "desktop.py", "--no-window", "--port", str(port)]
    proc = subprocess.Popen(argv, cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    base = f"http://127.0.0.1:{port}"
    print(f"=== desktop.py --no-window (pid {proc.pid}) ===")
    if not wait_for_server(base, proc):
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
    if not os.path.exists(MARKER):
        print("*** the OCR phase never started — upload path changed? ***")
    else:
        print("OCR phase is running (blocking, as a long run would)")

    sse = None
    if with_sse:
        sse = socket.create_connection(("127.0.0.1", port))
        sse.sendall(f"GET /api/ocr/stream/{job['job_id']} HTTP/1.1\r\n"
                    "Host: x\r\nAccept: text/event-stream\r\n\r\n".encode())
        time.sleep(1.0)
        print("SSE stream open:", sse.recv(60)[:25])

    t0 = time.time()
    if use_signal:
        print("sending SIGTERM")
        proc.send_signal(signal.SIGTERM)
    else:
        req = urllib.request.Request(base + "/api/app/quit", data=b"",
                                     headers=QUIT_HEADERS, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                print("quit response:", r.status, r.read().decode())
        except Exception as exc:
            print("quit request failed:", exc)

    try:
        rc = proc.wait(timeout=30)
        print(f"RESULT: exited after {time.time() - t0:.1f}s (rc={rc})")
    except subprocess.TimeoutExpired:
        print(f"RESULT: *** STILL RUNNING after {time.time() - t0:.1f}s — HANG ***")
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    finally:
        if sse:
            sse.close()
        out = proc.stdout.read()
        print("--- child log (tail) ---")
        print("\n".join(out.splitlines()[-18:]))
        shutil.rmtree(site, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
