"""The unconditional ``ocrmypdf-unlimited`` plugin's own error type.

This module is self-contained: the plugin never imports anything from a host
application.  ``UnavailableError`` signals a *pure setup problem* (missing
dependency, missing configuration) and is expected to be caught by whatever
host (the ``pdf-ocr-embed`` backend, a CLI, a library caller) to surface a
friendly message instead of a stack trace.

A host that embeds this plugin should alias this class (see the host's
``backend.errors``) so it can catch exactly what the plugin raises.
"""
from __future__ import annotations


class UnavailableError(RuntimeError):
    """Raised when the unlimited-OCR engine (or its setup) is not usable.

    A pure setup problem (missing dependency, missing configuration) — not a
    failure of the OCR job itself.  Genuine OCR failures raise ``RuntimeError``
    instead.
    """
