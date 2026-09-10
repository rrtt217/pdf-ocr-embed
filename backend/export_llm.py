"""Export pre-processing before markdown/LaTeX rendering (backend.export).

Two passes over the page dicts, BEFORE the pure builders run:

* **P0 deterministic reflow (default ON, offline)** — the line-split structure
  the sidecars carry exists for the PDF text layer, not for exports: for
  markdown/LaTeX it is an anti-pattern.  ``reflow_text`` unwraps hard-wrapped
  lines (CJK joins directly, latin joins with a space, ``word-`` + ``break``
  hyphens merge), list lines are marked so they are never joined, ragged
  tab-text tables are padded (single-column ones downgrade to text), and
  page-boundary paragraphs are merged.  Pure functions, no network.
* **P1 LLM post-processing (default OFF — export options)** — two independent
  steps: the outline refinement (``?llm_outline=1`` — all headings plus the
  detected table of contents, correcting the hierarchy) and the block fix-up
  (``?llm_blocks=1`` — tables / equations / low-confidence blocks sent in
  numbered ``<<<BLOCK n>>>`` batches).  The legacy ``?llm=1`` enables both.
  The outline runs FIRST — it is one or two small requests while the block
  fix-up can be dozens — so the chapter hierarchy is never left sitting
  behind the block queue.  Thinking-capable models are supported: responses
  are located with a robust JSON extractor (fences, prose, think blocks), a
  call whose JSON does not parse is retried once, truncation
  (``finish_reason=length``) retries with a larger budget, and per-block /
  per-heading guards reject hallucinations; any failure falls back to the P0
  result, so export NEVER fails because of the LLM.

The pass is a pages->pages transform: ``preprocess`` deep-copies and rewrites
blocks in place, tagging them with an ``llm`` field — ``backend.export``'s
builders stay untouched and pick the new content up through ``block_source``
(``llm > raw > text``).  The ``llm`` tag lives only in this copy: sidecars,
hOCR and the embedded text layer are never touched, and
``page_store.blocks_to_hocr`` ignores unknown fields (pinned by tests).

Config keys (backend/config.py ``resolve()``; env aliases ``OCR_EXPORT_*``):
``export_reflow`` (default on), ``export_llm_blocks`` / ``export_llm_outline``
(default off; the legacy ``export_llm`` master defaults both),
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
import math
import re
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from backend.config import as_bool

log = logging.getLogger(__name__)

PROMPT_V = 1

# How many headings one outline request may carry.  A 200-heading book must
# not ride on one fragile call: a rejected chunk falls back per chunk instead
# of killing the whole outline, and each chunk stays small enough that even a
# thinking model finishes it inside the request budget.  The
# monotonic-deepening guard stays global across chunks.
_OUTLINE_CHUNK = 60
# Parse retries: real models occasionally wrap the JSON in prose or fences
# once; retry that one call before falling back to the deterministic result.
_PARSE_RETRIES = 1

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


def _extract_json_object(raw: str) -> Optional[dict]:
    """Extract a top-level JSON object from a model response (never raises).

    Real models rarely emit the bare JSON we ask for: they wrap it in ```json
    fences, lead with a newline or a sentence, or trail prose after the
    closing brace (and thinking models may prepend a think block).  Tries, in
    order: the stripped text as-is; the text minus code fences and think
    blocks; then a brace-balanced scan that returns the LAST parseable
    ``{...}`` object — the one whose closing brace is farthest right (trailing
    prose after the answer is common), tie-broken by the larger span (so the
    outer object wins over an inner one).
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    # Fast path: the whole text is the JSON.
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    # Code fences (```json … ```) and a leading/trailing think block — both
    # shapes seen from real providers (<think>… and <|think|>…<|/think|>).
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()
    cleaned = re.sub(r"<(?:think|\|?think\|)>.*?</(?:think|\|?/think\|)>",
                     "", cleaned, flags=re.S).strip()
    if cleaned and cleaned != text:
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        text = cleaned
    # Brace-balanced scan: collect every balanced ``{...}`` span, then try
    # them in order of (farthest closing brace, largest span) — trailing
    # prose after the answer is common, and for nested objects the OUTER one
    # closes last and wins over an inner one.
    spans: List[Tuple[int, int]] = []  # (end, start), end preferred
    for start in [m.start() for m in re.finditer(r"\{", text)]:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    break
                if depth == 0:
                    spans.append((i, start))
                    break
    best = None
    for end, start in sorted(spans, key=lambda p: (-p[0], p[1])):
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            continue
        if isinstance(data, dict):
            best = data
            break
    return best


def parse_blocks_response(raw: str) -> Optional[List[dict]]:
    """Parse the model's JSON answer into item dicts (never raises)."""
    data = _extract_json_object(raw)
    if not data:
        return None
    items = data.get("blocks")
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
        # Whether the gateway accepted ``response_format`` structured output
        # on this client (a 400/422 turns it off for the rest of the calls).
        self._json_mode_ok = True

    @classmethod
    def from_config(cls, cfg: Dict[str, Any], model: str) -> "ExportLlmClient":
        """Build from a resolved config dict (``backend.config.resolve()``)."""
        return cls(
            base_url=str(cfg.get("base_url") or ""),
            api_key=str(cfg.get("api_key") or ""),
            model=model,
            timeout_s=float(cfg.get("export_llm_timeout_s") or 240.0),
            max_retries=int(cfg.get("max_retries") or 2),
            retry_base_delay=float(cfg.get("retry_base_delay") or 1.0),
            retry_max_delay=float(cfg.get("retry_max_delay") or 15.0),
        )

    def chat_json(self, system: str, user: str,
                  max_tokens: int, json_mode: bool = True) -> Optional[str]:
        """One chat-completions call; returns the message content or ``None``
        on any failure (transport error, 5xx after retries, bad shape).

        Thinking-capable models are handled: their answer arrives in
        ``message.content`` (``reasoning_content`` carries the chain of
        thought), but some gateways count reasoning tokens against
        ``max_tokens``, so the answer can be cut off before the JSON closes —
        a ``finish_reason="length"`` retries with a larger budget.  A missing
        content falls back to ``reasoning_content`` as a last resort (the
        callers' per-type JSON guards reject anything that is not the
        requested shape).

        ``json_mode=True`` (default) requests **structured output**
        (``response_format: {"type": "json_object"}``): gateways that support
        it (the OpenAI-compatible standard — e.g. qwen3.8-chat on vLLM
        answers in 2–3s instead of 1–2 min of unconstrained thinking) return
        guaranteed-parseable JSON.  A gateway that rejects the field with
        400/422 has it silently dropped and the call is retried once in plain
        mode, so the client keeps working on any endpoint.
        """
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
        json_ok = json_mode and self._json_mode_ok
        if json_ok:
            payload["response_format"] = {"type": "json_object"}
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
                if (resp.status_code in (400, 422) and json_ok
                        and attempt < self.max_retries):
                    # Structured output not implemented by this gateway: drop
                    # the field and retry once in plain mode (the callers'
                    # JSON guards still catch any free-form answer).
                    log.warning("export LLM gateway rejected response_format "
                                "(%s) — retrying in plain JSON mode",
                                resp.status_code)
                    self._json_mode_ok = False
                    payload.pop("response_format", None)
                    json_ok = False
                    time.sleep(min(delay, self.retry_max_delay))
                    delay = min(delay * 2, self.retry_max_delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                message = choice.get("message") or {}
                content = (message.get("content") or "").strip()
                if not content:
                    # Last resort: some gateways return only reasoning when
                    # the answer slot is empty.
                    content = (message.get("reasoning_content") or "").strip()
                # A thinking model burning its token budget before the answer
                # finished: retry once with a bigger budget (still < 32768).
                if (choice.get("finish_reason") == "length"
                        and attempt < self.max_retries):
                    log.warning("export LLM response truncated "
                                "(finish_reason=length); retrying with a "
                                "larger token budget")
                    time.sleep(min(delay, self.retry_max_delay))
                    delay = min(delay * 2, self.retry_max_delay)
                    payload["max_tokens"] = min(
                        32767, int(payload["max_tokens"] * 1.6))
                    continue
                return content
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
    """The effective export pre-processing settings from a resolved config.

    The two LLM post-processing steps are independent: ``export_llm_blocks``
    (block fix-up) and ``export_llm_outline`` (heading refinement).  The
    legacy ``export_llm`` master (default for both) applies only where a
    granular key is absent.
    """
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
    master = as_bool(cfg.get("export_llm", "false"))
    return {
        "reflow": as_bool(cfg.get("export_reflow", "true")),
        "blocks": as_bool(cfg.get("export_llm_blocks", str(master))),
        "outline": as_bool(cfg.get("export_llm_outline", str(master))),
        "model": model,
        "threshold": threshold,
        "batch_size": batch_size,
    }


# --- document-level heading sizing (P0.5) -----------------------------------------

def _line_height(block: dict) -> float:
    """One rendered line's height for a block: bbox height / line count."""
    bbox = block.get("bbox") or [0, 0, 0, 0]
    height = max(0, float(bbox[3]) - float(bbox[1]))
    lines = [ln for ln in (block.get("lines") or [])
             if isinstance(ln, str) and ln.strip()]
    if not lines:
        lines = [ln for ln in (block.get("text") or "").split("\n") if ln.strip()]
    return height / max(1, len(lines))


def assign_heading_levels(pages: List[dict]) -> int:
    """Document-level heading sizing (pure): tag heading blocks with
    ``llm_level`` (1..4).  Returns the tagged count.

    Heading level is a DOCUMENT property — the per-block numbering heuristic
    cannot see it.  Signals, in order:

    * **numbering** (``heading_numbering_depth``: ``1.2`` / ``第一章`` /
      ``一、`` / ``（一）`` / ``Appendix A``) wins when present;
    * **font size** (bbox height per line vs the body's median line height,
      clustered into size bands) fixes UNNUMBERED headings — the biggest
      heading size is level 1, each ~15% smaller cluster one level deeper;
    * the first heading is the document title (level 1).

    Blocks without a heading kind (tesseract-derived sidecars carry
    ``kind="text"`` everywhere) are left untouched — the builders fall back
    to the per-block heuristic.
    """
    from backend.export import heading_numbering_depth

    heads: List[dict] = []
    body_heights: List[float] = []
    for page in pages:
        for block in (page.get("blocks") or []):
            kind = str(block.get("kind") or "text")
            if kind in ("title", "heading"):
                heads.append(block)
            elif kind == "text" and (block.get("text") or "").strip():
                body_heights.append(_line_height(block))
    if not heads:
        return 0

    body = statistics.median(body_heights) if body_heights else 0.0
    if body <= 0:
        body = statistics.median([_line_height(h) for h in heads])

    # Size bands: distinct heading line heights, biggest first; a cluster is
    # a group of heights within ~15% of the band's top.
    heights = sorted({_line_height(h) for h in heads}, reverse=True)
    bands: List[float] = []
    for value in heights:
        if not bands or value < bands[-1] * 0.85:
            bands.append(value)

    def size_level(block: dict) -> int:
        if not bands or body <= 0:
            return 1
        height = _line_height(block)
        for i, top in enumerate(bands):
            if height >= top * 0.85:
                return min(i + 1, 4)
        return min(len(bands), 4)

    tagged = 0
    for i, block in enumerate(heads):
        depth = heading_numbering_depth(block.get("text") or "")
        if depth is not None:
            level = depth
        else:
            level = size_level(block)
        if i == 0 and depth in (None, 1):
            level = 1  # the document title
        block["llm_level"] = int(max(1, min(level, 4)))
        tagged += 1
    return tagged


# --- TOC detection (table of contents, first pages) --------------------------------

# A TOC line: title + dot leaders + trailing page number.
_TOC_LINE_RE = re.compile(
    r"^(?P<title>.+?)[\s.．·⋅…‥⋯\u2026]{2,}\s*(?P<page>\d{1,4})\s*$")
_INDENT_RE = re.compile(r"^[\s\u3000]+")

# A real book's front matter (covers, forewords, a multi-page 目录) can span a
# dozen pages — far beyond the old 5-page window — so scan a larger region…
_TOC_MAX_PAGES = 30
# …but stop after this many consecutive pages without a TOC line once at
# least one entry was found (bounds false positives from body-text pages).
_TOC_STOP_GAP = 2


def detect_toc_entries(pages: List[dict],
                       max_pages: Optional[int] = None) -> List[dict]:
    """Parse the document's table of contents from the front matter (pure).

    A TOC line is a title followed by dot leaders and a trailing page number
    (``第一章 绪论........1``).  The entry depth comes from the numbering
    (``heading_numbering_depth``) when present, else from the leading
    indentation (~2 spaces per level).  The default scan window covers a long
    front matter (a book's 目录 often starts on page 5+); scanning stops two
    pages after the last TOC-looking page once entries were found, so body
    pages are never scanned.  Must run on the ORIGINAL line structure — the
    reflow pass joins dot-leader lines.
    """
    from backend.export import heading_numbering_depth

    entries: List[dict] = []
    scan = pages[:max(1, max_pages or _TOC_MAX_PAGES)]
    gap = 0
    for page in scan:
        before = len(entries)
        for block in (page.get("blocks") or []):
            kind = str(block.get("kind") or "text")
            if kind not in ("text", "heading", "title"):
                continue
            lines = [ln for ln in (block.get("lines") or [])
                     if isinstance(ln, str) and ln.strip()]
            if not lines:
                lines = [ln for ln in (block.get("text") or "").split("\n")
                         if ln.strip()]
            for raw_ln in lines:
                for ln in raw_ln.split("\n"):
                    match = _TOC_LINE_RE.match(ln.strip())
                    if not match:
                        continue
                    title = match.group("title").strip()
                    if not title:
                        continue
                    depth = heading_numbering_depth(title)
                    if depth is None:
                        indent = _INDENT_RE.match(ln)
                        width = len(indent.group(0)) if indent else 0
                        depth = 1 + width // 2
                    entries.append({"title": title,
                                    "level": int(max(1, min(depth, 4)))})
        if len(entries) > before:
            gap = 0
        else:
            gap += 1
            if gap >= _TOC_STOP_GAP and entries:
                break
    return entries


# --- LLM outline refinement (P1b, opt-in) --------------------------------------------

_OUTLINE_SYSTEM = (
    "You refine the heading hierarchy of an OCR-exported document. You "
    "receive the headings in document order with their current level, plus "
    "the document's table of contents when one was detected. You return "
    "strict JSON only.")


def _outline_prompt(items: List[dict], toc_entries: List[dict]) -> str:
    """The outline user prompt: numbered headings (current level + text) and
    the detected TOC as ground truth."""
    lines = ["Document headings, in document order (n|current level|text):"]
    for it in items:
        lines.append(f"{it['n']}|L{it['level']}|{it['text'][:80]}")
    if toc_entries:
        lines += [
            "",
            "The document's table of contents (first pages), in order:",
        ]
        for entry in toc_entries:
            lines.append(f"L{entry['level']}|{entry['title'][:80]}")
    ground_truth = ("the numbering and the table of contents as ground truth"
                    if toc_entries
                    else "the numbering and the document flow as ground truth")
    lines += [
        "",
        f"Correct each heading's level (1 = chapter/part, 2 = section, "
        f"3 = subsection, 4 = minor) using {ground_truth}. A level may only "
        "deepen by 1 at a time. "
        "Respond with JSON only: "
        '{"headings":[{"n":0,"level":1}]}',
    ]
    return "\n".join(lines)


def parse_outline_response(raw: str) -> Optional[List[dict]]:
    """Parse the model's outline answer into item dicts (never raises)."""
    data = _extract_json_object(raw)
    if not data:
        return None
    items = data.get("headings")
    if not isinstance(items, list):
        return None
    return [it for it in items if isinstance(it, dict)]


def _refine_outline(pages: List[dict], settings: Dict[str, Any], client: Any,
                    toc_entries: List[dict],
                    progress: Optional[Any] = None) -> int:
    """LLM outline refinement over the heading blocks (in place).  Returns
    the applied count.

    Headings are processed in ``_OUTLINE_CHUNK``-sized requests so a huge
    book (200+ headings) never rides on one fragile call: each chunk gets
    ``_PARSE_RETRIES`` attempts, and a rejected chunk keeps the deterministic
    ``llm_level`` instead of killing the whole outline.  Per-heading guards
    (level range, monotonic deepening, first heading) reject bad corrections
    and stay GLOBAL across chunk boundaries, so a chunk split can never
    introduce a deeper-by-more-than-one jump.
    """
    heads = [block for page in pages
             for block in (page.get("blocks") or [])
             if str(block.get("kind") or "text") in ("title", "heading")]
    if len(heads) < 2:
        return 0
    items = [{"n": i, "level": (block.get("llm_level") or 2),
              "text": (block.get("text") or "").strip()}
             for i, block in enumerate(heads)]
    n_chunks = math.ceil(len(items) / _OUTLINE_CHUNK)

    def notify(done: int) -> None:
        if progress is None:
            return
        try:
            progress({"phase": "outline", "done": done, "total": n_chunks})
        except Exception:  # noqa: BLE001 — cosmetic
            pass

    notify(0)
    applied = 0
    prev = 1
    for ci, start in enumerate(range(0, len(items), _OUTLINE_CHUNK)):
        chunk = items[start:start + _OUTLINE_CHUNK]
        prompt = _outline_prompt(chunk, toc_entries)
        # Generous budget: thinking models burn tokens before the answer; the
        # chat_json length-retry grows it further on truncation.
        max_tokens = max(1024, min(32767, len(prompt) * 3 + 512))
        parsed = None
        for attempt in range(_PARSE_RETRIES + 1):
            raw = client.chat_json(_OUTLINE_SYSTEM, prompt, max_tokens)
            parsed = parse_outline_response(raw or "")
            if parsed:
                break
            if attempt < _PARSE_RETRIES:
                log.warning("export LLM outline chunk %d/%d returned "
                            "unparseable JSON — retrying once",
                            ci + 1, n_chunks)
        if not parsed:
            log.warning("export LLM outline chunk %d/%d returned no usable "
                        "JSON — kept the deterministic levels for it",
                        ci + 1, n_chunks)
            notify(ci + 1)
            continue
        by_n: Dict[int, dict] = {}
        for it in parsed:
            try:
                by_n[int(it.get("n"))] = it
            except (TypeError, ValueError):
                continue
        for gi in range(start, min(start + _OUTLINE_CHUNK, len(items))):
            item = by_n.get(gi)
            if not item:
                continue
            try:
                level = int(item.get("level"))
            except (TypeError, ValueError):
                continue
            if not (1 <= level <= 5):
                continue
            if gi == 0 and level not in (1, 2):
                continue
            # Global monotonic-deepening guard (carried across chunks).
            if gi > 0 and level - prev > 1:
                continue
            heads[gi]["llm_level"] = level
            prev = level
            applied += 1
        notify(ci + 1)
    return applied


# --- the orchestrator (pages -> pages) ----------------------------------------------

def preprocess(pages: List[dict], cfg: Optional[Dict[str, Any]] = None,
               fmt: str = "markdown", *, enable_llm: Optional[bool] = None,
               enable_blocks: Optional[bool] = None,
               enable_outline: Optional[bool] = None,
               reflow: Optional[bool] = None, client: Any = None,
               cache_path: Optional[Path] = None,
               progress: Optional[Any] = None) -> List[dict]:
    """Pre-process page dicts for export (the ONLY entry point callers need).

    Deep-copies ``pages`` and applies, in order: the deterministic reflow
    (default on), the cross-page paragraph merge, then the two opt-in LLM
    post-processing steps — the block fix-up (``enable_blocks``) and the
    heading/outline refinement (``enable_outline``) — which are independent
    of each other.  Returns the new list; the input is never mutated.  With
    every pass off the original list is returned unchanged.

    ``enable_llm`` is the legacy master switch (both LLM steps); the granular
    flags win when given.  ``reflow`` overrides the config when not ``None``.
    ``client`` replaces the built ``ExportLlmClient`` (tests inject fakes);
    when an LLM step is enabled but no API key is configured, that step is
    skipped with a warning — export never fails because of it.

    ``progress`` (callable) receives ``{"phase": "reflow"|"llm"|"outline",
    "done": n, "total": m}`` events — the LLM steps can take minutes, so
    callers stream a progress bar from them.  Callback failures never break
    the export.
    """
    settings = resolve_settings(cfg or {})
    if reflow is not None:
        settings["reflow"] = bool(reflow)
    if enable_llm is not None:
        # legacy master switch: both LLM steps
        settings["blocks"] = bool(enable_llm)
        settings["outline"] = bool(enable_llm)
    if enable_blocks is not None:
        settings["blocks"] = bool(enable_blocks)
    if enable_outline is not None:
        settings["outline"] = bool(enable_outline)
    if not settings["reflow"] and not settings["blocks"] \
            and not settings["outline"]:
        return pages

    pages = copy.deepcopy(pages)

    def notify(ev: dict) -> None:
        if progress is None:
            return
        try:
            progress(ev)
        except Exception:  # noqa: BLE001 — cosmetic, never breaks the export
            log.debug("export progress callback failed", exc_info=True)

    # TOC entries: read from the ORIGINAL line structure (the reflow pass
    # joins dot-leader lines); needed by the LLM outline refinement.
    toc_entries = detect_toc_entries(pages) if settings["outline"] else []

    # P0: deterministic reflow (offline).  Heading sizing runs FIRST — it
    # reads the original per-line structure (bbox height / line count) that
    # the reflow pass is about to unwrap.
    if settings["reflow"]:
        assign_heading_levels(pages)
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

    # P1a: LLM outline refinement / P1b: LLM block fix-up — two independent
    # post-processing steps sharing one client.  The outline runs FIRST: it is
    # one or two small requests, while the block fix-up can be dozens of
    # calls (every table/equation), so the chapter hierarchy must not sit
    # behind the block queue.
    if settings["blocks"] or settings["outline"]:
        llm_client = client
        if llm_client is None:
            if not (cfg or {}).get("api_key"):
                log.warning("export LLM enabled but no api_key configured — "
                            "skipping the LLM pass (deterministic reflow kept)")
            else:
                llm_client = ExportLlmClient.from_config(cfg or {},
                                                         settings["model"])
        if llm_client is not None:
            if settings["outline"] and not settings["reflow"]:
                # Base heading levels: the reflow path already assigned them
                # pre-unwrap; here the original line structure is intact too.
                assign_heading_levels(pages)
            if settings["outline"]:
                _refine_outline(pages, settings, llm_client, toc_entries,
                                progress=notify)
            if settings["blocks"]:
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
        # Generous budget: thinking models burn tokens before the answer; the
        # chat_json length-retry grows it further on truncation.
        max_tokens = max(2048, min(32767, int(
            sum(len(p["text"]) for p in pending) * 2.5) + 512))
        items = None
        for attempt in range(_PARSE_RETRIES + 1):
            raw = client.chat_json(_SYSTEM_PROMPT, prompt, max_tokens)
            items = parse_blocks_response(raw or "")
            if items:
                break
            if attempt < _PARSE_RETRIES:
                log.warning("export LLM batch returned unparseable JSON "
                            "(%d block(s)) — retrying once", len(pending))
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
