"""System font registry for embedded-text rendering.

The built-in Base-14 PDF fonts (helv, china-s, japan, korea) are crude CJK
placeholders: in china-s every character — digits, ASCII spaces included — is
rendered a full em wide, so mixed Han+Latin text gets huge gaps between
digits / words.  Real installed system fonts (Noto Sans SC, Microsoft YaHei,
...) render Han proportionally with narrow ASCII spaces and correct digit
widths, and can be embedded into the output PDF as searchable text.

This module scans a curated set of known system font paths and exposes a
name -> font-spec map.  The user can select a font by name in the WebUI, or set
the ``embed_font`` key in ``backend/ocr_config.toml`` to a name or an absolute
path; ``resolve_font`` also accepts any existing .ttf/.otf/.ttc path directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import fitz

log = logging.getLogger(__name__)


@dataclass
class FontSpec:
    """A usable embedded font: resource name + file path + measured metrics."""

    name: str                 # display / config name
    fontname: str             # PDF resource key used with insert_font
    path: str
    family: str = ""
    ink_fraction: float = 0.88   # ink height / fontsize (measured, per font)
    ink_up: float = 0.80         # distance baseline->ink top, in em

    # lazily built fitz.Font holder (not serialized)
    _font: object = field(default=None, repr=False)

    def fit(self) -> "fitz.Font":
        if self._font is None:
            self._font = fitz.Font(fontfile=self.path)
        return self._font

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "family": self.family}


# --------------------------------------------------------------------------
# Curated pool of known-good installed fonts.  Ordered by preference; the
# first path that exists becomes that entry's file.  Keys are stable names.
# --------------------------------------------------------------------------
_CANDIDATES: Dict[str, List[str]] = {
    "Noto Sans SC": [
        "/home/david/.local/share/fonts/NotoSansSC-VF.ttf",   # VF subsets small
        "/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ],
    "Noto Serif SC": [
        "/home/david/.local/share/fonts/NotoSerifSC-VF.ttf",
        "/usr/share/fonts/google-noto-serif-cjk-fonts/NotoSerifCJK-Regular.ttc",
    ],
    "Microsoft YaHei": [
        "/home/david/.local/share/fonts/msyh.ttc",
    ],
    "SimSun": [
        "/home/david/.local/share/fonts/simsun.ttc",
    ],
    "SimHei": [
        "/home/david/.local/share/fonts/simhei.ttf",
    ],
    "KaiTi": [
        "/home/david/.local/share/fonts/simkai.ttf",
    ],
    "FangSong": [
        "/home/david/.local/share/fonts/simfang.ttf",
    ],
    "Liberation Sans": [
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
    ],
    "DejaVu Sans": [
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ],
}


# Files that exist on this machine, discovered once.
_REGISTRY: Dict[str, FontSpec] = {}
_registry_loaded = False


def _first_existing(paths: List[str]) -> Optional[str]:
    for p in paths:
        if Path(p).exists():
            return p
    return None


def _unique_fontname(i: int) -> str:
    return f"UFont{''.join(chr(ord('A') + (i // 26) % 26) + chr(ord('A') + i % 26))}"


def _measure_ink(font: "fitz.Font") -> "tuple[float, float]":
    """Return (ink_fraction, ink_up) estimated for a loaded font.

    ink_fraction = ink height / fontsize; ink_up = baseline -> ink top in em.
    For proportional CJK fonts (Noto / YaHei) Han glyphs are near-square and
    the ink sits just under the cap, so we estimate from the font's
    ascender/descender.  Falls back to conservative CJK defaults.
    """
    try:
        asc = font.ascender or 1.0
        desc = font.descender or -0.2
        # Han glyph ink is roughly (asc-desc)*~0.8, slightly above baseline.
        em = max(asc - desc, 0.5)
        ink_fraction = max(0.6, min(1.0, em * 0.80))
        ink_up = max(0.6, min(asc, asc * 0.88))
        return round(ink_fraction, 3), round(ink_up, 3)
    except Exception:  # noqa: BLE001
        return 0.88, 0.80


def load_registry() -> Dict[str, FontSpec]:
    """Populate and cache the font registry."""
    global _REGISTRY, _registry_loaded
    if _registry_loaded:
        return _REGISTRY
    idx = 0
    for name, paths in _CANDIDATES.items():
        path = _first_existing(paths)
        if not path:
            continue
        try:
            font = fitz.Font(fontfile=path)
            ink, up = _measure_ink(font)
            asc = font.ascender or 1.0
            desc = font.descender or -0.2
            family = (font.name or name).strip()
            _REGISTRY[name] = FontSpec(
                name=name,
                fontname=_unique_fontname(idx),
                path=path,
                family=family,
                ink_fraction=round(ink, 3),
                ink_up=round(up, 3),
                _font=font,
            )
            idx += 1
            log.debug("register font %s -> %s (ink=%s up=%s)", name, path,
                      round(ink, 3), round(up, 3))
        except Exception as exc:  # noqa: BLE001
            log.warning("skip font %s at %s: %s", name, path, exc)
    # Always include a minimal fallback list so callers never ref empty.
    _registry_loaded = True
    log.info("font registry loaded: %d fonts", len(_REGISTRY))
    return _REGISTRY


def available_fonts() -> List[dict]:
    return [s.to_dict() for s in load_registry().values()]


def resolve_font(name_or_path: str | None) -> FontSpec:
    """Resolve a user-supplied font name or file path to a FontSpec.

    If ``name_or_path`` is an existing file path, build a spec from it directly.
    If it matches a registry name, return that.  Otherwise return the default.
    """
    registry = load_registry()
    if not registry:
        raise RuntimeError("No usable system font found for embedding.")
    if not name_or_path:
        return next(iter(registry.values()))

    key = str(name_or_path).strip()
    # absolute path
    if key.startswith("/") or key.endswith((".ttf", ".otf", ".ttc")):
        if Path(key).exists():
            try:
                font = fitz.Font(fontfile=key)
                ink, up = _measure_ink(font)
                return FontSpec(
                    name=Path(key).stem,
                    fontname="UFontAA",
                    path=key,
                    family=(font.name or Path(key).stem),
                    ink_fraction=round(ink, 3),
                    ink_up=round(up, 3),
                    _font=font,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot load font path %s: %s", key, exc)
    # registry name (case-insensitive)
    for name, spec in registry.items():
        if name.lower() == key.lower():
            return spec
    log.warning("font %r not found, using %s", key, next(iter(registry.values())).name)
    return next(iter(registry.values()))