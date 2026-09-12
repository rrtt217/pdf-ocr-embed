"""Desktop lifecycle: the Quit endpoint, its CSRF guard, and CORS scope.

Together these make the packaged app stoppable from its own UI without letting
a random web page in the user's browser shut the local server down.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import cleanup as cleanup_mod
from backend import lifecycle, ocr_service
from backend.main import app

QUIT_HEADERS = {"X-PDF-OCR-Embed": "quit"}


@pytest.fixture(autouse=True)
def _clean_lifecycle():
    lifecycle.reset()
    yield
    lifecycle.reset()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ocr_service, "restore_jobs", lambda: 0)
    monkeypatch.setattr(cleanup_mod, "start_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "stop_background_cleanup", lambda: None)
    with TestClient(app) as test_client:
        yield test_client


# --- the desktop flag the WebUI reads to show its Quit button ----------------

def test_health_reports_plain_server_mode_by_default(client):
    assert client.get("/api/health").json()["desktop"] is False


def test_health_reports_desktop_mode_once_set(client):
    lifecycle.set_desktop_mode(True)
    assert client.get("/api/health").json()["desktop"] is True


# --- the quit endpoint -------------------------------------------------------

def test_quit_requires_the_confirmation_header(client):
    resp = client.post("/api/app/quit")
    assert resp.status_code == 403
    assert lifecycle.is_quit_requested() is False


def test_quit_with_the_header_requests_shutdown(client):
    resp = client.post("/api/app/quit", headers=QUIT_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "quitting"
    assert lifecycle.is_quit_requested() is True


def test_quit_invokes_the_registered_handler(client):
    called: list[bool] = []
    lifecycle.set_quit_handler(lambda: called.append(True))

    client.post("/api/app/quit", headers=QUIT_HEADERS)

    assert called == [True]


def test_a_failing_quit_handler_still_answers_200(client):
    """Tearing down must not turn into a 500 for the UI."""
    def boom() -> None:
        raise RuntimeError("nope")

    lifecycle.set_quit_handler(boom)
    resp = client.post("/api/app/quit", headers=QUIT_HEADERS)
    assert resp.status_code == 200
    assert lifecycle.is_quit_requested() is True


def test_quit_is_idempotent(client):
    lifecycle.request_quit()
    lifecycle.request_quit()
    assert lifecycle.is_quit_requested() is True


def test_wait_for_quit_unblocks_after_a_request():
    assert lifecycle.wait_for_quit(timeout=0) is False
    lifecycle.request_quit()
    assert lifecycle.wait_for_quit(timeout=0) is True


# --- the shutdown hook (what actually stops OCR jobs) ------------------------

def test_quit_runs_the_shutdown_hook(client):
    """Desktop registers this hook so the Quit button stops running jobs."""
    order: list[str] = []
    lifecycle.set_shutdown_hook(lambda: order.append("hook"))
    lifecycle.set_quit_handler(lambda: order.append("handler"))

    client.post("/api/app/quit", headers=QUIT_HEADERS)

    # The hook runs BEFORE the window handler: jobs are already stopping when
    # the window goes away.
    assert order == ["hook", "handler"]


def test_a_failing_shutdown_hook_still_answers_200(client):
    def boom() -> None:
        raise RuntimeError("nope")

    lifecycle.set_shutdown_hook(boom)
    resp = client.post("/api/app/quit", headers=QUIT_HEADERS)
    assert resp.status_code == 200


def test_the_quit_endpoint_is_not_held_up_by_a_slow_hook(client):
    """A hook stuck mid-page must not turn the quit into a browser timeout.

    The endpoint bounds its wait (``QUIT_REQUEST_TIMEOUT``); the hook keeps
    running on its own thread while the response is already on its way.
    """
    import threading
    import time

    released = threading.Event()

    def slow_hook() -> None:
        released.wait(10)

    lifecycle.set_shutdown_hook(slow_hook)
    start = time.monotonic()
    resp = client.post("/api/app/quit", headers=QUIT_HEADERS)
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert elapsed < lifecycle.QUIT_REQUEST_TIMEOUT + 2.0
    released.set()


# --- CORS: loopback only (this is what makes the header guard real) ----------

def test_cors_allows_loopback_origins(client):
    resp = client.get("/api/health", headers={"Origin": "http://127.0.0.1:5000"})
    assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:5000"


def test_cors_allows_localhost(client):
    resp = client.get("/api/health", headers={"Origin": "http://localhost:8000"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:8000"


def test_cors_does_not_allow_remote_origins(client):
    resp = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers
