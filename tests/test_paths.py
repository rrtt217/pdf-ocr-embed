"""Path resolution: source checkout vs frozen bundle (``backend/paths.py``).

Getting this wrong is what breaks a packaged app — writes land in a read-only
or temporary directory — so the two modes are pinned here.
"""
from __future__ import annotations

import sys
from pathlib import Path

from backend import paths


# --- source checkout (what the test suite runs in) ---------------------------

def test_source_mode_keeps_the_repo_layout():
    assert paths.is_frozen() is False

    backend_dir = Path(paths.__file__).resolve().parent
    repo_root = backend_dir.parent

    assert paths.resource_dir() == repo_root
    assert paths.FRONTEND_DIR == repo_root / "frontend"
    assert paths.FRONTEND_DIR.is_dir()          # a source checkout has it

    # Writable state stays repo-relative so dev/CLI/tests are unchanged.
    assert paths.data_dir() == repo_root
    assert paths.UPLOAD_DIR == repo_root / "uploads"
    assert paths.WORK_DIR == repo_root / "work"
    assert paths.OUTPUT_DIR == repo_root / "output"
    assert paths.config_dir() == backend_dir
    assert paths.CONFIG_FILE == backend_dir / "ocr_config.toml"


def test_ensure_dir_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    assert paths.ensure_dir(target) == target
    assert target.is_dir()


# --- frozen mode -------------------------------------------------------------

def _fake_frozen(monkeypatch, bundle: Path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)


def test_frozen_resource_dir_comes_from_the_bundle(monkeypatch, tmp_path):
    _fake_frozen(monkeypatch, tmp_path / "bundle")

    assert paths.is_frozen() is True
    assert paths.resource_dir() == tmp_path / "bundle"
    # Read-only assets follow the bundle.  (FRONTEND_DIR & co. are import-time
    # snapshots of these functions — in a real frozen app sys.frozen is already
    # set before the module is imported, so they are correct there.)
    assert paths.resource_dir() / "frontend" == tmp_path / "bundle" / "frontend"
    assert paths.resource_dir() / "config.example.toml" == \
        tmp_path / "bundle" / "config.example.toml"


def test_frozen_writable_dirs_go_to_per_user_locations(monkeypatch, tmp_path):
    _fake_frozen(monkeypatch, tmp_path / "bundle")
    monkeypatch.setattr(paths, "_user_dir", lambda kind: tmp_path / kind)

    assert paths.data_dir() == tmp_path / "data"
    assert paths.config_dir() == tmp_path / "config"
    assert paths.log_dir() == tmp_path / "log"
    # … and must NOT be inside the (read-only) bundle.
    assert tmp_path / "bundle" not in paths.data_dir().parents


def test_frozen_uses_platformdirs_by_default(monkeypatch, tmp_path):
    """Without a stub, the per-user dirs are real platformdirs locations."""
    _fake_frozen(monkeypatch, tmp_path / "bundle")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    data = paths.data_dir()
    assert paths.APP_NAME in str(data)
    assert paths.is_frozen() is True


# --- downstream effect: plugin discovery in a frozen build -------------------

def test_plugin_discovery_is_frozen_safe(monkeypatch):
    """Frozen builds never rely on entry-point metadata.

    Packaging does not copy the plugin's dist-info, so returning False here
    makes ``plugin_path()`` use the dotted module name instead — the same
    module never gets registered twice by pluggy.
    """
    from backend import ocr_service

    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    assert ocr_service.plugin_auto_loaded() is False
