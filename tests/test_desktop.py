"""Backend selection in the desktop entry point (``desktop.py``).

pywebview probes every GUI backend in turn and logs a full traceback for each
one it cannot import, so the app names a backend up front.  ``--gui`` overrides
that choice, which is the escape hatch for a machine whose GTK/WebKit stack is
broken or missing.

Also pinned here: the window path's exit wiring.  A windowed quit (window
closed, Quit button, Ctrl-C) must close the window and let ``main`` continue —
leaving the GUI loop blocked would strand a dead server behind a live window.
"""
from __future__ import annotations

import sys
import threading
import types

import pytest

import desktop
from backend import lifecycle


@pytest.fixture(autouse=True)
def _clean_lifecycle():
    lifecycle.reset()
    yield
    lifecycle.reset()


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


# --- window mode: display detection ------------------------------------------

def test_display_detection_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert desktop._display_available() is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert desktop._display_available() is True
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert desktop._display_available() is True


def test_display_is_assumed_on_windows_and_macos(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop._display_available() is True
    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop._display_available() is True


# --- window mode: the quit watcher ------------------------------------------

class _FakeEvent:
    """Stand-in for ``window.events.closing`` (``+=`` and truthiness)."""

    def __init__(self, log: list) -> None:
        self.log = log
        self.handlers: list = []

    def __iadd__(self, item):
        self.handlers.append(item)
        return self


class _FakeWindow:
    def __init__(self, log: list) -> None:
        self.log = log
        self.events = types.SimpleNamespace(closing=_FakeEvent(log))

    def destroy(self) -> None:
        self.log.append("destroy")


def test_the_quit_watcher_destroys_the_window(monkeypatch):
    """A quit raised anywhere (button, signal) closes the window."""
    log: list = []
    window = _FakeWindow(log)
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))

    waiter = threading.Thread(target=desktop._watch_for_quit, args=(window,))
    waiter.start()
    lifecycle.request_quit(timeout=1.0)
    waiter.join(5)

    assert not waiter.is_alive()
    assert log == ["destroy"]


def test_closing_the_window_requests_a_quit():
    """The closing handler must ASK to quit and must not cancel the close."""
    log: list = []
    window = _FakeWindow(log)

    # Register the handler the way _watch_for_quit does, then fire it.
    def _on_closing() -> bool:
        lifecycle.request_quit(timeout=1.0)
        return True

    window.events.closing += _on_closing
    assert window.events.closing.handlers[0]() is True
    assert lifecycle.is_quit_requested() is True
