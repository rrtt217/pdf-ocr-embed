#!/usr/bin/env python3
"""Desktop entry point for pdf-ocr-embed.

This is the script PyInstaller freezes into the double-clickable app.  It:

1. calls ``multiprocessing.freeze_support()`` **before** importing anything
   heavy (OCRmyPDF uses a process pool when ``use_threads`` is false; a frozen
   child would otherwise re-execute this executable in a loop),
2. starts the FastAPI app on a background thread, bound to ``127.0.0.1`` on a
   kernel-assigned free port (see ``backend.server``),
3. shows it in a native window via ``pywebview`` when available, and falls back
   to the system browser otherwise — in **both** modes the WebUI's Quit button
   (``POST /api/app/quit``) ends the process,
4. on exit, gracefully stops any running OCR job (the project's ``cancel``
   flag contract) and shuts the server down.

**Exiting must always work.**  A quit can arrive from four directions — the
window being closed, the UI's Quit button, Ctrl-C, or ``SIGTERM`` — and each
one has to end the process even while a minutes-long OCR job is in flight.
Three things make that true:

* OCR runs on *daemon* worker threads (``ocr_service.start_job``), so a
  running job can never hold the interpreter open after ``main`` returns.  A
  job started on a non-daemon thread (uvicorn's/anyio's default executor, for
  instance) is joined at interpreter exit and the app looks hung forever.
* the shutdown hook stops the jobs the graceful way (cancel flag, then a
  bounded wait) and then stops the server with its own deadlines, so a normal
  quit takes a couple of seconds and never waits for a whole page;
* a final hard deadline force-exits the process (``os._exit``) if any thread
  is still wedged — the user asked us to quit, so we quit.

Run it from a source checkout with ``python desktop.py``; ``--no-window`` and
``--browser`` are useful for smoke-testing a packaged build.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger("pdf_ocr_embed.desktop")

WINDOW_TITLE = "PDF OCR Embed"
WINDOW_SIZE = (1280, 900)

# --- quitting is bounded -----------------------------------------------------
# The mechanisms (bounded job stop, guaranteed hard exit, signal routing) live
# in ``backend.shutdown`` so the plain server and the desktop app cannot drift
# apart.  This is the desktop app's own timing.
#: How long a quit request that arrives over HTTP may spend in the teardown
#: before the browser gets its answer.  The teardown continues on its helper
#: thread; this only keeps the request snappy while OCR is mid-page.
QUIT_REQUEST_TIMEOUT = 2.0
#: How long a quit that comes from a SIGNAL (or the window closing) may spend
#: stopping jobs before the teardown continues.  Nothing has to answer a
#: request here, so this can wait out one in-flight OCR page.
QUIT_TEARDOWN_TIMEOUT = 20.0


def _serve_until_quit() -> None:
    """Block until the UI asks us to quit (or the user hits Ctrl-C)."""
    from backend import lifecycle

    try:
        lifecycle.wait_for_quit()
    except KeyboardInterrupt:
        # A Ctrl-C that raced the handler installation still means "quit".
        lifecycle.request_quit(timeout=QUIT_REQUEST_TIMEOUT)


def _shutdown(server=None) -> None:
    """Stop OCR jobs and stop the server.  Never raises.

    Callable from ANY thread and idempotent, so it can run both from the quit
    hook (Quit button, signal) and from ``main``'s teardown.
    """
    from backend import shutdown

    shutdown.stop_jobs()
    shutdown.stop_server(server)


def _quit_hook(server) -> None:
    """The hook every quit path runs: stop the jobs, then the server.

    The cancel sweep runs FIRST and never waits, so the engine is already
    stopping when the hook's deadline releases whoever asked for the quit
    (``request_quit(timeout=...)`` continues the hook on its helper thread).
    The full teardown — bounded job wait, then the server stop, which also
    arms the hard-exit deadline — therefore happens even when the caller is a
    Quit button request that must answer a browser, and even if the main
    thread is still blocked inside a GUI loop that will not return.
    """
    from backend import ocr_service, shutdown

    try:
        ocr_service.request_all_cancels()
    except Exception:  # noqa: BLE001 - the hook must never raise
        log.debug("cancel sweep failed", exc_info=True)
    _shutdown(server)


def _install_signal_handlers() -> bool:
    """Turn SIGINT/SIGTERM into a clean quit.  Main thread only."""
    from backend import lifecycle, shutdown

    handler = shutdown.make_signal_handler(
        lambda: lifecycle.request_quit(timeout=QUIT_TEARDOWN_TIMEOUT),
        label="the app",
    )
    return shutdown.install_signal_handlers(handler)


def _preferred_gui(forced: str | None = None) -> str | None:
    """Name a GUI backend to force, or None to let pywebview choose.

    pywebview probes every backend in turn and logs a full traceback for each
    one it cannot import (e.g. "QT cannot be loaded" when qtpy is absent),
    which looks alarming in a desktop app's log.  On Linux we can tell it up
    front when GTK/WebKit is usable — that is the backend
    ``requirements-desktop.txt`` installs.  Elsewhere we keep auto-detection
    (WebView2 on Windows, WKWebView on macOS).

    ``forced`` comes from ``--gui`` and wins over the detection, so a user
    whose GTK stack is broken (or who prefers the Qt/WebEngine renderer) can
    pick a backend explicitly.
    """
    if forced:
        return forced
    if not sys.platform.startswith("linux"):
        return None
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import Gdk, Gtk, WebKit2  # noqa: F401
    except Exception:  # noqa: BLE001 - let pywebview probe for itself
        return None
    return "gtk"


def _display_available() -> bool:
    """True when a native window can actually be shown.

    ``os.environ`` is read directly here on purpose: this is a GUI-session
    probe in the desktop entry point, not application configuration (which
    lives exclusively in ``backend/config.py``).
    """
    if not sys.platform.startswith("linux"):
        return True          # WebView2 / WKWebView assume a desktop session
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _create_window(url: str):
    """Create the native window, or return None when pywebview is unusable."""
    try:
        import webview
    except ImportError:
        log.info("pywebview is not installed; falling back to the system browser")
        return None
    try:
        return webview.create_window(WINDOW_TITLE, url,
                                     width=WINDOW_SIZE[0], height=WINDOW_SIZE[1])
    except Exception as exc:  # noqa: BLE001 - any backend problem -> browser
        log.warning("could not create a native window (%s); using the browser", exc)
        return None


def _browser_mode(url: str) -> None:
    import webbrowser

    log.info("opening %s in the system browser", url)
    webbrowser.open(url)
    _serve_until_quit()


def _watch_for_quit(window) -> None:
    """Close the native window on quit, from the thread that owns it.

    Called by ``webview.start(func=...)`` *after* the GUI loop is running, so
    this thread is not the loop itself but the window API is live.  Destroying
    the window is what makes the GUI loop return; without it a Quit pressed in
    the UI (or Ctrl-C) would stop the server but leave the window on screen.
    """
    import webview

    from backend import lifecycle

    # Closing the window is itself a quit (the flag makes the main thread
    # continue its teardown instead of blocking in webview.start forever).
    # The handler must return True: pywebview CANCELS a close whose handler
    # returned a falsy value, which would leave a dead window on screen.
    def _on_closing() -> bool:
        lifecycle.request_quit(timeout=QUIT_REQUEST_TIMEOUT)
        return True

    try:
        window.events.closing += _on_closing
    except Exception:  # noqa: BLE001 - older pywebview, or no event support
        log.debug("could not subscribe to the window closing event",
                  exc_info=True)

    lifecycle.wait_for_quit()
    try:
        window.destroy()
    except Exception:  # noqa: BLE001 - a stuck GUI must not stop the exit
        log.debug("destroying the native window failed", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDF OCR Embed (desktop)")
    parser.add_argument("--port", type=int, default=0,
                        help="pin the local HTTP port (default: pick a free one)")
    parser.add_argument("--browser", action="store_true",
                        help="use the system browser instead of a native window")
    parser.add_argument("--no-window", action="store_true",
                        help="serve only; open no window (smoke tests, CI)")
    parser.add_argument("--window", action="store_true",
                        help="force the native window even when there is no "
                             "DISPLAY (useful to test the window path)")
    parser.add_argument("--gui", default=None,
                        help="force a pywebview backend (e.g. gtk, qt); "
                             "default: GTK on Linux when usable, else auto")
    args = parser.parse_args(argv)

    from backend import lifecycle, paths
    from backend.logging_config import setup_logging

    setup_logging()
    lifecycle.set_desktop_mode(True)
    # Ctrl-C (console launch) and SIGTERM (session manager, `kill`) become a
    # clean quit instead of an abrupt interpreter interrupt; the hard-exit
    # deadline is armed when that quit starts, never at startup.
    _install_signal_handlers()
    log.info("pdf-ocr-embed desktop starting (frozen=%s)", paths.is_frozen())
    log.info("data dir: %s", paths.data_dir())
    log.info("config file: %s", paths.CONFIG_FILE)

    from backend.main import app
    from backend.server import EmbeddedServer

    server = EmbeddedServer(app, port=args.port)
    try:
        server.start()
    except Exception:  # noqa: BLE001 - report, do not show a bare traceback
        log.exception("could not start the local server")
        return 1

    want_window = args.window or (not args.no_window and not args.browser)
    if want_window and not args.window and not _display_available():
        log.info("no DISPLAY/Wayland session detected; using the browser")
        args.browser = True
        want_window = False

    window = _create_window(server.url) if want_window else None
    started_windowed = window is not None
    if not started_windowed and want_window:
        log.info("no usable native window; using the system browser")

    # The Quit button (and Ctrl-C) reach the same teardown as a window close.
    # The hook runs on the REQUEST thread with a short deadline, so the browser
    # gets its "quitting" response while the jobs stop in the background.
    lifecycle.set_shutdown_hook(lambda: _quit_hook(server))
    lifecycle.set_quit_handler(None)

    try:
        if not started_windowed:
            log.info("serving at %s (no window)", server.url)
            if args.no_window:
                _serve_until_quit()
            else:
                _browser_mode(server.url)
        else:
            try:
                import webview

                # blocks until the last window is closed; the watcher closes
                # it when a quit is requested from the UI or a signal
                webview.start(lambda: _watch_for_quit(window),
                              gui=_preferred_gui(args.gui))
            except Exception as exc:  # noqa: BLE001 - GUI backend unavailable
                log.warning("native window failed to start (%s); "
                            "falling back to the system browser", exc)
                _browser_mode(server.url)
    finally:
        lifecycle.set_shutdown_hook(None)
        lifecycle.set_quit_handler(None)
        _shutdown(server)
        log.info("pdf-ocr-embed desktop stopped")
    return 0


if __name__ == "__main__":
    # Required before any multiprocessing use in a frozen app.
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
