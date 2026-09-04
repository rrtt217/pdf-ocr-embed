"""Deprecated alias -> :mod:`ocrmypdf_unlimited.settings`.

Re-exported for backward compatibility with pdf-ocr-embed code that imports
``backend.ocrmypad.settings``.  The plugin's own settings store is standalone;
see ``ocrmypdf_unlimited/settings.py``.
"""
from __future__ import annotations

import ocrmypdf_unlimited.settings as _impl

globals().update({_k: _v for _k, _v in vars(_impl).items()
                  if not _k.startswith("__")})
__doc__ = _impl.__doc__
