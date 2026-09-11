"""Logging configuration for the PDF OCR Embed backend.

Log level comes from the ``log_level`` key in the OCR config file (default
INFO; use DEBUG for verbose tracing).  Recent log lines are also kept in an
in-memory ring buffer so they can be inspected via the ``GET /api/logs``
endpoint without digging through server stdout.

When packaged (a double-clicked desktop app has no visible console) the same
lines are additionally written to a log file under the per-user log directory
(see ``backend/paths.py``) — that file is the only way to diagnose a startup
failure in a windowed build.
"""
from __future__ import annotations

import logging
import sys
from collections import deque

from backend import paths
from backend.config import redact_secrets, resolve

_LOG_BUFFER: deque[str] = deque(maxlen=1000)


class BufferHandler(logging.Handler):
    """Captures formatted log records into an in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def setup_logging() -> None:
    """Configure root logging once (console/file + in-memory buffer)."""
    level_name = str(resolve().get("log_level") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if setup_logging() is called more than once.
    # A windowed PyInstaller build has no streams at all (sys.stderr is None),
    # and logging would raise on flush — so only add the console handler when
    # there is somewhere to write.
    if sys.stderr is not None and not any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, BufferHandler)
            for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    # File log for packaged builds (and whenever there is no console at all):
    # never created in a source checkout, to keep the repo clean.
    if (paths.is_frozen() or sys.stderr is None) and not any(
            isinstance(h, logging.FileHandler) for h in root.handlers):
        try:
            paths.ensure_dir(paths.LOG_DIR)
            fh = logging.FileHandler(paths.LOG_FILE, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:  # pragma: no cover - unwritable log dir
            root.warning("could not open log file %s: %s", paths.LOG_FILE, exc)
    if not any(isinstance(h, BufferHandler) for h in root.handlers):
        bh = BufferHandler()
        bh.setFormatter(fmt)
        root.addHandler(bh)


def recent_logs(n: int = 200) -> list[str]:
    """Return up to the last `n` formatted log lines (oldest first).

    Lines are scrubbed with :func:`backend.config.redact_secrets` so the WebUI
    debug panel can never echo an API key that happened to reach a log message.
    """
    items = list(_LOG_BUFFER)
    return [redact_secrets(line) for line in items[-n:]]


def clear_logs() -> None:
    _LOG_BUFFER.clear()
