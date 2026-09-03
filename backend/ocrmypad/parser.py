"""Unlimited-OCR output parser: markers -> blocks -> hOCR / editor JSON.

Parses the ``<|det|>type [x1,y1,x2,y2]<|/det|>content`` marker format, maps the
1000x1000 normalized canvas bboxes back to real pixel coordinates using the
page image width/height, and produces:

* a list of :class:`Block` (the parsed, normalized page content), and
* one hOCR document per page (``blocks_to_hocr``) that ocrmypdf's fpdf2
  renderer turns into the invisible text layer.

All block bboxes are **integers in raw pixel space** (top-left origin) — the
same invariant the rest of this project has always preserved.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from backend.errors import normalize_bbox as _normalize_bbox
from backend.ocrmypad import text_norm

log = logging.getLogger(__name__)

# Bump whenever the raw-output -> Block mapping changes, so a pre-change
# cached/sidecar result keeps serving stale content that the new parser would
# have handled differently.
PARSE_VERSION = 6

_MARKER_RE = re.compile(
    r"<\|det\|>(?P<kind>[a-z_]+)(?:\s*\[(?P<bbox>[-+0-9.,\s]+)\])?<\|/det\|>(?P<content>.*?)(?=<\|det\|>|\Z)",
    re.DOTALL,
)


@dataclass
class Block:
    """One recognized block on a page (bbox in raw pixel space)."""

    kind: str
    bbox: List[int]
    text: str = ""
    caption: str = ""
    caption_bbox: Optional[List[int]] = None
    conf: Optional[float] = None
    font_scale: float = 1.0
    lines: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {"kind": self.kind, "bbox": self.bbox, "text": self.text}
        if self.caption:
            d["caption"] = self.caption
        if self.caption_bbox:
            d["caption_bbox"] = self.caption_bbox
        if self.conf is not None:
            d["conf"] = self.conf
        if self.font_scale != 1.0:
            d["font_scale"] = self.font_scale
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Block":
        fs = data.get("font_scale")
        try:
            font_scale = float(fs) if fs is not None else 1.0
        except (TypeError, ValueError):
            font_scale = 1.0
        caption_bbox = data.get("caption_bbox") or None
        if caption_bbox is not None:
            caption_bbox = [int(float(v)) for v in caption_bbox]
        conf = data.get("conf")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        return cls(
            kind=data.get("kind", "text"),
            bbox=[int(float(v)) for v in data.get("bbox", [0, 0, 0, 0])],
            text=data.get("text", ""),
            caption=data.get("caption", ""),
            caption_bbox=caption_bbox,
            conf=conf,
            font_scale=font_scale,
        )


@dataclass
class Page:
    """Parsed OCR result for one page (blocks in raw pixel space)."""

    page_index: int
    width: int
    height: int
    blocks: List[Block] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page_index": self.page_index,
            "width": self.width,
            "height": self.height,
            "parse_version": PARSE_VERSION,
            "blocks": [b.to_dict() for b in self.blocks],
        }


def split_block_lines(text: str) -> List[str]:
    """Split a block's text into renderable lines.

    The block text may contain tabs (table cell separators); a tab becomes a
    space-separated gap inside one line so the whole row stays on one baseline.
    """
    return [ln.strip() for ln in text.replace("\t", "  ").split("\n") if ln.strip()]


def parse_response(text: str, width: int, height: int, page_index: int) -> Page:
    """Parse one page's marker stream into a normalized Page."""
    blocks: List[Block] = []
    pending_caption: Optional[Block] = None

    for match in _MARKER_RE.finditer(text):
        kind = (match.group("kind") or "text").strip().lower()
        bbox_str = match.group("bbox")
        content = (match.group("content") or "").strip()

        bbox = _parse_bbox(bbox_str)
        if bbox is None:
            continue
        px_bbox = _normalize_bbox(bbox, width, height)

        if pending_caption is not None and kind != "image_caption":
            # Any marker other than the expected caption closes the current
            # figure binding.
            blocks.append(pending_caption)
            pending_caption = None

        if kind == "image_caption":
            caption_text = text_norm.clean_math_spacing(
                text_norm.latex_to_plain(content))
            if pending_caption is not None:
                # Bind the caption text to its figure, keeping this marker's
                # bbox as the caption bbox.
                pending_caption.caption = caption_text
                pending_caption.caption_bbox = px_bbox
                blocks.append(pending_caption)
                pending_caption = None
            else:
                # No preceding <|det|>image: keep the caption in its own
                # block with text set so embedding writes it into the PDF
                # text layer.
                blocks.append(Block(kind="image_caption", bbox=px_bbox,
                                    text=caption_text))
            continue

        if kind in ("image", "image_ref") and not content:
            pending_caption = Block(kind="image", bbox=px_bbox)
            continue

        block_text = text_norm.normalize_engine_text(kind, content)
        blocks.append(Block(kind=kind, bbox=px_bbox, text=block_text,
                            lines=split_block_lines(block_text)))

    if pending_caption is not None:
        blocks.append(pending_caption)

    return Page(page_index=page_index, width=width, height=height,
                blocks=blocks)


def _parse_bbox(bbox_str: Optional[str]) -> Optional[List[int]]:
    if not bbox_str:
        return None
    parts = [p for p in re.split(r"[\s,]+", bbox_str.strip()) if p]
    if len(parts) != 4:
        return None
    try:
        return [int(float(p)) for p in parts]
    except ValueError:
        return None


def _hocr_escape(text: str) -> str:
    """Escape text for embedding in the hOCR (X)HTML document."""
    import html as _html
    return _html.escape(text, quote=True)


def _line_bboxes(block: Block, n_lines: int) -> List[List[int]]:
    """Distribute a block's bbox vertically across ``n_lines`` equal rows.

    Only used for the invisible text layer: the model reports block-level
    bboxes, and each renderable line gets an equal vertical slice.  A line
    never extends outside its block's bbox.
    """
    x1, y1, x2, y2 = block.bbox
    if n_lines <= 0:
        return []
    height = y2 - y1
    bboxes = []
    for i in range(n_lines):
        top = y1 + int(round(height * i / n_lines))
        bottom = y1 + int(round(height * (i + 1) / n_lines))
        if bottom <= top:
            bottom = top + 1
        bboxes.append([x1, top, x2, min(bottom, y2)])
    return bboxes


def blocks_to_hocr(width: int, height: int, blocks: List[Block],
                   dpi: float = 300.0, ppageno: int = 0,
                   per_line_overrides: Optional[dict] = None) -> str:
    """Render one page's blocks as an hOCR document for ocrmypdf.

    Structure (what ``ocrmypdf.hocrtransform`` parses):
      div.ocr_page (title: bbox + ppageno + scan_res)
        p.ocr_par (title: bbox)
          span.ocr_line (title: bbox)  -- one per renderable line
            span.ocrx_word (title: bbox)  -- one full-width word per line

    ``per_line_overrides`` (optional): ``{block_index: [(line_text, bbox), ...]}``
    overrides a block's derived render lines AND their bboxes with explicit
    (text, integer-bbox) pairs — used to place each rendered line at its actual
    printed location (see ``backend.ocrmypad.line_split``).  When absent or
    empty, lines fall back to the block's own text split with equal-slice
    bboxes — this function's long-standing behavior.

    Gotchas honored:
      * ``scan_res`` MUST be present: the renderer's px->pt transform derives
        from it; a missing value would place text at the wrong scale.
      * an ocr_line with no ocrx_word child is DROPPED by the parser, so every
        line carries exactly one word span.
      * pure image blocks (no text, no caption) contribute nothing.
    """
    dpi_i = max(1, int(round(dpi)))
    body: List[str] = []
    for block_index, block in enumerate(blocks):
        override = None
        if per_line_overrides:
            override = per_line_overrides.get(block_index)
        if override:
            render_pairs = [(str(t), [int(v) for v in bb])
                            for t, bb in override
                            if str(t).strip() and len(bb) == 4]
        else:
            render_lines = list(block.lines)
            if not render_lines and block.text.strip():
                render_lines = split_block_lines(block.text)
            if not render_lines and block.caption.strip():
                # A figure whose caption is its only text: place the caption.
                render_lines = [block.caption.strip()]
            render_pairs = [
                (line, lb)
                for line, lb in zip(render_lines,
                                    _line_bboxes(block, len(render_lines)))
            ]
        if not render_pairs:
            continue

        line_class = _hocr_line_class(block.kind)
        par_lines: List[str] = []
        for line_text, lb in render_pairs:
            escaped = _hocr_escape(line_text)
            title = f"bbox {lb[0]} {lb[1]} {lb[2]} {lb[3]}"
            par_lines.append(
                f'  <span class="{line_class}" title="{title}">'
                f'<span class="ocrx_word" title="{title}">{escaped}</span>'
                f"</span>"
            )
        if not par_lines:
            continue
        b = block.bbox
        body.append(
            f' <p class="ocr_par" title="bbox {b[0]} {b[1]} {b[2]} {b[3]}">\n'
            + "\n".join(par_lines)
            + "\n </p>"
        )

    body_text = "\n".join(body)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"\n'
        '    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="und" lang="und">\n'
        "<head>\n"
        "<title>Unlimited-OCR page</title>\n"
        '<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
        "<meta name='ocr-system' content='Unlimited-OCR (pdf-ocr-embed ocrmypad plugin)'/>\n"
        "</head>\n"
        "<body>\n"
        f"<div class='ocr_page' id='page_{ppageno + 1}' "
        f"title='bbox 0 0 {width} {height}; ppageno {ppageno}; "
        f"scan_res {dpi_i} {dpi_i}'>\n"
        f"{body_text}\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


def _hocr_line_class(kind: str) -> str:
    """Map our block kind to the closest standard hOCR line class."""
    return {
        "heading": "ocr_header",
        "footnote": "ocr_footer",
        "image_caption": "ocr_caption",
    }.get(kind, "ocr_line")


def hocr_page_sidecar_text(blocks: List[Block]) -> str:
    """Plain-text sidecar for a page (joined block text, for ocrmypdf's txt)."""
    parts: List[str] = []
    for block in blocks:
        text = block.text.strip()
        if not text and block.caption.strip():
            text = block.caption.strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts) + ("\n" if parts else "")
