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
    """Without a tray, closing the window quits — exactly as it always did.

    Calls the REAL handler (``desktop._make_closing_handler``): the previous
    version of this test re-implemented the handler inline, so it would not
    have noticed a change in desktop.py.
    """
    log: list = []
    window = _FakeWindow(log)
    handler = desktop._make_closing_handler(window, hide_on_close=False)

    assert handler() is True                      # allow the close
    assert lifecycle.is_quit_requested() is True
    assert log == []                              # hiding never happened


def test_closing_with_a_tray_hides_and_keeps_running(monkeypatch):
    """With a tray, closing must HIDE and must NOT quit: the OCR job keeps
    running in the server, and the tray is the way back."""
    log: list = []
    window = _FakeWindow(log)
    window.hide = lambda: log.append("hide")
    handler = desktop._make_closing_handler(window, hide_on_close=True)

    assert handler() is False                     # literal False cancels the close
    assert log == ["hide"]
    assert lifecycle.is_quit_requested() is False


def test_hiding_is_announced_once(monkeypatch):
    """A window that vanishes silently reads as a crash: say it once."""
    from backend import tray as tray_mod

    notices: list = []
    monkeypatch.setattr(tray_mod, "notify",
                        lambda title, body: notices.append((title, body)) or True)
    log: list = []
    window = _FakeWindow(log)
    window.hide = lambda: log.append("hide")
    handler = desktop._make_closing_handler(window, hide_on_close=True)

    handler()
    handler()
    handler()
    assert len(notices) == 1
    assert log == ["hide", "hide", "hide"]


def test_a_window_that_cannot_hide_still_quits(monkeypatch):
    """If hiding fails we must not stay alive invisibly: fall back to a quit."""
    log: list = []
    window = _FakeWindow(log)

    def boom() -> None:
        raise RuntimeError("no window system")

    window.hide = boom
    handler = desktop._make_closing_handler(window, hide_on_close=True)

    assert handler() is True                      # allow the close
    assert lifecycle.is_quit_requested() is True


@pytest.mark.parametrize("tray_available,tray_requested,expected", [
    (True, True, True),        # a tray exists and was wanted -> hide
    (False, True, False),      # wanted, but none could be created -> quit
    (True, False, False),      # --no-tray -> quit
    (False, False, False),     # nothing to hide to -> quit
])
def test_should_hide_on_close(tray_available, tray_requested, expected):
    """The rule that protects the user from an unreachable hidden window:
    the exit behaviour only changes when a tray really exists."""
    assert desktop._should_hide_on_close(
        tray_available=tray_available, tray_requested=tray_requested) is expected


def test_tray_status_is_built_from_the_job_list(monkeypatch):
    from backend import ocr_service

    monkeypatch.setattr(ocr_service, "list_jobs", lambda: [
        {"status": "running", "pages_done": 5, "num_pages": 9}])
    assert "5/9" in desktop._tray_status()


def test_tray_status_survives_a_broken_job_list(monkeypatch):
    from backend import ocr_service

    def boom():
        raise RuntimeError("registry gone")

    monkeypatch.setattr(ocr_service, "list_jobs", boom)
    assert desktop._tray_status() == ""
