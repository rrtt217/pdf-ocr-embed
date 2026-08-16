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
import os
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

from backend.models import OcrBlock, OcrPage

# The system font selected for embedding (a backend.fonts.FontSpec).  Set before
# an embed / preview call via set_embed_font().  When set, all width measurement,
# font-size derivation and text insertion use this real font (narrow ASCII
# spaces, correct digit widths) instead of the built-in Base-14 placeholders.
ACTIVE_FONT = None


def set_embed_font(font_spec) -> None:
    """Set the active system font used for embedding (or None for built-ins)."""
    global ACTIVE_FONT
    ACTIVE_FONT = font_spec


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
    """Render page to PNG on disk. Returns (path, width_px, height_px).

    The PNG is written atomically (temp file + os.replace): the OCR pre-render
    phase and the lazy on-demand preview render (cache-hit pages are rendered
    only when first viewed) may race in practice, and no reader should ever
    observe a half-written image.
    """
    out_path.mkdir(parents=True, exist_ok=True)
    pix, w, h = render_page(page)
    png_path = out_path / f"page_{page_index:04d}.png"
    tmp_path = out_path / f".page_{page_index:04d}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        # The .tmp suffix hides the format from PyMuPDF — say it explicitly.
        pix.save(str(tmp_path), output="png")
        os.replace(tmp_path, png_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
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
#     spans. Empirically ~0.87-0.90 for CJK and ~0.72-0.95 for Latin/mixed
#     (a CJK glyph nearly fills the em square, so this is stable; Latin varies
#     with ascenders/descenders).  The old hardcoded 0.75 sat *below every
#     script*, so text never filled the bbox (it embedded at ~72% of true size).
#   line_height: the font's natural line box in em units (ascender -
#     descender). Replaces the old hardcoded 1.15 so multi-line leading
#     matches real PDF typesetting instead of being packed too tight.
#   ink_up: distance (in em) from the baseline up to the top of the glyph ink.
#     @ fs such that ink height = box_h, setting the first line's baseline to
#     bbox_top + ink_up*fs centres the ink vertically in the bbox (fixes the
#     text sitting a few pt below the box).
_FONT_METRICS = {
    "helv":    {"ink_fraction": 0.90, "line_height": 1.374, "ink_up": 0.72},
    "china-s": {"ink_fraction": 0.90, "line_height": 1.309, "ink_up": 0.80},
    "japan":   {"ink_fraction": 0.90, "line_height": 1.309, "ink_up": 0.80},
    "korea":   {"ink_fraction": 0.90, "line_height": 1.309, "ink_up": 0.80},
}

# Fallbacks for any font not in the table above / unknown.
_DEFAULT_INK_FRACTION = 0.90
_DEFAULT_LINE_HEIGHT = 1.30
_DEFAULT_INK_UP = 0.75

# OCR bboxes are *tight* ink extents, measurably narrower than the true
# typographic advance width of the text (a few percent on real renders).  We
# allow a *tiny* overfill (1.01 = 1%) to absorb sub-pixel OCR width rounding
# and the natural tightness of ink-width measurement, so a well-formed single
# line stays on one line.  Visible overflow is NOT acceptable — the font-size
# derivation (see _derive_fontsize) constrains width so lines stay inside the
# bbox.  Applied consistently in _derive_fontsize and _wrap_to_width.
_FILL_WIDTH_TOLERANCE = 1.01


def _font_metrics(fontname: str) -> Tuple[float, float, float]:
    """Return (ink_fraction, line_height, ink_up) for the active font.

    When a system font is active (embed_font selected), its real metrics are
    used to size and centre the text; otherwise the built-in table applies.
    """
    if ACTIVE_FONT is not None:
        try:
            fit_ = ACTIVE_FONT.fit()
            asc = fit_.ascender or 1.0
            desc = fit_.descender or -0.2
            line_height = max(asc - desc, 0.5)
            return (ACTIVE_FONT.ink_fraction, line_height, ACTIVE_FONT.ink_up)
        except Exception:  # noqa: BLE001
            pass
    m = _FONT_METRICS.get(fontname)
    if m:
        return m["ink_fraction"], m["line_height"], m["ink_up"]
    return (_DEFAULT_INK_FRACTION, _DEFAULT_LINE_HEIGHT, _DEFAULT_INK_UP)


def embed_invisible_text(pdf_bytes_path: str, pages: List[OcrPage],
                         out_dir: Optional[Path] = None,
                         embed_font=None,
                         img_mode: Optional[str] = None,
                         img_quality: Optional[int] = None,
                         img_downscale: Optional[int] = None,
                         linearize: bool = False) -> Tuple[Path, Path, dict]:
    """Embed editable OCR text into a copy of the PDF.

    Writes `<stem>_embedded.pdf`. Uses render_mode=3 so text is searchable /
    selectable but invisible.  Optional output tweaks:
      - `img_mode` in {none, jpeg, gray-jpeg} recompresses page images when
        it shrinks them (see ``optimize_images``),
      - `img_downscale` (2 or 4) additionally halves/quarters the raster,
      - `linearize` saves a web-first (linearized) PDF.
    Returns (output_path, thumb_path, image_stats).

    ``embed_font`` is an optional backend.fonts.FontSpec; when given, the real
    system font is embedded (subset, for a small file) and used for measuring
    and placing the text.  Text blocks are placed into their bbox region with a
    fontsize measured so glyphs fit the box height, clipped to the page.
    """
    prev_font = ACTIVE_FONT
    set_embed_font(embed_font)
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

            # Indentation is preserved by the bbox start x (blob.x0) the text is
            # placed at — no per-page left margin is needed here.

            for block in page_cfg.blocks:
                if block.kind in ("image", "image_ref") or not block.text.strip():
                    continue
                total_blocks += 1
                try:
                    _insert_block(page, block, rect, w_scale, h_scale)
                except Exception as exc:  # noqa: BLE001
                    skipped_blocks += 1
                    log.warning("embed: page %d block skipped: %s", pidx, exc)

        # Subset embedded fonts to keep the output small (variable/full CJK
        # fonts are megabytes; the used subset is tens of KB).
        try:
            doc.subset_fonts()
        except Exception as exc:  # noqa: BLE001
            log.warning("font subsetting skipped: %s", exc)

        # Optional image recompression (size win on scanned input).
        img_stats = optimize_images(
            doc, (img_mode or "none").strip().lower(), img_quality or 75,
            img_downscale)

        linear_done = False
        if linearize:
            try:
                doc.save(str(out_file), deflate=True, linear=True)
                linear_done = True
            except Exception as exc:  # noqa: BLE001
                # The bundled MuPDF may drop linearization ("Linearisation is
                # no longer supported") — degrade gracefully, never fail the
                # embed over a size/UX optimization.
                log.warning("embed: linearization unavailable (%s), "
                            "saving a normal PDF", exc)
                doc.save(str(out_file), garbage=4, deflate=True)
        else:
            doc.save(str(out_file), garbage=4, deflate=True)
        img_stats["linearized"] = linear_done
        log.info("embedded PDF saved: %s (%d pages, %d/%d blocks embedded)",
                 out_file, len(pages), total_blocks - skipped_blocks, total_blocks)
        if skipped_blocks:
            log.warning("embed: %d block(s) skipped due to errors", skipped_blocks)
        img_stats["saved_bytes"] = max(0, img_stats["before_bytes"]
                                       - img_stats["after_bytes"])
    finally:
        doc.close()
        set_embed_font(prev_font)

    # The thumbnail must show the FINAL bytes (post-optimization), so re-open
    # the saved file instead of paging through the in-memory document.
    with fitz.open(str(out_file)) as final:
        first = final[0] if final.page_count else None
        if first is not None:
            pix = first.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), alpha=False)
            pix.save(str(thumb_file))

    return out_file, thumb_file, img_stats


def _page_font_name(page: fitz.Page) -> str:
    """Register the active system font on *page* once; return the resource name.

    When no system font is active, returns None (callers use built-in names).
    """
    if ACTIVE_FONT is None:
        return None
    ctx = getattr(page, "_active_font_ctx", None)
    if ctx and ctx[0] == ACTIVE_FONT.name:
        return ctx[1]
    res = page.insert_font(fontname=ACTIVE_FONT.fontname, fontfile=ACTIVE_FONT.path)
    page._active_font_ctx = (ACTIVE_FONT.name, ACTIVE_FONT.fontname)
    return ACTIVE_FONT.fontname


def _insert_block(page: fitz.Page, block: OcrBlock, rect: fitz.Rect,
                  w_scale: float, h_scale: float) -> None:
    """Embed one OCR block as invisible text that fills the bbox.

    Delegates geometry to the shared _compute_block_layout, then inserts each
    line with render_mode=3 (invisible, searchable).
    """
    layout = _compute_block_layout(block, page, w_scale, h_scale,
                                   font_scale=block.font_scale)
    _page_font_name(page)  # ensure font resource is present if active
    for x, y_base, line, fontsize, fontname in layout["lines"]:
        if not line:
            continue
        page.insert_text(
            fitz.Point(x, y_base), line,
            fontsize=fontsize, fontname=fontname,
            render_mode=3, overlay=True,
        )


def _compute_block_layout(block: OcrBlock, page: fitz.Page, w_scale: float,
                          h_scale: float, font_scale: float = 1.0) -> dict:
    """Compute the exact placed text geometry for one block.

    Returns a dict:
      {
        "fontname": str,
        "fontsize": float,          # derived size AFTER font_scale applied
        "derived":  float,          # size before font_scale
        "lines":    [(x, baseline_y, text, fontsize, fontname), ...],
      }

    The OCR bbox encodes both the position and the visual size of the text.
    We derive the font size so the ink fills the bbox (see _derive_fontsize),
    optionally scaled by ``font_scale`` (the interactive per-block debug gain).
    First-line indentation is NOT reconstructed: OCR engines strip leading
    spaces from the text, but the indent is already preserved by placing the
    text at the bbox start x (blob.x0).
    """
    blob = _pixel_rect_to_pdf(block.bbox, page, w_scale, h_scale)
    text = block.text
    # Use the active system font's resource name when one is selected, else the
    # built-in placeholder font.
    fontname = ACTIVE_FONT.fontname if ACTIVE_FONT is not None else _pick_fontname(text)
    txt = _clip_text(text)
    empty = {"fontname": fontname, "fontsize": 0.0, "derived": 0.0, "lines": []}
    if not txt.strip():
        return empty

    box_w = blob.width
    box_h = blob.height
    max_w = page.rect.width - blob.x0 - 2
    box_w = min(box_w, max_w) if box_w > 0 else max_w

    lines = txt.splitlines() or [""]

    # NOTE: we intentionally do NOT reconstruct first-line indentation here.
    # OCR engines (tesseract / unlimited) strip leading spaces from the *text*
    # but the indent is already preserved by the bbox start x (blob.x0) we place
    # the text at.  Some engines (unlimited) even include the indent region in
    # the bbox but not in the text — trying to re-add spaces from x0 vs the page
    # left margin is unreliable and caused spurious indentation.

    # --- Derive the auto font size that fills the bbox ---
    fontsize = _derive_fontsize(lines, box_w, box_h, fontname)
    derived = fontsize

    # --- 2b. Apply the interactive font_scale gain (debug tool) ---
    try:
        fs_gain = float(font_scale) if font_scale is not None else 1.0
    except (TypeError, ValueError):
        fs_gain = 1.0
    if fs_gain <= 0:
        fs_gain = 1.0
    fontsize = max(fontsize * fs_gain, 0.5)

    # --- 3. Wrap each logical line to the bbox width ---
    wrapped = _wrap_to_width(lines, box_w, fontsize, fontname)

    # --- 4. If a genuine multi-line reflow overflows the bbox height, shrink ---
    # box_h is the *ink height of one line*, so a single line's natural line
    # box is normally taller than box_h and must NOT be shrunk.  We only shrink
    # when the text actually reflowed onto multiple wrapped lines and overflows.
    # When the user sets an explicit font_scale (interactive debug), that gain is
    # authoritative — skip the height-shrink so the preview shows the true size.
    _, line_height_em, ink_up = _font_metrics(fontname)
    line_height = fontsize * line_height_em
    total_h = len(wrapped) * line_height
    if fs_gain == 1.0 and len(wrapped) > 1 and box_h > 0 and total_h > box_h * 1.1:
        scale = box_h / total_h
        fontsize = max(fontsize * scale, 0.5)
        line_height = fontsize * line_height_em
        wrapped = _wrap_to_width(lines, box_w, fontsize, fontname)

    # --- 5. Compute baselines, vertically centring the text INK in the bbox ---
    # ink fills box_h by construction (fs = box_h / ink_fraction), so we centre
    # the (possibly multi-line) ink stack; a single line's ink top sits at the
    # bbox top and its ink bottom at the bbox bottom => visually centred.
    ink_fraction, line_height_em, ink_up = _font_metrics(fontname)
    line_height = fontsize * line_height_em
    n = len(wrapped)
    if n > 1:
        stack_ink = (n - 1) * line_height + ink_fraction * fontsize
        y_start = blob.y0 + (box_h - stack_ink) / 2 + ink_up * fontsize
    else:
        y_start = blob.y0 + (box_h - ink_fraction * fontsize) / 2 + ink_up * fontsize
    out_lines = []
    y_cursor = y_start
    for line in wrapped:
        out_lines.append((blob.x0, y_cursor, line, fontsize, fontname))
        y_cursor += line_height

    return {
        "fontname": fontname,
        "fontsize": fontsize,
        "derived": derived,
        "lines": out_lines,
    }


def render_overlay(pdf_bytes_path: str, pages: List[OcrPage],
                   page_index: int, out_dir: Optional[Path] = None,
                   for_page: Optional[int] = None,
                   embed_font=None) -> Path:
    """Render one page's scan with the placed text drawn VISIBLY (red).

    This is the interactive debug view: it shows exactly where/how big each
    block's text would be embedded (honouring each block's ``font_scale``), so
    the user can judge whether a line is too big or too small.  The placed text
    uses the same geometry as the real invisible embed (shared layout function),
    drawn with render_mode=0 and a red fill for visibility.

    Returns the path to the rendered PNG.
    """
    prev_font = ACTIVE_FONT
    set_embed_font(embed_font)
    out_dir = out_dir or ensure_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_bytes_path)
    try:
        # Page to render: the page a block lives on (for_page overrides).
        target = for_page if for_page is not None else page_index
        target = max(0, min(target, doc.page_count - 1))
        page = doc[target]
        rect = page.rect
        _page_font_name(page)  # register the active system font if selected

        page_cfg = next((p for p in pages if p.page_index == target), None)
        if page_cfg is None:
            raise ValueError(f"no OCR page data for page index {target}")
        w_scale = rect.width / page_cfg.width if page_cfg.width else 1.0
        h_scale = rect.height / page_cfg.height if page_cfg.height else 1.0

        out_file = out_dir / f"overlay_{target:04d}.png"
        for block in page_cfg.blocks:
            if block.kind in ("image", "image_ref") or not block.text.strip():
                continue
            layout = _compute_block_layout(block, page, w_scale, h_scale,
                                           font_scale=block.font_scale)
            for x, y_base, line, fontsize, fontname in layout["lines"]:
                if not line:
                    continue
                page.insert_text(
                    fitz.Point(x, y_base), line,
                    fontsize=fontsize, fontname=fontname,
                    render_mode=0, overlay=True, color=(0.9, 0.1, 0.1),
                )
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
        pix.save(str(out_file))
        return out_file
    finally:
        doc.close()
        set_embed_font(prev_font)


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

    ink_fraction, line_height_em, _ink_up = _font_metrics(fontname)

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

    # --- Single logical line ---
    text = lines[0]
    # Measure text width at 1pt to get the proportionality constant k
    # (get_text_length is linear in fontsize for most fonts).
    k = _text_width(text, 1.0, fontname)
    if k <= 0:
        return max(box_h / ink_fraction, 1.0)

    fs_h = box_h / ink_fraction            # font size that makes one line's
                                           # ink fill the bbox height
    w_at_h = _text_width(text, fs_h, fontname)
    # Case 1: the line fits within the bbox width (tiny tolerance only —
    # OCR ink-width is a hair narrow) at the height-filling size.
    if w_at_h <= box_w * _FILL_WIDTH_TOLERANCE:
        return max(fs_h, 1.0)

    # The line is too wide for the box at the height-filling size.  It is
    # either (a) a single OCR line whose box is simply too narrow (shrink to
    # fit the width, keep it on one line) or (b) a multi-line paragraph
    # (wrap into lines that fill box_h).  Solve self-consistently:
    #     n = ceil(text_width(fs) / box_w)   lines at font size fs
    #     fs = box_h / (n * line_height_em)  n lines fill box_h
    # If the iteration collapses to a single line (n == 1), it was a single
    # line squeezed by a narrow box → shrink to fit the width, not wrap.
    fs = math.sqrt(box_w * box_h / (k * line_height_em))
    for _ in range(12):
        n = max(1, math.ceil(_text_width(text, fs, fontname) / box_w - 1e-9))
        if n == 1:
            # single line that must shrink to fit width
            return max(fs_h * box_w / w_at_h, 0.5)
        fs_new = box_h / (n * line_height_em)
        if abs(fs_new - fs) < 0.2:
            fs = fs_new
            break
        fs = fs_new
    return max(fs, 0.5)


def _text_width(text: str, fontsize: float, fontname: str) -> float:
    """Measure rendered text width in PDF points (with fallback).

    Uses the active system font's real advance when one is set (correct narrow
    spaces / digit widths), otherwise the built-in font's get_text_length.
    """
    if ACTIVE_FONT is not None:
        try:
            return ACTIVE_FONT.fit().text_length(text, fontsize=fontsize)
        except Exception:  # noqa: BLE001
            pass
    try:
        return fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)
    except Exception:
        return len(text) * fontsize * 0.55


def _wrap_to_width(lines: list, box_w: float, fontsize: float,
                   fontname: str, tol: float | None = None) -> list:
    """Wrap each line to fit within *box_w* (in PDF points).

    Breaks at character boundaries (works for CJK and Latin).  Keeps all
    wrapped sub-lines of a single block consecutive to preserve reading order.
    ``tol`` is a tiny multiplicative overfill allowance (1.0 = exact fit); it
    exists to absorb sub-pixel OCR width rounding, not to permit visible
    overflow.
    """
    if tol is None:
        tol = _FILL_WIDTH_TOLERANCE
    line_limit = box_w * tol
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
    coverage (Greek, math operators, arrows, dashes, etc.):
      - CJK ideographs   -> 'china-s'
      - Japanese kana    -> 'japan'
      - Korean hangul    -> 'korea'
      - Greek letters    -> 'china-s'  (α β ε Σ etc.)
      - Math / arrows    -> 'china-s'  (≤ ≥ × ± ≠ ≈ ∞ ∫ √ → etc.)
      - em/en dashes & any other char outside Latin-1 -> 'china-s'
    The last is important: a stray `—` (U+2014), `−` (U+2212), superscript or
    similar glyph that 'helv' cannot render must not silently become a dot —
    it also makes get_text_length() mis-measure the width and overflow the box.
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
        # Helvetica (helv) is Latin-1 only.  Anything outside it (em/en dashes,
        # superscripts, box-drawing, currency glyphs, ...) must not go to helv.
        if cp > 0x00FF or 0x2013 <= cp <= 0x2015:
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


def _recompress_image(data, mode, quality, downscale=None):
    """Re-encode one image stream as (gray-)JPEG. Returns bytes or None.

    ``downscale`` accepts 2 (halve) or 4 (quarter) pixel dimensions.
    Images with an alpha channel are flattened onto the target colorspace
    (JPEG has no alpha); anything un-decodable yields None so the caller
    keeps the original stream untouched.
    """
    try:
        pix = fitz.Pixmap(data)
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)  # drop alpha channel
        # Pixmap.shrink(n) divides each dimension by 2**n.
        steps = {2: 1, 4: 2}.get(int(downscale or 0), 0)
        if steps and pix.width >= 32 and pix.height >= 32:
            pix.shrink(steps)
        if mode == "gray-jpeg":
            pix = fitz.Pixmap(fitz.csGRAY, pix)
        elif pix.n != 1:
            # ICC-based RGB / CMYK and friends: JPEG-bytes only after a
            # plain-DeviceRGB conversion (tobytes raises otherwise).
            pix = fitz.Pixmap(fitz.csRGB, pix)
        return pix.tobytes("jpeg", jpg_quality=int(quality))
    except Exception:  # noqa: BLE001
        return None


def optimize_images(doc, mode='none', quality=75, downscale=None) -> dict:
    """Recompress / downscale embedded images of *doc* when it helps.

    Scanned PDFs are the bulk of the file weight; re-encoding their page
    rasters to JPEG (optionally grayscale / downscaled) usually shrinks the
    output several-fold without touching the invisible text layer.
    Only replacements **smaller than the original** are applied; soft-masked
    images (SMask) and failures are skipped and counted.
    """
    stats = {'attempted': 0, 'replaced': 0, 'skipped': 0,
             'before_bytes': 0, 'after_bytes': 0, 'saved_bytes': 0}
    if mode not in ('jpeg', 'gray-jpeg'):
        return stats

    for pno in range(doc.page_count):
        page = doc[pno]
        for item in page.get_images(full=True):
            freq = item[0]
            smask = item[1]
            if smask > 0:
                stats['skipped'] += 1  # SMask flattening can alter look
                continue
            try:
                info = doc.extract_image(freq)
                data = info.get('image') or b''
                if not data:
                    stats['skipped'] += 1
                    continue
                stats['attempted'] += 1
                stats['before_bytes'] += len(data)
                new_data = _recompress_image(data, mode, quality, downscale)
                if new_data is None or len(new_data) >= len(data):
                    stats['skipped'] += 1
                    stats['after_bytes'] += len(data)
                    continue
                page.replace_image(freq, stream=new_data)
                stats['replaced'] += 1
                stats['after_bytes'] += len(new_data)
            except Exception as exc:  # noqa: BLE001
                stats['skipped'] += 1
                log.warning('image optimize: page %d xref %d skipped: %s',
                            pno, freq, exc)
    stats['saved_bytes'] = max(0, stats['before_bytes'] - stats['after_bytes'])
    return stats


