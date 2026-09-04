"""Deprecated alias -> :mod:`ocrmypdf_unlimited.engine`.

Re-exported for backward compatibility with pdf-ocr-embed code that imports
``backend.ocrmypad.unlimited_engine``.  The engine is standalone and is the
same class object living in ``ocrmypdf_unlimited/engine.py``.
"""
from __future__ import annotations

import ocrmypdf_unlimited.engine as _impl

globals().update({_k: _v for _k, _v in vars(_impl).items()
                  if not _k.startswith("__")})
__doc__ = _impl.__doc__
