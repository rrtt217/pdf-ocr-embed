"""Deprecated alias -> :mod:`ocrmypdf_unlimited.batching`.

Re-exported for backward compatibility with pdf-ocr-embed code that imports
``backend.ocrmypad.batching``.  The batcher is standalone; see
``ocrmypdf_unlimited/batching.py``.
"""
from __future__ import annotations

import ocrmypdf_unlimited.batching as _impl

globals().update({_k: _v for _k, _v in vars(_impl).items()
                  if not _k.startswith("__")})
__doc__ = _impl.__doc__
