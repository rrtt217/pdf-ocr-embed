"""Generic OpenAI-compatible vision adapter (full implementation).

Wires *any* OpenAI-compatible multimodal model into the normalized ``OcrPage``
schema.  It sends the page image to the model with a prompt that asks for
structured JSON — one block per visible text line, each with a bounding box in
the normalized 0..1000 canvas (same convention as ``unlimited_ocr_adapter`` so
``normalize_bbox`` can map it back to real pixel coordinates).

Unlike the unlimited adapter, this does not assume the model emits the special
``<|det|>`` tokens; it uses a plain, model-agnostic JSON request that works
with typical OpenAI-compatible chat-vision endpoints.

Configuration is identical to the unlimited adapter, set in
``backend/ocr_config.toml``: ``api_key`` / ``base_url`` / ``model`` (+ optional
``provider`` preset).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from backend.config import resolve
from backend.models import OcrBlock, OcrPage
from backend.sources.base import OcrSource, normalize_bbox

log = logging.getLogger(__name__)

__all__ = ["GenericOpenAiAdapter"]

# System-style prompt instructing the model to return a JSON block list.
_PROMPT = (
    "You are an OCR engine. Transcribe the text in this document page image.\n"
    "Output ONLY a JSON object, no markdown, no prose, of the form:\n"
    "{\"blocks\": [{\"kind\": \"text|heading|equation|table|footnote\", "
    "\"bbox\": [x1, y1, x2, y2], \"text\": \"...\"}, ...]}\n"
    "Rules:\n"
    "- bbox is in the normalized 0..1000 canvas (each dimension scaled "
    "independently to 1000).\n"
    "- Return one block per visible text line.\n"
    "- Preserve the exact text content, including digits, punctuation and "
    "math symbols.\n"
    "- kind: 'heading' for titles/captions, 'equation' for math/formulas, "
    "'table' for table cells row, 'text' for body text.\n"
    "- Return an empty array [] if there is no text."
)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Robustly pull a JSON object out of a model response.

    Handles responses that embed extra prose around the JSON, or that wrap the
    JSON in ```json ... ``` fences.
    """
    if not text:
        return None
    text = text.strip()
    # Try to strip markdown code fences.
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # If the whole thing isn't JSON, find the first { ... } block.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


class GenericOpenAiAdapter(OcrSource):
    """OCR via an arbitrary OpenAI-compatible vision model."""

    name = "generic_openai"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None,
                 prompt: str | None = None, max_tokens: int = 16384,
                 temperature: float = 0.0):
        cfg = resolve()
        self.base_url = (base_url or cfg.get("base_url") or
                         "https://api.llm.ustc.edu.cn/v1").rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "gpt-4o-mini"
        self.prompt = prompt or cfg.get("generic_prompt") or _PROMPT
        self.max_tokens = min(int(max_tokens), 32767)  # must stay < 32768
        self.temperature = float(temperature)

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
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
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
        log.debug("page %d: model returned %d chars", page_index, len(text))
        if not text:
            log.warning("page %d: empty model response, usage=%s",
                        page_index, raw.get("usage"))
        data = _extract_json(text)
        return self._parse_json_blocks(data, width, height, page_index)

    @staticmethod
    def _extract_content(raw: dict) -> str:
        try:
            return raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def _parse_json_blocks(self, data: Optional[dict], width: int, height: int,
                           page_index: int) -> OcrPage:
        blocks: List[OcrBlock] = []
        if data and isinstance(data.get("blocks"), list):
            for item in data["blocks"]:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind", "text")).strip().lower() or "text"
                bbox = self._coerce_bbox(item.get("bbox"))
                if bbox is None:
                    continue
                px_bbox = normalize_bbox(bbox, width, height)
                text = (item.get("text") or "").strip()
                if kind == "image" and not text:
                    blocks.append(OcrBlock(kind="image", bbox=px_bbox))
                else:
                    blocks.append(OcrBlock(kind=kind, bbox=px_bbox, text=text))
        else:
            log.warning("page %d: no parseable 'blocks' in model response", page_index)
        return OcrPage(page_index=page_index, width=width, height=height,
                       blocks=blocks)

    @staticmethod
    def _coerce_bbox(bbox: Any) -> Optional[List[float]]:
        """Accept a 4-number list/tuple in normalized 0..1000 canvas."""
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return None
        try:
            return [float(v) for v in bbox]
        except (TypeError, ValueError):
            return None

    def close(self) -> None:
        pass