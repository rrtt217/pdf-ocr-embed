"""Unlimited-OCR engine as an OCRmyPDF ``OcrEngine`` plugin.

The engine receives ocrmypdf-rasterized page images, calls the unlimited-ocr
OpenAI-compatible API (``client``), parses the marker stream into blocks
(``parser``), and writes:

* an hOCR file (what ocrmypdf's fpdf2 renderer turns into the invisible text
  layer, and what ``ocrmypdf._hocr_to_ocr_pdf`` renders after user edits), and
* a plain-text sidecar, and
* a block sidecar JSON (``*_ocr_hocr.blocks.json``) that editor UIs edit.

This module is **fully standalone**: it never imports anything from a host
application.  Configuration comes from ``settings.effective(options)`` —
plugin CLI/API options, ``OCR_UNLIMITED_*`` env, or a host-injected snapshot.

Multi-page (batch) mode: when ``ocr_batch_size > 1`` is configured, pages
arriving concurrently from ocrmypdf's worker threads are grouped into ONE
"Multi page parsing." request (``batching``); the ``<PAGE>``-delimited response
sections are handed back per page.  Every failure path degrades to a per-page
request, so batching never loses a page.

Decoupling: the engine communicates with its host ONLY through the job's work
folder on disk (hOCR, sidecars, the ``<job>/cancel`` flag file).  It derives
the job dir from ``options.output_folder`` (the hOCR pipeline's work folder,
whose parent is the job dir) and never imports job/progress state.

Cancellation is an EXCLUSIVE capability of this plugin: ocrmypdf itself has no
mid-run cancel hook, so the engine polls ``<job_dir>/cancel`` per page (see
``files.is_cancelled``) and stops gracefully, keeping the pages already on
disk.  A plain ``ocrmypdf`` run can only be hard-interrupted, losing progress.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ocrmypdf import hookimpl
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence

from ocrmypdf_unlimited import settings as unlimited_settings
from ocrmypdf_unlimited.batching import BatchCancelled, BatchTimeout, MultiPageBatcher
from ocrmypdf_unlimited.client import (
    UnlimitedOcrClient,
    image_dpi,
    image_size,
)
from ocrmypdf_unlimited.files import (
    hocr_path,
    is_cancelled,
    page_index_from_name,
)
from ocrmypdf_unlimited.line_split import (
    split_block_into_lines,
    split_block_text_across_bands,
)
from ocrmypdf_unlimited.parser import (
    blocks_to_hocr,
    hocr_page_sidecar_text,
    parse_response,
)

if TYPE_CHECKING:
    from ocrmypdf._options import OcrOptions

log = logging.getLogger(__name__)


def _per_line_overrides(input_file: Path, page) -> dict:
    """Recover accurate per-line placement for the invisible text layer.

    The model reports paragraph-level bboxes; each renderable line is placed
    on an equal vertical slice today (approximate).  This re-derives the true
    printed line rows from the page image (``line_split``):

    * blocks whose text already splits into >1 line get one bbox per line,
      aligned to that line's ink;
    * blocks reported as a single long line over a multi-line bbox get their
      text distributed across the detected printed line bands.

    Returns ``{block_index: [(line_text, [x1, y1, x2, y2]), ...]}`` for
    ``blocks_to_hocr``.  Every path falls back to the block's own lines with
    equal-slice bboxes when the image cannot support the split, so the render
    is never worse than today.  Editor semantics are untouched: the block
    sidecar still carries one bbox per block.
    """
    overrides: dict = {}
    try:
        from PIL import Image
        with Image.open(input_file) as img:
            gray = img.convert("L")
            for bi, block in enumerate(page.blocks):
                if block.kind == "image" or not block.lines:
                    continue
                pairs: list = []
                n_lines = len(block.lines)
                if n_lines > 1:
                    boxes = split_block_into_lines(gray, block.bbox, n_lines)
                    if len(boxes) == n_lines:
                        pairs = list(zip(block.lines, boxes))
                elif block.text.strip():
                    split = split_block_text_across_bands(
                        gray, block.bbox, block.text)
                    if split:
                        pairs = list(split)
                if pairs:
                    overrides[bi] = pairs
    except Exception:  # noqa: BLE001  (never degrade the run for placement)
        log.warning("per-line bbox recovery failed for %s; using equal slices",
                    input_file, exc_info=True)
    if overrides:
        log.debug("page %s: recovered per-line placement for %d block(s)",
                  input_file.name, len(overrides))
    return overrides


def _job_dir_from_options(options: "OcrOptions | None") -> Path:
    """Derive the job dir from the hOCR pipeline's work folder.

    Convention: ``_pdf_to_hocr`` is called with ``output_folder =
    work/<job_id>/hocr`` — the parent of that folder is the job dir (where the
    ``cancel`` flag lives).  Unknown conventions return the work folder's own
    parent, which simply never has a cancel flag.
    """
    if options is None:
        return Path()
    folder = getattr(options, "output_folder", None)
    if not folder:
        return Path()
    try:
        return Path(folder).resolve().parent
    except (OSError, ValueError, TypeError):
        return Path()


# --- multi-page (batch) helpers ----------------------------------------------

def _batch_size(cfg: dict) -> int:
    """Configured pages per multi-image request (0 = batching disabled)."""
    try:
        return max(0, int(cfg.get("ocr_batch_size") or 0))
    except (TypeError, ValueError):
        return 0


def _batch_timeout(cfg: dict) -> float:
    """Window flush timeout in seconds (backstop for the last partial window)."""
    try:
        return max(0.05, float(cfg.get("ocr_batch_timeout_ms") or 3000) / 1000.0)
    except (TypeError, ValueError):
        return 3.0


def _batch_max_wait(batch_size: int, client: UnlimitedOcrClient) -> float:
    """How long a follower may wait for its batch response.

    The leader's HTTP read timeout scales with the batch's max_tokens; add
    connect + retry headroom so a slow-but-legit batch is not abandoned early.
    """
    budget = min(client.max_tokens, client.batch_per_page_tokens * batch_size)
    read_timeout = max(client.READ_TIMEOUT_MIN, budget * client.READ_TIMEOUT_PER_TOKEN)
    return read_timeout + 300.0


def _pending_page_indices(work_dir: Path) -> set:
    """0-based page indices whose rasterized image exists but whose hOCR
    result is not on disk yet (their worker thread has not finished).

    Used by the batcher to tell whether more pages are still expected after
    the current batch window.  A page with an hOCR file (even stale from a
    previous run) is treated as done: in a forced re-run the worst case is
    that the re-run pages simply form their own windows.
    """
    work_dir = Path(work_dir)
    pending: set = set()
    for candidate in work_dir.glob("*_rasterize*.png"):
        idx = page_index_from_name(candidate.name)
        if not hocr_path(work_dir, idx + 1).exists():
            pending.add(idx)
    return pending


#: Per-job batchers so two concurrent jobs' worker threads never share a window.
_BATCHERS: dict = {}
_batchers_lock = threading.Lock()


def _batcher_for(job_dir: Path, batch_size: int, client: UnlimitedOcrClient,
                 cfg: dict) -> MultiPageBatcher:
    """Return the batcher for this job, created on the first page of a run.

    Anonymous runs (no job dir — no ocrmypdf conventions) get a per-thread
    batcher so worker threads stay separate and never batch across pages.
    """
    if not job_dir or job_dir == Path():
        key = f"anon-{threading.get_ident()}"
        job_dir_arg = None
    else:
        key = str(job_dir)
        job_dir_arg = job_dir
    timeout = _batch_timeout(cfg)
    with _batchers_lock:
        batcher = _BATCHERS.get(key)
        if batcher is None or batcher.batch_size != batch_size:
            batcher = MultiPageBatcher(
                batch_size=batch_size,
                timeout=timeout,
                sender=client.recognize_multi,
                pending_pages=_pending_page_indices,
                is_cancelled=_cancel_check(job_dir_arg),
                max_wait=_batch_max_wait(batch_size, client),
            )
            _BATCHERS[key] = batcher
        return batcher


def _cancel_check(job_dir: Optional[Path]) -> Callable[[], bool]:
    """An is_cancelled() check for the batcher (anonymous runs: never)."""
    if job_dir is None:
        return lambda: False
    return lambda: is_cancelled(job_dir)


def _batch_recognize(job_dir: Path, batch_size: int, client: UnlimitedOcrClient,
                     page_index: int, input_file: Path, cfg: dict) -> str:
    """Route one page through the multi-page batcher, with single-page fallback.

    Never worse than a plain per-page request: batch failures, wait timeouts
    and empty/missing sections (a ``<PAGE>`` stream shorter than the image
    count) all degrade to ``client.recognize`` for THIS page only.  A cancel
    propagates so the run stops.
    """
    batcher = _batcher_for(job_dir, batch_size, client, cfg)
    try:
        raw = batcher.submit(page_index, input_file)
    except BatchCancelled:
        raise RuntimeError("OCR job cancelled by user")
    except BatchTimeout:
        log.debug("page %d: batch path unavailable; single-page fallback",
                  page_index + 1, exc_info=True)
        return client.recognize(input_file)
    if raw.strip():
        return raw
    log.debug("page %d: batch returned an empty section; single-page fallback",
              page_index + 1)
    return client.recognize(input_file)


class UnlimitedOcrEngine(OcrEngine):
    """OCRmyPDF engine backed by the unlimited-ocr vision model."""

    @staticmethod
    def version() -> str:
        """Return the engine version (tracks the parser mapping)."""
        from ocrmypdf_unlimited.parser import PARSE_VERSION
        return f"unlimited-ocr (parse v{PARSE_VERSION})"

    @staticmethod
    def creator_tag(options: "OcrOptions") -> str:
        """Return the creator tag for PDF metadata."""
        return "Unlimited-OCR"

    def __str__(self) -> str:
        """Return the human-readable engine name."""
        return "Unlimited-OCR (unlimited)"

    @staticmethod
    def languages(options: "OcrOptions") -> set[str]:
        """Accept any requested language: the engine is language-agnostic.

        Tesseract's internal-use languages (osd/equ) are already rejected by
        OCRmyPDF's global validation before this is consulted.
        """
        if options is not None:
            return set(options.languages) | {"und"}
        return {"und"}

    @staticmethod
    def get_orientation(input_file: Path, options: "OcrOptions") -> OrientationConfidence:
        """No rotation detection: the model OCRs the image as-is."""
        return OrientationConfidence(angle=0, confidence=0.0)

    @staticmethod
    def get_deskew(input_file: Path, options: "OcrOptions") -> float:
        """No deskew detection (deskew preprocessing stays opt-in)."""
        return 0.0

    @staticmethod
    def supports_generate_ocr() -> bool:
        """The editing flow renders from hOCR files, so hOCR is canonical."""
        return False

    @staticmethod
    def generate_pdf(input_file: Path, output_pdf: Path, output_text: Path,
                     options: "OcrOptions") -> None:
        """Not supported: the hOCR renderer produces the text layer."""
        raise NotImplementedError(
            "UnlimitedOcrEngine does not generate PDFs directly; "
            "use pdf_renderer='hocr' (the default 'auto' resolves to it).")

    @staticmethod
    def generate_hocr(input_file: Path, output_hocr: Path, output_text: Path,
                      options: "OcrOptions") -> None:
        """OCR one page image and write hOCR + sidecars.

        Runs inside ocrmypdf's worker threads.  Raises (fails the run) on
        genuine OCR failures; the caller's retry flow re-runs the job.
        """
        job_dir = _job_dir_from_options(options)
        if job_dir != Path() and is_cancelled(job_dir):
            raise RuntimeError("OCR job cancelled by user")

        width, height = image_size(input_file)
        dpi = image_dpi(input_file)
        page_index = page_index_from_name(output_hocr.name)

        # Config resolution (standalone): plugin CLI/API options >
        # OCR_UNLIMITED_* env > host-injected snapshot > client defaults.
        # Never a host's config module.
        cfg = unlimited_settings.effective(options)
        client = UnlimitedOcrClient(config=cfg)

        # generate_raw (default off): keep each block's raw (pre-normalized)
        # content — the block sidecar gains a 'raw' field and the hOCR gains
        # x_kind/x_raw properties on each ocr_par title (hOCR 1.2 extensions).
        # Purely additive metadata for other-format export; the invisible text
        # layer and the editor are unaffected.
        save_raw = unlimited_settings.as_bool(cfg.get("generate_raw"))

        # Multi-page (batch) mode: group concurrent pages into one request.
        # Skipped for serial runs (jobs=1) and when no ocrmypdf options are
        # available.  _batch_recognize falls back to a per-page request on any
        # batch failure, so this never makes a page worse than before.
        batch_size = _batch_size(cfg)
        if batch_size > 1 and options is not None \
                and getattr(options, "jobs", 0) != 1:
            raw = _batch_recognize(job_dir, batch_size, client,
                                   page_index, input_file, cfg)
        else:
            raw = client.recognize(input_file)

        page = parse_response(raw, width, height, page_index,
                              save_raw=save_raw)

        per_line = _per_line_overrides(input_file, page)
        hocr_text = blocks_to_hocr(width, height, page.blocks, dpi=dpi,
                                   ppageno=page_index,
                                   per_line_overrides=per_line)
        output_hocr.write_text(hocr_text, encoding="utf-8")
        output_text.write_text(
            hocr_page_sidecar_text(page.blocks), encoding="utf-8")

        # Block sidecar: the editor's editable representation of this page,
        # written next to the hOCR so a restart can restore progress from disk.
        # ``dpi`` is carried so the hOCR can be regenerated with the same
        # scan_res after user edits (the renderer's px->pt transform needs it).
        try:
            sidecar = output_hocr.with_name(
                output_hocr.stem + ".blocks.json")
            sidecar.write_text(
                json.dumps({"page": page.to_dict(), "dpi": dpi},
                           ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            log.warning("page %d: failed to write block sidecar", page_index)

        log.info("page %d: %d block(s) recognized", page_index + 1,
                 len(page.blocks))


@hookimpl
def get_ocr_engine(options):
    """Return the unlimited engine when ``ocr_engine`` selects it.

    Registered ahead of ocrmypdf's built-in engines, so anything other than
    ``'unlimited'`` falls through to them (e.g. ``'auto'``/``'tesseract'`` use
    ocrmypdf's built-in Tesseract, ``'none'`` disables OCR).
    """
    if options is not None:
        engine_name = getattr(options, "ocr_engine", "auto")
        if engine_name != "unlimited":
            return None
    return UnlimitedOcrEngine()
