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

# Standalone-face cache for .ttc collections (gitignored runtime dir; the
# cleaner never touches cache/).  Module attr so tests can point it elsewhere.
FACE_CACHE_DIR = Path("cache") / "fonts"


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


def _pick_face_index(names: List[str], prefer: str) -> int:
    """Pick the best-matching face index of a .ttc collection.

    ``names`` are the faces' full names in collection order, ``prefer`` the
    requested font name (e.g. "Noto Sans SC").  Tries the full name, then the
    trailing words ("SC" before "Sans" before "Noto"), so the SC face wins
    over the JP face that MuPDF would otherwise silently load (the first).
    """
    lowered = [n.lower() for n in names]
    want = (prefer or "").strip().lower()
    if want:
        if want in lowered:
            return lowered.index(want)
        for word in reversed(want.split()):
            for i, n in enumerate(lowered):
                if word and word in n:
                    return i
    return 0


def _ensure_single_face(path: str, prefer: str) -> str:
    """Return an embeddable single-face font file for ``path``.

    ``.ttc`` TrueType Collections embed every face and break PyMuPDF's
    ``subset_fonts()`` ("format error: Index bounds"), which silently leaves
    the whole multi-MB collection in the output PDF.  Extract the best-
    matching face (see ``_pick_face_index``) to a cached standalone TTF under
    ``FACE_CACHE_DIR`` and return that.  Non-.ttc files are returned as-is;
    without fontTools (or on any extraction error) the original path is
    returned with a logged hint — embedding still works, only subsetting
    stays broken.
    """
    if not path.lower().endswith(".ttc"):
        return path
    try:
        from fontTools.ttLib import TTCollection
    except ImportError:
        log.warning("font %s is a .ttc collection; subsetting needs the "
                    "fontTools package (pip install fonttools) — the output "
                    "will embed the full collection (much larger file)",
                    path)
        return path
    try:
        FACE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = FACE_CACHE_DIR / f"{Path(path).stem}-face.ttf"
        if out.exists() and out.stat().st_size > 0:
            return str(out)
        ttc = TTCollection(path, lazy=True)
        names = []
        for f in ttc.fonts:
            try:
                names.append(f["name"].getDebugName(4) or "")
            except Exception:  # noqa: BLE001
                names.append("")
        idx = _pick_face_index(names, prefer)
        ttc.fonts[idx].save(str(out))
        log.info("font %s: extracted face %d (%s) -> %s (%d MB)",
                 Path(path).name, idx, names[idx] or "?", out,
                 out.stat().st_size // (1024 * 1024))
        return str(out)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot extract a single face from %s: %s — using the "
                    "collection as-is (font subsetting will fail and bloat "
                    "the output)", path, exc)
        return path


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


def build_subset_font(spec: "FontSpec", text: str,
                      tag: str = "embed") -> Optional["FontSpec"]:
    """Pre-subset ``spec`` to exactly the glyphs used in ``text``.

    PyMuPDF's own ``subset_fonts()`` is unreliable for CJK (MuPDF throws
    "Index bounds" for many common glyphs; the fontTools fallback can drop
    glyphs and truncate the text layer).  Subsetting the font BEFORE embedding
    avoids both: the subset is built from the exact unicode set of the text,
    so every inserted glyph is present and the output stays small.

    Returns a FontSpec pointing at a cached subset file under
    ``FACE_CACHE_DIR``, or None when fontTools is missing, the text is empty,
    or subsetting fails (the caller keeps the original spec — embedding still
    works, only the output is larger).
    """
    if not spec.path.lower().endswith((".ttf", ".otf")) or not text.strip():
        return None
    try:
        from fontTools import subset
    except ImportError:
        return None
    try:
        unicodes = sorted({ord(c) for c in text} | {ord(" ")})
        import hashlib
        key = hashlib.sha1(",".join(map(str, unicodes)).encode()).hexdigest()[:12]
        FACE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = FACE_CACHE_DIR / f"{Path(spec.path).stem}-{tag}-{key}.ttf"
        if not (out.exists() and out.stat().st_size > 0):
            opts = subset.Options()
            opts.name_IDs = ["*"]       # keep the name table
            opts.notdef_outline = True  # keep .notdef drawable
            font = subset.load_font(spec.path, opts)
            s = subset.Subsetter(opts)
            s.populate(unicodes=unicodes)
            s.subset(font)
            subset.save_font(font, str(out), opts)
            log.info("font subset for %s: %d glyphs -> %s (%d KB)",
                     spec.name, len(unicodes), out, out.stat().st_size // 1024)
        # Subsetting preserves metrics, so ink measurements carry over.
        return FontSpec(name=spec.name, fontname=spec.fontname, path=str(out),
                        family=spec.family, ink_fraction=spec.ink_fraction,
                        ink_up=spec.ink_up)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot pre-subset font %s: %s — embedding the full "
                    "font instead", spec.name, exc)
        return None


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
            # .ttc collections: embed/measure the matching face only (a raw
            # TTC breaks subsetting and embeds the whole multi-MB collection).
            embed_path = _ensure_single_face(path, name)
            font = fitz.Font(fontfile=embed_path)
            ink, up = _measure_ink(font)
            asc = font.ascender or 1.0
            desc = font.descender or -0.2
            family = (font.name or name).strip()
            _REGISTRY[name] = FontSpec(
                name=name,
                fontname=_unique_fontname(idx),
                path=embed_path,
                family=family,
                ink_fraction=round(ink, 3),
                ink_up=round(up, 3),
                _font=font,
            )
            idx += 1
            log.debug("register font %s -> %s (ink=%s up=%s)", name, embed_path,
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
                embed_path = _ensure_single_face(key, Path(key).stem)
                font = fitz.Font(fontfile=embed_path)
                ink, up = _measure_ink(font)
                return FontSpec(
                    name=Path(key).stem,
                    fontname="UFontAA",
                    path=embed_path,
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