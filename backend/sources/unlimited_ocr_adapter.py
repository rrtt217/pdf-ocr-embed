"""Unlimited-OCR adapter (USTC / Baidu style OpenAI-compatible API).

Parses the `<|det|>type [x1,y1,x2,y2]<|/det|>content` marker format, maps the
1000x1000 normalized canvas bboxes back to real pixel coordinates using the page
image width/height, and returns a normalized OcrPage.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from backend.config import resolve
from backend.models import OcrBlock, OcrPage
from backend.sources.base import OcrSource, normalize_bbox

log = logging.getLogger(__name__)

_LATEX_CONV = None


def _get_latex_conv():
    global _LATEX_CONV
    if _LATEX_CONV is None:
        from pylatexenc.latex2text import LatexNodes2Text
        _LATEX_CONV = LatexNodes2Text()
    return _LATEX_CONV


def _latex_to_plain(text: str) -> str:
    """Convert LaTeX math delimiters/commands to readable plain text.

    Uses pylatexenc for robust conversion, then cleans up whitespace and
    spacing artifacts (especially from display math \\[ ... \\] blocks).
    """
    if not text or "\\" not in text:
        return text
    try:
        conv = _get_latex_conv()
        result = conv.latex_to_text(text)
    except Exception:
        return text  # if conversion fails, keep original

    # Clean up: collapse newlines from \[ \] display math into spaces,
    # collapse multiple spaces, strip per-line.
    lines = [ln.strip() for ln in result.splitlines() if ln.strip()]
    result = " ".join(lines)
    # Collapse multiple spaces (but keep single spaces between CJK/words).
    result = re.sub(r" {2,}", " ", result).strip()
    # Tighten "| x |" → "|x|" for readability.
    result = re.sub(r"\|\s+", "|", result)
    result = re.sub(r"\s+\|", "|", result)
    # Map ⩽ (U+2A7D, \leqslant) → ≤ (U+2264) — china-s font lacks ⩽ but has ≤.
    result = result.replace("\u2a7d", "\u2264")
    result = result.replace("\u2a7e", "\u2265")  # ⩾ → ≥
    # Map ⇒ (U+21D2) → → (U+2192) — china-s lacks double-arrow.
    result = result.replace("\u21d2", "\u2192")
    result = result.replace("\u21d0", "\u2190")  # ⇐ → ←
    result = result.replace("\u21d4", "\u2194")  # ⇔ → ↔
    # Fix "1 0" artifact from OCR (model inserts space in numbers like "10").
    result = re.sub(r"\b(\d)\s+(\d)\b", r"\1\2", result)
    # Fix "x ^ *" → "x^*" (pylatexenc adds spaces around ^).
    result = re.sub(r"\s*\^\s*", "^", result)
    # Fix "10^- 4" → "10^-4" (space after sign in exponents).
    result = re.sub(r"(\^[-+])\s+", r"\1", result)
    # Fix "× 5" → "×5" (space after operator in formulas).
    result = re.sub(r"([×÷±≤≥⩽⩾≠≈≡∼∈∉⊂⊃∪∩→←↑↓⇒⇐⇔])\s+", r"\1", result)
    return result

_MARKER_RE = re.compile(
    r"<\|det\|>(?P<kind>[a-z_]+)(?:\s*\[(?P<bbox>[0-9,\s]+)\])?<\|/det\|>(?P<content>.*?)(?=<\|det\|>|\Z)",
    re.DOTALL,
)


class UnlimitedOcrAdapter(OcrSource):
    name = "unlimited"

    # The model natively knows how to format output with <|det|> markers.
    # Per HuggingFace docs the prompt is just "document parsing." for single
    # image, "Multi page parsing." for multi-image.  No system prompt needed.
    SINGLE_PROMPT = "document parsing."
    MULTI_PROMPT = "Multi page parsing."

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None):
        cfg = resolve()
        self.base_url = (base_url or cfg.get("base_url") or
                         "https://api.llm.ustc.edu.cn/v1").rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "unlimited-ocr"

    def _chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _post(self, payload: dict) -> dict:
        if not self.api_key:
            raise RuntimeError(
                "No api_key configured. Set `api_key` in backend/ocr_config.toml, "
                "or fill in the WebUI settings page."
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = self._chat_url()
        log.debug("POST %s model=%s", url, payload.get("model"))
        t0 = time.time()
        # All responses stream as a single line; pass a generous read timeout.
        with httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
            resp = client.post(url, json=payload, headers=headers)
            log.debug("response %d in %.1fs (%d bytes)", resp.status_code,
                      time.time() - t0, len(resp.content))
            resp.raise_for_status()
            return resp.json()

    def _encode_image(self, image_path: str) -> str:
        with open(image_path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")

    def recognize_pixels(self, image_path: str, width: int, height: int,
                         page_index: int) -> OcrPage:
        b64 = self._encode_image(image_path)
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16384,  # must stay < 32768
            "temperature": 0.0,
            # CRITICAL: the model outputs <|det|>...<|/det|> markers as special
            # tokens.  If skip_special_tokens is True (the default), they are
            # stripped and the content becomes empty.  Must set False.
            "skip_special_tokens": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.SINGLE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                },
            ],
        }
        raw = self._post(payload)
        text = self._extract_content(raw)
        log.debug("page %d: OCR returned %d chars", page_index, len(text))
        if not text:
            log.warning("page %d: empty OCR response, usage=%s",
                        page_index, raw.get("usage"))
        return self.parse_response(text, width, height, page_index)

    @staticmethod
    def _extract_content(raw: dict) -> str:
        try:
            return raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def parse_response(self, text: str, width: int, height: int,
                       page_index: int) -> OcrPage:
        blocks: List[OcrBlock] = []
        pending_caption: Optional[OcrBlock] = None

        for match in _MARKER_RE.finditer(text):
            kind = (match.group("kind") or "text").strip().lower()
            bbox_str = match.group("bbox")
            content = (match.group("content") or "").strip()

            bbox = self._parse_bbox(bbox_str)
            if bbox is None:
                continue
            px_bbox = normalize_bbox(bbox, width, height)

            if kind == "image_caption":
                if pending_caption is not None:
                    pending_caption.caption = content
                    blocks.append(pending_caption)
                    pending_caption = None
                else:
                    blocks.append(OcrBlock(
                        kind="image_caption", bbox=px_bbox, caption=content))
                continue

            if kind in ("image", "image_ref") and not content:
                pending_caption = OcrBlock(kind="image", bbox=px_bbox)
                continue

            blocks.append(OcrBlock(kind=kind, bbox=px_bbox, text=_latex_to_plain(content)))

        if pending_caption is not None:
            blocks.append(pending_caption)

        # Merge adjacent table/equation segments from multi-page marker runs
        # that got split by content detection edge cases.
        return OcrPage(
            page_index=page_index,
            width=width,
            height=height,
            blocks=blocks,
        )

    @staticmethod
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