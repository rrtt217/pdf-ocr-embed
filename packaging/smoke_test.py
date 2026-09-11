#!/usr/bin/env python3
"""Smoke-test a built desktop bundle.

Starts the frozen executable headless, checks that the UI and API answer, that
the plugin is importable, then quits it through the real Quit endpoint and
asserts a clean exit.  Used by CI and handy locally::

    python packaging/smoke_test.py
    python packaging/smoke_test.py --exe dist/pdf-ocr-embed/pdf-ocr-embed

Cross-platform (Linux/macOS/Windows) and dependency-light: stdlib only, so it
runs before anything else is installed.  Exits non-zero with the captured log
on failure.
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_NAME = "pdf-ocr-embed"
EXE_NAMES = (f"{BUNDLE_NAME}.exe", BUNDLE_NAME)
_URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")


def find_executable(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            sys.exit(f"smoke: executable not found: {path}")
        return path
    for name in EXE_NAMES:
        candidate = ROOT / "dist" / BUNDLE_NAME / name
        if candidate.exists():
            return candidate
    sys.exit(f"smoke: no built executable under {ROOT / 'dist' / BUNDLE_NAME} "
             f"(run `python packaging/build.py` first)")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get(url: str, timeout: float = 5.0):
    """GET that returns (status, body_bytes) instead of raising on 4xx/5xx."""
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post(url: str, headers: dict[str, str], timeout: float = 5.0):
    request = urllib.request.Request(url, data=b"", method="POST",
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", default=None, help="path to the built binary")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds to wait for the server to come up")
    args = parser.parse_args(argv)

    exe = find_executable(args.exe)
    print(f"smoke: testing {exe}")

    workdir = Path(tempfile.mkdtemp(prefix="pdf-ocr-embed-smoke-"))
    log_path = workdir / "app.log"
    env = dict(os.environ)
    # Keep the run self-contained on Linux; macOS/Windows use their own
    # per-user dirs, which are ephemeral on a CI runner.
    env.update({
        "XDG_DATA_HOME": str(workdir / "data"),
        "XDG_CONFIG_HOME": str(workdir / "config"),
        "XDG_STATE_HOME": str(workdir / "state"),
    })

    port = free_port()
    failures: list[str] = []

    def fail(message: str) -> None:
        failures.append(message)

    with log_path.open("wb") as log_file:
        proc = subprocess.Popen([str(exe), "--no-window", "--port", str(port)],
                                stdout=log_file, stderr=subprocess.STDOUT,
                                env=env, cwd=str(workdir))
        try:
            url = f"http://127.0.0.1:{port}/"
            deadline = time.time() + args.timeout
            started = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    fail(f"process exited early (code {proc.returncode})")
                    break
                try:
                    status, body = get(url + "api/health", timeout=2.0)
                except Exception:  # noqa: BLE001 - not up yet
                    time.sleep(0.3)
                    continue
                if status == 200 and b'"status"' in body:
                    started = True
                    break
                time.sleep(0.3)

            if started:
                print("smoke: server is up, checking endpoints")
                # 1. the frontend is actually bundled
                status, body = get(url)
                if status != 200 or b"<html" not in body.lower():
                    fail(f"GET / -> {status} ({len(body)} bytes)")
                status, _ = get(url + "static/app.js")
                if status != 200:
                    fail(f"GET /static/app.js -> {status}")
                # 2. the plugin imported => the unlimited engine is available
                status, body = get(url + "api/health")
                compact = body.replace(b" ", b"")
                if b'"desktop":true' not in compact:
                    fail("health does not report desktop mode")
                if b'"unlimited"' not in body:
                    fail("health does not list the unlimited engine")
                # 3. quitting is guarded, then works
                status, _ = post(url + "api/app/quit", {})
                if status != 403:
                    fail(f"quit without header -> {status} (expected 403)")
                status, _ = post(url + "api/app/quit",
                                 {"X-PDF-OCR-Embed": "quit"})
                if status != 200:
                    fail(f"quit with header -> {status} (expected 200)")

                # the process must go away on its own
                try:
                    code = proc.wait(timeout=20)
                    if code != 0:
                        fail(f"exit code {code} (expected 0)")
                except subprocess.TimeoutExpired:
                    fail("did not exit within 20s of the quit request")
            elif not failures:
                fail(f"server did not come up within {args.timeout:.0f}s")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    if failures:
        print("smoke: FAILED")
        for message in failures:
            print(f"  - {message}")
        print(f"smoke: --- {log_path} ---")
        print(log_path.read_text(errors="replace")[-4000:])
        return 1
    print("smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
