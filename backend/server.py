"""Programmatic, freeze-safe local server for the desktop app.

``uvicorn.run(..., reload=True)`` cannot be used in a packaged build: the
reloader re-executes ``sys.executable``, and in a frozen app that IS the
application itself — it would restart itself in a loop.  It also requires the
string import form (``"backend.main:app"``).

This module instead runs uvicorn on a **background thread**, bound to
``127.0.0.1`` on a **kernel-assigned port** (so several instances, or a busy
port, never collide).  uvicorn only installs signal handlers on the main
thread, so the thread must be stopped by setting ``server.should_exit``.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional, Tuple

import uvicorn

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_START_TIMEOUT = 20.0
DEFAULT_STOP_TIMEOUT = 8.0
# uvicorn's own default is None: "wait for every connection to finish, however
# long that takes".  A WebUI holds an SSE stream open (per running job) for the
# whole life of a job, so that default makes ``stop()`` wait for the OCR run —
# the app looks hung after the user asked it to quit.  A finite grace period
# lets in-flight requests finish and then cancels the long-lived streaming
# responses, so quitting is bounded.
DEFAULT_GRACEFUL_SHUTDOWN = 2.0
DEFAULT_FORCE_TIMEOUT = 3.0


def bind_loopback(host: str = DEFAULT_HOST) -> Tuple[socket.socket, int]:
    """Bind a listening socket on ``host`` and return ``(socket, port)``.

    Binding with port ``0`` and *keeping* the socket removes the race in the
    usual "probe a free port, close it, then bind again" sequence: the port is
    ours from the moment the kernel picks it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(128)
    return sock, int(sock.getsockname()[1])


class EmbeddedServer:
    """A uvicorn server running on a background thread.

    Typical desktop use::

        server = EmbeddedServer(app)
        server.start()
        # ... show a window pointed at server.url, or webbrowser.open(server.url)
        server.stop()
    """

    def __init__(self, app, host: str = DEFAULT_HOST,
                 port: int = 0, access_log: bool = False,
                 graceful_shutdown: float = DEFAULT_GRACEFUL_SHUTDOWN) -> None:
        self._app = app
        self.host = host
        self.graceful_shutdown = float(graceful_shutdown)
        # A pre-bound socket when the caller did not pin a port.
        self._sock: Optional[socket.socket] = None
        if port:
            self.port = int(port)
        else:
            self._sock, self.port = bind_loopback(host)
        self.url = f"http://{host}:{self.port}/"

        config = uvicorn.Config(
            app,                       # pass the object: no string import
            host=host,
            port=self.port,
            reload=False,              # never: the reloader re-runs the exe
            workers=1,                 # ditto for the multi-process path
            access_log=access_log,
            log_config=None,           # keep the app's own logging setup
            # Bounded: an open SSE stream must not keep the process alive.
            # None (uvicorn's default) means "wait forever for connections".
            timeout_graceful_shutdown=self.graceful_shutdown,
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self.server.run,
            kwargs=({"sockets": [self._sock]} if self._sock else {}),
            daemon=True,
            name="uvicorn",
        )

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = DEFAULT_START_TIMEOUT) -> "EmbeddedServer":
        """Start serving and block until the port answers (or raise)."""
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self._thread.is_alive():
                raise RuntimeError("embedded server thread died during startup")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"embedded server did not start within {timeout:.0f}s")
            time.sleep(0.05)
        log.info("embedded server listening on %s", self.url)
        return self

    @property
    def was_started(self) -> bool:
        """True once ``start()`` ran (``stop()`` is safe either way)."""
        return self._thread.ident is not None

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        """Ask uvicorn to shut down and wait for the thread to finish.

        Always bounded, and always safe to call — including on a server whose
        ``start()`` never ran or raised (teardown code must not have to know).
        ``should_exit`` starts uvicorn's graceful drain;
        ``timeout_graceful_shutdown`` (set in ``__init__``) caps how long open
        connections — notably SSE streams the WebUI keeps open for a running
        job — may hold that drain up.  ``force_exit`` is the hard deadline: it
        is set from a watchdog thread so uvicorn abandons the drain even if the
        graceful path is wedged, and the join below can therefore never block
        the caller indefinitely.
        """
        self.server.should_exit = True       # uvicorn's only external stop knob
        # A server that never started has nothing to join and nothing to
        # close: uvicorn's ``startup()`` never ran, so the pre-bound socket
        # (if any) is still ours to release.
        if not self.was_started:
            self._close_bound_socket()
            return
        force = threading.Timer(max(0.1, timeout), self._force_exit)
        force.daemon = True
        force.name = "server-force-exit"
        force.start()
        try:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("embedded server did not stop in %.0fs; forcing",
                            timeout)
                self._force_exit()
                self._thread.join(DEFAULT_FORCE_TIMEOUT)
                if self._thread.is_alive():
                    log.error("embedded server thread is still alive after "
                              "%.0fs; the process will exit without it",
                              DEFAULT_FORCE_TIMEOUT)
        finally:
            force.cancel()

    def _close_bound_socket(self) -> None:
        """Release the pre-bound listening socket (never-started servers)."""
        if self._sock is None:
            return
        try:
            self._sock.close()
        except OSError:  # noqa: BLE001
            log.debug("closing the pre-bound socket failed", exc_info=True)
        finally:
            self._sock = None

    def _force_exit(self) -> None:
        """Tell uvicorn to abandon the graceful drain (idempotent, no raise)."""
        try:
            self.server.force_exit = True
        except Exception:  # noqa: BLE001 - teardown must never raise
            log.debug("could not force-exit the embedded server", exc_info=True)

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive() and not self.server.should_exit

    def __enter__(self) -> "EmbeddedServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
