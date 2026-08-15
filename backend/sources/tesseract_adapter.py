"""Tesseract OCR adapter (full implementation).

Runs Tesseract via pytesseract, reading the per-word TSV data
(``image_to_data``) and clustering words into normalized ``OcrPage`` blocks.

The TSV output already lives in *original pixel space* (``left/top/width/
height`` are pixel coordinates from the page image), so no canvas remapping
is needed — unlike the normalized 1000x1000 canvas used by the
``unlimited_ocr_adapter``.

Configuration (all optional, sensible defaults, set in
``backend/ocr_config.toml``):
    lang   -> ``tess_lang``   e.g. "eng", "chi_sim", "chi_sim+eng"
    psm    -> ``tess_psm``    (page segmentation mode)
    oem    -> ``tess_oem``    (engine mode)
    tessdata_dir -> ``tessdata_dir``
    config / extra flags -> ``tess_config``

If the ``tesseract`` binary is not on ``PATH``, set ``tess_cmd`` to its
absolute path.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from backend.config import resolve
from backend.models import OcrBlock, OcrPage
from backend.sources.base import OcrSource, UnavailableError

log = logging.getLogger(__name__)

__all__ = ["TesseractAdapter"]

# Kind heuristics -----------------------------------------------------------
# Block classification based on the font/line characteristics reported by
# Tesseract's ``oem``/``psm`` heuristics and simple geometry.
_MATH_HINTS = set("≤≥≠≈≡±×÷∑∏∫√∞→←⇒⇔∈∉⊂⊃∂∇∆αβγδεζηθικλμνξπρστυφχψω"
                  "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ^_*/=+()[]{}")

# Characters that signal a heading (numbered/localizable prefixes are common).
_HEADING_RE = None


def _build_heading_re() -> Any:
    import re
    global _HEADING_RE
    if _HEADING_RE is None:
        # e.g. "1.", "1.2", "第一章", "§1", "引言", "CHAPTER 1", "§ 1.2.3"
        _HEADING_RE = re.compile(
            r"^(?:\s*\d+(?:[\.、．]\d+)*[\.、．]?\s*|"
            r"\s*[第][一二三四五六七八九十百千万0-9]+[章节篇部分]?\s*|"
            r"\s*§\s*\d+[\.\d]*\s*|"
            r"\s*(?:CHAPTER|Chapter|Chapter\s+\d+)\s*|"
            r"\s*[\u4e00-\u9fff]{1,8}\s*)$"
        )
    return _HEADING_RE


class TesseractAdapter(OcrSource):
    """OCR page images with Tesseract, clustering words into blocks."""

    name = "tesseract"

    def __init__(self, lang: Optional[str] = None, psm: Optional[int] = None,
                 oem: Optional[int] = None, config: Optional[str] = None,
                 tessdata_dir: Optional[str] = None,
                 tess_cmd: Optional[str] = None,
                 min_line_fontsize: Optional[float] = None):
        # Resolve settings from the shared config first, then explicit args.
        cfg = resolve()
        self.lang = lang or cfg.get("tess_lang") or "eng"
        self.psm = int(psm if psm is not None
                       else cfg.get("tess_psm") or "3")
        self.oem = int(oem if oem is not None
                       else cfg.get("tess_oem") or "3")
        self.config = config or cfg.get("tess_config") or ""
        self.tessdata_dir = tessdata_dir or cfg.get("tessdata_dir") or None
        self.tess_cmd = tess_cmd or cfg.get("tess_cmd") or None
        self.min_line_fontsize = float(
            min_line_fontsize if min_line_fontsize is not None else 0.0)

        if self.tessdata_dir:
            os.environ.setdefault("TESSDATA_PREFIX", self.tessdata_dir)
        if self.tess_cmd:
            try:
                import pytesseract
                pytesseract.pytesseract.tesseract_cmd = self.tess_cmd
            except Exception:  # noqa: BLE001
                log.warning("tess_cmd set but pytesseract not importable")

    # -- deps ---------------------------------------------------------------
    @staticmethod
    def _require_pytesseract():
        try:
            import pytesseract
            return pytesseract
        except ImportError as exc:  # pragma: no cover
            raise UnavailableError(
                "pytesseract is not installed. Run: pip install pytesseract Pillow"
            ) from exc

    def _tess_lang_argv(self) -> List[str]:
        # Ensure the binary actually ships the requested language data; Tesseract
        # silently ignores unknown langs, so we surface a clear error instead.
        langs = [l.strip() for l in self.lang.replace("+", " ").split()
                 if l.strip()]
        if not langs:
            langs = ["eng"]
        return langs

    def recognize_pixels(self, image_path: str, width: int, height: int,
                         page_index: int) -> OcrPage:
        pytesseract = self._require_pytesseract()
        try:
            img = Image.open(image_path)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Cannot open page image {image_path}: {exc}") from exc

        extra = self.config or ""
        # Only add psm/oem flags when the user-level config doesn't already set
        # them (so an advanced `config` string can override the structured knob).
        if "--psm" not in extra:
            extra = f"--psm {self.psm} " + extra
        if "--oem" not in extra:
            extra = f"--oem {self.oem} " + extra
        extra = extra.strip()

        try:
            data = pytesseract.image_to_data(
                img,
                lang=self._tess_lang_key(),
                config=extra,
                output_type=pytesseract.Output.DICT,
            )
        except pytesseract.TesseractNotFoundError as exc:  # noqa: F821
            raise UnavailableError(
                "Tesseract binary not found. Install Tesseract (e.g. "
                "`apt install tesseract-ocr` / `dnf install tesseract`) and "
                "ensure it is on PATH, or set `tess_cmd` in "
                "backend/ocr_config.toml."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            # Tesseract emits a "Error, no such lang" style message on stderr.
            if "lang" in msg.lower() or "traineddata" in msg.lower():
                raise UnavailableError(
                    f"Language data unavailable for {self.lang}. Install the "
                    f"matching langpack (e.g. tesseract-ocr-chi-sim / "
                    f"tesseract-langpack-chi_sim) or set `tessdata_dir` in "
                    f"backend/ocr_config.toml."
                ) from exc
            raise RuntimeError(f"Tesseract OCR failed: {msg}") from exc

        blocks = self._cluster_words(data)
        if not blocks:
            log.warning("page %d: OCR returned no text blocks (lang=%s)",
                        page_index, self.lang)
        return OcrPage(page_index=page_index, width=width, height=height,
                       blocks=blocks)

    def _tess_lang_key(self) -> str:
        """Return the language key that pytesseract expects (e.g. 'chi_sim+eng')."""
        return "+".join(self._tess_lang_argv())

    # -- word clustering ----------------------------------------------------
    def _cluster_words(self, data: Dict[str, List[Any]]) -> List[OcrBlock]:
        """Group Tesseract word-level TSV rows into blocks.

        Strategy: emit **one block per text line** (grouped by Tesseract's
        block/par/line numbers).  Line-level granularity is the right unit for
        this tool: the bbox precisely matches one rendered line, which makes
        invisible-text embedding (font size derived from bbox height) and
        per-line editing both clean.  Blocks are emitted in reading order.
        """
        n = len(data.get("text", []))
        lines: Dict[tuple, List[dict]] = {}
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text or int(data["conf"][i]) < 0:
                continue
            key = (int(data["block_num"][i]), int(data["par_num"][i]),
                   int(data["line_num"][i]))
            left = int(data["left"][i])
            top = int(data["top"][i])
            w = int(data["width"][i])
            h = int(data["height"][i])
            lines.setdefault(key, []).append({
                "text": text,
                "x1": left, "y1": top,
                "x2": left + w, "y2": top + h,
                "conf": float(data["conf"][i]),
                "height": h,
            })

        # Sort lines by geometric position (y then x) for stable reading order.
        sorted_lines = sorted(
            lines.values(),
            key=lambda ws: (min(w["y1"] for w in ws),
                            min(w["x1"] for w in ws)),
        )

        blocks: List[OcrBlock] = []
        for words in sorted_lines:
            blocks.append(self._make_block(words))
        return blocks

    def _make_block(self, words: List[dict]) -> OcrBlock:
        """Assemble one OcrBlock from the words of a single OCR text line.

        The stored bbox is the *tight* union of the words.  Tesseract sometimes
        emits an anomalous word in a line (a taller/dropped glyph, a stray
        punctuation mark, an over-wide box) whose height or vertical offset is
        a strong outlier vs. the rest of the line.  Such a single bad word
        would inflate the block height and make the embedded text far too big
        to fill the bbox, so we trim outliers before taking the min/max.
        """
        # Choose the words used for the bounding box: reject any word whose
        # height, top offset, or bottom offset is an extreme outlier relative
        # to the line median.  Keeps the common case (all words similar)
        # unchanged; only removes clearly anomalous words.
        import statistics

        def _trim(vals: List[float], factor: float = 3.0) -> List[float]:
            if not vals:
                return vals
            med = statistics.median(vals)
            # MAD-based robust spread (scaled to ~σ for normal data).
            mad = statistics.median([abs(v - med) for v in vals]) or 0.0
            if mad <= 0.0:
                return vals  # no spread -> nothing to trim
            lo = med - factor * 1.4826 * mad
            hi = med + factor * 1.4826 * mad
            return [v for v in vals if lo <= v <= hi]

        he = [w["height"] for w in words]
        top = [w["y1"] for w in words]
        bottom = [w["y2"] for w in words]
        he_ok = set(_trim(he))
        top_ok = set(_trim(top))
        bottom_ok = set(_trim(bottom))

        # A word counts toward the bbox only if its extent isn't an outlier
        # on any axis.  If trimming would drop *everything* (degenerate), fall
        # back to all words.
        bbox_words = [
            w for w in words
            if w["height"] in he_ok and w["y1"] in top_ok and w["y2"] in bottom_ok
        ]
        if not bbox_words:
            bbox_words = words

        x1 = min(w["x1"] for w in bbox_words)
        y1 = min(w["y1"] for w in bbox_words)
        x2 = max(w["x2"] for w in bbox_words)
        y2 = max(w["y2"] for w in bbox_words)
        # Concatenate words in reading order (already geometrically sorted).
        # For CJK text Tesseract splits into single characters with no spaces,
        # so we join without separators; for Latin scripts we keep word gaps.
        joined = "".join(w["text"] for w in words)
        if any("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"
               or "\uac00" <= ch <= "\ud7af" for ch in joined):
            text = joined
        else:
            text = " ".join(w["text"] for w in words).strip()
        conf = sum(w["conf"] for w in words) / len(words)
        kind = self._classify(text, y2 - y1, bbox_words)
        return OcrBlock(
            kind=kind,
            bbox=[x1, y1, x2, y2],
            text=text,
            conf=round(conf, 4),
        )

    @staticmethod
    def _classify(text: str, block_h: int, words: List[dict]) -> str:
        """Heuristic block classification: heading / equation / text."""
        stripped = text.strip()
        if not stripped:
            return "text"

        # Equation heuristic: dense math symbols / operators.
        math_count = sum(1 for ch in stripped if ch in _MATH_HINTS)
        if math_count >= 2 and math_count / max(len(stripped), 1) > 0.15:
            return "equation"

        # Heading heuristic: short line(s), matches numbering/localizable title
        # patterns, or is significantly taller/centred than body text.
        heading_re = _build_heading_re()
        if len(stripped) <= 40 and heading_re.match(stripped):
            return "heading"
        # Large vertical extent relative to neighbours hints at a title.
        if words:
            avg_h = sum(w["height"] for w in words) / len(words)
        else:
            avg_h = block_h
        if avg_h > 40 and len(stripped) <= 30 and "，。；：" not in stripped:
            return "heading"

        return "text"

    def close(self) -> None:
        # Nothing to release (pytesseract spawns a short-lived subprocess).
        pass

    def cache_fingerprint(self) -> dict:
        """Output-affecting settings: language + segmentation/engine flags."""
        return {
            "engine": self.name,
            "lang": self.lang,
            "psm": self.psm,
            "oem": self.oem,
            "config": self.config,
            "tessdata_dir": self.tessdata_dir or "",
            "tess_cmd": self.tess_cmd or "",
        }