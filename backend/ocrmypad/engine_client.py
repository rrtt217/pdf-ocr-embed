"""Deprecated alias -> :mod:`ocrmypdf_unlimited.client`.

Re-exported for backward compatibility with pdf-ocr-embed code that imports
``backend.ocrmypad.engine_client``.  The plugin's client is standalone; see
``ocrmypdf_unlimited/client.py``.
"""
from __future__ import annotations

import ocrmypdf_unlimited.client as _impl

globals().update({_k: _v for _k, _v in vars(_impl).items()
                  if not _k.startswith("__")})
__doc__ = _impl.__doc__
