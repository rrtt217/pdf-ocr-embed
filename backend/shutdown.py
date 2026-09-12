"""Bounded shutdown for every entry point (desktop app, plain server, CLI).

Quitting must always work, and it must work the same way whichever door the
user came through.  Two mechanisms make that true, and they live here so the
entry points do not each reinvent them:

* :func:`stop_jobs` — ask in-flight OCR jobs to stop (the project's ``cancel``
  flag contract) and wait a BOUNDED time for their workers.  OCR worker threads
  are daemons (``ocr_service.start_job``), so a straggler can never keep the
  process alive; the wait only exists to keep the graceful path graceful.
* :func:`exit_soon` — a last-resort deadline.  Third-party code (a GUI toolkit
  loop, a library thread) can still wedge teardown, and a user who asked to
  quit must not be left with a zombie process.

Why a hard deadline is needed at all: Python's interpreter exit JOINS every
non-daemon thread (``concurrent.futures.thread._python_exit`` joins the default
executor's workers).  A run started on such a thread therefore hangs the
process after uvicorn has already logged "Finished server process" — the exact
symptom this module exists to prevent.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Optional

log = logging.getLogger(__name__)

#: How long shutdown waits for in-flight OCR jobs to reach a page boundary
#: (the engine polls the cancel flag per page, so this bounds one page).
JOB_STOP_WAIT = 5.0
#: How long the embedded server may take to drain and stop.
SERVER_STOP_TIMEOUT = 5.0
#: Last-resort deadline: after a quit, the process is gone by this point no
#: matter what a third-party thread is doing.
EXIT_DEADLINE = 20.0

_jobs_lock = threading.Lock()
_jobs_stopped = False
_exit_timer: Optional[threading.Timer] = None
_exit_lock = threading.Lock()


def stop_jobs(wait: float = JOB_STOP_WAIT) -> None:
    """Ask every in-flight OCR job to stop, then wait a bounded time for them.

    Idempotent: a quit can reach the teardown twice (the Quit button plus the
    main thread that woke up for it) and the second call must return at once.
    Never raises — this runs inside a shutdown path.
    """
    global _jobs_stopped
    with _jobs_lock:
        if _jobs_stopped:
            return
        _jobs_stopped = True
    try:
        from backend import ocr_service

        active = ocr_service.active_jobs()
        if active:
            log.info("stopping %d running OCR job(s) before exit", len(active))
        ocr_service.shutdown_jobs(wait=wait)
    except Exception:  # noqa: BLE001 - shutdown must not raise
        log.debug("stop-on-exit unavailable", exc_info=True)


def stop_server(server, timeout: float = SERVER_STOP_TIMEOUT) -> None:
    """Stop an ``EmbeddedServer`` (or anything with ``stop(timeout=...)``).

    Also arms the hard-exit deadline: reaching this point means a quit is
    under way, so from here on the process leaves within ``EXIT_DEADLINE``
    whatever else is still running.
    """
    if server is None:
        return
    exit_soon()
    try:
        server.stop(timeout=timeout)
    except Exception:  # noqa: BLE001
        log.debug("stopping the embedded server failed", exc_info=True)


def exit_soon(delay: float = EXIT_DEADLINE, force: bool = True) -> threading.Timer:
    """Guarantee the process dies: force-exit after ``delay`` seconds.

    Armed once per process and refreshed to the latest deadline.  Never call
    this at STARTUP — the deadline is a deadline for the shutdown, not for the
    session.  ``force`` exists for tests, which must be able to inspect the
    timer without the interpreter actually going away.
    """
    global _exit_timer

    def _die() -> None:
        log.warning("still shutting down after %.0fs; forcing exit", delay)
        if not force:
            return
        logging.shutdown()
        # os._exit: threading/futures atexit hooks are exactly what may be
        # wedged, so interpreter teardown is skipped on purpose.
        os._exit(0)

    with _exit_lock:
        if _exit_timer is not None:
            _exit_timer.cancel()
        _exit_timer = threading.Timer(delay, _die)
        _exit_timer.daemon = True
        _exit_timer.name = "exit-deadline"
        _exit_timer.start()
        return _exit_timer


def force_exit_now() -> None:
    """Leave immediately, skipping interpreter teardown (a second signal)."""
    logging.shutdown()
    os._exit(0)


#: How long interpreter exit may spend waiting for leftover worker threads
#: before the process is force-exited.  Long enough for a page that is about
#: to finish, short enough that a user never reaches for Ctrl-C again.
EXIT_JOIN_GRACE = 5.0


def _blocking_threads() -> list:
    """Worker threads still running when the main thread is done.

    Everything except the main thread counts.  Non-daemon threads are joined
    outright, and on Python 3.13+ ``threading._shutdown`` ALSO waits for the
    thread state of daemon threads to be deleted — which is why a daemon
    worker parked in a long blocking call (an OCR page, an HTTP read) can keep
    the process alive there even though it is "only a daemon".
    """
    import threading

    main = threading.main_thread()
    return [t for t in threading.enumerate() if t is not main and t.is_alive()]


def watchdog_on_exit(grace: float = EXIT_JOIN_GRACE) -> list:
    """Bound how long interpreter exit waits for leftover worker threads.

    This is the fix for "the process needs a second Ctrl-C".  The OCR pipeline
    runs pages on ThreadPoolExecutor workers that are uninterruptible while a
    request is in flight, and interpreter shutdown waits for them — on 3.13+
    it waits even for daemon ones, by design.  When the server stops while a
    worker is inside a slow model call, that wait lasts as long as the call:

        INFO:     Finished server process [1234]
        Exception ignored on threading shutdown: ... _python_exit / t.join()

    By the time this hook runs the main thread is done: the process has
    finished serving.  If worker threads are still alive, arm a short deadline
    and let it force the exit; if they finish first (the common case: the page
    in flight was nearly done, and writing its result is exactly what we want)
    the normal teardown proceeds untouched.  Force-exiting an idle process is
    therefore impossible — a process with nothing left to wait for exits on
    its own, without consulting the timer.

    (Verified while fixing this: clearing
    ``concurrent.futures.thread._threads_queues`` does NOT help on Python
    3.13+ — the wait that hangs is the C-level ``_thread._shutdown()``, which
    that registry has no influence on, and it waits for daemon threads too.
    Only a hard exit beats it.)

    Returns the threads that were considered blocking (empty when none were).
    """
    try:
        blocking = _blocking_threads()
        if not blocking:
            return []
        log.warning("exit: %d worker thread(s) still running (%s); leaving "
                    "within %.0fs", len(blocking),
                    ", ".join(sorted(t.name for t in blocking)), grace)
        exit_soon(delay=grace)
        return blocking
    except Exception:  # noqa: BLE001 - an exit hook must never raise
        log.debug("exit watchdog failed", exc_info=True)
        return []


def register_exit_cleanup(func=None) -> bool:
    """Register ``func`` to run at interpreter exit, BEFORE threads are joined.

    Two subtleties, both learned the hard way:

    * ``atexit.register`` alone is not enough.  Since Python 3.13/3.14
      ``concurrent.futures.thread`` registers its join hook through
      ``threading._register_atexit``, which runs inside ``threading._shutdown``
      — a queue ordinary atexit callbacks cannot get in front of.
    * within that queue the order is REVERSED, so registering early is not
      enough either: our hook must be registered AFTER
      ``concurrent.futures.thread`` did, or the blocking join runs first and
      the watchdog is never reached.  ``concurrent.futures`` is therefore
      imported here on purpose (it is a stdlib import, and the OCR pipeline
      pulls it in anyway) before the hook goes in.

    Returns True when at least one registration succeeded.
    """
    if func is None:
        func = watchdog_on_exit
    try:
        import concurrent.futures.thread  # noqa: F401 - ordering, see above
    except Exception:  # noqa: BLE001 - the hook still works without it
        log.debug("could not pre-import concurrent.futures", exc_info=True)
    registered = False
    try:
        import threading

        register = getattr(threading, "_register_atexit", None)
        if callable(register):
            register(func)
            registered = True
    except Exception:  # noqa: BLE001 - never break startup over this
        log.debug("could not register the threading exit hook", exc_info=True)
    try:
        import atexit

        atexit.register(func)
        registered = True
    except Exception:  # noqa: BLE001
        log.debug("could not register the atexit hook", exc_info=True)
    return registered


def reset() -> None:
    """Clear the once-only state (tests, and a fresh start in one process)."""
    global _jobs_stopped, _exit_timer
    with _jobs_lock:
        _jobs_stopped = False
    with _exit_lock:
        if _exit_timer is not None:
            _exit_timer.cancel()
            _exit_timer = None


# --- signal handling ---------------------------------------------------------

def install_signal_handlers(handler, signals=None) -> bool:
    """Route SIGINT/SIGTERM to ``handler``.  Main thread only.

    Ctrl-C in a console-launched app and ``SIGTERM`` from a session manager or
    ``kill`` then reach the same graceful path as the UI's Quit button.  (The
    embedded server runs on a background thread, so uvicorn installs no
    handlers of its own — this is the only thing listening.)  Returns True when
    at least one handler was installed.
    """
    if signals is None:
        signals = [getattr(signal, "SIGINT", None),
                   getattr(signal, "SIGTERM", None)]
    installed = False
    for sig in signals:
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
            installed = True
        except (ValueError, OSError, AttributeError):
            # Not the main thread, or a platform without that signal: the
            # other quit paths still work.
            log.debug("could not install a %s handler", sig, exc_info=True)
    return installed


def make_signal_handler(request_quit, force_exit=None, label: str = "app"):
    """Build a signal handler that asks the app to quit, twice = force.

    The returned handler stays trivial (it only pokes a flag), so a second
    signal — or a signal while the graceful path is blocked — is never
    swallowed: the user asked twice, so the process goes away now.
    """
    if force_exit is None:
        force_exit = force_exit_now
    state = {"seen": False}

    def handler(signum, _frame):  # noqa: ANN001 - signal handler signature
        try:
            name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            name = str(signum)
        if state["seen"]:
            # A second signal means "now", even if force_exit was overridden.
            log.warning("second %s received; exiting immediately", name)
            try:
                force_exit()
            except Exception:  # noqa: BLE001
                log.debug("forced exit failed", exc_info=True)
            return
        state["seen"] = True
        log.info("received %s; shutting %s down", name, label)
        # A quit is under way: from here the process leaves within the
        # deadline even if some thread refuses to cooperate.
        exit_soon()
        # Wake the main thread; the handler itself must stay trivial.
        request_quit()

    return handler


def chain_signal_handler(on_signal, signals=None):
    """Add ``on_signal`` to whatever signal handling is already installed.

    Needed for the one entry point the app does not own: a plain
    ``uvicorn backend.main:app``, where uvicorn installs its own handlers and
    never calls ``backend.main.run()``.  Wrapping instead of replacing keeps
    uvicorn in charge of the shutdown — we only add the one thing it cannot
    know about (telling the OCR engine to stop, so its threads are not left
    with an hour of work to finish).

    Called from the app's lifespan, i.e. after uvicorn installed its handler.
    Returns a restore callable, or None when chaining is not possible (not the
    main thread, a platform without the signal) — in which case the app's
    other quit paths remain in force.
    """
    if signals is None:
        signals = [getattr(signal, "SIGINT", None),
                   getattr(signal, "SIGTERM", None)]
    saved = []
    for sig in signals:
        if sig is None:
            continue
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError, AttributeError):
            continue

        def handler(signum, frame, _previous=previous):  # noqa: ANN001
            try:
                on_signal(signum)
            except Exception:  # noqa: BLE001 - never break the real handler
                log.debug("pre-shutdown work on signal failed", exc_info=True)
            if callable(_previous):
                _previous(signum, frame)

        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            log.debug("could not chain a %s handler", sig, exc_info=True)
            continue
        saved.append((sig, previous))

    if not saved:
        return None

    def restore() -> None:
        for sig, previous in saved:
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError, AttributeError):
                log.debug("could not restore the %s handler", sig,
                          exc_info=True)

    return restore
