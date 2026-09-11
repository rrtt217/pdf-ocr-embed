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


# Libraries the host must always provide (glibc and the loader).  Everything
# else tesseract needs has to come from inside the app, or the bundle is not
# actually self-contained.  Kept in sync with packaging/bundle_tesseract.py.
_SYSTEM_LIB_RE = re.compile(
    r"^(libc|libm|libdl|libpthread|librt|libutil|libnsl|libresolv|libcrypt"
    r"|ld-linux|ld-musl)[.-]"
)


def _is_host_library(name: str, resolved: str) -> bool:
    if sys.platform == "darwin":
        return resolved.startswith(("/usr/lib/", "/System/"))
    return bool(_SYSTEM_LIB_RE.match(name))


def _loader_resolution(app_root: Path, binary: Path, lib_dir: Path) -> list[str]:
    """Assert the loader takes every non-host library from inside the app.

    Executing the bundled tesseract is not enough on a build host: the system
    copies are still on the default search path, so a missing or mis-named
    bundled library would go unnoticed.  Ask the loader instead.
    """
    if os.name == "nt":
        # Windows keeps the DLLs next to the executable and finds them via
        # PATH; running it (below) is the meaningful check there.
        return []
    if not lib_dir.is_dir():
        return [f"bundled tesseract has no lib/ directory ({lib_dir})"]

    env = dict(os.environ)
    loader = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
    env[loader] = str(lib_dir)
    command = ["otool", "-L", str(binary)] if sys.platform == "darwin" \
        else ["ldd", str(binary)]
    try:
        out = subprocess.run(command, env=env, capture_output=True, text=True,
                             timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"could not inspect {binary.name}: {exc}"]

    problems: list[str] = []
    for line in out.splitlines():
        match = re.search(r"=>\s+(\S+)\s+\(", line)
        if match:                                    # Linux: name => path
            name, resolved = Path(match.group(1)).name, match.group(1)
        elif sys.platform == "darwin" and line.startswith("\t"):
            resolved = line.strip().split(" (")[0]    # macOS: install name
            name = Path(resolved).name
        else:
            continue
        if _is_host_library(name, resolved):
            continue
        if not resolved.startswith(str(app_root)):
            problems.append(f"{name} resolves outside the app: {resolved}")
    return problems


def check_bundled_tesseract(exe: Path) -> list[str]:
    """Prove the staged Tesseract is complete and actually runs.

    Checking ``/api/health`` only shows the directory was found.  This runs the
    real binary with only its bundled libraries and language data, and asks the
    loader whether it is really using them.
    """
    root = exe.parent / "_internal" / "tesseract"
    binary = root / "bin" / ("tesseract.exe" if os.name == "nt" else "tesseract")
    if not binary.is_file():
        return [f"bundled tesseract is missing at {binary}"]

    failures = _loader_resolution(exe.parent, binary, root / "lib")

    env = dict(os.environ)
    # Deliberately do NOT expose the host's tesseract or its libraries.
    env["PATH"] = str(binary.parent)
    env.pop("TESSDATA_PREFIX", None)
    lib_dir = root / "lib"
    if lib_dir.is_dir():
        loader = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        env[loader] = str(lib_dir)
    env["TESSDATA_PREFIX"] = str(root / "tessdata")

    try:
        result = subprocess.run([str(binary), "--list-langs"], env=env,
                                capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        failures.append(f"bundled tesseract did not run: {exc}")
        return failures
    if result.returncode != 0:
        failures.append(f"bundled tesseract --list-langs failed "
                        f"({result.returncode}): {result.stderr.strip()[:400]}")
        return failures
    languages = [line.strip() for line in result.stdout.splitlines()[1:]
                 if line.strip() and not line.startswith("List of")]
    print(f"smoke: bundled tesseract runs; languages: {languages}")
    if not languages:
        failures.append("bundled tesseract reported no languages")
    return failures


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
    parser.add_argument("--expect-tesseract", action="store_true",
                        help="fail unless the app reports a bundled tesseract")
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

    # Prove the staged Tesseract itself works, not just that its directory is
    # there (see check_bundled_tesseract).
    if args.expect_tesseract:
        failures += check_bundled_tesseract(exe)

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
                if args.expect_tesseract and b'"source":"bundled"' not in compact:
                    fail("no bundled tesseract (health did not report "
                         "'source: bundled')")
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
