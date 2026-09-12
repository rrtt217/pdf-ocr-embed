"""Shared test bootstrap: make the repo root importable (backend package)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _clean_shutdown_state():
    """Keep the once-only shutdown guards from leaking between tests.

    ``shutdown_jobs`` latches "the app is shutting down" for the life of the
    process (correct for an app, wrong for a test session), and the bounded
    teardown guards in ``backend.shutdown`` are once-only by design.
    """
    from backend import ocr_service, shutdown

    ocr_service.reset_shutdown_state()
    shutdown.reset()
    yield
    ocr_service.reset_shutdown_state()
    shutdown.reset()
