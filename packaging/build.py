#!/usr/bin/env python3
"""Build the pdf-ocr-embed desktop app with PyInstaller (cross-platform).

    python packaging/build.py --clean

Produces an onedir bundle in ``dist/pdf-ocr-embed/``; run the ``pdf-ocr-embed``
executable inside it.  Build on each target OS separately — PyInstaller is not
a cross-compiler.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "pdf_ocr_embed.spec"
BUNDLE_NAME = "pdf-ocr-embed"   # must match the EXE/COLLECT name in the spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clean", action="store_true",
                        help="delete build/ and dist/ before building")
    parser.add_argument("--distpath", default=str(ROOT / "dist"))
    parser.add_argument("--workpath", default=str(ROOT / "build"))
    args = parser.parse_args(argv)

    if args.clean:
        for folder in (Path(args.distpath), Path(args.workpath)):
            shutil.rmtree(folder, ignore_errors=True)

    # NOTE: no --name here — PyInstaller rejects makespec options (including
    # --name) when a .spec file is given.  The bundle name lives in the spec.
    cmd = [
        sys.executable, "-m", "PyInstaller", str(SPEC),
        "--noconfirm",
        "--distpath", args.distpath,
        "--workpath", args.workpath,
    ]
    print("+", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc == 0:
        print(f"\nBuilt: {Path(args.distpath) / BUNDLE_NAME}")
        print(f"Run:   {Path(args.distpath) / BUNDLE_NAME / BUNDLE_NAME}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
