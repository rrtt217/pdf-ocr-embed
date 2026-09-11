"""Backend selection in the desktop entry point (``desktop.py``).

pywebview probes every GUI backend in turn and logs a full traceback for each
one it cannot import, so the app names a backend up front.  ``--gui`` overrides
that choice, which is the escape hatch for a machine whose GTK/WebKit stack is
broken or missing.
"""
from __future__ import annotations

import sys

import desktop


def test_forced_gui_wins_over_detection():
    assert desktop._preferred_gui("qt") == "qt"
    assert desktop._preferred_gui("gtk") == "gtk"


def test_auto_detection_only_forces_gtk_on_linux(monkeypatch):
    """Off Linux (WebView2 / WKWebView) pywebview picks for itself."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop._preferred_gui() is None
    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop._preferred_gui() is None


def test_auto_detection_returns_a_backend_or_none_on_linux():
    """Either GTK was found (a name) or pywebview is left to probe (None)."""
    monkeypatch_ok = sys.platform.startswith("linux")
    if not monkeypatch_ok:
        return
    assert desktop._preferred_gui() in ("gtk", None)
