"""Headless CLI entry point: ``python -m backend.cli``.

Reuses the SAME backend logic as the WebUI (``backend.ocr_service``) with no
HTTP server running.  It reads a PDF from disk, runs OCRmyPDF with the
``unlimited`` plugin engine (optionally a selected page range), and produces
``<stem>_embedded_<id>.pdf`` under ``output/``.

Config is resolved through ``backend.config.resolve()`` — no direct
``os.environ`` reads, no hardcoded keys.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from backend import ocr_service, paths, pdf_processing
from backend.errors import UnavailableError
from backend.logging_config import setup_logging

log = logging.getLogger(__name__)

DEFAULT_ENGINE = "unlimited"


def _push_plugin_config() -> None:
    """Push the effective config into the standalone plugin's settings store.

    The plugin reads its own store, not backend.config; a no-op when the
    plugin is not installed (the same contract as ocr_service/main.py).
    """
    try:
        from ocrmypdf_unlimited import settings as ocrmypad_settings
    except ImportError:
        return
    from backend.config import resolve as resolve_config
    ocrmypad_settings.configure(resolve_config())


def parse_pages(spec: Optional[str], num_pages: int) -> List[int]:
    """Parse a 1-based, inclusive page-range spec into sorted 0-based indices.

    Supports single pages (``1``), comma-separated mixes (``1,3,5``),
    inclusive ranges (``1-3``) and open-ended ranges (``1-``, ``-3``).
    Out-of-range bounds are clamped to the document (``1..num_pages``).
    Returns a de-duplicated, ascending list of 0-based indices.

    ``spec=None`` (the ``--pages`` flag was omitted) returns every page.

    Raises ``ValueError`` for an empty/invalid spec so the CLI can exit 1.
    """
    if spec is None:
        return list(range(num_pages))
    text = str(spec).strip()
    if not text:
        raise ValueError("empty page spec")

    selected: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty element in page spec {spec!r}")
        if "-" in part:
            left, _, right = part.partition("-")
            lo_s, hi_s = left.strip(), right.strip()
            lo = int(lo_s) if lo_s else 1
            hi = int(hi_s) if hi_s else num_pages
            if lo > hi:
                raise ValueError(
                    f"range {part!r} is reversed (start {lo} > end {hi})")
            selected.extend(range(lo, hi + 1))
        else:
            selected.append(int(part))

    # Clamp every 1-based value into the valid document range (inclusive).
    clamped = [max(1, min(num_pages, p)) for p in selected]
    # De-duplicate and convert to 0-based ascending.
    return sorted({p - 1 for p in clamped})


def _pages_arg(pages_0based: List[int], num_pages: int) -> Optional[str]:
    """Format a 0-based index list as an ocrmypdf 1-based ``pages`` string."""
    if not pages_0based or len(pages_0based) == num_pages:
        return None  # all pages: let ocrmypdf run the full document
    return ",".join(str(i + 1) for i in pages_0based)


def main(argv: Optional[List[str]] = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="OCR a (scanned) PDF with OCRmyPDF + the unlimited "
                    "plugin engine; embeds a searchable invisible text layer.")
    parser.add_argument("input", help="input PDF path")
    parser.add_argument("-o", "--output",
                        help="output PDF path (default: output/<stem>_embedded_<id>.pdf)")
    parser.add_argument("--pages", default=None,
                        help="1-based pages: '1', '1,3,5', '1-3', open '1-'")
    parser.add_argument("--engine", default=DEFAULT_ENGINE,
                        help=f"ocr_engine: {DEFAULT_ENGINE} (default) | tesseract | none")
    parser.add_argument("--jobs", type=int, default=None,
                        help="worker count (default: config ocrmypdf_jobs, else auto)")
    parser.add_argument("--sidecar-text", action="store_true",
                        help="print the recognized text to stdout as JSON pages")
    parser.add_argument("--export", default=None, metavar="FMT",
                        help="export the recognized pages instead of embedding "
                             "a text layer: markdown | latex (the sidecars' "
                             "raw content is used when present)")
    parser.add_argument("--no-reflow", action="store_true",
                        help="skip the deterministic export reflow (the "
                             "sidecars' line-split structure is kept)")
    parser.add_argument("--export-llm", action="store_true",
                        help="run BOTH export LLM post-processing steps "
                             "(block fix-up + heading refinement); needs the "
                             "configured provider api_key")
    parser.add_argument("--export-llm-blocks", action="store_true",
                        help="run the LLM block fix-up only (tables / "
                             "equations / low-conf)")
    parser.add_argument("--export-llm-outline", action="store_true",
                        help="run the LLM heading/outline refinement only "
                             "(uses the detected table of contents)")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"error: input not found: {input_path}", file=sys.stderr)
        return 1
    try:
        num_pages = pdf_processing.page_count(str(input_path))
    except Exception as exc:  # noqa: BLE001
        print(f"error: cannot open PDF: {exc}", file=sys.stderr)
        return 1

    try:
        pages_0based = parse_pages(args.pages, num_pages)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_dir = paths.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = (Path(args.output) if args.output else
                   out_dir / f"{input_path.stem}_embedded_{uuid.uuid4().hex[:8]}.pdf")

    import ocrmypdf.api
    overrides = {"ocr_engine": args.engine}
    if args.jobs:
        overrides["jobs"] = args.jobs
    pages_arg = _pages_arg(pages_0based, num_pages)

    # Export-only mode (pure non-embed use): run the OCR phase, render the
    # recognized pages as markdown/LaTeX and stop — no text layer is embedded.
    if args.export:
        from backend import export as export_mod

        if args.export.strip().lower() not in ("markdown", "md", "latex", "tex"):
            print("error: --export must be 'markdown' or 'latex'", file=sys.stderr)
            return 1

        work_dir = out_dir.parent / "work" / f"cli-export-{uuid.uuid4().hex[:8]}"
        hocr_dir = work_dir / "hocr"
        hocr_dir.mkdir(parents=True, exist_ok=True)

        _push_plugin_config()
        plugins = [ocr_service.plugin_path()] if ocr_service.plugin_path() else []
        if args.engine == "unlimited" and not ocr_service.plugin_available():
            print("error: the 'unlimited' OCR engine is not available: the "
                  "standalone ocrmypdf-unlimited plugin is not installed "
                  "(choose --engine tesseract, or install the plugin).",
                  file=sys.stderr)
            return 1
        try:
            ocrmypdf.api._pdf_to_hocr(
                input_path, hocr_dir,
                plugins=plugins,
                **ocr_service._ocrmypdf_options(**overrides),
                **({"pages": pages_arg} if pages_arg else {}),
            )
        except UnavailableError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            from backend.config import redact_secrets
            print(f"error: {redact_secrets(str(exc))}", file=sys.stderr)
            return 1

        pages = []
        for sidecar in sorted(hocr_dir.glob("*_ocr_hocr.blocks.json")):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                page = data.get("page") or data
                if page:
                    pages.append(page)
            except (OSError, ValueError):
                continue
        if not pages:
            print("error: OCR produced no pages to export", file=sys.stderr)
            return 1
        fmt_norm = ("markdown" if args.export.strip().lower()
                    in ("markdown", "md") else "latex")
        use_llm_blocks = args.export_llm_blocks or args.export_llm
        use_llm_outline = args.export_llm_outline or args.export_llm
        if not args.no_reflow or use_llm_blocks or use_llm_outline:
            # Deterministic reflow (default on) + the two opt-in LLM steps:
            # both run on a deep copy; the per-job LLM cache lives in the
            # work folder.
            from backend import config as config_mod, export_llm
            pages = export_llm.preprocess(
                pages, config_mod.resolve(), fmt=fmt_norm,
                enable_blocks=bool(use_llm_blocks),
                enable_outline=bool(use_llm_outline),
                reflow=not args.no_reflow,
                cache_path=work_dir / "export_llm_cache.json")
        export_ext = "md" if fmt_norm == "markdown" else "tex"
        export_path = (Path(args.output) if args.output else
                       out_dir / f"{input_path.stem}.{export_ext}")
        try:
            text = export_mod.export_document(
                fmt_norm, pages,
                title=input_path.stem,
                use_raw=True)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        export_path.write_text(text, encoding="utf-8")
        print(f"ok: {export_path}")
        return 0

    work_dir = out_dir.parent / "work" / f"cli-{uuid.uuid4().hex[:8]}"
    hocr_dir = work_dir / "hocr"
    hocr_dir.mkdir(parents=True, exist_ok=True)

    # The plugin reads its own settings store, not backend.config: push the
    # effective config in before the pipeline runs (a no-op when the
    # standalone plugin is not installed).
    _push_plugin_config()

    plugins = [ocr_service.plugin_path()] if ocr_service.plugin_path() else []
    if args.engine == "unlimited" and not ocr_service.plugin_available():
        print("error: the 'unlimited' OCR engine is not available: the "
              "standalone ocrmypdf-unlimited plugin is not installed "
              "(choose --engine tesseract, or install the plugin).",
              file=sys.stderr)
        return 1

    try:
        # Phase 1: OCR -> per-page hOCR + block sidecars (plugin engine runs).
        ocrmypdf.api._pdf_to_hocr(
            input_path, hocr_dir,
            plugins=plugins,
            **ocr_service._ocrmypdf_options(**overrides),
            **({"pages": pages_arg} if pages_arg else {}),
        )
        # Phase 2: (possibly edited) hOCR -> final PDF with the text layer.
        ocrmypdf.api._hocr_to_ocr_pdf(
            hocr_dir, output_path,
            use_threads=True,
        )
    except UnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        from backend.config import redact_secrets
        print(f"error: {redact_secrets(str(exc))}", file=sys.stderr)
        return 1

    if not output_path.exists():
        print("error: OCR produced no output", file=sys.stderr)
        return 1

    print(f"ok: {output_path}")
    if args.sidecar_text:
        pages = []
        for sidecar in sorted(hocr_dir.glob("*_ocr_hocr.blocks.json")):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                pages.append(data.get("page") or data)
            except (OSError, ValueError):
                continue
        print(json.dumps(pages, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
