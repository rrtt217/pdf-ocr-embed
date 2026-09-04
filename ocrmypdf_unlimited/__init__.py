"""ocrmypdf-unlimited: a standalone OCRmyPDF plugin (Unlimited-OCR engine).

A packaged OCRmyPDF plugin (module prefix follows the ``ocrmypdf_``
convention requested by the [plugin docs]
(https://ocrmypdf.readthedocs.io/en/latest/plugins.html)) that adds the
``unlimited`` OCR engine: an OpenAI-compatible vision model that outputs
``<|det|>`` marker streams.

Load it any of the three documented ways:

* **Script plugin**::

      ocrmypdf --plugin ocrmypdf_unlimited --ocr-engine unlimited ...

* **Packaged plugin** (after ``pip install ocrmypdf-unlimited``), via the
  ``ocrmypdf`` setuptools entry point — loaded automatically, or explicitly
  with ``--plugin ocrmypdf_unlimited``.

* **Library**: pass ``plugins=['ocrmypdf_unlimited']`` to
  ``ocrmypdf.ocr()`` / ``ocrmypdf.api._pdf_to_hocr()``.

Configuration is fully standalone: ``--unlimited-*`` CLI/API arguments,
``OCR_UNLIMITED_*`` environment variables, or a host-injected snapshot
(``settings.configure``).  The package never imports a host application.
"""
from __future__ import annotations

from ocrmypdf_unlimited.engine import (
    UnlimitedOcrEngine,
    get_ocr_engine,
)
from ocrmypdf_unlimited.options import (
    add_options,
    check_options,
    initialize,
)

__version__ = "1.0.0"

__all__ = [
    "UnlimitedOcrEngine",
    "get_ocr_engine",
    "add_options",
    "check_options",
    "initialize",
    "__version__",
]
