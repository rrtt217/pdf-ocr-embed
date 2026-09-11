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
DEFAULT_STOP_TIMEOUT = 10.0


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
                 port: int = 0, access_log: bool = False) -> None:
        self._app = app
        self.host = host
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

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        """Ask uvicorn to shut down and wait for the thread to finish."""
        self.server.should_exit = True       # uvicorn's only external stop knob
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("embedded server did not stop in %.0fs; forcing",
                        timeout)
            self.server.force_exit = True
            self._thread.join(2.0)

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive() and not self.server.should_exit

    def __enter__(self) -> "EmbeddedServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
