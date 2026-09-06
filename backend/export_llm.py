"""Export pre-processing before markdown/LaTeX rendering (backend.export).

Two passes over the page dicts, BEFORE the pure builders run:

* **P0 deterministic reflow (default ON, offline)** — the line-split structure
  the sidecars carry exists for the PDF text layer, not for exports: for
  markdown/LaTeX it is an anti-pattern.  ``reflow_text`` unwraps hard-wrapped
  lines (CJK joins directly, latin joins with a space, ``word-`` + ``break``
  hyphens merge), list lines are marked so they are never joined, ragged
  tab-text tables are padded (single-column ones downgrade to text), and
  page-boundary paragraphs are merged.  Pure functions, no network.
* **P1 LLM block fix-up (default OFF — ``?llm=1`` / ``--export-llm``)** — the
  marked hard blocks (tables / equations / low-confidence / cross-page merges)
  are sent to an OpenAI-compatible endpoint in numbered ``<<<BLOCK n>>>``
  batches and must answer strict JSON.  Per-block guards (length band, token
  diff) reject hallucinations; any failure falls back to the P0 result, so
  export NEVER fails because of the LLM.

The pass is a pages->pages transform: ``preprocess`` deep-copies and rewrites
blocks in place, tagging them with an ``llm`` field — ``backend.export``'s
builders stay untouched and pick the new content up through ``block_source``
(``llm > raw > text``).  The ``llm`` tag lives only in this copy: sidecars,
hOCR and the embedded text layer are never touched, and
``page_store.blocks_to_hocr`` ignores unknown fields (pinned by tests).

Config keys (backend/config.py ``resolve()``; env aliases ``OCR_EXPORT_*``):
``export_reflow`` (default on), ``export_llm`` (default off),
``export_llm_model`` (falls back to ``model``), ``export_llm_threshold``,
``export_llm_batch``, ``export_llm_timeout_s``.  The provider fields
(api_key/base_url) are shared with the OCR engine; this module never imports
``ocrmypdf_unlimited`` (engine-agnostic boundary).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from backend.config import as_bool

log = logging.getLogger(__name__)

PROMPT_V = 1

# Block kinds that are page furniture: never content, never reflowed.
_FURNITURE_KINDS = {"page_number", "header", "footer", "page_footnote",
                    "image_footnote"}

# Kinds the deterministic reflow touches (paragraph-like content).
_REFLOW_KINDS = {"text", "heading"}


# --- CJK / structural line detection ------------------------------------------

_CJK_RE = re.compile(
    r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]")


def _is_cjk(ch: str) -> bool:
    """True for a CJK character (ideographs, kana, hangul, fullwidth forms)."""
    return bool(ch) and bool(_CJK_RE.match(ch))


# A "structural" line must stay on its own line: list items, table rows,
# headings, blockquotes.
_STRUCTURAL_RE = re.compile(
    r"^\s*(?:[#>|]|\d+[.)、]\s|[•●▪○‣·]\s|[-*+]\s)")

# A line that opens with a list/bullet marker (cross-page merge guard).
_LIST_START_RE = re.compile(r"^\s*(?:[-*+•●▪○]\s|\d+[.)、]\s)")

# Terminal punctuation: a block ending with one is a finished paragraph.
_TERMINAL_RE = re.compile(r"[。．.!？?;；:：」』\"'\)）》\]]$")


def _join_two(left: str, right: str) -> str:
    """Join two lines per export typography (pure).

    ``word-`` + ``break`` merges (latin lowercase or CJK continuation), CJK
    joins directly (CJK typography has no inter-word space), anything else
    joins with a single space.
    """
    if not left:
        return right
    if not right:
        return left
    if (left.endswith("-")
            and (_is_cjk(right[0])
                 or (right[0].isascii() and right[0].islower()))):
        return left[:-1] + right
    if _is_cjk(left[-1]) or _is_cjk(right[0]):
        return left + right
    return left + " " + right


def unwrap_lines(text: str) -> str:
    """Unwrap hard-wrapped lines into paragraphs (pure).

    Blank-line-separated paragraphs are preserved; inside a paragraph the
    hard wraps are joined (see ``_join_two``).  Structural lines (list items,
    table rows, headings, quotes) and labels ending with a colon start a new
    line instead of being joined.  The line-split structure the sidecar
    carries exists for the PDF text layer; for markdown/LaTeX export it is
    noise.
    """
    if not text or "\n" not in text:
        return text
    paras: List[str] = []
    for para in re.split(r"\n[ \t]*\n+", text.strip()):
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        # joined lines merge into one string; a structural break (list item,
        # table row, heading, label line) keeps its own single newline
        para_out: List[str] = []
        cur = lines[0]
        for nxt in lines[1:]:
            if (_STRUCTURAL_RE.match(cur) or _STRUCTURAL_RE.match(nxt)
                    or cur.endswith(":") or cur.endswith("：")):
                para_out.append(cur)
                cur = nxt
                continue
            cur = _join_two(cur, nxt)
        para_out.append(cur)
        paras.append("\n".join(para_out))
    # paragraph breaks stay blank lines (markdown needs them; a single
    # newline is a soft break and would visually merge paragraphs)
    return "\n\n".join(paras)


def reflow_text(text: str) -> str:
    """The export text for a paragraph-like block (pure)."""
    return unwrap_lines(text)


def is_list_item(text: str) -> bool:
    """True when a block's first line opens with a list/bullet marker."""
    return bool(_LIST_START_RE.match((text or "").strip()))


def mark_list_items(blocks: List[dict]) -> int:
    """Tag list blocks with ``llm_list`` so the reflow never joins them and
    the LLM pass knows their role (informational; the builders read it not).
    Returns the tagged count."""
    n = 0
    for block in blocks:
        kind = str(block.get("kind") or "text")
        if kind not in _REFLOW_KINDS:
            continue
        if is_list_item(block.get("text") or ""):
            block["llm_list"] = True
            n += 1
    return n


# --- table reflow (tab-text tables without raw HTML) ---------------------------

def _tab_rows(text: str) -> List[List[str]]:
    """Parse the normalized tab-separated table text into cell rows."""
    rows: List[List[str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.split("\t")]
        if any(cells):
            rows.append(cells)
    return rows


def reflow_table_rows(rows: List[List[str]]) -> Optional[List[List[str]]]:
    """Pad ragged rows to the widest row (pure).

    ``None`` when the rows are not a real table: empty, or a single column
    (one value per line — a paragraph the engine mis-tagged as a table, which
    exports better as text).
    """
    if not rows:
        return None
    width = max(len(r) for r in rows)
    if width <= 1:
        return None
    return [r + [""] * (width - len(r)) for r in rows]


def reflow_table_block(block: dict) -> bool:
    """Fix a tab-text table block in place (no raw HTML present).  Returns
    True when the block was rewritten (padded) or downgraded to text."""
    raw = (block.get("raw") or "").strip()
    if "<t" in raw.lower() or "<tr" in raw.lower():
        return False  # raw HTML: the builders parse it directly; untouched
    text = (block.get("text") or "").strip()
    rows = reflow_table_rows(_tab_rows(text))
    if rows is None:
        if text and block.get("kind") == "table":
            # single-column "table": export as a paragraph instead
            block["kind"] = "text"
            return True
        return False
    new_text = "\n".join("\t".join(r) for r in rows)
    if new_text != text:
        block["text"] = new_text
        block["lines"] = [ln for ln in new_text.split("\n") if ln.strip()]
        return True
    return False


# --- cross-page paragraph merge -------------------------------------------------

def _is_content(block: dict) -> bool:
    """True when a block carries exportable content (mirrors backend.export)."""
    kind = str(block.get("kind") or "text")
    if kind in _FURNITURE_KINDS:
        return False
    if kind in ("image", "image_ref"):
        return bool((block.get("text") or "").strip()
                    or (block.get("caption") or "").strip())
    return True


def _last_content_block(page: dict) -> Optional[dict]:
    blocks = [b for b in (page.get("blocks") or []) if _is_content(b)]
    return blocks[-1] if blocks else None


def _first_content_block(page: dict) -> Optional[dict]:
    blocks = [b for b in (page.get("blocks") or []) if _is_content(b)]
    return blocks[0] if blocks else None


def _mergeable(tail_text: str, head_text: str) -> bool:
    """True when a page-tail text block and the next page's first text block
    are one paragraph split across the page boundary (pure heuristic)."""
    if not tail_text or not head_text:
        return False
    if _TERMINAL_RE.search(tail_text):
        return False
    if _STRUCTURAL_RE.match(tail_text) or _LIST_START_RE.match(head_text):
        return False
    first = head_text[0]
    return _is_cjk(first) or (first.isascii() and first.islower())


def merge_cross_page(pages: List[dict]) -> int:
    """Merge page-boundary paragraphs in place (P0).  The tail block absorbs
    the next page's first block (``raw`` dropped: the reflowed ``text`` is the
    export artifact); the emptied trailing block is filtered out by the
    builders.  The merged block gets an ``llm`` tag with ``merged_from``.
    Returns the merge count."""
    merged = 0
    for i in range(len(pages) - 1):
        last = _last_content_block(pages[i])
        first = _first_content_block(pages[i + 1])
        if last is None or first is None or last is first:
            continue
        if (last.get("kind") != "text" or first.get("kind") != "text"
                or (last.get("llm") or {}).get("merged_from")):
            continue
        tail = (last.get("text") or "").strip()
        head = (first.get("text") or "").strip()
        if not _mergeable(tail, head):
            continue
        new_text = _join_two(tail, head)
        last["text"] = new_text
        last.pop("raw", None)
        last["lines"] = [ln for ln in new_text.split("\n") if ln.strip()]
        meta = last.setdefault("llm", {})
        meta.update({"text": new_text, "model": "reflow", "prompt_v": 0})
        next_index = pages[i + 1].get("page_index")
        meta.setdefault("merged_from", []).append(
            int(next_index) + 1 if next_index is not None else i + 1)
        # empty the absorbed block (stays a content kind with empty text —
        # the builders filter it out)
        first["text"] = ""
        first.pop("raw", None)
        first["lines"] = []
        merged += 1
    return merged


# --- LLM block fix-up (P1, opt-in) ------------------------------------------------

_SYSTEM_PROMPT = (
    "You repair OCR-export text blocks. You receive numbered blocks of "
    "recognized text (ground truth) and return strict JSON only.")

# Per-kind guard bands: (length lo, length hi), max dropped-token fraction,
# max added-token fraction (None = unchecked; equations legitimately gain
# LaTeX command tokens, so their drop check is the real net).
_LEN_BAND = {"text": (0.5, 2.0), "heading": (0.5, 2.0),
             "table": (0.4, 5.0), "equation": (0.3, 8.0)}
_DROP_MAX = {"text": 0.1, "heading": 0.1, "table": 0.1, "equation": 0.05}
_ADD_MAX = {"text": 0.2, "heading": 0.1, "table": 0.5, "equation": None}

_TOKEN_RE = re.compile(r"[0-9]+|[A-Za-z]+")


def guard_output(kind: str, source: str, out: str) -> bool:
    """Per-block sanity guard for an LLM rewrite (pure).

    Rejects empty output, rewrites outside the kind's length band, and
    rewrites that drop or invent too many alphanumeric tokens (the
    anti-hallucination net).
    """
    if not out or not out.strip():
        return False
    lo, hi = _LEN_BAND.get(kind, (0.5, 2.0))
    ratio = len(out) / max(1, len(source))
    if not (lo <= ratio <= hi):
        return False
    src_toks = set(_TOKEN_RE.findall(source))
    out_toks = set(_TOKEN_RE.findall(out))
    if src_toks:
        dropped = len(src_toks - out_toks) / len(src_toks)
        if dropped > _DROP_MAX.get(kind, 0.1):
            return False
        add_max = _ADD_MAX.get(kind, 0.2)
        if add_max is not None:
            added = len(out_toks - src_toks) / len(src_toks)
            if added > add_max:
                return False
    return True


def _user_prompt(items: List[dict], fmt: str) -> str:
    """The numbered-block user prompt for one batch."""
    target = "GitHub-flavored markdown" if fmt == "markdown" else "LaTeX"
    table_target = ("markdown pipe tables" if fmt == "markdown"
                    else "LaTeX tabular environments")
    lines = [
        f"Target format: {target}.",
        "Rules:",
        f"- Rebuild tables as {table_target}, preserving every cell value "
        "and the row/column order.",
        "- Equations: output clean LaTeX math source.",
        "- Merge broken words and hard-wrapped lines; fix obvious OCR "
        "spacing inside math.",
        "- Keep every fact, number, name and language EXACTLY as given. "
        "Never add, drop or translate content.",
        "- If a block is not really a table/equation, or you are unsure, "
        "set ok=false for it.",
        "",
        "Blocks:",
    ]
    for it in items:
        lines.append(f"<<<BLOCK {it['n']}>>> kind={it['kind']}")
        lines.append(it["text"])
    lines.append("")
    lines.append('Respond with JSON only: {"blocks":[{"n":0,"ok":true,'
                 '"text":"..."},{"n":1,"ok":false,"reason":"..."}]}')
    return "\n".join(lines)


def parse_blocks_response(raw: str) -> Optional[List[dict]]:
    """Parse the model's JSON answer into item dicts (never raises)."""
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    items = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [it for it in items if isinstance(it, dict)]


class ExportLlmClient:
    """One OpenAI-compatible client for export fix-up (httpx, tiny retry).

    Deliberately independent of ``ocrmypdf_unlimited`` (engine-agnostic
    boundary): the shared provider config reaches it through
    ``backend.config.resolve()``.
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_s: float = 120.0, max_retries: int = 2,
                 retry_base_delay: float = 1.0,
                 retry_max_delay: float = 15.0):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout_s = float(timeout_s)
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = float(retry_base_delay)
        self.retry_max_delay = float(retry_max_delay)

    @classmethod
    def from_config(cls, cfg: Dict[str, Any], model: str) -> "ExportLlmClient":
        """Build from a resolved config dict (``backend.config.resolve()``)."""
        return cls(
            base_url=str(cfg.get("base_url") or ""),
            api_key=str(cfg.get("api_key") or ""),
            model=model,
            timeout_s=float(cfg.get("export_llm_timeout_s") or 120.0),
            max_retries=int(cfg.get("max_retries") or 2),
            retry_base_delay=float(cfg.get("retry_base_delay") or 1.0),
            retry_max_delay=float(cfg.get("retry_max_delay") or 15.0),
        )

    def chat_json(self, system: str, user: str,
                  max_tokens: int) -> Optional[str]:
        """One chat-completions call; returns the message content or ``None``
        on any failure (transport error, 5xx after retries, bad shape)."""
        if not self.api_key or not self.base_url:
            return None
        payload = {
            "model": self.model,
            "max_tokens": max(1, min(int(max_tokens), 32767)),
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"
        delay = self.retry_base_delay
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=httpx.Timeout(
                        self.timeout_s, connect=30.0)) as client:
                    resp = client.post(url, json=payload, headers=headers)
                if (resp.status_code in (429, 500, 502, 503, 504)
                        and attempt < self.max_retries):
                    time.sleep(min(delay, self.retry_max_delay))
                    delay = min(delay * 2, self.retry_max_delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"] or ""
            except (httpx.HTTPError, ValueError,
                    KeyError, IndexError, TypeError):
                if attempt >= self.max_retries:
                    return None
                time.sleep(min(delay, self.retry_max_delay))
                delay = min(delay * 2, self.retry_max_delay)
        return None


# --- eligibility / cache / source text -------------------------------------------

def llm_eligible(block: dict, threshold: float) -> bool:
    """True when a block should be offered to the LLM fix-up."""
    if not _is_content(block):
        return False
    kind = str(block.get("kind") or "text")
    if kind in ("table", "equation"):
        return True
    meta = block.get("llm") or {}
    if meta.get("merged_from"):
        return True
    conf = block.get("conf")
    try:
        if conf is not None and float(conf) < threshold:
            return True
    except (TypeError, ValueError):
        pass
    return False


def cache_key(model: str, kind: str, source: str) -> str:
    """Stable cache key for one block fix-up."""
    basis = f"{model}|{PROMPT_V}|{kind}|{source}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def load_cache(path: Optional[Path]) -> Dict[str, Any]:
    """Load the per-job export LLM cache (empty on any problem)."""
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(path: Optional[Path], cache: Dict[str, Any]) -> None:
    """Persist the per-job export LLM cache (best effort)."""
    if not path or not cache:
        return
    try:
        Path(path).write_text(json.dumps(cache, ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        log.warning("failed to write export LLM cache %s", path, exc_info=True)


def block_source_text(block: dict) -> str:
    """The text offered to the LLM / reflow for one block: raw when present,
    else normalized (the pre-``llm`` content)."""
    return (block.get("raw") or "").strip() or (block.get("text") or "").strip()


def resolve_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The effective export pre-processing settings from a resolved config."""
    cfg = cfg or {}
    model = (str(cfg.get("export_llm_model") or "").strip()
             or str(cfg.get("model") or "").strip())
    try:
        threshold = float(cfg.get("export_llm_threshold") or 0.85)
    except (TypeError, ValueError):
        threshold = 0.85
    try:
        batch_size = max(1, int(cfg.get("export_llm_batch") or 8))
    except (TypeError, ValueError):
        batch_size = 8
    return {
        "reflow": as_bool(cfg.get("export_reflow", "true")),
        "llm": as_bool(cfg.get("export_llm", "false")),
        "model": model,
        "threshold": threshold,
        "batch_size": batch_size,
    }


# --- the orchestrator (pages -> pages) ----------------------------------------------

def preprocess(pages: List[dict], cfg: Optional[Dict[str, Any]] = None,
               fmt: str = "markdown", *, enable_llm: Optional[bool] = None,
               reflow: Optional[bool] = None, client: Any = None,
               cache_path: Optional[Path] = None,
               progress: Optional[Any] = None) -> List[dict]:
    """Pre-process page dicts for export (the ONLY entry point callers need).

    Deep-copies ``pages`` and applies, in order: the deterministic reflow
    (default on), the cross-page paragraph merge, then the opt-in LLM block
    fix-up.  Returns the new list; the input is never mutated.  With both
    passes off the original list is returned unchanged.

    ``enable_llm``/``reflow`` override the config when not ``None``.
    ``client`` replaces the built ``ExportLlmClient`` (tests inject fakes);
    when the LLM is enabled but no API key is configured, the LLM pass is
    skipped with a warning — export never fails because of it.

    ``progress`` (callable) receives ``{"phase": "reflow"|"llm", "done": n,
    "total": m}`` events — the LLM pass can take minutes, so callers stream a
    progress bar from them.  Callback failures never break the export.
    """
    settings = resolve_settings(cfg or {})
    if reflow is not None:
        settings["reflow"] = bool(reflow)
    if enable_llm is not None:
        settings["llm"] = bool(enable_llm)
    if not settings["reflow"] and not settings["llm"]:
        return pages

    pages = copy.deepcopy(pages)

    def notify(ev: dict) -> None:
        if progress is None:
            return
        try:
            progress(ev)
        except Exception:  # noqa: BLE001 — cosmetic, never breaks the export
            log.debug("export progress callback failed", exc_info=True)

    # P0: deterministic reflow (offline).
    if settings["reflow"]:
        for page in pages:
            blocks = page.get("blocks") or []
            for block in blocks:
                kind = str(block.get("kind") or "text")
                if kind in _REFLOW_KINDS:
                    original = (block.get("text") or "").strip()
                    new_text = reflow_text(original)
                    if new_text and new_text != original:
                        block["text"] = new_text
                        # raw carried the pre-reflow line breaks too; the
                        # reflowed text is the export artifact.
                        block["raw"] = new_text
                        block["lines"] = [ln for ln in new_text.split("\n")
                                          if ln.strip()]
                elif kind == "table":
                    reflow_table_block(block)
            mark_list_items(blocks)
        merge_cross_page(pages)
        notify({"phase": "reflow", "done": 1, "total": 1})

    # P1: LLM block fix-up (opt-in).
    if settings["llm"]:
        llm_client = client
        if llm_client is None:
            if not (cfg or {}).get("api_key"):
                log.warning("export LLM enabled but no api_key configured — "
                            "skipping the LLM pass (deterministic reflow kept)")
            else:
                llm_client = ExportLlmClient.from_config(cfg or {},
                                                         settings["model"])
        if llm_client is not None:
            _fix_with_llm(pages, settings, fmt, llm_client, cache_path,
                          progress=notify)

    return pages


def _fix_with_llm(pages: List[dict], settings: Dict[str, Any], fmt: str,
                  client: Any, cache_path: Optional[Path],
                  progress: Optional[Any] = None) -> int:
    """Run the LLM fix-up over the eligible blocks (in place).  Returns the
    fixed count.  Per-block guards reject bad rewrites; every failure keeps
    the block's pre-LLM content."""
    cache = load_cache(cache_path)
    model = settings["model"]
    batch_size = settings["batch_size"]

    eligible: List[dict] = []
    for page in pages:
        for block in (page.get("blocks") or []):
            if not block_source_text(block):
                continue
            if llm_eligible(block, settings["threshold"]):
                eligible.append(block)
    if not eligible:
        return 0
    total = len(eligible)

    def notify(done: int) -> None:
        if progress is None:
            return
        try:
            progress({"phase": "llm", "done": done, "total": total})
        except Exception:  # noqa: BLE001 — cosmetic
            pass

    fixed = 0
    notify(0)
    for start in range(0, len(eligible), batch_size):
        chunk = eligible[start:start + batch_size]
        pending: List[dict] = []
        for n, block in enumerate(chunk):
            source = block_source_text(block)
            kind = str(block.get("kind") or "text")
            key = cache_key(model, kind, source)
            hit = cache.get(key)
            if isinstance(hit, dict) and isinstance(hit.get("text"), str):
                _apply_llm_text(block, hit["text"], model)
                fixed += 1
                continue
            pending.append({"n": n, "kind": kind, "text": source,
                            "_block": block, "_key": key})
        if not pending:
            notify(fixed)
            continue
        prompt = _user_prompt(pending, fmt)
        max_tokens = max(1024, min(32767, int(
            sum(len(p["text"]) for p in pending) * 2) + 256))
        raw = client.chat_json(_SYSTEM_PROMPT, prompt, max_tokens)
        items = parse_blocks_response(raw or "")
        if not items:
            log.warning("export LLM batch returned no usable JSON (%s block(s)) "
                        "— kept as-is", len(pending))
            notify(fixed)
            continue
        by_n: Dict[int, dict] = {}
        for it in items:
            try:
                by_n[int(it.get("n"))] = it
            except (TypeError, ValueError):
                continue
        for p in pending:
            item = by_n.get(p["n"])
            if not item or not item.get("ok"):
                continue
            out = item.get("text")
            if not isinstance(out, str) or not guard_output(
                    p["kind"], p["text"], out):
                log.debug("export LLM rewrite rejected by guard (block %d)",
                          p["n"])
                continue
            _apply_llm_text(p["_block"], out, model)
            cache[p["_key"]] = {"text": out}
            fixed += 1
        notify(fixed)
    save_cache(cache_path, cache)
    return fixed


def _apply_llm_text(block: dict, text: str, model: str) -> None:
    """Write an accepted rewrite into the block's ``llm`` tag (in place)."""
    meta = block.setdefault("llm", {})
    meta.update({"text": text, "model": model, "prompt_v": PROMPT_V})
