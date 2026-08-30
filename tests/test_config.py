"""Precedence: env > WebUI-saved > TOML file (never reads os.environ elsewhere)."""
from __future__ import annotations

from backend import config


def test_precedence_env_beats_saved_beats_file(monkeypatch):
    monkeypatch.setattr(config, "_saved", {"base_url": "saved-url"})
    monkeypatch.setattr(config, "_load_file_config",
                         lambda: {"base_url": "file-url",
                                  "model": "file-model",
                                  "provider": "ustc"})
    monkeypatch.setattr(config, "_load_env", lambda: {"model": "env-model"})
    cfg = config.resolve()
    assert cfg["base_url"] == "saved-url"
    assert cfg["model"] == "env-model"
    assert cfg["provider"] == "ustc"


def test_provider_preset_fills_defaults(monkeypatch):
    monkeypatch.setattr(config, "_saved", {})
    monkeypatch.setattr(config, "_load_file_config",
                         lambda: {"provider": "openai"})
    monkeypatch.setattr(config, "_load_env", lambda: {})
    cfg = config.resolve()
    assert cfg["model"] == "gpt-4o-mini"
    assert cfg["base_url"] == "https://api.openai.com/v1"


def test_preset_never_overrides_explicit_value(monkeypatch):
    monkeypatch.setattr(config, "_saved", {})
    monkeypatch.setattr(config, "_load_file_config",
                         lambda: {"provider": "openai", "model": "mine"})
    monkeypatch.setattr(config, "_load_env", lambda: {})
    assert config.resolve()["model"] == "mine"


def test_masking():
    assert config._mask_key("") == ""
    assert config._mask_key("short") == "*" * 5
    m = config._mask_key("sk-abcdefghijklmnopq")
    assert m.startswith("sk-a") and m.endswith("nopq") and "*" in m
    assert config._has_mask(m) and not config._has_mask("plain")


def test_bool_coercion():
    assert config._as_str(True) == "true"
    assert config._as_str(False) == "false"
    assert config._as_str(720) == "720"


def test_empty_string_falls_back_to_preset(monkeypatch):
    """A WebUI save writes empty strings for cleared fields; resolve() must
    treat them as absent so the provider preset fills base_url/model again."""
    monkeypatch.setattr(config, "_saved", {})
    monkeypatch.setattr(config, "_load_file_config",
                        lambda: {"provider": "ustc", "base_url": "", "model": ""})
    monkeypatch.setattr(config, "_load_env", lambda: {})
    cfg = config.resolve()
    assert cfg["base_url"] == "https://api.llm.ustc.edu.cn/v1"
    assert cfg["model"] == "unlimited-ocr"


def test_unlimited_batch_keys_resolve_from_file(monkeypatch, tmp_path):
    cfg_file = tmp_path / "ocr_config.toml"
    cfg_file.write_text(
        'unlimited_batch_enabled = "false"\n'
        'unlimited_max_pages_per_batch = "5"\n',
        encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_file)
    cfg = config.resolve()
    assert cfg["unlimited_batch_enabled"] == "false"
    assert cfg["unlimited_max_pages_per_batch"] == "5"


def test_unlimited_batch_env_aliases(monkeypatch):
    monkeypatch.setenv("OCR_UNLIMITED_BATCH_ENABLED", "false")
    monkeypatch.setenv("OCR_UNLIMITED_MAX_PAGES_PER_BATCH", "3")
    cfg = config.resolve()
    assert cfg["unlimited_batch_enabled"] == "false"
    assert cfg["unlimited_max_pages_per_batch"] == "3"
