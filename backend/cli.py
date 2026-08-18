"""Headless CLI entry point: ``python -m backend.cli``.

Reuses the SAME backend logic as the WebUI (``backend.ocr_service`` and
``backend.pdf_processing``) with no HTTP server running.  It reads a PDF from
disk, OCRs its pages (optionally a selected page range) and embeds an invisible
searchable text layer into ``<stem>_embedded_<id>.pdf`` under ``output/``.

Config is resolved through ``backend.config.resolve()`` — no direct
``os.environ`` reads, no hardcoded keys.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from backend import ocr_service, pdf_processing
from backend.sources.base import UnavailableError
from backend.sources import factory as factory_mod

log = logging.getLogger(__name__)

# The backend hard invariant that max_tokens must stay below 32768 (and be > 0).
MAX_TOKENS_LIMIT = 32768

# Default adapter mirrors the WebUI's default ("unlimited-ocr" / "unlimited").
DEFAULT_ADAPTER = "unlimited"


def _page_count(pdf_path: str) -> int:
    """Total number of pages in the source PDF (via the backend's own path)."""
    return pdf_processing.page_count(pdf_path)


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


def _page_text(page_dict: dict) -> str:
    """Flatten one stored page dict into a plain-text string for stdout."""
    lines = []
    for block in page_dict.get("blocks", []):
        text = block.get("text", "") or ""
        text = text.strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="Headless OCR + invisible-text embedding, reusing the "
                    "backend logic (backend.ocr_service / backend.pdf_processing).",
        epilog=(
            "Page selection happens in the CLI itself: create_job() then run_ocr() "
            "with only_missing=True, pre-filling skipped pages so only the "
            "requested range is rendered and OCR'd."
        ),
    )
    parser.add_argument("in_pdf", metavar="in.pdf",
                        help="Path to the source (scanned) PDF.")
    parser.add_argument("--adapter", default=DEFAULT_ADAPTER,
                        help=f"OCR adapter name (default: {DEFAULT_ADAPTER}). "
                             f"Use '--adapter list' to print available adapters.")
    parser.add_argument("--pages", default=None, metavar="SPEC",
                        help="1-based page range to OCR, e.g. '1-20', '1,3,5-7', "
                             "'1-' or '-5'. Omitting embeds all pages.")
    parser.add_argument("--concurrency", type=int, default=1, metavar="N",
                        help="Number of parallel OCR workers (default: 1).")
    parser.add_argument("--out", default="output", metavar="DIR",
                        help="Output directory for the embedded PDF (default: output/).")
    parser.add_argument("--no-embed", action="store_true",
                        help="OCR only: print per-page recognized text to stdout "
                             "instead of embedding.")
    parser.add_argument("--max-tokens", type=int, default=None, metavar="N",
                        help=f"Validation guard: must satisfy 0 < N < {MAX_TOKENS_LIMIT} "
                             f"(enforced by this CLI; the backend API exposes no "
                             f"max_tokens knob).")
    parser.add_argument("--json", action="store_true",
                        help="Print a final machine-readable JSON summary to stdout.")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.concurrency < 1:
        raise ValueError(f"--concurrency must be >= 1 (got {args.concurrency})")
    if args.max_tokens is not None and not (0 < args.max_tokens < MAX_TOKENS_LIMIT):
        raise ValueError(
            f"--max-tokens must satisfy 0 < N < {MAX_TOKENS_LIMIT} "
            f"(got {args.max_tokens})")


def _run(args: argparse.Namespace) -> int:
    in_pdf = str(args.in_pdf)

    # ``--adapter list`` prints the registry and exits before any file work.
    if args.adapter.strip().lower() == "list":
        for name in sorted(factory_mod._REGISTRY):
            print(name)
        return 0

    if not Path(in_pdf).is_file():
        print(f"error: input PDF not found: {in_pdf}", file=sys.stderr)
        return 1

    _validate_args(args)

    num_pages = _page_count(in_pdf)
    if num_pages <= 0:
        print(f"error: {in_pdf} has no pages", file=sys.stderr)
        return 1

    requested = parse_pages(args.pages, num_pages)
    if not requested:
        print("error: no pages selected after parsing "
              f"--pages {args.pages!r}", file=sys.stderr)
        return 1
    wanted = set(requested)

    # Robustly attempt the adapter up front so a missing engine / bad config
    # fails fast with a clear message before we start rendering pages.
    try:
        factory_mod.get_adapter(args.adapter)
    except UnavailableError as exc:
        print(f"error: adapter '{args.adapter}' unavailable: {exc}",
              file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        with open(in_pdf, "rb") as fh:
            file_bytes = fh.read()
        job = ocr_service.create_job(Path(in_pdf).name, file_bytes)
    except OSError as exc:
        print(f"error: cannot read {in_pdf}: {exc}", file=sys.stderr)
        return 1
    job_id = job["id"]
    log.info("job %s: %s, %d page(s), OCR pages %s",
             job_id, Path(in_pdf).name, num_pages,
             [i + 1 for i in sorted(wanted)])

    # Restrict OCR to the requested pages before it runs: pre-fill every
    # non-requested page with a placeholder so run_ocr(only_missing=True)
    # skips them (they are never rendered nor sent to the engine).
    for i in range(num_pages):
        if i not in wanted:
            ocr_service.update_page(job_id, i, {"_headless_skip": True})

    try:
        ocr_service.run_ocr(job_id, args.adapter, concurrency=args.concurrency,
                            only_missing=True)
    except UnavailableError as exc:
        print(f"error: adapter '{args.adapter}' unavailable: {exc}",
              file=sys.stderr)
        ocr_service.clear_job(job_id)
        return 1
    except RuntimeError as exc:
        log.debug("run_ocr raised", exc_info=True)
        ocr_service.clear_job(job_id)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s: OCR failed", job_id)
        err = job.get("error") or str(exc)
        print(f"error: OCR failed: {err}", file=sys.stderr)
        ocr_service.clear_job(job_id)
        return 1

    if job.get("status") == "error":
        print(f"error: OCR failed: {job.get('error')}", file=sys.stderr)
        ocr_service.clear_job(job_id)
        return 1
    if job.get("status") == "stopped":
        print("error: OCR stopped before completion", file=sys.stderr)
        ocr_service.clear_job(job_id)
        return 1

    # Pull per-page results only for the requested range.
    all_pages = ocr_service.get_pages(job_id)
    ocr_pages = [all_pages[i] for i in sorted(wanted)
                 if i < len(all_pages) and all_pages[i] is not None]
    missing = sorted(i for i in wanted
                     if i >= len(all_pages) or all_pages[i] is None)

    if missing:
        print(f"warning: {len(missing)} requested page(s) produced no text "
              f"(skipped): {[i + 1 for i in missing]}", file=sys.stderr)

    if args.no_embed:
        for i in sorted(wanted):
            if i < len(all_pages) and all_pages[i] is not None:
                text = _page_text(all_pages[i])
                print(f"===== Page {i + 1} =====")
                print(text)
        if args.json:
            _dump_summary(job, "no_embed", None, sorted(wanted), missing)
        return 0

    # Embed only the requested pages that actually produced OCR results.
    if not ocr_pages:
        print("error: no completed pages to embed (all requested pages failed)",
              file=sys.stderr)
        ocr_service.clear_job(job_id)
        return 1

    out_dir = Path(args.out)
    try:
        out_file, img_stats = ocr_service.embed_job(job_id, ocr_pages,
                                                    out_dir=out_dir)
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s: embed failed", job_id)
        print(f"error: embedding failed: {exc}", file=sys.stderr)
        ocr_service.clear_job(job_id)
        return 1

    print(f"embedded: {out_file}")
    if args.json:
        _dump_summary(job, "embedded", str(out_file), sorted(wanted), missing)

    return 0


def _dump_summary(job: dict, mode: str, out_file: Optional[str],
                  requested: List[int], missing: List[int]) -> None:
    summary = {
        "job_id": job.get("id"),
        "filename": job.get("filename"),
        "adapter": job.get("adapter"),
        "mode": mode,
        "status": job.get("status"),
        "requested_pages": [i + 1 for i in requested],
        "missing_pages": [i + 1 for i in missing],
        "output": out_file,
    }
    print(json.dumps(summary, ensure_ascii=False))
    log.info("summary: %s", summary)


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\naborted by user", file=sys.stderr)
        raise SystemExit(130)
