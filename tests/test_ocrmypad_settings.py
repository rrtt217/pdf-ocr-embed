"""The plugin reads config from its own host-injected store, not backend.config.

Where the plugin's config comes from: the backend resolves the effective
config (TOML file + WebUI-saved + OCR_* env) and pushes a snapshot into
``backend.ocrmypad.settings`` via ``configure()`` at startup and on settings
changes.  The engine's client reads that store; it never imports
``backend.config``.  This file pins that decoupling contract.
"""
from __future__ import annotations

from pathlib import Path

from backend.ocrmypad import settings as ocrmypad_settings
from backend.ocrmypad.engine_client import UnlimitedOcrClient

# The real plugin package (backend/ocrmypad is only a compatibility alias).
PKG_DIR = Path(__file__).resolve().parents[1] / "ocrmypdf_unlimited"


def _reset_store() -> None:
    ocrmypad_settings.configure({})


def test_configure_snapshot_get():
    _reset_store()
    try:
        ocrmypad_settings.configure(
            {"base_url": "https://injected.example/v1", "model": "m"})
        assert ocrmypad_settings.snapshot() == {
            "base_url": "https://injected.example/v1", "model": "m"}
        assert ocrmypad_settings.get("base_url") == "https://injected.example/v1"
        assert ocrmypad_settings.get("missing", "dflt") == "dflt"
        # snapshot() returns a copy: mutating it must not touch the store.
        snap = ocrmypad_settings.snapshot()
        snap["base_url"] = "https://hacked.example/v1"
        assert ocrmypad_settings.get("base_url") == "https://injected.example/v1"
    finally:
        _reset_store()


def test_configure_replaces_previous_snapshot():
    _reset_store()
    try:
        ocrmypad_settings.configure({"base_url": "one"})
        ocrmypad_settings.configure({"model": "two"})
        assert ocrmypad_settings.snapshot() == {"model": "two"}
    finally:
        _reset_store()


def test_configure_accepts_none():
    _reset_store()
    ocrmypad_settings.configure(None)
    assert ocrmypad_settings.snapshot() == {}


def test_empty_store_falls_back_to_client_defaults():
    _reset_store()
    client = UnlimitedOcrClient()
    assert client.base_url == "https://api.llm.ustc.edu.cn/v1"
    assert client.model == "unlimited-ocr"
    assert client.api_key == ""
    assert client.max_tokens == 16384


def test_client_reads_host_injected_settings():
    _reset_store()
    try:
        ocrmypad_settings.configure({
            "base_url": "https://injected.example/v1",
            "api_key": "sk-injected",
            "model": "injected-model",
            "max_retries": "2",
            "retry_base_delay": "0.5",
            "retry_max_delay": "4.0",
            "rate_limit_rps": "1",
        })
        client = UnlimitedOcrClient()
        assert client.base_url == "https://injected.example/v1"
        assert client.api_key == "sk-injected"
        assert client.model == "injected-model"
        assert client.max_retries == 2
        assert client.retry_base_delay == 0.5
        assert client.retry_max_delay == 4.0
        assert client.rate_limit_rps == 1.0
    finally:
        _reset_store()


def test_explicit_config_still_wins_over_store():
    _reset_store()
    try:
        ocrmypad_settings.configure({"model": "from-store"})
        client = UnlimitedOcrClient(config={"model": "from-arg"})
        assert client.model == "from-arg"
    finally:
        _reset_store()


def test_plugin_package_never_imports_backend_config():
    """The decoupling contract: no file in the standalone plugin may import ANY
    ``backend.*`` module.

    `ocrmypdf_unlimited/` is the real, self-contained plugin (`backend/ocrmypad/`
    is just a compatibility alias).  The host pushes a snapshot into its store
    and the plugin reads only its own state — in a fully separate package that
    imports nothing from the app.
    """
    import re
    import_re = re.compile(r"^\s*(?:import backend|from backend\b)", re.MULTILINE)
    offenders = []
    for py in sorted(PKG_DIR.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        if import_re.search(text):
            offenders.append(py.name)
    assert not offenders, (
        f"ocrmypdf_unlimited must not import backend.*; found in: {offenders}")
