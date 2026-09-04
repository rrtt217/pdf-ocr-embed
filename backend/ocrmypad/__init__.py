"""Backward-compatible alias of the standalone ``ocrmypdf_unlimited`` plugin.

The real OCRmyPDF plugin now lives at the repo root in :mod:`ocrmypdf_unlimited`
(a self-contained, pip-installable package that strictly follows
https://ocrmypdf.readthedocs.io/en/latest/plugins.html).  This module exists
only so existing pdf-ocr-embed code and tests importing ``backend.ocrmypad.*``
keep working — it is NOT the plugin itself.  New code should import
``ocrmypdf_unlimited`` directly.
"""
from __future__ import annotations

from ocrmypdf_unlimited.engine import UnlimitedOcrEngine, get_ocr_engine

__all__ = ["UnlimitedOcrEngine", "get_ocr_engine"]
