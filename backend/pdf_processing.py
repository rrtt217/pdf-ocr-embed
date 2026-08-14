"""PDF processing with PyMuPDF (fitz).

Responsibilities:
  - Render each PDF page to a PNG image (raw pixel space for OCR).
  - Embed invisible / searchable text into a copy, saving an *_embedded.pdf.
  - Map pixel-space bboxes to PDF user space (y-axis flip).

PDF user space has its origin at the bottom-left with y increasing upward,
whereas image pixel space has its origin at the top-left with y increasing
downward.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

from backend.models import OcrBlock, OcrPage

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def open_pdf(source_path: str) -> fitz.Document:
    return fitz.open(source_path)


def page_count(pdf_path: str) -> int:
    with open_pdf(pdf_path) as doc:
        return doc.page_count


def get_page_pixel_size(page: fitz.Page) -> Tuple[int, int]:
    """Return (width, height) in *pixels*.

    PyMuPDF renders at the page's chosen DPI. We rasterize at a fixed zoom to
    keep OCR inputs reasonably sized, then report the rendered pixel size.
    """
    # Rasterize once to compute dimensions; callers usually call render_page.
    zoom = PIXEL_RENDER_ZOOM
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return pix.width, pix.height


# Rasterization zoom factor: 2x ~ 144 DPI. Keep memory sane for OCR.
PIXEL_RENDER_ZOOM = 2.0


def render_page(page: fitz.Page) -> "tuple[fitz.Pixmap, int, int]":
    """Render a page to a pixmap and return it plus pixel width/height."""
    mat = fitz.Matrix(PIXEL_RENDER_ZOOM, PIXEL_RENDER_ZOOM)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return pix, pix.width, pix.height


def render_page_to_file(page: fitz.Page, out_path: Path, page_index: int) -> Tuple[str, int, int]:
    """Render page to PNG on disk. Returns (path, width_px, height_px)."""
    out_path.mkdir(parents=True, exist_ok=True)
    pix, w, h = render_page(page)
    png_path = out_path / f"page_{page_index:04d}.png"
    pix.save(str(png_path))
    log.debug("rendered page %d -> %s (%dx%d)", page_index, png_path, w, h)
    return str(png_path), w, h


def pixel_to_pdf(point: Tuple[float, float], page: fitz.Page,
                 img_w: int, img_h: int) -> fitz.Point:
    """Map a pixel-space point to PDF user-space point (y flip + scale)."""
    rect = page.rect
    x = point[0] * (rect.width / img_w)
    # PDF y is bottom-up; image y is top-down.
    y_px = point[1]
    y_pdf = rect.height - y_px * (rect.height / img_h)
    return fitz.Point(x, y_pdf)


# Rendering metrics calibrated from real PyMuPDF output (see _derive_fontsize).
# Each entry:
#   ink_fraction: what fraction of a 1pt fontsize the OCR ink-bbox height
#     spans. Empirically ~0.95-0.97 for mixed-case Latin, ~0.90 for CJK
#     (a CJK glyph nearly fills the em square), ~0.80 for all-caps Latin.
#     The old hardcoded 0.75 sat *below every script*, so text never filled
#     the bbox (it embedded at ~72% of true size).
#   line_height: the font's natural line box in em units (ascender -
#     descender). Replaces the old hardcoded 1.15 so multi-line leading
#     matches real PDF typesetting instead of being packed too tight.
_FONT_METRICS = {
    "helv":    {"ink_fraction": 0.95, "line_height": 1.374},
    "china-s": {"ink_fraction": 0.90, "line_height": 1.309},
    "japan":   {"ink_fraction": 0.90, "line_height": 1.309},
    "korea":   {"ink_fraction": 0.90, "line_height": 1.309},
}

# Fallbacks for any font not in the table above / unknown.
_DEFAULT_INK_FRACTION = 0.92
_DEFAULT_LINE_HEIGHT = 1.30

# OCR bboxes are *tight* ink extents and measurably narrower than the true
# typographic advance width of the text (measured ~0.4-7% on real renders;
# CJK tightness is typically larger than Latin).  Without a tolerance, a
# single OCR line that is a hair too wide for its bbox gets reflowed into
# wrapped lines, and the code then treats the bbox *height* (the ink height
# of ONE line) as the room for many lines — collapsing the font size and
# wrecking the layout.  We allow a modest overfill so a near-fit line stays
# on one line at its ink-filling font size.  Applied consistently in
# _derive_fontsize and _wrap_to_width.
_FILL_WIDTH_TOLERANCE = 1.15


def _font_metrics(fontname: str) -> Tuple[float, float]:
    """Return (ink_fraction, line_height) for a built-in font name."""
    m = _FONT_METRICS.get(fontname)
    if m:
        return m["ink_fraction"], m["line_height"]
    return _DEFAULT_INK_FRACTION, _DEFAULT_LINE_HEIGHT


def embed_invisible_text(pdf_bytes_path: str, pages: List[OcrPage],
                         out_dir: Optional[Path] = None) -> Tuple[Path, Path]:
    """Embed editable OCR text into a copy of the PDF.

    Writes `<stem>_embedded.pdf`. Uses render_mode=3 so text is searchable /
    selectable but invisible. Returns (output_path, thumb_path).

    Text blocks are placed into their bbox region with a fontsize measured so
    glyphs fit the box height (approximately), clipped to the page.
    """
    out_dir = out_dir or ensure_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex[:12]
    src_stem = Path(pdf_bytes_path).stem
    out_file = out_dir / f"{src_stem}_embedded_{job_id}.pdf"
    thumb_file = out_dir / f"{src_stem}_thumb_{job_id}.png"

    # Operate on an in-memory copy so the original file is untouched.
    with open(pdf_bytes_path, "rb") as fh:
        doc = fitz.open(stream=fh.read(), filetype="pdf")

    try:
        total_blocks = 0
        skipped_blocks = 0
        for page_cfg in pages:
            pidx = page_cfg.page_index
            if pidx >= len(doc):
                log.warning("embed: page_index %d out of range (doc has %d pages)", pidx, len(doc))
                continue
            page = doc[pidx]
            rect = page.rect
            w_scale = rect.width / page_cfg.width if page_cfg.width else 1.0
            h_scale = rect.height / page_cfg.height if page_cfg.height else 1.0
            log.debug("embed page %d: %d blocks, scale=%.3fx%.3f",
                      pidx, len(page_cfg.blocks), w_scale, h_scale)

            # Compute the page's left margin (min x1 of text blocks) so we can
            # detect per-block indentation from bbox x1 position.
            text_blocks = [b for b in page_cfg.blocks
                           if b.kind not in ("image", "image_ref") and b.text.strip()]
            if text_blocks:
                page_left_margin_px = min(b.bbox[0] for b in text_blocks)
                page_left_margin = page_left_margin_px * w_scale
            else:
                page_left_margin = 0.0
            log.debug("page %d: left margin = %.1fpt (px=%.1f)",
                      pidx, page_left_margin, page_left_margin_px)

            for block in page_cfg.blocks:
                if block.kind in ("image", "image_ref") or not block.text.strip():
                    continue
                total_blocks += 1
                try:
                    _insert_block(page, block, rect, w_scale, h_scale,
                                  page_left_margin)
                except Exception as exc:  # noqa: BLE001
                    skipped_blocks += 1
                    log.warning("embed: page %d block skipped: %s", pidx, exc)

        doc.save(str(out_file), garbage=4, deflate=True)
        log.info("embedded PDF saved: %s (%d pages, %d/%d blocks embedded)",
                 out_file, len(pages), total_blocks - skipped_blocks, total_blocks)
        if skipped_blocks:
            log.warning("embed: %d block(s) skipped due to errors", skipped_blocks)
        # Render the first page as a preview thumbnail.
        first = doc[0] if len(doc) else None
        if first is not None:
            pix = first.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), alpha=False)
            pix.save(str(thumb_file))
    finally:
        doc.close()

    return out_file, thumb_file


def _insert_block(page: fitz.Page, block: OcrBlock, rect: fitz.Rect,
                  w_scale: float, h_scale: float,
                  page_left_margin: float = 0.0) -> None:
    """Embed one OCR block as invisible text that fills the bbox.

    The OCR bbox encodes both the position AND the visual size of the text.
    We derive the font size so the text fills the bbox:
      - Single-line blocks: font_size = bbox_height / ink_fraction (calibrated
        per script so the glyph ink spans the bbox height).
      - Multi-line blocks: iteratively solve so wrapped lines fill bbox_width
        and total height fills bbox_height, using the font's natural line box
        (ascender - descender) as the leading.

    Leading-space / indentation analysis:
      The raw OCR text has no leading spaces (the adapter strips them).  But
      the bbox x1 encodes the left-edge position — if x1 > page_left_margin,
      the block is indented.  We restore leading spaces proportional to the
      indent so extracted text preserves the original formatting.
    """
    blob = _pixel_rect_to_pdf(block.bbox, page, w_scale, h_scale)
    text = block.text
    fontname = _pick_fontname(text)
    txt = _clip_text(text)
    if not txt.strip():
        return

    box_w = blob.width
    box_h = blob.height
    max_w = page.rect.width - blob.x0 - 2
    box_w = min(box_w, max_w) if box_w > 0 else max_w

    # --- 0. Detect indentation from bbox x1 vs page left margin ---
    indent_pt = max(0.0, blob.x0 - page_left_margin)
    lines = txt.splitlines() or [""]

    # --- 1. Derive the font size that fills the bbox ---
    fontsize = _derive_fontsize(lines, box_w, box_h, fontname)

    # --- 2. Add leading-space indent for indented paragraph blocks ---
    # Only `text` blocks get indent restoration; titles/headers may be centred
    # (large x1) which is NOT indentation.  Cap at 2 full-width spaces — the
    # standard Chinese paragraph first-line indent (2em).  The bbox x1 encodes
    # the block's left edge; comparing with the page left margin reveals indent.
    if indent_pt > fontsize * 0.5 and block.kind == "text":
        n_indent = max(1, round(indent_pt / fontsize))
        n_indent = min(n_indent, 2)  # cap at 2em (standard Chinese indent)
        indent_str = "\u3000" * n_indent  # full-width space (ideographic space)
        lines[0] = indent_str + lines[0]
        log.debug("indent: block x0=%.1f margin=%.1f indent=%.1fpt -> %d full-width spaces",
                  blob.x0, page_left_margin, indent_pt, n_indent)

    # --- 3. Wrap each logical line to the bbox width ---
    wrapped = _wrap_to_width(lines, box_w, fontsize, fontname)

    # --- 4. If a genuine multi-line reflow overflows the bbox height, shrink ---
    # box_h is the *ink height of one line*, so a single line's natural line
    # box (fontsize * line_height_em) is normally taller than box_h — and must
    # NOT be shrunk, or the text stops filling the bbox.  We only shrink when
    # the text actually reflowed onto multiple wrapped lines and overflows.
    _, line_height_em = _font_metrics(fontname)
    line_height = fontsize * line_height_em
    total_h = len(wrapped) * line_height
    if len(wrapped) > 1 and box_h > 0 and total_h > box_h * 1.1:
        scale = box_h / total_h
        fontsize = max(fontsize * scale, 0.5)
        line_height = fontsize * line_height_em
        wrapped = _wrap_to_width(lines, box_w, fontsize, fontname)

    # --- 5. Insert lines top-to-bottom, centred vertically in the bbox ---
    total_h = len(wrapped) * line_height
    if box_h > total_h and len(wrapped) > 1:
        # Vertically centre the text block within the bbox
        y_start = blob.y0 + (box_h - total_h) / 2 + fontsize
    else:
        y_start = blob.y0 + fontsize
    y_cursor = y_start
    for line in wrapped:
        if not line:
            y_cursor += line_height
            continue
        page.insert_text(
            fitz.Point(blob.x0, y_cursor),
            line,
            fontsize=fontsize,
            fontname=fontname,
            render_mode=3,
            overlay=True,
        )
        y_cursor += line_height


def _derive_fontsize(lines: list, box_w: float, box_h: float,
                     fontname: str) -> float:
    """Estimate the font size that makes the text fill the bbox.

    The OCR bbox height encodes the *ink* extent of the glyphs (not the full
    em box / line box).  For a known font we map ink height -> font size via
    ``fontsize = box_h / ink_fraction``, where ``ink_fraction`` is calibrated
    per script (~0.95 mixed-case Latin, ~0.90 CJK).  Multi-line blocks are
    laid out with the font's natural line box (ascender - descender) as the
    leading, i.e. ``line_height = fontsize * line_height_em``.

    For a single logical line, solve iteratively:
        n_lines = ceil(text_width(fs) / box_w)
        fs.fill  = box_h / ink_fraction           # ink fills the bbox height
        fs.wrap  = box_h / (n_lines * line_height_em)   # wrapped lines fit box_h
    A direct initial estimate via fs² ∝ box_w * box_h / text_width(1pt)
    speeds convergence.  For blocks with explicit \\n (equations), trust the
    line count.
    """
    import math

    ink_fraction, line_height_em = _font_metrics(fontname)

    if box_h <= 0:
        return 8.0
    if box_w <= 0:
        return max(box_h / ink_fraction, 1.0)

    n_explicit = len(lines)

    # --- Multi-line block with explicit newlines (e.g. equations) ---
    if n_explicit > 1:
        fs = box_h / (n_explicit * line_height_em)
        for ln in lines:
            w = _text_width(ln, fs, fontname)
            if w > box_w:
                fs = min(fs, fs * box_w / w)
        return max(fs, 0.5)

    # --- Single logical line ---
    text = lines[0]
    # Measure text width at 1pt to get the proportionality constant k
    # (get_text_length is linear in fontsize for most fonts).
    k = _text_width(text, 1.0, fontname)
    if k <= 0:
        return max(box_h / ink_fraction, 1.0)

    # Case 1: text fits on one line (within a small overfill tolerance — OCR
    # bboxes are tight ink extents slightly narrower than the true advance
    # width) at a font size whose ink fills box_h.
    fs_single = box_h / ink_fraction
    if _text_width(text, fs_single, fontname) <= box_w * _FILL_WIDTH_TOLERANCE:
        return max(fs_single, 1.0)

    # Case 2: text needs wrapping — solve fs² ≈ box_w * box_h / (k * line_height_em)
    # This comes from: n = ceil(k*fs / box_w) and fs = box_h / (n * line_height_em)
    # => k*fs / box_w ≈ box_h / (fs * line_height_em)
    # => fs² ≈ box_w * box_h / (k * line_height_em)
    fs = math.sqrt(box_w * box_h / (k * line_height_em))

    # Refine with iteration (usually converges in 2-3 steps)
    for _ in range(8):
        text_w = _text_width(text, fs, fontname)
        n = max(1, math.ceil(text_w / box_w - 1e-9))
        fs_new = box_h / (n * line_height_em)
        if abs(fs_new - fs) < 0.2:
            break
        fs = fs_new

    return max(fs, 0.5)


def _text_width(text: str, fontsize: float, fontname: str) -> float:
    """Measure rendered text width in PDF points (with fallback)."""
    try:
        return fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)
    except Exception:
        return len(text) * fontsize * 0.55


def _wrap_to_width(lines: list, box_w: float, fontsize: float,
                   fontname: str) -> list:
    """Wrap each line to fit within *box_w* (in PDF points).

    Breaks at character boundaries (works for CJK and Latin).  Keeps all
    wrapped sub-lines of a single block consecutive to preserve reading order.
    A small overfill tolerance (*_FILL_WIDTH_TOLERANCE) keeps a single OCR
    line that is a hair wider than its (tight) bbox from being reflowed into
    multiple lines.
    """
    line_limit = box_w * _FILL_WIDTH_TOLERANCE
    if box_w <= 0:
        return lines
    out = []
    for line in lines:
        if not line:
            out.append("")
            continue
        w = _text_width(line, fontsize, fontname)
        if w <= line_limit:
            out.append(line)
            continue
        # Greedy character-level wrap
        cur = ""
        for ch in line:
            if _text_width(cur + ch, fontsize, fontname) > line_limit and cur:
                out.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            out.append(cur)
    return out


def _pick_fontname(text: str) -> str:
    """Choose a built-in PDF font capable of rendering the text's script.

    The default 'helv' (Helvetica) only covers Latin-1.  For CJK text,
    Greek letters, or math symbols it silently produces dots — so we detect
    non-Latin Unicode ranges and pick 'china-s', which has much broader
    coverage (Greek, math operators, arrows, etc.):
      - CJK ideographs -> 'china-s'
      - Greek letters  -> 'china-s'  (α β ε Σ etc.)
      - Math symbols   -> 'china-s'  (≤ ≥ × ± ≠ ≈ ∞ ∫ √ → etc.)
      - Japanese kana  -> 'japan'
      - Korean hangul  -> 'korea'
    """
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            return "china-s"
        if 0x3040 <= cp <= 0x30FF:
            return "japan"
        if 0xAC00 <= cp <= 0xD7AF:
            return "korea"
        # Greek (α-ω, Α-Ω) — china-s covers these, helv does not.
        if 0x0370 <= cp <= 0x03FF:
            return "china-s"
        # Common math operators (≤ ≥ × ± ≠ ≈ ∞ ∫ √ → etc.) — china-s covers
        # these, helv only has × and ±.
        if 0x2200 <= cp <= 0x22FF:  # Mathematical Operators block
            return "china-s"
        if 0x2190 <= cp <= 0x21FF:  # Arrows block
            return "china-s"
    return "helv"


def _clip_text(text: str, limit: int = 2000) -> str:
    return (text or "")[:limit]


def _pixel_rect_to_pdf(bbox, page: fitz.Page, w_scale: float,
                       h_scale: float) -> fitz.Rect:
    x1, y1, x2, y2 = bbox
    tl = _pixel_point_to_pdf((x1, y1), page, w_scale, h_scale)
    br = _pixel_point_to_pdf((x2, y2), page, w_scale, h_scale)
    # Normalize so x0<x1, y0<y1. PyMuPDF does NOT auto-normalize, and an
    # un-normalized rect has y0/y1 swapped (y0=top, y1=bottom) which breaks
    # the text placement logic that assumes y0=bottom, y1=top in PDF space.
    return fitz.Rect(
        min(tl.x, br.x), min(tl.y, br.y),
        max(tl.x, br.x), max(tl.y, br.y),
    )


def _pixel_point_to_pdf(pt, page: fitz.Page, w_scale: float,
                        h_scale: float) -> fitz.Point:
    """Map a pixel-space point to PyMuPDF page coordinates.

    PyMuPDF's coordinate system (used by insert_text, get_text, page.rect,
    etc.) has its origin at the TOP-LEFT with y increasing downward — exactly
    the same convention as image pixel space.  So we only need to scale, NOT
    flip the y-axis.  (Flipping is only needed for raw PDF operators, which
    we don't use directly.)
    """
    x = pt[0] * w_scale
    y = pt[1] * h_scale
    return fitz.Point(x, y)