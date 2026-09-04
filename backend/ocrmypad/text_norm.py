"""Deprecated alias -> :mod:`ocrmypdf_unlimited.text_norm`.

Re-exported for backward compatibility with pdf-ocr-embed code that imports
``backend.ocrmypad.text_norm``.  The module is standalone; see
``ocrmypdf_unlimited/text_norm.py``.
"""
from __future__ import annotations

import ocrmypdf_unlimited.text_norm as _impl

globals().update({_k: _v for _k, _v in vars(_impl).items()
                  if not _k.startswith("__")})
__doc__ = _impl.__doc__
