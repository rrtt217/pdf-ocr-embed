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

Run it from a source checkout with ``python desktop.py``; ``--no-window`` and
``--browser`` are useful for smoke-testing a packaged build.
"""
from __future__ import annotations

import argparse
import logging
import sys

log = logging.getLogger("pdf_ocr_embed.desktop")

WINDOW_TITLE = "PDF OCR Embed"
WINDOW_SIZE = (1280, 900)


def _cancel_running_jobs() -> None:
    """Ask every in-flight OCR job to stop at its next page boundary.

    Reuses the app's own contract (``page_store.request_cancel`` writes the
    ``cancel`` file the plugin polls), so a quit never hard-kills a run and
    loses completed pages.
    """
    try:
        from backend import ocr_service

        for job in ocr_service.all_jobs():
            if job.get("status") in ("running", "queued", "stopping"):
                try:
                    ocr_service.stop_job(job["job_id"])
                    log.info("requested stop for job %s on exit", job["job_id"])
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    log.debug("could not stop job %s", job.get("job_id"),
                              exc_info=True)
    except Exception:  # noqa: BLE001
        log.debug("cancel-on-exit unavailable", exc_info=True)


def _serve_until_quit() -> None:
    """Block until the UI asks us to quit (or the user hits Ctrl-C)."""
    from backend import lifecycle

    try:
        lifecycle.wait_for_quit()
    except KeyboardInterrupt:
        pass


def _preferred_gui() -> str | None:
    """Name a GUI backend to force, or None to let pywebview choose.

    pywebview probes every backend in turn and logs a full traceback for each
    one it cannot import (e.g. "QT cannot be loaded" when qtpy is absent),
    which looks alarming in a desktop app's log.  On Linux we can tell it up
    front when GTK/WebKit is usable — that is the backend
    ``requirements-desktop.txt`` installs.  Elsewhere we keep auto-detection
    (WebView2 on Windows, WKWebView on macOS).
    """
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDF OCR Embed (desktop)")
    parser.add_argument("--port", type=int, default=0,
                        help="pin the local HTTP port (default: pick a free one)")
    parser.add_argument("--browser", action="store_true",
                        help="use the system browser instead of a native window")
    parser.add_argument("--no-window", action="store_true",
                        help="serve only; open no window (smoke tests, CI)")
    args = parser.parse_args(argv)

    from backend import lifecycle, paths
    from backend.logging_config import setup_logging

    setup_logging()
    lifecycle.set_desktop_mode(True)
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

    window = None
    if not args.no_window and not args.browser:
        window = _create_window(server.url)
    if window is not None:
        # Closing the window, or the UI's Quit button, ends the process.
        lifecycle.set_quit_handler(window.destroy)
    else:
        lifecycle.set_quit_handler(None)

    try:
        if args.no_window:
            log.info("serving at %s (no window requested)", server.url)
            _serve_until_quit()
        elif window is not None:
            try:
                import webview

                # blocks until the last window is closed
                webview.start(gui=_preferred_gui())
            except Exception as exc:  # noqa: BLE001 - GUI backend unavailable
                log.warning("native window failed to start (%s); "
                            "falling back to the system browser", exc)
                _browser_mode(server.url)
        else:
            _browser_mode(server.url)
    finally:
        _cancel_running_jobs()
        server.stop()
        log.info("pdf-ocr-embed desktop stopped")
    return 0


if __name__ == "__main__":
    # Required before any multiprocessing use in a frozen app.
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
