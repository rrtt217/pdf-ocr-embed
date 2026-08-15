"""Logging configuration for the PDF OCR Embed backend.

Log level comes from the ``log_level`` key in ``backend/ocr_config.toml``
(default INFO; use DEBUG for verbose tracing).  Recent log lines are also kept
in an in-memory ring buffer so they can be inspected via the
``GET /api/logs`` endpoint without digging through server stdout.
"""
from __future__ import annotations

import logging
from collections import deque

from backend.config import resolve

_LOG_BUFFER: deque[str] = deque(maxlen=1000)


class BufferHandler(logging.Handler):
    """Captures formatted log records into an in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def setup_logging() -> None:
    """Configure root logging once (console + in-memory buffer)."""
    level_name = str(resolve().get("log_level") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if setup_logging() is called more than once.
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, BufferHandler)
               for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if not any(isinstance(h, BufferHandler) for h in root.handlers):
        bh = BufferHandler()
        bh.setFormatter(fmt)
        root.addHandler(bh)


def recent_logs(n: int = 200) -> list[str]:
    """Return up to the last `n` formatted log lines (oldest first)."""
    items = list(_LOG_BUFFER)
    return items[-n:]


def clear_logs() -> None:
    _LOG_BUFFER.clear()
