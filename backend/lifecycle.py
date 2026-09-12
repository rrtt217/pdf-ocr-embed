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

#: How long the Quit ENDPOINT may spend in the shutdown hook before it answers
#: the browser.  The hook keeps running on its helper thread; this only bounds
#: the request, so an in-flight OCR page can never turn a quit into a timeout.
QUIT_REQUEST_TIMEOUT = 2.0

_quit_event = threading.Event()
_handler: Optional[Callable[[], None]] = None
_shutdown_hook: Optional[Callable[[], None]] = None
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
    """Register how to tear the app down (the window, or nothing at all).

    Runs on whichever thread requested the quit (the HTTP request thread for
    the UI button), so it must only wake the main thread up — never do work
    that belongs there.
    """
    global _handler
    with _lock:
        _handler = handler


def set_shutdown_hook(hook: Optional[Callable[[], None]]) -> None:
    """Register the graceful-shutdown work to run for every quit path.

    Called by ``desktop.py`` once the server is up.  It runs on the thread
    that requested the quit, which is exactly what makes it useful: a quit
    requested over HTTP (the WebUI Quit button) can then still stop and wait
    for in-flight OCR jobs *before* the request returns, while a quit
    requested on the main thread (Ctrl-C, SIGTERM) runs it before the process
    starts tearing the interpreter down.
    """
    global _shutdown_hook
    with _lock:
        _shutdown_hook = hook


def request_quit(timeout: Optional[float] = None) -> None:
    """Ask the app to exit.  Idempotent, and never raises.

    ``timeout`` optionally bounds the shutdown hook, so a quit request that
    arrives over HTTP still answers the browser even if OCR is mid-page (the
    hook keeps running on its helper thread; the cancel flag it wrote is what
    actually stops the engine).
    """
    _quit_event.set()
    with _lock:
        handler = _handler
        hook = _shutdown_hook
    if hook is not None:
        _run_with_timeout(hook, timeout, "shutdown hook")
    if handler is not None:
        _run_with_timeout(handler, timeout, "quit handler")


def _run_with_timeout(func: Callable[[], None], timeout: Optional[float],
                      label: str) -> None:
    """Run ``func``, optionally with a deadline, swallowing every failure.

    The timeout exists so a stuck job cannot make the Quit endpoint hang: the
    work continues on its helper thread while the caller is released, and the
    caller's own process teardown deadline decides what to do next.
    """
    if not timeout or timeout <= 0:
        _call_quietly(func, label)
        return
    done = threading.Event()

    def runner() -> None:
        try:
            _call_quietly(func, label)
        finally:
            done.set()

    threading.Thread(target=runner, daemon=True, name=f"{label}-runner").start()
    if not done.wait(timeout):
        log.warning("%s did not finish within %.1fs; continuing shutdown",
                    label, timeout)


def _call_quietly(func: Callable[[], None], label: str) -> None:
    try:
        func()
    except Exception:  # noqa: BLE001 - quitting must not blow up
        log.warning("%s failed", label, exc_info=True)


def is_quit_requested() -> bool:
    return _quit_event.is_set()


def wait_for_quit(timeout: Optional[float] = None) -> bool:
    """Block until a quit is requested; returns True when one was."""
    return _quit_event.wait(timeout)


def reset() -> None:
    """Clear all lifecycle state (used by tests)."""
    global _handler, _shutdown_hook, _desktop_mode
    _quit_event.clear()
    with _lock:
        _handler = None
        _shutdown_hook = None
    _desktop_mode = False
