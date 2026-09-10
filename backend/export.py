"""Other-format export: markdown + LaTeX from the stored OCR pages.

Reads the engine-agnostic page representation (block sidecar dicts, via
``backend.page_store``) and renders whole documents:

* **markdown** — headings, paragraphs, pipe tables, display math, figures.
* **latex**    — ``\\section{}``, ``tabular``, ``equation*``, ``figure``.

Raw content (skipping the lossy normalization): blocks whose sidecar carries a
``raw`` field — written by the unlimited engine when the ``generate_raw``
option is on — are used in preference to the normalized ``text`` (the
normalization is lossy by design: math spacing collapse, LaTeX -> plain,
table HTML -> tab-separated rows).  The raw HTML of a table block renders as a
real markdown table / LaTeX ``tabular``; the raw LaTeX of an equation block
keeps ``\\frac{..}{..}`` and friends.  Blocks without ``raw`` (tesseract-derived
sidecars, old sidecars, option off) fall back to the normalized text.

LLM fix-up: blocks whose page copy carries an ``llm`` field (tagged by
``backend.export_llm.preprocess`` BEFORE the builders run — a pages->pages
transform) are used first of all: ``block_source`` prefers ``llm > raw >
text``, and a table's ``llm`` markdown pipe table is parsed by
``_md_table_to_rows``.  The builders themselves stay untouched.

All functions here are pure (string in, string out) so the suite can pin the
formats; only the loaders touch the filesystem.
"""
from __future__ import annotations

import html as _html
import re
from typing import Callable, Dict, List, Optional

# Block kinds that are page furniture, not content: skipped in exports
# (a page number or running header is noise in a markdown/LaTeX document).
_FURNITURE_KINDS = {"page_number", "header", "footer", "page_footnote",
                    "image_footnote"}

# Heading kinds → markdown levels / latex commands.
_HEADING_KINDS = {"title", "heading"}


# --- block text access --------------------------------------------------------

def block_source(block: dict, use_raw: bool = True) -> str:
    """The text to export for one block: LLM fix-up when present, else raw,
    else normalized.

    ``llm`` is the export pre-processing rewrite (backend.export_llm tags it
    on a deep copy of the pages); ``raw`` is the engine's pre-normalization
    content (the ``generate_raw`` option writes it); the normalized ``text``
    is the lossy fallback (math spacing, LaTeX -> plain, table HTML -> rows).
    """
    if use_raw:
        llm = ((block.get("llm") or {}).get("text") or "").strip()
        if llm:
            return llm
        raw = (block.get("raw") or "").strip()
        if raw:
            return raw
    return (block.get("text") or "").strip()


def _is_content(block: dict) -> bool:
    """True when a block carries exportable content (not furniture, not a
    pure image placeholder without a caption).

    An image block with a bbox is content even without a caption: it is
    potentially extractable (backend.image_export crops it from the source
    PDF).  Without an image resolver such a block still renders empty in
    markdown, so plain exports are unchanged.
    """
    kind = str(block.get("kind") or "text")
    if kind in _FURNITURE_KINDS:
        return False
    if kind == "image":
        return bool(block_source(block) or (block.get("caption") or "").strip()
                    or block.get("bbox"))
    return True


# --- heading numbering ---------------------------------------------------------

_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)[.、]?\s*")

_CJK_NUM = "一二三四五六七八九十百"


def heading_numbering_depth(text: str) -> Optional[int]:
    """The heading depth from its leading numbering pattern (pure).

    Recognizes arabic dotted numbers (``1.`` -> 1, ``1.2`` -> 2, ``1.2.3``
    -> 3), CJK chapter/section forms (``第一章`` -> 1, ``第二节`` -> 2,
    ``一、`` -> 1, ``（一）`` -> 2) and latin forms (``Part II`` /
    ``Chapter 3`` / ``Appendix A`` -> 1, roman ``I.`` -> 1).  ``None`` when
    the text carries no recognized numbering — the caller falls back to its
    own heuristic (``heading_level`` uses 2).
    """
    t = (text or "").strip()
    if not t:
        return None
    match = _NUM_RE.match(t)
    if match:
        return min(match.group(1).count(".") + 1, 4)
    if re.match(rf"^第[{_CJK_NUM}0-9]+[章篇部]", t):
        return 1
    if re.match(rf"^第[{_CJK_NUM}0-9]+[节讲]", t):
        return 2
    if re.match(rf"^[{_CJK_NUM}]+[、.．]", t):
        return 1
    if re.match(rf"^[（(][{_CJK_NUM}]+[)）]", t):
        return 2
    if re.match(r"^(?:part|chapter|appendix)\s+\S+", t, re.I):
        return 1
    if re.match(r"^[IVXLCDM]+[.、)]\s*", t):
        return 1
    return None


def heading_level(text: str) -> int:
    """Heading level (1-4) from a leading numbering pattern (pure heuristic).

    ``"1. 标题"`` -> 1 (section), ``"1.2 标题"`` -> 2 (subsection),
    ``"1.2.3 标题"`` -> 3 (subsubsection); extended forms (``第一章`` -> 1,
    ``一、`` -> 1, ``（一）`` -> 2, ``Appendix A`` -> 1) go through
    ``heading_numbering_depth``.  Unnumbered titles get 2 — they are usually
    sub-headings under the document title (the document-level sizing in
    ``backend.export_llm.assign_heading_levels`` fixes this when it runs).
    """
    depth = heading_numbering_depth(text)
    if depth is not None:
        return depth
    return 2


def block_heading_level(block: dict, text: str) -> int:
    """The export level for one heading block (pure).

    The export pre-processing assignment (``llm_level``, set by
    ``backend.export_llm.assign_heading_levels`` from the document-wide font
    sizes) wins; the per-block numbering heuristic is the fallback.
    """
    lvl = block.get("llm_level")
    if isinstance(lvl, int) and 1 <= lvl <= 6:
        return lvl
    return heading_level(text)


# --- markdown rendering ---------------------------------------------------------

def _latex_delimiters(raw: str) -> Optional[str]:
    """The math body of a raw LaTeX fragment, when it carries delimiters.

    Handles ``$...$``, ``$$...$$``, ``\\(...\\)`` and ``\\[...\\]``; ``None``
    when the text has no recognized math delimiters (it is then exported as-is
    inside the display-math fence).
    """
    for pattern in (r"^\$\$(.*)\$\$$", r"^\$(.*)\$$",
                    r"^\\\((.*)\\\)$", r"^\\\[(.*)\\\]$"):
        match = re.match(pattern, raw.strip(), re.S)
        if match:
            return match.group(1).strip()
    return None


def _math_body(raw: str, fallback: str) -> str:
    """The body to put inside a display-math fence: a raw LaTeX fragment's
    own body (already delimited) or the text as-is."""
    stripped = _latex_delimiters(raw)
    if stripped is not None:
        return stripped
    return raw.strip() or fallback


def _html_table_to_rows(raw: str) -> List[List[str]]:
    """Parse an HTML table fragment into cell rows (never raises)."""
    content = _html.unescape(raw).replace("\u00a0", " ")
    rows: List[List[str]] = []
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", content, re.S | re.I):
        cells = re.findall(r"<t(?:h|d)\b[^>]*>(.*?)</t(?:h|d)>", tr, re.S | re.I)
        if not cells:
            cells = re.split(r"</t(?:h|d)>", tr, flags=re.I)
        row = []
        for cell in cells:
            cell = re.sub(r"<[^>]+>", " ", cell)
            cell = _html.unescape(cell)
            row.append(" ".join(cell.split()))
        rows.append(row)
    return rows


def _tab_text_to_rows(text: str) -> List[List[str]]:
    """Parse the normalized tab-separated table text into cell rows."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append([c.strip() for c in line.split("\t")])
    return rows


def _md_escape_cell(cell: str) -> str:
    """Escape a cell for a markdown pipe table."""
    return cell.replace("|", "\\|").replace("\n", " ")


def _md_table_to_rows(text: str) -> List[List[str]]:
    """Parse a markdown pipe table into cell rows (never raises).

    The export LLM fix-up writes tables as pipe tables into the block's
    ``llm`` field; this reads them back.  The ``---`` separator row is
    dropped; ``\\|`` unescapes to ``|``.
    """
    rows: List[List[str]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            if rows:
                break  # table ended
            continue
        cells = re.split(r"(?<!\\)\|", stripped[1:-1])
        row = [c.replace("\\|", "|").strip() for c in cells]
        if row and set("".join(row)) <= set("-: "):
            continue  # the --- separator row
        rows.append(row)
    return rows


def _table_rows(block: dict, use_raw: bool) -> List[List[str]]:
    """Cell rows for a table block: the LLM-rebuilt pipe table when present
    (best structure), else the raw HTML (keeps the real structure), else the
    normalized tab-separated text."""
    if use_raw:
        llm = ((block.get("llm") or {}).get("text") or "").strip()
        if llm and "|" in llm:
            rows = _md_table_to_rows(llm)
            if rows:
                return rows
        raw = (block.get("raw") or "").strip()
        if "<t" in raw.lower() or "<tr" in raw.lower():
            rows = _html_table_to_rows(raw)
            if rows:
                return rows
    return _tab_text_to_rows(block_source(block, use_raw=False))


def block_to_markdown(block: dict, use_raw: bool = True,
                      page_index: Optional[int] = None,
                      block_index: Optional[int] = None,
                      image_resolver: Optional[Callable] = None) -> str:
    """Render one block as markdown (pure).

    Tables become pipe tables, equations display math, headings ``##`` (level
    from the leading numbering), images a captioned placeholder — or, when
    ``image_resolver`` returns a link for the block, a real ``![alt](url)``
    image (backend.image_export: relative path in ZIP mode, data URI in
    base64 mode); everything else a paragraph.
    """
    kind = str(block.get("kind") or "text")
    raw = block_source(block, use_raw=use_raw)
    caption = (block.get("caption") or "").strip()

    if kind == "table":
        rows = _table_rows(block, use_raw)
        if not rows:
            return ""
        widths = [max(len(r[c]) if c < len(r) else 0 for r in rows)
                  for c in range(max(len(r) for r in rows))]
        lines = ["| " + " | ".join(
            _md_escape_cell(r[c]) if c < len(r) else ""
            for c in range(len(widths))) + " |" for r in rows]
        lines.insert(1, "| " + " | ".join("-" * w for w in widths) + " |")
        return "\n".join(lines)

    if kind == "equation":
        body = _math_body(raw, block.get("text") or "")
        return f"$$\n{body}\n$$" if body else ""

    if kind in _HEADING_KINDS:
        text = raw or (block.get("text") or "")
        return "#" * block_heading_level(block, text) + " " + text.strip()

    if kind in ("image", "image_ref"):
        url = None
        if image_resolver is not None:
            try:
                url = image_resolver(block, page_index, block_index)
            except Exception:  # noqa: BLE001 - a resolver failure is a placeholder
                url = None
        text = raw or caption
        if url:
            alt = (caption or raw or "image").strip()
            return f"![{alt}]({url})"
        if not text:
            return ""
        label = f" (page {page_index + 1})" if page_index is not None else ""
        return f"> [图] {text}{label}"

    # text / list / aside / everything else: a plain paragraph.
    return raw or (block.get("text") or "").strip()


def pages_to_markdown(pages: List[dict], use_raw: bool = True,
                      title: Optional[str] = None,
                      page_markers: bool = False,
                      image_resolver: Optional[Callable] = None) -> str:
    """Render a whole document (list of page dicts) as markdown.

    ``title`` becomes the ``#`` H1; ``page_markers`` inserts an HTML comment
    page marker between pages (off by default — comments are noise in rendered
    markdown).  ``image_resolver`` (optional) maps image blocks to the link
    the markdown carries — see ``backend.image_export.make_resolver``; blocks
    it resolves render as real ``![alt](url)`` images.
    """
    parts: List[str] = []
    if title:
        parts.append(f"# {title}")
    for page in pages:
        blocks = page.get("blocks") or []
        page_parts = []
        for block_index, block in enumerate(blocks):
            if not _is_content(block):
                continue
            md = block_to_markdown(
                block, use_raw=use_raw,
                page_index=page.get("page_index"),
                block_index=block_index,
                image_resolver=image_resolver)
            if md.strip():
                page_parts.append(md)
        if page_parts and page_markers:
            parts.append(f"<!-- page {(page.get('page_index') or 0) + 1} -->")
        parts.extend(page_parts)
    return "\n\n".join(parts).strip() + "\n"


# --- chapter splitting ------------------------------------------------------------

def split_pages_by_chapter(pages: List[dict]) -> List[dict]:
    """Split whole pages into chapter chunks (pure).

    A chapter begins at the first page whose leading block is a level-1
    heading (``llm_level``/numbering — see ``block_heading_level``).  Whole
    pages are kept (page_index and block indices stay intact, so image
    resolvers keep working); pages before the first level-1 heading form the
    front-matter chunk.  Returns ``[{"title", "pages"}, ...]`` — the caller
    renders each chunk with ``pages_to_markdown``.
    """
    chunks: List[dict] = []
    cur_title: Optional[str] = None
    cur_pages: List[dict] = []
    for page in pages:
        title = None
        for block in page.get("blocks") or []:
            kind = str(block.get("kind") or "text")
            if kind not in _HEADING_KINDS:
                continue
            text = block_source(block)
            if block_heading_level(block, text) == 1:
                title = text.strip() or (block.get("text") or "").strip()
                break
        if title is not None:
            if cur_pages:
                chunks.append({"title": cur_title, "pages": cur_pages})
            cur_title = title
            cur_pages = [page]
        else:
            cur_pages.append(page)
    if cur_pages:
        chunks.append({"title": cur_title, "pages": cur_pages})
    return chunks


def chapter_filename(index: int, title: Optional[str]) -> str:
    """A stable per-chapter markdown filename (pure).

    ``index`` is the 0-based chunk position: ``01_第1章 插值.md``,
    ``02_...``.  Path-unsafe characters are stripped; a missing title (the
    front-matter chunk) names itself ``01_front-matter.md``.
    """
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", title or "").strip()
    base = re.sub(r"\s+", " ", base).strip()[:60]
    if not base:
        base = "front-matter" if index == 0 else f"chapter-{index + 1}"
    return f"{index + 1:02d}_{base}.md"


# --- latex rendering -------------------------------------------------------------

_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def latex_escape(text: str) -> str:
    """Escape plain text for a LaTeX document (pure)."""
    return "".join(_LATEX_SPECIALS.get(ch, ch) for ch in text)


def _latex_table(rows: List[List[str]]) -> str:
    """A LaTeX ``tabular`` from cell rows (column count = widest row)."""
    n_cols = max(len(r) for r in rows)
    col_spec = "c" * n_cols
    body_lines = []
    for r in rows:
        cells = [latex_escape(r[c]) if c < len(r) else ""
                 for c in range(n_cols)]
        body_lines.append(" & ".join(cells) + r" \\")
    return ("\n\\begin{tabular}{" + col_spec + "}\n"
            + "\n".join(body_lines)
            + "\n\\end{tabular}")


def _tabular_rows(block: dict, use_raw: bool) -> List[List[str]]:
    """Cell rows for LaTeX: same source as markdown (raw HTML preferred)."""
    return _table_rows(block, use_raw)


def _latex_math_escape(text: str) -> str:
    """Escape plain text for a LaTeX MATH environment (valid in math mode)."""
    math_specials = dict(_LATEX_SPECIALS)
    math_specials["\\"] = r"\backslash "  # \textbackslash is undefined in math mode
    return "".join(math_specials.get(ch, ch) for ch in text)


def block_to_latex(block: dict, use_raw: bool = True,
                   page_index: Optional[int] = None) -> str:
    """Render one block as LaTeX (pure).

    Tables become ``tabular``, equations ``equation*`` (raw LaTeX body when
    available), headings ``\\section``/``\\subsection``/... (level from the
    leading numbering), images a captioned ``figure`` placeholder.
    """
    kind = str(block.get("kind") or "text")
    raw = block_source(block, use_raw=use_raw)
    caption = (block.get("caption") or "").strip()

    if kind == "table":
        rows = _tabular_rows(block, use_raw)
        if not rows:
            return ""
        return _latex_table(rows)

    if kind == "equation":
        # Raw content is LaTeX source: verbatim (escaping it would destroy
        # \sum & friends).  The normalized fallback is plain text: escaped,
        # math-safely (\backslash is defined in math mode; \textbackslash is not).
        if raw.strip():
            body = _math_body(raw, block.get("text") or "")
            escape = False
        else:
            body = (block.get("text") or "").strip()
            escape = True
        if not body:
            return ""
        if escape:
            body = _latex_math_escape(body)
        return ("\\begin{equation*}\n"
                + body
                + "\n\\end{equation*}")

    if kind in _HEADING_KINDS:
        text = raw or (block.get("text") or "")
        level = block_heading_level(block, text)
        command = {1: "section", 2: "subsection",
                   3: "subsubsection"}.get(level, "subsubsection")
        return f"\\{command}{{{latex_escape(text.strip())}}}"

    if kind in ("image", "image_ref"):
        text = raw or caption
        if not text:
            return ""
        return ("\\begin{figure}[htbp]\n"
                "\\centering\n"
                "% \\includegraphics[width=0.8\\textwidth]{...}\n"
                f"\\caption{{{latex_escape(text)}}}\n"
                "\\end{figure}")

    # text / list / everything else: a plain paragraph.
    return latex_escape(raw or (block.get("text") or "").strip())


def pages_to_latex(pages: List[dict], use_raw: bool = True,
                   title: Optional[str] = None) -> str:
    """Render a whole document (list of page dicts) as a LaTeX document.

    The preamble is minimal but complete (ctex for CJK, amsmath for the
    equation environments); compiling needs any UTF-8-capable LaTeX with
    ``ctex`` installed for Chinese text.
    """
    parts: List[str] = [
        "% Generated by pdf-ocr-embed (backend.export)",
        "\\documentclass[11pt]{article}",
        "\\usepackage[UTF8]{ctex}",
        "\\usepackage{amsmath}",
        "\\usepackage{graphicx}",
        "\\begin{document}",
    ]
    if title:
        parts.append(f"\\title{{{latex_escape(title)}}}\\maketitle")
    for page in pages:
        for block in page.get("blocks") or []:
            if not _is_content(block):
                continue
            tex = block_to_latex(
                block, use_raw=use_raw,
                page_index=page.get("page_index"))
            if tex.strip():
                parts.append(tex)
    parts.append("\\end{document}")
    return "\n\n".join(parts) + "\n"


# --- loaders (the only filesystem-touching part) ----------------------------------

def load_job_pages(job: dict) -> List[dict]:
    """Completed page dicts for a job, in page order (via the page store)."""
    from backend import ocr_service

    return ocr_service.get_pages(job["job_id"])


def export_document(fmt: str, pages: List[dict], title: Optional[str] = None,
                    use_raw: bool = True,
                    image_resolver: Optional[Callable] = None) -> str:
    """Render a list of page dicts as ``fmt`` (``markdown`` | ``latex``).

    ``image_resolver`` (optional) applies to markdown only — LaTeX keeps its
    captioned figure placeholders.  Raises ``ValueError`` for an unknown
    format so callers map it to a 400.
    """
    fmt = (fmt or "").strip().lower()
    if fmt in ("markdown", "md"):
        return pages_to_markdown(pages, use_raw=use_raw, title=title,
                                 image_resolver=image_resolver)
    if fmt in ("latex", "tex"):
        return pages_to_latex(pages, use_raw=use_raw, title=title)
    raise ValueError(f"unknown export format: {fmt!r} (markdown | latex)")
