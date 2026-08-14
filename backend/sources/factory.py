"""OCR adapter factory and registry.

Choose an adapter by name. The backend must always go through this OcrSource
abstraction rather than hard-coding a single engine.
"""
from __future__ import annotations

from typing import Dict, Type

from backend.sources.base import OcrSource
from backend.sources.generic_openai_adapter import GenericOpenAiAdapter
from backend.sources.tesseract_adapter import TesseractAdapter
from backend.sources.unlimited_ocr_adapter import UnlimitedOcrAdapter

_REGISTRY: Dict[str, Type[OcrSource]] = {
    UnlimitedOcrAdapter.name: UnlimitedOcrAdapter,
    TesseractAdapter.name: TesseractAdapter,
    GenericOpenAiAdapter.name: GenericOpenAiAdapter,
}


def get_adapter(name: str | None = None) -> OcrSource:
    name = (name or "unlimited").strip().lower()
    adapter_cls = _REGISTRY.get(name)
    if adapter_cls is None:
        raise ValueError(
            f"Unknown OCR adapter '{name}'. Available: {', '.join(_REGISTRY)}"
        )
    return adapter_cls()


def available_adapters() -> list:
    return [{"name": k, "cls": v.__name__} for k, v in _REGISTRY.items()]