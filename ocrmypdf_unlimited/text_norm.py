"""Safe glyph + whitespace normalization for Unlimited-OCR output.

Ported from the pre-rebuild ``unlimited_ocr_adapter`` (logic unchanged, tests
ported alongside).  These functions never merge two adjacent alphanumeric
tokens across a single space, so they are safe to run on any recognized text
that ends up in the searchable PDF layer.
"""
from __future__ import annotations

import re

_LATEX_CONV = None

# The unlimited model renders formulas as spaced-out tokens ("X _ p = f (x)").
# These rules collapse that spacing *inside* a math expression without ever
# touching normal words; they run only on equation blocks and per table cell.
# A single space between two digits is deliberately NOT merged: it may encode
# matrix/list separators that must survive.
_MATH_FIX_RULES: tuple[tuple[str, str], ...] = (
    # "X _ p"/"X_ p" -> "X_p"; "max _ 1" -> "max_1"
    (r"[ \t]*_[ \t]*", "_"),
    # "x  (y+1)" -> "x(y+1)"
    (r"([A-Za-z0-9])[ \t]+\(", r"\1("),
    # ") (" -> ")("
    (r"\)[ \t]+\(", ")("),
    # multiple consecutive tokenization spaces -> one
    (r"[ \t]{2,}", " "),
    # "≤ 5" -> "≤5"; always safe in math
    (r"([×÷±≤≥≠≈≡∼∈∉⊂⊃∪∩→←↑↓])[ \t]+", r"\1"),
    # ")x", "]x" etc. after close paren/bracket
    (r"\)[ \t]+([A-Za-z0-9\[{])", r")\1"),
)


def _get_latex_conv():
    global _LATEX_CONV
    if _LATEX_CONV is None:
        from pylatexenc.latex2text import LatexNodes2Text
        _LATEX_CONV = LatexNodes2Text()
    return _LATEX_CONV


def clean_math_spacing(text: str) -> str:
    """Collapse the tokenized spacing the model uses inside math blocks.

    Applied only to ``equation`` blocks and to each table cell, where every
    space between tokens is model padding rather than meaningful whitespace.
    """
    if not text:
        return text
    result = text.strip()
    for pattern, repl in _MATH_FIX_RULES:
        result = re.sub(pattern, repl, result)
    return result


def tidy_ocr_text(text: str) -> str:
    """Safe glyph + whitespace normalization applied to every text block.

    Never merges two adjacent alphanumeric tokens across a single space.
    """
    if not text:
        return text
    result = text
    # Tighten "| x |" → "|x|" for readability.
    result = re.sub(r"\|\s+", "|", result)
    result = re.sub(r"\s+\|", "|", result)
    # Map ⩽ (U+2A7D, \leqslant) → ≤ (U+2264) — china-s font lacks ⩽ but has ≤.
    result = result.replace("\u2a7d", "\u2264")
    result = result.replace("\u2a7e", "\u2265")  # ⩾ → ≥
    # Map ⇒ (U+21D2) → → (U+2192) — CJK fallback fonts lack double-arrow.
    result = result.replace("\u21d2", "\u2192")
    result = result.replace("\u21d0", "\u2190")  # ⇐ → ←
    result = result.replace("\u21d4", "\u2194")  # ⇔ → ↔
    # Fix "x ^ *" -> "x^*" (also applies to tokenized plain math).
    result = re.sub(r"[ \t]*\^[ \t]*", "^", result)
    # Fix "10^- 4" -> "10^-4".
    result = re.sub(r"(\^[-+])[ \t]+", r"\1", result)
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"[ \t]+\n", "\n", result)
    return result.strip()


def latex_to_plain(text: str, join_lines: bool = False) -> str:
    """Convert LaTeX math delimiters/commands to readable plain text.

    Uses pylatexenc for robust conversion when the text contains real LaTeX
    commands (e.g. ``\\frac``); otherwise the raw content is returned and put
    through the same safe tidy.  Math-spacing tightening is handled separately
    in ``clean_math_spacing``.  ``join_lines`` collapses every newline (for
    equation blocks, which are single-line); the default keeps line structure
    and only collapses blank-line runs.
    """
    if not text or "\\" not in text:
        return tidy_ocr_text(text)

    try:
        result = _get_latex_conv().latex_to_text(text)
    except Exception:
        # pylatexenc can choke on mixed plain/Latex fragments; keep the
        # recognized content rather than dropping it.
        result = text

    if join_lines:
        # Display math (\\[ ... \\]) introduces blank lines around itself, and
        # an equation is a single line: join non-blank lines.
        lines = [ln.strip() for ln in result.splitlines() if ln.strip()]
        result = " ".join(lines)
    else:
        # Keep the line structure: collapse blank-line runs only.  Joining
        # every line would let a single backslash (LaTeX escapes like \\%,
        # file paths, ...) flatten a whole paragraph's newlines.
        result = re.sub(r"\n[ \t]*\n+[ \t]*", "\n", result).strip()
    return tidy_ocr_text(result)


def table_html_to_text(content: str) -> str:
    """Turn the model's HTML table fragment into searchable plain rows.

    Each ``<tr>`` becomes one line and each ``<td>/<th>`` cell is separated
    by a tab.  Partial/non-HTML fragments simply have their tags stripped so
    no markup ever reaches the searchable text layer.
    """
    if not content:
        return ""
    import html as _html

    raw = _html.unescape(content).replace("\u00a0", " ")
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", raw, re.S | re.I)
    if not rows:
        # Partial / invalid table: drop every tag so no markup reaches the
        # searchable text layer.
        return " ".join(re.sub(r"<[^>]+>", " ", raw).split())

    lines: list[str] = []
    for row in rows:
        cells = re.findall(r"<t(?:h|d)\b[^>]*>(.*?)</t(?:h|d)>",
                           row, re.S | re.I)
        if not cells:
            cells = re.split(r"</t(?:h|d)>", row, flags=re.I)
        cell_texts: list[str] = []
        for cell in cells:
            cell = re.sub(r"<[^>]+>", " ", cell).strip()
            cell = " ".join(cell.split())
            # Math-spacing cleanup runs per cell (not per row) so the tab
            # separators between table cells survive.
            cell_texts.append(clean_math_spacing(cell))
        lines.append("\t".join(cell for cell in cell_texts if cell))
    return "\n".join(line for line in lines if line.strip()).strip()


def normalize_engine_text(kind: str, content: str) -> str:
    """Dispatch per-block-kind text normalization (single entry point)."""
    if kind == "table":
        return tidy_ocr_text(table_html_to_text(content))
    if kind == "equation":
        return clean_math_spacing(latex_to_plain(content, join_lines=True))
    return latex_to_plain(content)
