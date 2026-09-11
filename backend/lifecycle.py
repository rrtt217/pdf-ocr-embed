"""Desktop lifecycle: how the app asks itself to shut down.

A packaged app must be stoppable from its own UI — most importantly in the
browser-fallback mode, where there is no window to close.  The state lives here
(not in ``desktop.py``) so the FastAPI route can be tested without opening a
window, and so the desktop entry point is the only thing that needs to know how
to actually tear down (destroy the window, or release a wait).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

# A browser cannot send a custom header cross-origin without a successful CORS
# preflight; since CORS is restricted to loopback origins, requiring this header
# stops an arbitrary web page from POSTing "quit" at the local server.
QUIT_HEADER = "x-pdf-ocr-embed"
QUIT_HEADER_VALUE = "quit"

_quit_event = threading.Event()
_handler: Optional[Callable[[], None]] = None
_lock = threading.Lock()
_desktop_mode = False


# --- desktop mode flag (drives the WebUI's Quit button) ----------------------

def set_desktop_mode(enabled: bool = True) -> None:
    """Mark the process as running as a desktop app (see ``desktop.py``)."""
    global _desktop_mode
    _desktop_mode = bool(enabled)


def desktop_mode() -> bool:
    """True when launched by ``desktop.py`` rather than a plain server."""
    return _desktop_mode


# --- quit ----------------------------------------------------------------

def set_quit_handler(handler: Optional[Callable[[], None]]) -> None:
    """Register how to tear the app down (the window, or nothing at all)."""
    global _handler
    with _lock:
        _handler = handler


def request_quit() -> None:
    """Ask the app to exit.  Idempotent, and never raises."""
    _quit_event.set()
    with _lock:
        handler = _handler
    if handler is not None:
        try:
            handler()
        except Exception:  # noqa: BLE001 - quitting must not blow up
            log.warning("quit handler failed", exc_info=True)


def is_quit_requested() -> bool:
    return _quit_event.is_set()


def wait_for_quit(timeout: Optional[float] = None) -> bool:
    """Block until a quit is requested; returns True when one was."""
    return _quit_event.wait(timeout)


def reset() -> None:
    """Clear all lifecycle state (used by tests)."""
    global _handler, _desktop_mode
    _quit_event.clear()
    with _lock:
        _handler = None
    _desktop_mode = False
