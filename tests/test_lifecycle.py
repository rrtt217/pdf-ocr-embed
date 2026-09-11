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
