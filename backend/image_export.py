"""Image extraction for markdown export: crop image blocks from the source PDF.

The page sidecars' ``image`` blocks carry bboxes in **raw pixel space of the
OCR page image** (the project's hard invariant). To embed the actual figures
into an exported markdown document, the source PDF's page is rendered (via
``backend.pdf_processing`` — pypdfium2) and each image block's bbox region
cropped:

* the pixel→point mapping goes through ``clip_rect`` (pure): the sidecar page
  width/height give the scale, so the crop renders 1:1 with the bbox pixels —
  the resolution the OCR engine actually saw.
* crops are written once into ``work/<job>/export_images/`` and re-extracted
  on demand (the temp-file cleanup may remove them between exports).
* two delivery modes: a ZIP archive (an ``images/`` folder + relative links —
  renders everywhere including GitHub) or base64 data URIs inline in the .md
  (one self-contained file; Typora/VSCode render it, GitHub does not).

Nothing here touches the block sidecars, hOCR or the embedded text layer —
the extraction only READS the source PDF.
"""
from __future__ import annotations

import base64
import logging
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from backend import pdf_processing

log = logging.getLogger(__name__)

# Image kind(s) the markdown builder embeds.  ``image_ref`` is handled too so
# an engine that emits references with real bboxes benefits as well.
IMAGE_KINDS = ("image", "image_ref")

# Render-zoom cap: never rasterize a clip at more than 432 dpi even if the OCR
# raster was larger (protects RAM on huge pages).
_MAX_ZOOM = 6.0

_IMAGES_DIRNAME = "export_images"


# --- pure helpers ----------------------------------------------------------------

def clip_rect(bbox: List[int], page_w: int, page_h: int,
              rect_w: float, rect_h: float) -> Optional[Tuple[float, float, float, float]]:
    """Map a raw-pixel bbox to PDF point space (pure).

    ``page_w``/``page_h`` are the sidecar page dimensions in OCR pixels;
    ``rect_w``/``rect_h`` the PDF page size in points.  Returns
    ``(x0, y0, x1, y1)`` or ``None`` for a degenerate bbox/scale so the caller
    skips the block instead of failing the export.
    """
    try:
        x1, y1, x2, y2 = (float(v) for v in (bbox or [])[:4])
    except (TypeError, ValueError):
        return None
    if not (page_w > 0 and page_h > 0 and rect_w > 0 and rect_h > 0):
        return None
    if not (x2 > x1 and y2 > y1):
        return None
    sx, sy = rect_w / page_w, rect_h / page_h
    # Clamp to the page: a bbox slightly outside the raster must not produce
    # an out-of-page clip (the renderer would still draw, but keep it honest).
    x0, y0 = max(0.0, x1 * sx), max(0.0, y1 * sy)
    x2p, y2p = min(rect_w, x2 * sx), min(rect_h, y2 * sy)
    if not (x2p > x0 and y2p > y0):
        return None
    return (x0, y0, x2p, y2p)


def image_filename(page_index: int, block_index: int) -> str:
    """Deterministic crop filename: ``page-003-img-1.png`` (1-based page)."""
    return f"page-{(page_index or 0) + 1:03d}-img-{max(block_index or 0, 0)}.png"


def relative_url(filename: str) -> str:
    """The link the markdown carries in ZIP mode (``images/`` folder)."""
    return f"images/{filename}"


def data_uri(png_path: Path) -> str:
    """A ``data:image/png;base64,...`` URI for one crop (pure given the bytes)."""
    data = base64.b64encode(Path(png_path).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def make_resolver(mode: str, image_map: Dict[Tuple[int, int], Path],
                  url_prefix: str = ""
                  ) -> Callable[[dict, Optional[int], Optional[int]], Optional[str]]:
    """Build the ``image_resolver`` the markdown builder receives (pure).

    The resolver maps ``(page_index, block_index)`` to the link markdown
    carries: a relative ``images/…`` path in ZIP mode, a data URI in base64
    mode.  ``None`` (no such crop) falls back to the captioned placeholder.

    ``url_prefix`` prepends to the ZIP link — ``""`` for a zip whose ``.md``
    sits at the archive root, ``"../"`` for split chapter files that live
    under a ``chapters/`` folder.  base64 mode ignores it.
    """
    if mode == "base64":
        def resolver(block: dict, page_index: Optional[int],
                     block_index: Optional[int]) -> Optional[str]:
            path = image_map.get((page_index, block_index))
            try:
                return data_uri(path) if path else None
            except OSError:
                return None
    else:
        def resolver(block: dict, page_index: Optional[int],
                     block_index: Optional[int]) -> Optional[str]:
            path = image_map.get((page_index, block_index))
            return f"{url_prefix}images/{path.name}" if path else None
    return resolver


def build_markdown_zip(zip_path: str, md_text: str, md_name: str,
                       image_map: Dict[Tuple[int, int], Path]) -> int:
    """Write ``<stem>.md`` + its ``images/`` crops into one archive.

    The markdown is written from the in-memory text (it already exists); the
    images stream from disk in bounded chunks.  Returns the member count.
    """
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(md_name, md_text)
        written += 1
        for (_pi, _bi), path in sorted(image_map.items()):
            if not path.exists():
                continue
            zf.write(path, f"images/{path.name}")
            written += 1
    return written


def build_chapters_zip(zip_path: str, chapters: List[Dict[str, str]],
                       image_map: Dict[Tuple[int, int], Path]) -> int:
    """Write per-chapter markdown under ``chapters/`` + the shared
    ``images/`` folder into one archive (split export).

    ``chapters`` is a list of ``{"name", "text"}`` (names from
    ``backend.export.chapter_filename``); the chapter markdown references
    ``../images/…`` (the resolver prefix), which resolves once extracted.
    Returns the member count.
    """
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for ch in chapters:
            zf.writestr(f"chapters/{ch['name']}", ch["text"])
            written += 1
        for (_pi, _bi), path in sorted(image_map.items()):
            if not path.exists():
                continue
            zf.write(path, f"images/{path.name}")
            written += 1
    return written


def default_zip_name(stem: str) -> str:
    """Archive name for a ZIP export: ``<stem>_markdown.zip``."""
    return f"{stem or 'export'}_markdown.zip"


# --- filesystem extraction ---------------------------------------------------------

def extract_images(job: dict, pages: List[dict],
                   progress: Optional[Callable] = None
                   ) -> Dict[Tuple[int, int], Path]:
    """Crop every image block of ``pages`` from the job's source PDF.

    Returns ``{(page_index, block_index): png_path}``.  Blocks without a
    usable bbox are skipped; a page that fails to render is skipped with a
    warning (the markdown falls back to placeholders for it) — a broken crop
    never fails the export.  Raises only when the job has no readable source
    PDF at all.

    ``progress`` (optional callable) receives
    ``{"phase": "images", "done": p, "total": n}`` events as each page is
    processed, so a multi-page extraction can drive a progress bar; callback
    failures are cosmetic and never break the extraction.
    """
    pdf_path = str(job.get("pdf_path") or "")
    if not pdf_path or not Path(pdf_path).exists():
        raise ValueError("job has no readable source PDF for image extraction")

    # Same folder contract as the per-job export LLM cache: work/<job>/…
    hocr_dir = job.get("hocr_dir")
    out_dir = (Path(hocr_dir).parent / _IMAGES_DIRNAME if hocr_dir
               else Path(pdf_path).parent / _IMAGES_DIRNAME)

    image_map: Dict[Tuple[int, int], Path] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    with pdf_processing.open_pdf(pdf_path) as doc:
        for pidx, page in enumerate(pages):
            if progress is not None:
                try:
                    progress({"phase": "images", "done": pidx + 1,
                              "total": len(pages)})
                except Exception:  # noqa: BLE001 — cosmetic
                    pass
            # Both page-dict shapes: the flat form the page store returns
            # ({page_index, width, height, blocks}) and the raw sidecar form
            # (the same fields nested under "page").
            inner = page.get("page") if isinstance(page.get("page"), dict) else page
            page_index = inner.get("page_index", page.get("page_index"))
            if page_index is None or not 0 <= page_index < doc.page_count:
                continue
            pw = int(inner.get("width") or page.get("width") or 0)
            ph = int(inner.get("height") or page.get("height") or 0)
            if pw <= 0 or ph <= 0:
                continue
            try:
                rect_w, rect_h = doc.page_size_pt(page_index)
            except Exception:  # noqa: BLE001 - unreadable page: skip it
                log.warning("image export: page %s unreadable", page_index)
                continue
            blocks = inner.get("blocks") or page.get("blocks") or []
            crops = []  # [(block_index, clip, zoom)]
            for bi, block in enumerate(blocks):
                kind = str(block.get("kind") or "text")
                if kind not in IMAGE_KINDS:
                    continue
                clip = clip_rect(block.get("bbox"), pw, ph, rect_w, rect_h)
                if clip is None:
                    continue
                # 1:1 with the OCR raster: zoom = OCR px / points ⇒ the crop
                # comes out at exactly (bbox_w × bbox_h) pixels.
                zoom = min(pw / rect_w, _MAX_ZOOM)
                crops.append((bi, clip, zoom))
            if not crops:
                continue
            for bi, clip, zoom in crops:
                try:
                    out_path = out_dir / image_filename(page_index, bi)
                    doc.render_clip_png(page_index, clip, zoom, out_path)
                    image_map[(page_index, bi)] = out_path
                except Exception as exc:  # noqa: BLE001 - one bad crop: skip it
                    log.warning("image export: crop failed on page %s block %d: %s",
                                page_index, bi, exc)
    log.info("image export: extracted %d image(s) from job %s",
             len(image_map), job.get("job_id"))
    return image_map
