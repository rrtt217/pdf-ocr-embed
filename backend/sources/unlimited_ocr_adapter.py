"""Unlimited-OCR adapter (USTC / Baidu style OpenAI-compatible API).

Parses the `<|det|>type [x1,y1,x2,y2]<|/det|>content` marker format, maps the
1000x1000 normalized canvas bboxes back to real pixel coordinates using the page
image width/height, and returns a normalized OcrPage.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from backend.config import resolve
from backend.models import OcrBlock, OcrPage
from backend.sources.base import OcrSource, normalize_bbox
from backend.sources.http_utils import RateLimiter, post_json_with_retry

log = logging.getLogger(__name__)

_LATEX_CONV = None


def _get_latex_conv():
    global _LATEX_CONV
    if _LATEX_CONV is None:
        from pylatexenc.latex2text import LatexNodes2Text
        _LATEX_CONV = LatexNodes2Text()
    return _LATEX_CONV


# The unlimited model renders formulas as spaced-out tokens ("X _ p = f (x)").
# These rules collapse that spacing *inside* a math expression without ever
# touching normal words; they run only on equation blocks and per table cell.
# A single space between two digits is deliberately NOT merged: it may encode
# matrix/list separators that must survive.
_MATH_FIX_RULES: tuple[tuple[str, str], ...] = (
    # "X _ p"/"X_ p" -> "X_p"; "max _ 1" -> "max_1"
    (r"[ \t]*_[ \t]*", "_"),
    # "x  (y+1)" -> "x(y+1)"
    (r"([A-Za-z0-9])[ \t]+\(", r"\1("),
    # ") (" -> ")("
    (r"\)[ \t]+\(", ")("),
    # multiple consecutive tokenization spaces -> one
    (r"[ \t]{2,}", " "),
    # "≤ 5" -> "≤5"; always safe in math
    (r"([×÷±≤≥≠≈≡∼∈∉⊂⊃∪∩→←↑↓])[ \t]+", r"\1"),
    # ")x", "]x" etc. after close paren/bracket
    (r"\)[ \t]+([A-Za-z0-9\[{])", r")\1"),
)


def _clean_math_spacing(text: str) -> str:
    """Collapse the tokenized spacing the model uses inside math blocks.

    Applied only to ``equation`` blocks and to each table cell, where every
    space between tokens is model padding rather than meaningful whitespace.
    """
    if not text:
        return text
    result = text.strip()
    for pattern, repl in _MATH_FIX_RULES:
        result = re.sub(pattern, repl, result)
    return result


def _tidy_ocr_text(text: str) -> str:
    """Safe glyph + whitespace normalization applied to every text block.

    Never merges two adjacent alphanumeric tokens across a single space.
    """
    if not text:
        return text
    result = text
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
    # Fix "x ^ *" -> "x^*" (also applies to tokenized plain math).
    result = re.sub(r"[ \t]*\^[ \t]*", "^", result)
    # Fix "10^- 4" -> "10^-4".
    result = re.sub(r"(\^[-+])[ \t]+", r"\1", result)
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"[ \t]+\n", "\n", result)
    return result.strip()


def _latex_to_plain(text: str) -> str:
    """Convert LaTeX math delimiters/commands to readable plain text.

    Uses pylatexenc for robust conversion when the text contains real LaTeX
    commands (e.g. ``\\frac``); otherwise the raw content is returned and put
    through the same safe tidy.  Math-spacing tightening is handled separately
    in ``_clean_math_spacing``.
    """
    if not text or "\\" not in text:
        return _tidy_ocr_text(text)

    try:
        result = _get_latex_conv().latex_to_text(text)
    except Exception:
        # pylatexenc can choke on mixed plain/Latex fragments; keep the
        # recognized content rather than dropping it.
        result = text

    # Display math (\\[ ... \\]) introduces blank lines around itself.
    lines = [ln.strip() for ln in result.splitlines() if ln.strip()]
    result = " ".join(lines)
    return _tidy_ocr_text(result)


def _table_html_to_text(content: str) -> str:
    """Turn the model's HTML table fragment into searchable plain rows.

    Each ``<tr>`` becomes one line and each ``<td>/<th>`` cell is separated
    by a tab.  Partial/non-HTML fragments simply have their tags stripped so
    no markup ever reaches the searchable text layer.
    """
    if not content:
        return ""
    import html as _html

    raw = _html.unescape(content).replace("\u00a0", " ")
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", raw, re.S | re.I)
    if not rows:
        # Partial / invalid table: drop every tag so no markup reaches the
        # searchable text layer.
        return " ".join(re.sub(r"<[^>]+>", " ", raw).split())

    lines: List[str] = []
    for row in rows:
        cells = re.findall(r"<t(?:h|d)\b[^>]*>(.*?)</t(?:h|d)>",
                           row, re.S | re.I)
        if not cells:
            cells = re.split(r"</t(?:h|d)>", row, flags=re.I)
        cell_texts: List[str] = []
        for cell in cells:
            cell = re.sub(r"<[^>]+>", " ", cell).strip()
            cell = " ".join(cell.split())
            # Math-spacing cleanup runs per cell (not per row) so the tab
            # separators between table cells survive.
            cell_texts.append(_clean_math_spacing(cell))
        lines.append("\t".join(cell for cell in cell_texts if cell))
    return "\n".join(line for line in lines if line.strip()).strip()


def _normalize_engine_text(kind: str, content: str) -> str:
    if kind == "table":
        return _tidy_ocr_text(_table_html_to_text(content))
    if kind == "equation":
        return _clean_math_spacing(_latex_to_plain(content))
    return _latex_to_plain(content)


_MARKER_RE = re.compile(
    r"<\|det\|>(?P<kind>[a-z_]+)(?:\s*\[(?P<bbox>[0-9,\s]+)\])?<\|/det\|>(?P<content>.*?)(?=<\|det\|>|\Z)",
    re.DOTALL,
)


class UnlimitedOcrAdapter(OcrSource):
    name = "unlimited"
    # Bump this whenever the raw-output -> OcrPage mapping changes: otherwise
    # a pre-change cached OcrPage keeps serving stale block content that the
    # new parser would have handled differently.
    PARSE_VERSION = 3

    # The model natively knows how to format output with <|det|> markers.
    # Per HuggingFace docs the prompt is just "document parsing." for single
    # image, "Multi page parsing." for multi-image.  No system prompt needed.
    SINGLE_PROMPT = "document parsing."
    MULTI_PROMPT = "Multi page parsing."

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, max_tokens: int | None = None):
        cfg = resolve()
        self.base_url = (base_url or cfg.get("base_url") or
                         "https://api.llm.ustc.edu.cn/v1").rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "unlimited-ocr"
        # Hard backend invariant: max_tokens must stay < 32768.
        self.max_tokens = min(int(max_tokens or 16384), 32767)
        # HTTP retry / rate-limit knobs (shared HTTP-adapter settings, see
        # backend/sources/http_utils.py).  These do NOT affect OCR output, so
        # they are intentionally absent from cache_fingerprint().
        self.max_retries = int(cfg.get("max_retries") or 3)
        self.retry_base_delay = float(cfg.get("retry_base_delay") or 1.0)
        self.retry_max_delay = float(cfg.get("retry_max_delay") or 30.0)
        self.rate_limit_rps = float(cfg.get("rate_limit_rps") or 0.0)
        self._rate_limiter = (
            RateLimiter.from_requests_per_second(self.rate_limit_rps)
            if self.rate_limit_rps > 0 else None
        )

    def _chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def cache_fingerprint(self) -> dict:
        """Output-affecting settings: endpoint, model, and key identity.

        Retry / rate-limit knobs are deliberately NOT included: they change
        call behavior but never the OCR output, so they must not alter the
        result-cache key.
        """
        import hashlib
        return {
            "engine": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "parser_version": self.PARSE_VERSION,
            "max_tokens": self.max_tokens,
            "api_key_sha": hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()
            if self.api_key else "",
        }

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
            resp = post_json_with_retry(
                client, url, json=payload, headers=headers,
                max_retries=self.max_retries,
                base_delay=self.retry_base_delay,
                max_delay=self.retry_max_delay,
                rate_limiter=self._rate_limiter,
            )
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
            "max_tokens": self.max_tokens,  # must stay < 32768
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
        # A truncated response silently drops the tail of the page (the last
        # <|det|> markers never arrive) — that must be a page *failure*, not a
        # silently-partial success, so the retry flow re-runs it.
        self._assert_not_truncated(raw, self.max_tokens, page_index)
        text = self._extract_content(raw)
        log.debug("page %d: OCR returned %d chars", page_index, len(text))
        if not text:
            # Likely a blank page (finish_reason=stop + no content).  Kept as a
            # valid empty result — validation flags it via empty_source, and a
            # force re-run re-recognizes it.
            log.warning("page %d: empty OCR response (blank page?), usage=%s",
                        page_index, raw.get("usage"))
        return self.parse_response(text, width, height, page_index)

    @staticmethod
    def _extract_content(raw: dict) -> str:
        try:
            return raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _extract_finish_reason(raw: dict) -> Optional[str]:
        try:
            return raw["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            return None

    @staticmethod
    def _assert_not_truncated(raw: dict, max_tokens: int,
                              page_index: int) -> None:
        """Raise when the response hit the max_tokens ceiling.

        ``finish_reason == "length"`` is the canonical signal; some providers
        only report usage, so ``completion_tokens >= max_tokens`` is checked
        as a fallback.
        """
        reason = UnlimitedOcrAdapter._extract_finish_reason(raw)
        truncated = reason == "length"
        if not truncated:
            try:
                usage = raw.get("usage") or {}
                completion = usage.get("completion_tokens")
                truncated = completion is not None and completion >= max_tokens
            except (AttributeError, TypeError):
                truncated = False
        if truncated:
            raise RuntimeError(
                f"page {page_index}: OCR output truncated "
                f"(finish_reason={reason or 'usage>=max_tokens'}, "
                f"max_tokens={max_tokens}). Re-run this page, or raise "
                f"max_tokens (must stay < 32768).")

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

            if pending_caption is not None and kind != "image_caption":
                # Any marker other than the expected caption closes the current
                # figure binding.
                blocks.append(pending_caption)
                pending_caption = None

            if kind == "image_caption":
                caption_text = _clean_math_spacing(_latex_to_plain(content))
                if pending_caption is not None:
                    # Bind the caption text to its figure, keeping this
                    # marker's bbox as the caption bbox.
                    pending_caption.caption = caption_text
                    pending_caption.caption_bbox = px_bbox
                    blocks.append(pending_caption)
                    pending_caption = None
                else:
                    # No preceding <|det|>image: keep the caption in its own
                    # block with text set so embedding writes it into the PDF
                    # text layer.
                    blocks.append(OcrBlock(
                        kind="image_caption", bbox=px_bbox,
                        text=caption_text))
                continue

            if kind in ("image", "image_ref") and not content:
                pending_caption = OcrBlock(kind="image", bbox=px_bbox)
                continue

            blocks.append(OcrBlock(kind=kind, bbox=px_bbox,
                                   text=_normalize_engine_text(kind, content)))

        if pending_caption is not None:
            blocks.append(pending_caption)

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