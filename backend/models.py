"""Internal normalized OCR schema.

Every OCR engine adapter parses its raw output into one `OcrPage` per PDF page.
All coordinates here live in the *original pixel space* of the page image
(converters are responsible for mapping any normalized canvas -> raw pixels).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class OcrBlock:
    """A single recognized block on a page.

    bbox is [x1, y1, x2, y2] in raw pixel coordinates (top-left origin),
    matching the page image dimensions given by `width` / `height`.
    """

    kind: str
    bbox: List[int]
    text: str = ""
    caption: str = ""
    conf: Optional[float] = None
    # Interactive per-block font-size gain for the debug tool (1.0 = auto).
    # When set, the embedded font size is derived font size * font_scale.
    font_scale: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "kind": self.kind,
            "bbox": self.bbox,
            "text": self.text,
        }
        if self.caption:
            d["caption"] = self.caption
        if self.conf is not None:
            d["conf"] = self.conf
        if self.font_scale != 1.0:
            d["font_scale"] = self.font_scale
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OcrBlock":
        fs = data.get("font_scale")
        try:
            font_scale = float(fs) if fs is not None else 1.0
        except (TypeError, ValueError):
            font_scale = 1.0
        return cls(
            kind=data.get("kind", "text"),
            bbox=list(data.get("bbox", [0, 0, 0, 0])),
            text=data.get("text", ""),
            caption=data.get("caption", ""),
            conf=data.get("conf"),
            font_scale=font_scale,
        )


@dataclass
class OcrPage:
    """Normalized OCR result for a single PDF page."""

    page_index: int
    width: int
    height: int
    blocks: List[OcrBlock] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_index": self.page_index,
            "width": self.width,
            "height": self.height,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OcrPage":
        return cls(
            page_index=int(data.get("page_index", 0)),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            blocks=[OcrBlock.from_dict(b) for b in data.get("blocks", [])],
        )


def page_to_dict(page: OcrPage) -> Dict[str, Any]:
    return page.to_dict()


def dict_to_page(data: Dict[str, Any]) -> OcrPage:
    return OcrPage.from_dict(data)


def blocks_to_dicts(blocks: List[OcrBlock]) -> List[Dict[str, Any]]:
    return [b.to_dict() for b in blocks]


def dicts_to_blocks(data: List[Dict[str, Any]]) -> List[OcrBlock]:
    return [OcrBlock.from_dict(d) for d in data]