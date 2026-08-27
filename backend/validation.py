"""Post-embed validation: extract embedded text and compare it with the OCR source.

After ``embed_invisible_text`` writes the *invisible* text layer, the only way
to know whether the output PDF really is searchable / faithful is to open it
back up and read the text PyMuPDF sees.  This module re-opens the embedded PDF,
extracts per-page text, and compares it against the OCR source pages
(``backend.models.OcrPage``) that were embedded — the "can I trust this PDF?"
closed loop of feature #17.

The comparison math lives in pure functions (``normalize_text``,
``token_coverage``, ``coverage``, ``text_metrics``, ``summarize_report``...)
so it is unit-testable without any real PDF; only ``build_report`` touches
PyMuPDF, and only defensively (missing / unreadable file -> an error dict, not
a traceback).

Validation only READS artifacts: it never modifies ``pdf_processing.py`` /
``ocr_service.py`` state, nor the embedded file itself.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

from backend.models import OcrBlock, OcrPage

log = logging.getLogger(__name__)

# Pages whose best token/character overlap falls below this are flagged
# ``low_coverage`` (see ``page_report`` / ``summarize_report``).
LOW_COVERAGE_THRESHOLD = 0.6

# Block kinds that ``embed_invisible_text`` places no text for (pure images);
# excluded from the comparison source so validation mirrors what was embedded.
_SKIPPED_KINDS = frozenset({"image", "image_ref"})

# Unicode blocks treated as single-glyph "words": CJK ideographs, kana and
# hangul carry no intra-word spaces, so they are tokenized per character to
# keep the comparison meaningful for Chinese / Japanese / Korean.
_CJK_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK Ext A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x31F0, 0x31FF),  # Katakana phonetic extensions
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
)

# Anything that is not an alphanumeric (or CJK ideograph) becomes a separator.
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


# ---------------------------------------------------------------------------
# Pure comparison math
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """Lowercase, replace punctuation with spaces, collapse whitespace runs.

    Makes ``source`` and ``embedded`` comparable even when they differ in line
    wrapping, capitalization or OCR punctuation noise ("Hello,\\nworld!" and
    "hello world" both normalize to ``"hello world"``).
    """
    norm = _PUNCT_RE.sub(" ", (text or "").lower())
    return " ".join(norm.split())


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str) -> List[str]:
    """Split normalized text into alphanumeric tokens.

    Latin/numeric words stay whole; CJK ideographs / kana / hangul are split
    into single characters so space-less scripts compare meaningfully too.
    """
    tokens: List[str] = []
    for chunk in normalize_text(text).split():
        buf = ""
        for ch in chunk:
            if _is_cjk(ch):
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(ch)
            else:
                buf += ch
        if buf:
            tokens.append(buf)
    return tokens


def token_counts(text: str) -> Counter:
    return Counter(tokenize(text))


def token_coverage(source: str, embedded: str) -> float:
    """Fraction of source alnum-tokens found in the embedded text.

    Computed as a multiset (``Counter``) overlap, so reordering, re-wrapping
    and whitespace differences do not hurt.  An empty source is treated as
    fully covered (there is nothing to embed, therefore nothing to lose).
    """
    src = token_counts(source)
    total = sum(src.values())
    if total == 0:
        return 1.0
    emb = token_counts(embedded)
    matched = sum(min(count, emb[token]) for token, count in src.items())
    return matched / total


def _alnum_stream(text: str) -> str:
    """Normalized text with every space removed (pure character stream)."""
    return "".join(normalize_text(text).split())


def _bigrams(text: str) -> Counter:
    stream = _alnum_stream(text)
    return Counter(stream[i:i + 2] for i in range(len(stream) - 1))


def _bigram_coverage(source: str, embedded: str) -> float:
    """Character-consecutiveness overlap over the space-free stream.

    Complements ``token_coverage`` for text where line wrapping splits a word
    mid-character: the fragments no longer match any source token, yet every
    character is still present in order, so most bigrams match and coverage
    stays high instead of falsely collapsing.  An empty / single-char source
    falls back to exact character equality.
    """
    src = _bigrams(source)
    total = sum(src.values())
    if total == 0:
        s = _alnum_stream(source)
        e = _alnum_stream(embedded)
        if not s:  # vacuous: nothing to reconstitute
            return 1.0
        return 1.0 if s == e else 0.0
    emb = _bigrams(embedded)
    matched = sum(min(count, emb[bg]) for bg, count in src.items())
    return matched / total


def coverage(source: str, embedded: str) -> float:
    """Tolerant coverage ratio in [0, 1] combining token + bigram overlap.

    Geometric mean of the two measures: a page scores 0 when EITHER the token
    overlap or the character-consecutiveness overlap is zero (e.g. fully
    garbled or reversed text — exactly what you do NOT want to trust), 1.0 for
    a faithful re-embed, and intermediate values for partial / re-wrapped text.
    """
    return math.sqrt(token_coverage(source, embedded)
                     * _bigram_coverage(source, embedded))


def text_metrics(text: str) -> Dict[str, int]:
    """Character / word counts for one side of the comparison (display only).

    ``chars`` counts non-whitespace characters; ``words`` counts tokens (whole
    Latin words + individual CJK glyphs).
    """
    return {"chars": len(_alnum_stream(text)), "words": len(tokenize(text))}


def _unit_conf(conf: Optional[float]) -> Optional[float]:
    """Normalize a block confidence to 0..1 (adapters may report 0..100)."""
    if conf is None:
        return None
    return conf / 100.0 if conf > 1 else conf


def conf_stats(blocks: List[OcrBlock]) -> Dict[str, object]:
    """min / avg / max of block confidences (blocks with ``conf is not None``).

    Also carries ``block_count`` (all blocks on the page) and per-block
    ``buckets`` so the overall summary can aggregate bucket counts without
    re-reading the blocks.  Confidence values are normalized to 0..1.
    """
    vals = [_unit_conf(b.conf) for b in blocks if b.conf is not None]
    out: Dict[str, object] = {
        "block_count": len(blocks),
        "count": len(vals),
        "buckets": {"low": 0, "medium": 0, "high": 0},
    }
    if vals:
        out["min"] = round(min(vals), 4)
        out["max"] = round(max(vals), 4)
        out["avg"] = round(sum(vals) / len(vals), 4)
        for c in vals:
            if c < 0.6:
                out["buckets"]["low"] += 1  # type: ignore[index]
            elif c < 0.8:
                out["buckets"]["medium"] += 1  # type: ignore[index]
            else:
                out["buckets"]["high"] += 1  # type: ignore[index]
    else:
        out["min"] = None  # type: ignore[assignment]
        out["avg"] = None  # type: ignore[assignment]
        out["max"] = None  # type: ignore[assignment]
    return out


def page_source_text(page: OcrPage) -> str:
    """The text actually embedded for a page (embeddable, non-empty blocks).

    Mirrors ``pdf_processing._text_blocks_to_place``: image-only blocks place
    their caption when present (at ``caption_bbox`` / the image bbox), and
    empty-text blocks place nothing, so validation compares against exactly
    the source that was placed into the PDF.
    """
    parts = []
    for b in page.blocks:
        if b.kind in _SKIPPED_KINDS:
            if (b.caption or "").strip():
                parts.append(b.caption)
            continue
        if (b.text or "").strip():
            parts.append(b.text)
    return "\n".join(parts)


def page_report(page: OcrPage, embedded_text: str,
                threshold: float = LOW_COVERAGE_THRESHOLD) -> Dict[str, object]:
    """One page's comparison row (pure: no PDF I/O here)."""
    source_text = page_source_text(page)
    src_m = text_metrics(source_text)
    emb_m = text_metrics(embedded_text)
    cov = coverage(source_text, embedded_text)
    empty_source = src_m["chars"] == 0
    empty_embedded = emb_m["chars"] == 0
    flags = {
        "empty_source": empty_source,
        "empty_embedded": empty_embedded,
        "low_coverage": (not empty_source) and cov < threshold,
    }
    return {
        "page_index": page.page_index,
        "source_chars": src_m["chars"],
        "source_words": src_m["words"],
        "embedded_chars": emb_m["chars"],
        "embedded_words": emb_m["words"],
        "coverage": round(cov, 4),
        "conf": conf_stats(page.blocks),
        "flags": flags,
    }


def summarize_report(page_reports: List[Dict[str, object]]) -> Dict[str, object]:
    """Aggregate per-page reports into the overall summary block.

    Pages with an empty source are excluded from the coverage average (there
    is nothing to validate) but counted in ``empty_source_pages``.
    """
    total_pages = len(page_reports)
    scorable = [r for r in page_reports if not r["flags"]["empty_source"]]
    if scorable:
        avg = round(sum(float(r["coverage"]) for r in scorable) / len(scorable), 4)
    else:
        avg = 1.0
    buckets = {"low": 0, "medium": 0, "high": 0}
    total_blocks = 0
    for r in page_reports:
        conf = r.get("conf") or {}
        total_blocks += int(conf.get("block_count", 0))
        for key in buckets:
            buckets[key] += int((conf.get("buckets") or {}).get(key, 0))
    return {
        "pages": total_pages,
        "avg_coverage": avg,
        "low_coverage_pages": [r["page_index"] for r in scorable
                               if r["flags"]["low_coverage"]],
        "empty_source_pages": [r["page_index"] for r in page_reports
                               if r["flags"]["empty_source"]],
        "total_blocks": total_blocks,
        "conf_buckets": buckets,
        "threshold": LOW_COVERAGE_THRESHOLD,
    }


# ---------------------------------------------------------------------------
# PDF-backed report builder
# ---------------------------------------------------------------------------
def build_report(embedded_pdf_path: str, ocr_pages: List[OcrPage]) -> Dict[str, object]:
    """Compare the embedded PDF's extracted text against the OCR source pages.

    ``ocr_pages`` must be the SAME pages that were embedded (the caller picks
    payload pages if provided, else the stored job pages).  Returns a report
    dict on success:

      ``{"ok": True, "generated_at": ISO-8601, "threshold": 0.6,
          "summary": {...}, "pages": [page_report, ...]}``

    or ``{"ok": False, "error": "..."}`` when the file is missing or not a
    readable PDF.  Purely a reader — never modifies the embedded file or job
    state.
    """
    path = Path(embedded_pdf_path)
    if not path.exists():
        return _error_report(f"embedded PDF not found: {path.name}")
    try:
        with fitz.open(str(path)) as doc:
            by_index = {p.page_index: p for p in ocr_pages}
            reports = []
            for idx in sorted(by_index):
                page = by_index[idx]
                embedded_text = doc[idx].get_text() if 0 <= idx < doc.page_count else ""
                reports.append(page_report(page, embedded_text))
    except Exception as exc:  # noqa: BLE001
        # PyMuPDF raises for truncated/corrupt files — surface as a report with
        # an error flag instead of a 500 traceback.
        log.warning("validation: failed to read %s: %s", path.name, exc)
        return _error_report(f"could not read embedded PDF: {exc}")
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "threshold": LOW_COVERAGE_THRESHOLD,
        "summary": summarize_report(reports),
        "pages": reports,
    }


def _error_report(message: str) -> Dict[str, str]:
    return {"ok": False, "error": message}
