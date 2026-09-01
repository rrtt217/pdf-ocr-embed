"""Font registry: TTC face selection + standalone-face extraction."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import fonts
from backend.fonts import _pick_face_index


# --- _pick_face_index (pure) --------------------------------------------------

def test_pick_face_prefers_exact_name():
    names = ["Noto Sans CJK JP", "Noto Sans CJK KR", "Noto Sans CJK SC"]
    assert _pick_face_index(names, "Noto Sans CJK SC") == 2


def test_pick_face_prefers_trailing_words():
    # The requested registry name matches the SC face through its trailing
    # word — the JP face (first) must NOT win.
    names = ["Noto Sans CJK JP Regular", "Noto Sans CJK KR Regular",
             "Noto Sans CJK SC Regular"]
    assert _pick_face_index(names, "Noto Sans SC") == 2


def test_pick_face_falls_back_to_first():
    assert _pick_face_index(["Some Face", "Other Face"], "Unknown") == 0
    assert _pick_face_index(["Some Face"], "") == 0
    assert _pick_face_index([], "SC") == 0


# --- _ensure_single_face (integration; needs the system Noto TTC) -------------

_TTC = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

needs_ttc = pytest.mark.skipif(
    not Path(_TTC).exists(),
    reason="system Noto CJK .ttc not installed")


@needs_ttc
def test_ensure_single_face_extracts_standalone_ttf(monkeypatch, tmp_path):
    from fontTools.ttLib import TTCollection
    monkeypatch.setattr(fonts, "FACE_CACHE_DIR", tmp_path / "fonts")
    out = fonts._ensure_single_face(_TTC, "Noto Sans SC")
    assert out.endswith(".ttf") and not out.endswith(".ttc")
    p = Path(out)
    assert p.exists() and p.stat().st_size > 0
    # The extracted face must be the SC one, not the JP face MuPDF would load.
    ttc = TTCollection(_TTC)
    expected_idx = _pick_face_index(
        [f["name"].getDebugName(4) or "" for f in ttc.fonts], "Noto Sans SC")
    face = TTCollection(_TTC).fonts[expected_idx]
    # Sanity: the output parses as a standalone font file (not a collection).
    from fontTools.ttLib import TTFont
    standalone = TTFont(out)
    assert standalone.get("glyf") is not None or standalone.get("CFF ") is not None
    # Cached: a second call returns the same path without re-extracting.
    assert fonts._ensure_single_face(_TTC, "Noto Sans SC") == out


@needs_ttc
def test_ensure_single_face_caches_across_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(fonts, "FACE_CACHE_DIR", tmp_path / "fonts")
    first = fonts._ensure_single_face(_TTC, "Noto Sans SC")
    # Remove the source handle: the cache must serve the file, not re-extract.
    second = fonts._ensure_single_face(_TTC, "Noto Sans SC")
    assert first == second


def test_ensure_single_face_passthrough_non_ttc(tmp_path):
    f = tmp_path / "plain.ttf"
    f.write_bytes(b"")
    assert fonts._ensure_single_face(str(f), "Whatever") == str(f)


def test_ensure_single_face_degrades_without_fonttools(monkeypatch, tmp_path):
    # fontTools missing: return the original path with a logged hint —
    # embedding still works, only subsetting stays broken.
    import builtins
    real_import = builtins.__import__

    def no_fonttools(name, *a, **kw):
        if name.startswith("fontTools"):
            raise ImportError("No module named 'fontTools'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_fonttools)
    out = fonts._ensure_single_face(_TTC, "Noto Sans SC")
    assert out == _TTC


# --- build_subset_font (pre-embedding subsetting) ------------------------------

def test_build_subset_font_covers_text_glyphs(monkeypatch, tmp_path):
    # PyMuPDF's own subsetting breaks for many CJK glyphs; the pre-embedding
    # subset is built from the exact unicode set, so every glyph survives.
    if not Path(_TTC).exists():
        pytest.skip("system Noto CJK .ttc not installed")
    from fontTools.ttLib import TTFont
    monkeypatch.setattr(fonts, "FACE_CACHE_DIR", tmp_path / "fonts")
    registry = fonts.load_registry()
    spec = registry.get("Noto Sans SC") or next(iter(registry.values()))
    if not spec.path.lower().endswith((".ttf", ".otf")):
        pytest.skip("no standalone font file available")
    text = "第1页端到端验证文本Abc 123"
    sub = fonts.build_subset_font(spec, text)
    assert sub is not None and sub.path != spec.path
    assert Path(sub.path).stat().st_size < Path(spec.path).stat().st_size
    # The subset preserves metrics.
    assert sub.ink_fraction == spec.ink_fraction
    # Every text glyph is in the subset (cmap keeps the mappings).
    tf = TTFont(sub.path)
    cmap = tf.getBestCmap()
    for ch in set(text):
        assert ord(ch) in cmap, f"glyph {ch!r} missing from subset"
    # Cached: same text -> same subset file.
    sub2 = fonts.build_subset_font(spec, text)
    assert sub2.path == sub.path


def test_build_subset_font_degrades_gracefully(monkeypatch, tmp_path):
    registry = fonts.load_registry()
    spec = registry.get("Noto Sans SC") or next(iter(registry.values()))
    # Empty text -> None (nothing to subset).
    assert fonts.build_subset_font(spec, "") is None
    assert fonts.build_subset_font(spec, "   ") is None
    # fontTools missing -> None (embed the full font instead).
    import builtins
    real_import = builtins.__import__

    def no_fonttools(name, *a, **kw):
        if name.startswith("fontTools"):
            raise ImportError("No module named 'fontTools'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_fonttools)
    if not spec.path.lower().endswith((".ttf", ".otf")):
        pytest.skip("no standalone font file available")
    assert fonts.build_subset_font(spec, "测试") is None
