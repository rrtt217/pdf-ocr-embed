"""Unlimited-OCR engine client (OpenAI-compatible chat-completions API).

Two request shapes:

* **Single-page** ("document parsing.") — one image per request.
* **Multi-page** ("Multi page parsing.") — K page images per request; the raw
  response carries one ``<PAGE>``-delimited section per image
  (``parser.split_multi_page_stream``).

Common to both:

* ``skip_special_tokens=False`` — CRITICAL: the model outputs <|det|> markers
  as special tokens; the default stripping would empty the content.
* truncation detection (``finish_reason=length`` / usage >= max_tokens).
* one retry on degenerate results (the hosted vLLM endpoint occasionally
  returns a 1-token empty response for pages that DO contain text).

Configuration comes from ``settings.effective()``: plugin options (CLI/API),
``OCR_UNLIMITED_*`` env, or a host-injected snapshot — this module never reads
a host's config modules or hardcodes keys.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ocrmypdf_unlimited import settings as unlimited_settings
from ocrmypdf_unlimited.errors import UnavailableError
from ocrmypdf_unlimited.http_retry import RateLimiter, post_json_with_retry
from ocrmypdf_unlimited.parser import split_multi_page_stream

log = logging.getLogger(__name__)


class UnlimitedOcrClient:
    """One OpenAI-compatible client for the unlimited-ocr model."""

    # The model natively knows how to format output with <|det|> markers.
    # Per HuggingFace docs the prompt is just "document parsing." for a single
    # image.  No system prompt needed.
    SINGLE_PROMPT = "document parsing."

    # Multi-page mode ("Multi page parsing." per the model card / infer_multi):
    # K page images go into ONE request, and the response sections are separated
    # by <PAGE> markers (parser.split_multi_page_stream).  Multi-image is
    # base-mode only (no crop); the coordinates stay on the 1000x1000 canvas.
    MULTI_PROMPT = "Multi page parsing."

    # Output runs ~1.5-2.5k tokens per dense page (measured on the live
    # endpoint), so the default budget leaves generous headroom.
    DEFAULT_MAX_TOKENS = 16384

    # Output budget per page inside a multi-page batch.  A batch's max_tokens is
    # per_page_budget * page_count, capped by the configured per-request budget
    # (self.max_tokens) and the <32768 hard limit.  If a batch truncates, the
    # engine falls back to single-page requests (each with the full budget), so
    # a tight per-page budget here is safe.
    BATCH_PER_PAGE_TOKENS = 2048

    # HTTP read timeout scales with the token budget: the hosted endpoint
    # decodes at ~10-15 tok/s, so a full max_tokens generation needs minutes.
    READ_TIMEOUT_MIN = 900.0
    READ_TIMEOUT_PER_TOKEN = 0.08

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, max_tokens: int | None = None,
                 config: Dict[str, Any] | None = None):
        # No explicit config -> the plugin settings store: the host may push a
        # snapshot via settings.configure() (the engine passes the fully
        # resolved settings.effective() config instead).
        cfg = dict(config) if config is not None else unlimited_settings.snapshot()
        self.base_url = (base_url or cfg.get("base_url") or
                         "https://api.llm.ustc.edu.cn/v1").rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "unlimited-ocr"
        # Hard plugin invariant: max_tokens must stay < 32768.
        self.max_tokens = min(int(max_tokens or self.DEFAULT_MAX_TOKENS), 32767)
        # Multi-page batch: per-page output budget (see multi_payload).
        self.batch_per_page_tokens = min(
            int(cfg.get("ocr_batch_per_page_tokens") or self.BATCH_PER_PAGE_TOKENS),
            32767)
        # HTTP retry / rate-limit knobs.  These do NOT affect OCR output.
        self.max_retries = int(cfg.get("max_retries") or 3)
        self.retry_base_delay = float(cfg.get("retry_base_delay") or 1.0)
        self.retry_max_delay = float(cfg.get("retry_max_delay") or 30.0)
        self.rate_limit_rps = float(cfg.get("rate_limit_rps") or 0.0)
        self._rate_limiter = (
            RateLimiter.from_requests_per_second(self.rate_limit_rps)
            if self.rate_limit_rps > 0 else None
        )

    def cache_fingerprint(self) -> dict:
        """Output-affecting settings (endpoint, model, key identity)."""
        return {
            "engine": "unlimited",
            "base_url": self.base_url,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "api_key_sha": hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()
            if self.api_key else "",
        }

    def _post(self, payload: dict) -> dict:
        if not self.api_key:
            # Pure setup problem -> UnavailableError (surfaces the friendly
            # setup message instead of a stack trace).
            raise UnavailableError(
                "No api_key configured for the unlimited-OCR engine. "
                "Pass --unlimited-api-key (or set OCR_UNLIMITED_API_KEY), "
                "or have the host application push its configuration in."
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        log.debug("POST %s model=%s", url, payload.get("model"))
        t0 = time.time()
        # Read timeout must cover a full generation up to max_tokens on the
        # hosted endpoint (~10-15 tok/s).  Multi-page payloads carry their own
        # (larger) max_tokens, so the timeout scales with the payload's budget.
        budget = int(payload.get("max_tokens") or self.max_tokens)
        read_timeout = max(self.READ_TIMEOUT_MIN,
                           budget * self.READ_TIMEOUT_PER_TOKEN)
        with httpx.Client(timeout=httpx.Timeout(read_timeout,
                                                connect=30.0)) as client:
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

    def _encode_image(self, image_path: str | Path) -> str:
        with open(image_path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")

    def single_payload(self, image_path: str | Path) -> Dict[str, Any]:
        """Build the single-image payload ("document parsing.")."""
        b64 = self._encode_image(image_path)
        return {
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

    def recognize(self, image_path: str | Path) -> str:
        """Single-page OCR: return the raw marker stream.

        One retry on degenerate results (the hosted endpoint intermittently
        returns degenerate output for pages that DO contain text: a 1-token
        empty response, or a lone whole-page image marker).  Either shape is
        retried once; a still-degenerate result keeps its natural semantics.
        """
        text = ""
        for attempt in range(2):
            raw = self._post(self.single_payload(image_path))
            self._assert_not_truncated(raw, self.max_tokens)
            candidate = self._extract_content(raw)
            if candidate.strip() and not self._looks_degenerate_raw(candidate):
                return candidate
            if candidate.strip():
                text = candidate
            if attempt == 0:
                log.warning("OCR result looks degenerate (no text blocks); "
                            "retrying once")
        return text

    def multi_payload(self, image_paths: List) -> Dict[str, Any]:
        """Build the multi-page payload ("Multi page parsing." + K images).

        All page images go into ONE user message.  The max_tokens budget is
        ``batch_per_page_tokens * K`` capped by the configured per-request
        budget (self.max_tokens) — the hard ``< 32768`` invariant holds because
        self.max_tokens is already clamped in the constructor.
        """
        content: List[Dict[str, Any]] = [{"type": "text", "text": self.MULTI_PROMPT}]
        for image_path in image_paths:
            b64 = self._encode_image(image_path)
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        n = max(1, len(image_paths))
        max_tokens = min(self.max_tokens,
                         self.batch_per_page_tokens * n)
        return {
            "model": self.model,
            "max_tokens": max_tokens,  # must stay < 32768
            "temperature": 0.0,
            "skip_special_tokens": False,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                },
            ],
        }

    def recognize_multi(self, image_paths: List) -> List[str]:
        """Multi-page OCR: ONE request for K page images.

        Returns the raw marker stream per image, index-aligned with
        ``image_paths`` (parser.split_multi_page_stream).  Raises on truncation
        (``finish_reason=length``) or a fully empty response; a degenerate
        (text-less) batch is retried once, mirroring ``recognize``.  Per-page
        empties are handed back as "" so the caller can fall back per page.
        """
        if len(image_paths) < 2:
            return [self.recognize(image_paths[0])]
        payload = self.multi_payload(image_paths)
        for attempt in range(2):
            raw = self._post(payload)
            self._assert_not_truncated(raw, payload["max_tokens"])
            content = self._extract_content(raw)
            if not content:
                raise RuntimeError(
                    "Empty multi-page OCR response "
                    f"({len(image_paths)} image(s))")
            if not self._looks_degenerate_raw(content):
                return split_multi_page_stream(content, len(image_paths))
            if attempt == 0:
                log.warning("multi-page OCR result looks degenerate "
                            "(no text blocks); retrying once")
        return split_multi_page_stream(content, len(image_paths))

    @staticmethod
    def _looks_degenerate_raw(text: str) -> bool:
        """True when a marker stream carries no usable text at all.

        Covers a lone whole-page image marker (the page "seen" as a pure
        figure).  Any text/table/equation/caption content means the page was
        read and is not retried.
        """
        import re
        for match in re.finditer(
                r"<\|det\|>(?P<kind>[a-z_]+)[^<]*<\|/det\|>(?P<content>.*?)(?=<\|det\|>|\Z)",
                text, re.DOTALL):
            kind = (match.group("kind") or "").strip().lower()
            content = (match.group("content") or "").strip()
            if kind in ("image", "image_ref"):
                continue
            if content:
                return False
        return True

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
    def _assert_not_truncated(raw: dict, max_tokens: int) -> None:
        """Raise when the response hit the max_tokens ceiling.

        ``finish_reason == "length"`` is the canonical signal; some providers
        only report usage, so ``completion_tokens >= max_tokens`` is checked
        as a fallback.
        """
        reason = UnlimitedOcrClient._extract_finish_reason(raw)
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
                f"OCR output truncated (finish_reason={reason or 'usage>=max_tokens'}, "
                f"max_tokens={max_tokens}). Re-run this page, or raise "
                f"max_tokens (must stay < 32768).")


def image_size(image_path: str | Path) -> tuple[int, int]:
    """Return (width, height) of a rasterized page image."""
    from PIL import Image
    with Image.open(image_path) as img:
        return int(img.width), int(img.height)


def image_dpi(image_path: str | Path, default: float = 300.0) -> float:
    """Return the DPI saved in the rasterized page image (fallback default).

    ocrmypdf writes square DPI into its page images; the hOCR ``scan_res``
    MUST carry the true DPI or the renderer's px->pt transform places text at
    the wrong scale.
    """
    from PIL import Image
    try:
        with Image.open(image_path) as img:
            dpi_info = img.info.get("dpi")
            if isinstance(dpi_info, tuple) and dpi_info[0]:
                return float(dpi_info[0])
            if dpi_info:
                return float(dpi_info)
    except Exception:  # noqa: BLE001
        log.debug("dpi read failed for %s", image_path, exc_info=True)
    return default


def split_pages_param(specs: List) -> str:
    """Format a list of 1-based page numbers as an ocrmypdf ``pages`` string."""
    return ",".join(str(s) for s in specs)
