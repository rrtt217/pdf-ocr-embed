"""Reproduce: does the PLAIN server (`backend.main.run`) exit on Ctrl-C?

This is the non-desktop mode: no lifecycle quit flag, no quit endpoint, just
`python -m backend.main`.  Ctrl-C is a real SIGINT here, exactly like the
console user pressed it.  Reported symptom: uvicorn shuts down, then the
process hangs in `concurrent.futures.thread._python_exit` (a non-daemon OCR
worker thread is joined at interpreter exit) until Ctrl-C is pressed again.

Usage: .venv/bin/python research/exit-hang/repro_ctrlc.py [--signal=SIGINT|SIGTERM]
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


def main() -> int:
    signame = "SIGINT"
    for arg in sys.argv[1:]:
        if arg.startswith("--signal="):
            signame = arg.split("=", 1)[1].upper()
    sig = getattr(signal, signame)

    os.makedirs(WORK, exist_ok=True)
    make_pdf(PDF)
    site = tempfile.mkdtemp(prefix="exit-hang-site-")
    with open(os.path.join(site, "sitecustomize.py"), "w") as fh:
        fh.write(SITECUSTOMIZE.format(marker=MARKER))
    if os.path.exists(MARKER):
        os.unlink(MARKER)

    env = dict(os.environ)
    env["PYTHONPATH"] = site + os.pathsep + ROOT
    env["PYTHONUNBUFFERED"] = "1"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # The documented way to run the server in the foreground.
    proc = subprocess.Popen([sys.executable, "-m", "backend.main",
                             "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    print(f"=== python -m backend.main (pid {proc.pid}) ===")

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

    t0 = time.time()
    print(f"sending {signame}")
    proc.send_signal(sig)
    try:
        rc = proc.wait(timeout=25)
        print(f"RESULT: exited after {time.time() - t0:.1f}s (rc={rc})")
    except subprocess.TimeoutExpired:
        print(f"RESULT: *** STILL RUNNING after {time.time() - t0:.1f}s — HANG ***")
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    finally:
        out = proc.stdout.read()
        print("--- child output (tail) ---")
        print("\n".join(out.splitlines()[-20:]))
        shutil.rmtree(site, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
