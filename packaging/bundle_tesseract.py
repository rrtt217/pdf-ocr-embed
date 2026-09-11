#!/usr/bin/env python3
"""Stage a self-contained Tesseract so the packaged app needs no install.

OCRmyPDF locates ``tesseract`` (and its ``tessdata``) at runtime, so bundling
means copying the program + its shared libraries + the language data into a
staging directory that ``packaging/pdf_ocr_embed.spec`` ships as
``_internal/tesseract/``.  ``backend.bundled_tools`` then puts that directory on
``PATH`` / ``LD_LIBRARY_PATH`` and points ``TESSDATA_PREFIX`` at it.

    python packaging/bundle_tesseract.py                 # eng + chi_sim
    python packaging/bundle_tesseract.py --langs eng
    python packaging/bundle_tesseract.py --clean

Only the Unlimited API engine needs no local Tesseract; this is what makes the
*Tesseract (local)* engine work on a machine that has nothing installed.

Layout produced::

    packaging/tesseract-staging/
    ├── bin/tesseract[.exe]     (+ *.dll on Windows)
    ├── lib/lib*.so*            (Linux/macOS only)
    └── tessdata/
        ├── eng.traineddata  chi_sim.traineddata
        ├── configs/         (hocr, txt, pdf — OCRmyPDF requires these)
        └── tessconfigs/
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "packaging" / "tesseract-staging"
DEFAULT_LANGS = ("eng", "chi_sim")

# Language data source when a language is not installed locally.
TESSDATA_FAST_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{lang}.traineddata"

# The C runtime / loader must always come from the host — bundling glibc is the
# classic way to produce a binary that crashes on another machine.
_SYSTEM_LIB_RE = re.compile(
    r"^(libc|libm|libdl|libpthread|librt|libutil|libnsl|libresolv|libcrypt"
    r"|ld-linux|ld-linux-x86-64|ld-musl)[.-]"
)
_NEVER_BUNDLE = {"linux-vdso.so.1", "libc.so.6", "libm.so.6", "libdl.so.2",
                 "libpthread.so.0", "librt.so.1", "ld-linux-x86-64.so.2"}


def log(message: str) -> None:
    print(f"[tesseract] {message}", flush=True)


# --- locating the host installation -----------------------------------------

def find_tesseract() -> Path:
    found = shutil.which("tesseract")
    if not found:
        sys.exit("[tesseract] no `tesseract` on PATH — install it first "
                 "(apt: tesseract-ocr, brew: tesseract, choco: tesseract)")
    return Path(found).resolve()


def find_tessdata(exe: Path) -> Path | None:
    """The host's tessdata directory, found next to the binary or on the disk."""
    candidates: list[Path] = [
        # Windows installers put it right next to tesseract.exe.
        exe.parent / "tessdata",
    ]
    # macOS (Homebrew) keeps it under share/tessdata next to the prefix.
    for base in (exe.parent.parent, exe.parent, Path("/usr"), Path("/usr/local"),
                 Path("/opt/homebrew")):
        candidates += [base / "share" / "tessdata",
                       base / "share" / "tesseract-ocr" / "tessdata"]
    if Path("/usr/share/tesseract-ocr").is_dir():
        candidates += sorted(Path("/usr/share/tesseract-ocr").glob("*/tessdata"))
    candidates.append(Path("/usr/share/tesseract/tessdata"))
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.traineddata")):
            return candidate
    return None


# --- shared-library closure --------------------------------------------------

def _ldd_closure(binary: Path) -> list[Path]:
    """Linux: every shared library the binary (transitively) needs.

    Paths are returned **unresolved**: the loader looks up the SONAME
    (``libtesseract.so.5.5``), which is usually a symlink to the real
    ``libtesseract.so.5.5.3``.  Copying the resolved target under its own name
    would leave the bundle unusable — the loader silently falls back to the
    system copy.
    """
    try:
        out = subprocess.run(["ldd", str(binary)], capture_output=True,
                             text=True, check=False).stdout
    except OSError as exc:
        log(f"ldd failed ({exc}); skipping the library closure")
        return []
    libs: list[Path] = []
    for line in out.splitlines():
        match = re.search(r"=>\s+(\S+)\s+\(", line) or \
            re.match(r"\s*(\S+\.so[^\s]*)\s+\(", line)
        if match:
            libs.append(Path(match.group(1)))
    return libs


def _otool_closure(binary: Path) -> list[Path]:
    """macOS: the dylibs the binary links against (non-system ones only).

    Kept unresolved for the same reason as :func:`_ldd_closure` — the install
    name the loader asks for is the symlink, not its target.
    """
    try:
        out = subprocess.run(["otool", "-L", str(binary)], capture_output=True,
                             text=True, check=False).stdout
    except OSError as exc:
        log(f"otool failed ({exc}); skipping the library closure")
        return []
    libs: list[Path] = []
    for line in out.splitlines()[1:]:
        path = line.strip().split(" (")[0]
        if not path.startswith("/"):
            continue
        if path.startswith(("/usr/lib/", "/System/")):
            continue          # provided by the OS
        if Path(path).exists():
            libs.append(Path(path))
    return libs


def _bundleable(lib: Path) -> bool:
    name = lib.name
    if name in _NEVER_BUNDLE:
        return False
    return not _SYSTEM_LIB_RE.match(name)


# --- language data -----------------------------------------------------------

def _download_lang(lang: str, dest: Path) -> bool:
    url = TESSDATA_FAST_URL.format(lang=lang)
    try:
        log(f"downloading {lang}.traineddata (tessdata_fast)")
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"  could not download {lang}: {exc}")
        return False
    if len(data) < 1024:
        log(f"  {lang}: suspiciously small download, ignoring")
        return False
    dest.write_bytes(data)
    return True


def collect_tessdata(dest: Path, langs: Iterable[str],
                     host: Path | None, keep_configs_from: Path | None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for lang in langs:
        target = dest / f"{lang}.traineddata"
        source = (host / target.name) if host else None
        if source and source.is_file():
            shutil.copy2(source, target)
            log(f"copied {target.name} from {host}")
        elif not _download_lang(lang, target):
            log(f"WARNING: language '{lang}' is missing from the bundle")
    # OCRmyPDF runs tesseract with the `hocr`/`txt`/`pdf` configs, so the
    # configs/ directory is mandatory, not optional.
    for name in ("configs", "tessconfigs"):
        source = (keep_configs_from or host)
        if source and (source / name).is_dir():
            shutil.rmtree(dest / name, ignore_errors=True)
            shutil.copytree(source / name, dest / name)
            log(f"copied {name}/")


# --- per-platform bundling ---------------------------------------------------

def bundle_posix(exe: Path, out: Path, use_otool: bool) -> None:
    (out / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe, out / "bin" / exe.name)
    os.chmod(out / "bin" / exe.name, 0o755)

    libs = _otool_closure(exe) if use_otool else _ldd_closure(exe)
    lib_dir = out / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    seen: set[str] = set()
    for lib in libs:
        if not _bundleable(lib) or lib.name in seen:
            continue
        seen.add(lib.name)
        target = lib_dir / lib.name          # keep the SONAME (symlink) name
        try:
            shutil.copy2(os.path.realpath(lib), target)   # content behind it
            os.chmod(target, 0o755)
            copied += 1
        except OSError as exc:
            log(f"  could not copy {lib}: {exc}")
    log(f"bundled {copied} shared librar{'y' if copied == 1 else 'ies'}")


def bundle_windows(exe: Path, out: Path) -> None:
    """Copy the whole Tesseract install directory (exe + DLLs + tessdata).

    The Windows installer ships a self-contained tree, so copying it is both
    simpler and safer than tracing DLL imports by hand.
    """
    (out / "bin").mkdir(parents=True, exist_ok=True)
    source_dir = exe.parent
    copied = 0
    for item in source_dir.iterdir():
        if item.suffix.lower() in (".exe", ".dll"):
            shutil.copy2(item, out / "bin" / item.name)
            copied += 1
    log(f"copied {copied} executable/DLL file(s) from {source_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--langs", default=",".join(DEFAULT_LANGS),
                        help="comma-separated language codes (default: eng,chi_sim)")
    parser.add_argument("--clean", action="store_true",
                        help="remove the staging directory first")
    parser.add_argument("--require", action="store_true",
                        help="exit non-zero when bundling is not possible")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]

    if args.clean:
        shutil.rmtree(out, ignore_errors=True)

    try:
        exe = find_tesseract()
    except SystemExit as exc:
        if args.require:
            raise
        log(str(exc))
        log("skipping tesseract bundling — the app will need a system tesseract")
        return 0

    log(f"host tesseract: {exe}")
    system = platform.system()
    if system == "Windows":
        bundle_windows(exe, out)
    elif system == "Darwin":
        bundle_posix(exe, out, use_otool=True)
    else:
        bundle_posix(exe, out, use_otool=False)
    collect_tessdata(out / "tessdata", langs, find_tessdata(exe), find_tessdata(exe))

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    log(f"staged into {out} ({total / 1048576:.1f} MB)")
    for lang in langs:
        if not (out / "tessdata" / f"{lang}.traineddata").exists():
            log(f"WARNING: {lang}.traineddata was not staged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
