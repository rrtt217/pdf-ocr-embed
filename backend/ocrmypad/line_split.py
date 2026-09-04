"""Deprecated alias -> :mod:`ocrmypdf_unlimited.line_split`.

Re-exported for backward compatibility with pdf-ocr-embed code that imports
``backend.ocrmypad.line_split``.  The module is standalone; see
``ocrmypdf_unlimited/line_split.py``.
"""
from __future__ import annotations

import ocrmypdf_unlimited.line_split as _impl

globals().update({_k: _v for _k, _v in vars(_impl).items()
                  if not _k.startswith("__")})
__doc__ = _impl.__doc__
