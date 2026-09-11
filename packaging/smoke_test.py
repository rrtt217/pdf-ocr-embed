#!/usr/bin/env python3
"""Smoke-test a built desktop bundle.

Starts the frozen executable headless, checks that the UI and API answer, that
the plugin is importable, then quits it through the real Quit endpoint and
asserts a clean exit.  With ``--ocr`` it also runs a **real OCR job** through
the app's local Tesseract and asserts the produced PDF has a text layer.  Used
by CI and handy locally::

    python packaging/smoke_test.py
    python packaging/smoke_test.py --expect-tesseract --ocr
    python packaging/smoke_test.py --exe dist/pdf-ocr-embed/pdf-ocr-embed

Cross-platform (Linux/macOS/Windows) and dependency-light: the base checks are
stdlib only, so it runs before anything else is installed.  ``--ocr`` lazily
imports fpdf2 / pypdfium2 / Pillow (all normal app dependencies) to build its
scanned-page fixture.  Exits non-zero with the captured log on failure.
"""
from __future__ import annotations

import argparse
import json
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

# Short, OCR-friendly English text for the end-to-end run.
OCR_FIXTURE_TEXT = "The quick brown fox 12345"
OCR_EXPECTED = "quick"


def _post_multipart(url: str, fields: dict[str, str], filename: str,
                    payload: bytes, timeout: float = 120.0):
    """POST a multipart/form-data body with the stdlib (no httpx needed)."""
    boundary = "----pdf-ocr-embed-smoke-boundary"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode())
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
        f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'
        .encode())
    chunks.append(payload)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(chunks)

    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _scanned_pdf(path: Path, text: str) -> Path:
    """A single-page, **image-only** PDF whose pixels spell ``text``.

    Laid out with fpdf2 (core font), rasterized with pypdfium2 and re-wrapped
    with Pillow — so the file carries no text layer at all and the OCR run has
    to produce one.  Imported lazily: this is the only check needing more than
    the standard library.
    """
    from fpdf import FPDF
    import pypdfium2 as pdfium
    from PIL import Image

    doc = FPDF(unit="pt", format=(600.0, 200.0))
    doc.set_auto_page_break(False)
    doc.add_page()
    doc.set_font("helvetica", size=30)
    doc.text(40, 110, text)
    rendered = bytes(doc.output())

    source = pdfium.PdfDocument(rendered)
    bitmap = source[0].render(scale=200 / 72)      # ~200 dpi, OCR-friendly
    bitmap.to_pil().convert("RGB").save(path, format="PDF", resolution=200.0)
    return path


def check_ocr_pipeline(url: str, workdir: Path, timeout: float,
                       label: str = "tesseract") -> list[str]:
    """Run a real OCR job through the frozen app and assert a text layer.

    This is what proves the whole chain on the target OS: upload -> OCRmyPDF ->
    tesseract -> hOCR -> invisible text layer -> downloadable searchable PDF.
    The earlier checks only show the pieces are present.
    """
    import pypdfium2 as pdfium            # already a hard app dependency

    failures: list[str] = []
    fixture = _scanned_pdf(workdir / "scan.pdf", OCR_FIXTURE_TEXT)
    print(f"smoke: running a real OCR job ({label} engine)")

    status, body = _post_multipart(
        url + "api/ocr/upload",
        {"ocr_engine": label, "lang": "eng", "page_start": "1", "page_end": "1"},
        fixture.name, fixture.read_bytes())
    if status != 200:
        return [f"OCR upload -> {status}: {body[:200]!r}"]
    job_id = json.loads(body)["job_id"]

    state = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = get(url + "api/jobs")
        listed = json.loads(body)
        listed = listed if isinstance(listed, list) else listed.get("jobs", [])
        job = next((j for j in listed if j.get("job_id") == job_id), None)
        state = (job or {}).get("status")
        if state in ("done", "error", "stopped"):
            break
        time.sleep(2)
    if state != "done":
        return [f"OCR job {job_id} ended as {state!r} "
                f"(did not finish within {timeout:.0f}s)"]
    print(f"smoke: OCR job {job_id} finished")

    request = urllib.request.Request(
        url + f"api/embed/{job_id}",
        data=json.dumps({"job_id": job_id}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as resp:
            embed_status = resp.status
    except urllib.error.HTTPError as exc:
        embed_status = exc.code
    if embed_status != 200:
        return [f"embed -> {embed_status}"]

    status, pdf_bytes = get(url + f"api/download/{job_id}.pdf", timeout=300)
    if status != 200 or len(pdf_bytes) < 1000:
        return [f"download -> {status} ({len(pdf_bytes)} bytes)"]

    document = pdfium.PdfDocument(pdf_bytes)
    text = document[0].get_textpage().get_text_range()
    print(f"smoke: text layer has {len(text)} chars: {text.strip()[:80]!r}")
    if not text.strip():
        failures.append("embedded PDF has no text layer")
    elif OCR_EXPECTED not in text.lower():
        failures.append(f"text layer is missing {OCR_EXPECTED!r}: "
                        f"{text.strip()[:120]!r}")
    return failures


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

        if resolved.startswith(("@rpath/", "@loader_path/", "@executable_path/")):
            # macOS: relative install names are the *good* case — they are how a
            # relocatable bundle refers to its own libraries.  Just make sure we
            # actually ship the file (PyInstaller rewrites these at build time).
            if not (lib_dir / name).exists() and not (app_root / name).exists():
                problems.append(f"{name} is not shipped with the app: {resolved}")
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
    parser.add_argument("--ocr", action="store_true",
                        help="also run a real OCR job and assert the output PDF "
                             "has a text layer (needs a tesseract to run)")
    parser.add_argument("--ocr-timeout", type=float, default=240.0,
                        help="seconds to allow the OCR job (default: 240)")
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
                # 3. a real OCR run produces a searchable PDF
                if args.ocr:
                    failures.extend(check_ocr_pipeline(url, workdir,
                                                       args.ocr_timeout))
                # 4. quitting is guarded, then works
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
