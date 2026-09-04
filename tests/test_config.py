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


def test_engine_keys_resolve_from_file(monkeypatch, tmp_path):
    cfg_file = tmp_path / "ocr_config.toml"
    cfg_file.write_text(
        'ocr_engine = "tesseract"\n'
        'ocrmypdf_mode = "skip"\n'
        'ocrmypdf_jobs = "4"\n',
        encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_file)
    cfg = config.resolve()
    assert cfg["ocr_engine"] == "tesseract"
    assert cfg["ocrmypdf_mode"] == "skip"
    assert cfg["ocrmypdf_jobs"] == "4"


def test_engine_env_aliases(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    monkeypatch.setenv("OCRMYPDF_MODE", "redo")
    monkeypatch.setenv("OCRMYPDF_JOBS", "2")
    cfg = config.resolve()
    assert cfg["ocr_engine"] == "tesseract"
    assert cfg["ocrmypdf_mode"] == "redo"
    assert cfg["ocrmypdf_jobs"] == "2"


def test_legacy_tess_lang_maps_to_ocrmypdf_language(monkeypatch, tmp_path):
    """The pre-rebuild config key ``tess_lang`` still controls the OCR
    language; ``ocrmypdf_language`` wins when both are present."""
    cfg_file = tmp_path / "ocr_config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config, "_saved", {})
    cfg_file.write_text('tess_lang = "chi_sim+eng"\n', encoding="utf-8")
    cfg = config.resolve()
    assert cfg.get("ocrmypdf_language") == "chi_sim+eng"
    # Explicit ocrmypdf_language takes precedence over the legacy alias.
    cfg_file.write_text('tess_lang = "eng"\nocrmypdf_language = "chi_sim+eng"\n',
                        encoding="utf-8")
    assert config.resolve().get("ocrmypdf_language") == "chi_sim+eng"


def test_effective_settings_exposes_ocrmypdf_language(monkeypatch, tmp_path):
    """The WebUI settings page reads/writes ocrmypdf_language; it must be
    returned by /api/settings (and visible when coming from tess_lang)."""
    cfg_file = tmp_path / "ocr_config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config, "_saved", {})
    cfg_file.write_text('tess_lang = "chi_sim+eng"\n', encoding="utf-8")
    eff = config.get_effective_settings()
    assert eff["ocrmypdf_language"] == "chi_sim+eng"


def test_settings_save_persists_ocrmypdf_language(monkeypatch, tmp_path):
    cfg_file = tmp_path / "ocr_config.toml"
    cfg_file.write_text('tess_lang = "eng"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(config, "_saved", {})
    config.save({"ocrmypdf_language": "chi_sim+eng"})
    saved = config._load_file_config()
    assert saved.get("ocrmypdf_language") == "chi_sim+eng"
    assert config.resolve().get("ocrmypdf_language") == "chi_sim+eng"
