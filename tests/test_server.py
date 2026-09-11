"""The embedded desktop server (``backend.server``).

These pin the properties that make a packaged build usable: loopback-only,
a kernel-assigned port, and a clean programmatic start/stop (uvicorn's signal
handlers only work on the main thread, so the stop path matters).
"""
from __future__ import annotations

import httpx

from backend import cleanup as cleanup_mod
from backend import ocr_service, server


def _stub_lifespan(monkeypatch):
    """Keep the server test offline and repo-clean."""
    monkeypatch.setattr(ocr_service, "restore_jobs", lambda: 0)
    monkeypatch.setattr(cleanup_mod, "start_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "stop_background_cleanup", lambda: None)


# --- port binding -------------------------------------------------------------

def test_bind_loopback_keeps_the_socket_and_picks_a_free_port():
    sock, port = server.bind_loopback()
    try:
        assert sock.getsockname()[0] == server.DEFAULT_HOST
        assert 1024 < port < 65536
    finally:
        sock.close()


def test_two_binds_get_different_ports():
    a, port_a = server.bind_loopback()
    b, port_b = server.bind_loopback()
    try:
        assert port_a != port_b
    finally:
        a.close()
        b.close()


# --- start / serve / stop -----------------------------------------------------

def test_embedded_server_serves_the_api_and_stops(monkeypatch):
    _stub_lifespan(monkeypatch)
    from backend.main import app

    srv = server.EmbeddedServer(app)
    try:
        srv.start(timeout=30)
        assert srv.is_running
        assert srv.url == f"http://127.0.0.1:{srv.port}/"
        # Never exposed beyond the loopback interface.
        assert "0.0.0.0" not in srv.url

        resp = httpx.get(srv.url + "api/health", timeout=15.0)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        srv.stop()

    assert not srv.is_running


def test_embedded_server_serves_the_frontend(monkeypatch):
    _stub_lifespan(monkeypatch)
    from backend.main import app

    srv = server.EmbeddedServer(app)
    try:
        srv.start(timeout=30)
        index = httpx.get(srv.url, timeout=15.0)
        assert index.status_code == 200
        assert "text/html" in index.headers["content-type"]
        # The JS the page loads must be reachable too (StaticFiles mount).
        static = httpx.get(srv.url + "static/app.js", timeout=15.0)
        assert static.status_code == 200
    finally:
        srv.stop()


def test_context_manager_starts_and_stops(monkeypatch):
    _stub_lifespan(monkeypatch)
    from backend.main import app

    with server.EmbeddedServer(app, port=0) as srv:
        assert srv.is_running
        assert httpx.get(srv.url + "api/health", timeout=15.0).status_code == 200
    assert not srv.is_running
