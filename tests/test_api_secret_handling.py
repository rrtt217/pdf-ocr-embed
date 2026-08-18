"""Frontend-facing surfaces must never echo the configured OCR API key.

The key may arrive from ``backend/ocr_config.toml`` or from the highest-priority
``OCR_API_KEY`` / ``USTC_API_KEY`` environment override.  The settings route is
supposed to reveal only a masked hint, and the debug-log route must scrub any
key that accidentally reached a log record.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path

from fastapi.testclient import TestClient

from backend import cleanup as cleanup_mod
from backend import config
from backend import logging_config
from backend import ocr_service
from backend.main import app

FILE_SECRET = "file-audit-secret-0123456789abcdef"
ENV_SECRET = "env-audit-secret-abcdef0123456789"

READ_ENDPOINTS = (
    "/api/settings",
    "/api/health",
    "/api/logs?n=100",
    "/api/jobs",
    "/api/cache",
    "/api/cleanup",
    "/api/fonts",
)


def _stub_lifespan(monkeypatch):
    """Keep API-secret tests offline: no job restore and no cleanup scan."""
    monkeypatch.setattr(ocr_service, "restore_jobs", lambda: 0)
    monkeypatch.setattr(cleanup_mod, "start_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "stop_background_cleanup", lambda: None)
    monkeypatch.setattr(cleanup_mod, "inventory", lambda: {"totals": {}, "areas": {}})


def _write_file_config(path: Path, secret: str) -> None:
    path.write_text(
        'provider = "ustc"\n'
        f'api_key = "{secret}"\n'
        'base_url = "https://example.test/v1"\n'
        'model = "audit-model"\n',
        encoding="utf-8",
    )


def _clear_env_api_keys(monkeypatch) -> None:
    monkeypatch.delenv("OCR_API_KEY", raising=False)
    monkeypatch.delenv("USTC_API_KEY", raising=False)


def test_redact_secrets_covers_file_saved_and_env_sources(monkeypatch, tmp_path):
    cfg_path = tmp_path / "ocr_config.toml"
    _write_file_config(cfg_path, FILE_SECRET)
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(config, "_saved", {"api_key": "saved-audit-secret-9876543210"})
    monkeypatch.setenv("OCR_API_KEY", ENV_SECRET)

    text = f"a={FILE_SECRET} b=saved-audit-secret-9876543210 c={ENV_SECRET}"
    redacted = config.redact_secrets(text)
    assert FILE_SECRET not in redacted
    assert "saved-audit-secret-9876543210" not in redacted
    assert ENV_SECRET not in redacted
    assert "[REDACTED]" in redacted


def test_effective_settings_masks_file_key(monkeypatch, tmp_path):
    cfg_path = tmp_path / "ocr_config.toml"
    _write_file_config(cfg_path, FILE_SECRET)
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(config, "_saved", {})
    _clear_env_api_keys(monkeypatch)

    settings = config.get_effective_settings()

    target = FILE_SECRET
    assert settings["has_api_key"] is True
    assert settings["api_key_masked"] == config._mask_key(target)
    assert target not in settings["api_key_masked"]
    # Never serialize the raw key, even if a future change returns extra fields.
    assert target not in json.dumps(settings)


def test_effective_settings_env_override_also_masked(monkeypatch, tmp_path):
    cfg_path = tmp_path / "ocr_config.toml"
    _write_file_config(cfg_path, FILE_SECRET)
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(config, "_saved", {})
    monkeypatch.setenv("OCR_API_KEY", ENV_SECRET)

    settings = config.get_effective_settings()

    assert settings["api_key_masked"] == config._mask_key(ENV_SECRET)
    serialized = json.dumps(settings)
    assert FILE_SECRET not in serialized
    assert ENV_SECRET not in serialized


def test_frontend_endpoints_and_logs_do_not_expose_api_key(monkeypatch, tmp_path):
    cfg_path = tmp_path / "ocr_config.toml"
    _write_file_config(cfg_path, FILE_SECRET)
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(config, "_saved", {})
    monkeypatch.setenv("OCR_API_KEY", ENV_SECRET)
    _stub_lifespan(monkeypatch)

    logging_config.clear_logs()

    with TestClient(app) as client:
        for path in READ_ENDPOINTS:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)
            body = response.text
            assert FILE_SECRET not in body, path
            assert ENV_SECRET not in body, path

        settings = client.get("/api/settings").json()
        assert settings["api_key_masked"] == config._mask_key(ENV_SECRET)
        assert settings["has_api_key"] is True

        # A log record containing the key must be scrubbed by /api/logs.
        logging.getLogger("audit.secret").warning("token=%s leaked", ENV_SECRET)
        logs = client.get("/api/logs?n=100").json()["lines"]
        combined = "\n".join(logs)
        assert ENV_SECRET not in combined
        assert "[REDACTED]" in combined


def test_ocr_error_message_is_redacted_before_frontend(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "ocr_config.toml")
    monkeypatch.setattr(config, "_saved", {})
    monkeypatch.setenv("OCR_API_KEY", ENV_SECRET)

    job_id = "audit-secret-job"
    img_dir = tmp_path / "work"
    job = {
        "id": job_id,
        "filename": "audit.pdf",
        "pdf_path": str(tmp_path / "audit.pdf"),
        "img_dir": str(img_dir),
        "pages": [],
        "num_pages": 0,
        "current": 0,
        "status": "uploaded",
        "adapter": "unlimited",
        "concurrency": 1,
        "error": None,
        "embedded_path": None,
        "thumb_path": None,
        "created": 0,
        "cancel_event": threading.Event(),
    }
    with ocr_service._jobs_lock:
        ocr_service._JOBS[job_id] = job
    with ocr_service._streams_lock:
        ocr_service._STREAMS[job_id] = deque(maxlen=10)

    def boom(*_args, **_kwargs):
        raise RuntimeError(f"provider rejected key {ENV_SECRET}")

    monkeypatch.setattr(ocr_service, "_make_adapter", boom)

    try:
        ocr_service.run_ocr(job_id, "unlimited")
        assert job["error"] is not None
        assert ENV_SECRET not in job["error"]
        assert "[REDACTED]" in job["error"]
        events = ocr_service.drain_events(job_id)
        messages = [str(ev.get("message", "")) for ev in events]
        assert ENV_SECRET not in json.dumps(messages)
    finally:
        with ocr_service._jobs_lock:
            ocr_service._JOBS.pop(job_id, None)
        with ocr_service._streams_lock:
            ocr_service._STREAMS.pop(job_id, None)
