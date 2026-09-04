"""The pdf-ocr-embed OCRmyPDF plugin package.

Loaded by ocrmypdf as a plugin (``plugins=[<this package's __init__.py>]``).
Provides the ``unlimited`` OCR engine (an OpenAI-compatible vision model that
outputs <|det|> marker streams) behind OCRmyPDF's ``OcrEngine`` interface, plus
per-job progress reporting shared with the WebUI backend.
"""
from __future__ import annotations

from backend.ocrmypad.unlimited_engine import UnlimitedOcrEngine, get_ocr_engine

__all__ = ["UnlimitedOcrEngine", "get_ocr_engine"]
