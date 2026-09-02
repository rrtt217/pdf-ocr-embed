# Research: recovering LINE-level text placement for the unlimited-ocr engine

**Status: implemented & validated (prototype) on branch `research/unlimited-block-lines`.**

## The problem (confirmed)

The unlimited model reports **one `<|det|>` marker per paragraph/block with a
single block bbox**. `backend/ocrmypad/parser.py::parse_response` splits the
marker content by newlines into `Block.lines` but keeps **no per-line
geometry**, and `_line_bboxes` / `page_store._line_bboxes` place each rendered
line on an **equal-height vertical slice** of the block bbox. For irregular
layouts (varied line heights, headings, display math, short last lines) the
invisible text layer is misaligned with the printed lines; when the model
collapses a whole paragraph into one text line, the entire paragraph is
rendered as ONE line stretched across the block (font size derived from the
full block height).

### Live-model confirmation (page15.pdf, real API call)

On a real 300-dpi rasterized page the model emitted 19 blocks; **8 of them
carry one text line but a bbox spanning 2–3 printed lines**. The old pipeline
rendered e.g. paragraph `[241,429,2223,671]` as ONE `ocr_line` **250 px tall**
containing the whole paragraph; the image shows 3 real printed rows at
y 438–497 / 523–581 / 615–666.

## Findings

1. **No per-line geometry exists in the raw stream.** `engine_client.py` sends
   the standard `"document parsing."` prompt (`SINGLE_PROMPT`); the model emits
   whatever marker format it wants. Newlines occur in content only for some
   constructs (on this page, only `\[ … \]` display-math wrappers, which
   `text_norm` already collapses). So per-line bboxes **must be re-derived** —
   they are not recoverable from the marker text.
2. **The page image is available at the right seam.** `generate_hocr` receives
   the rasterized page (`input_file`), so post-processing has full image access
   with zero extra rasterization.
3. **Projection-profile segmentation works extremely well here.** Otsu
   binarization → row-ink profile → text-band scan recovered **every** printed
   line row of page15 with ~0 error (all 19 blocks' bands matched the
   full-page ground-truth profile; folded paragraphs recovered exactly).
4. **hOCR tolerates overlapping/uneven line bboxes.** `ocrmypdf.hocrtransform`
   (`hocr_parser.py`) parses each `ocr_line` independently (no overlap checks),
   and the fpdf2 renderer (`fpdf_renderer/renderer.py`) derives each line's
   baseline & font size **solely from that line's own bbox** — so accurate
   per-line bboxes directly fix both vertical placement and per-line font
   size. Verified end-to-end by rendering the new hOCR to PDF and extracting
   text: 28 lines placed on their true baselines.
5. **Two sub-cases need two mechanisms**:
   - **Text already newline-split** (`Block.lines` > 1): align each line to an
     image band — the fix the bug report asked for.
   - **Paragraph collapsed to one text line** (the dominant case on page15):
     no per-line text exists; distribute the single text stream across the
     detected bands **proportionally to each band's ink width** (reconstruction
     is exact; seams snap to spaces). This recovers per-line granularity +
     placement for the worst-affected real-world case.

## Recommended approach (implemented)

**Keep block-level editor data; emit accurate per-line hOCR.** The block
sidecar JSON (what the WebUI edits, `models.OcrBlock` = one bbox per block)
is **unchanged** — no frontend/editor/model changes, no data-model risk. The
per-line bboxes only feed the **hOCR emission path**, i.e. the invisible text
layer (search/select/copy) — exactly where "embedding result" lives.

Ranked options:

1. **(Implemented) Image-based per-line bbox recovery inside the plugin hOCR
   path** — pure-Python + PIL (already a dependency), CPU cost ~0.3 s/page on
   the real page, hard fallbacks to today's equal slices. Lowest risk.
2. (Not done) **True line-level blocks in the editor sidecar + frontend** —
   bigger win for the editor UX (per-line editing/resizing) but touches
   `models.py`, `page_store.py`, `frontend/app.js` block rendering & edit
   round-trips. Recommended as a follow-up, **reusing this splitter**; keep the
   `line_bboxes` field optional so Tesseract-derived sidecars are unaffected.
3. (Considered, not done) **Prompt change to request per-line markers** — the
   robust *model-side* fix for the collapsed-paragraph case (perfect per-line
   text), but it is not post-processing, depends on the model complying, may
   raise token usage ~2–3×, and needs live evaluation. Keep as an option.
4. (Considered, rejected for this slice) **hOCR-only fallback ordering by
   `page_store`** — applying the splitter to post-edit regenerated hOCR too
   would need page rasterization from `origin.pdf` there; flagged as a
   follow-up (see Risks).

## What changed (worktree only)

- **`backend/ocrmypad/line_split.py`** (new, pure):
  - `split_block_into_lines(image, bbox, n_lines) -> list[[x1,y1,x2,y2]]` —
    Otsu-binarized row-ink profile → text bands → reconcile band count to
    `n_lines` (merge over-splits at smallest gap; split under-splits at the
    deepest valley) → per-band x-extent. Falls back to equal slices whenever
    the image can't support the split.
  - `split_block_text_across_bands(image, bbox, text) ->
    list[(chunk, bbox)] | None` — conservative proportional text→band
    distribution for collapsed paragraphs (gated by a "text-like bands" check:
    uniform height, sane ink density — rejects rules/graphics); `None` = keep
    single line.
- **`backend/ocrmypad/parser.py`** — `blocks_to_hocr(..., per_line_overrides=
  {block_index: [(text, bbox), …]})` (backward compatible). Overrides replace
  the derived lines+equal-slice bboxes per block; absent → unchanged behavior.
- **`backend/ocrmypad/unlimited_engine.py`** — `_per_line_overrides(input_file,
  page)` builds the overrides from the page image inside `generate_hocr`
  (both sub-cases above); failures degrade to old behavior, never raise.
- **`tests/test_line_split.py`** (new, 11 tests) — synthetic PIL images with
  text at KNOWN uneven rows: band tracking, over/under-segmentation
  reconciliation, blank-image fallback (equal slices), rule/graphic rejection,
  text reconstruction, and engine-wiring tests through `blocks_to_hocr`.
- **`RESEARCH_block_lines.md`** — this report.

## Validation

- `pytest`: **157 passed** (146 pre-existing + 11 new), no regressions.
- Real page15.pdf end-to-end (live model → parser → splitter → hOCR →
  ocrmypdf fpdf2 render → text extraction):
  - 19 → **28** `ocr_line`s; folded paragraphs now split exactly onto the
    image's printed bands (e.g. `(429,671)` → `(438,497) (523,581) (615,666)`),
    extracted baselines land on the true rows; all lines render (no
    aspect-ratio suppression), copy order preserved, `scan_res` intact.
  - Splitter cost ~0.27 s for all blocks on one page.

## Risks / open items

- **Proportional text distribution is approximate horizontally** (chunks
  stretched to their band width by the renderer's Tz). Vertical placement is
  exact; seams may be slightly off for proportional-width-lossy content
  (e.g. Latin vs CJK density). Verified acceptable; a prompt-side per-line
  text change (option 3) would remove this entirely.
- **Block bboxes that leak into adjacent blocks** can pull foreign printed
  rows into the split; text-likeness gate + line-count reconciliation limit the
  damage, and content/order is never corrupted.
- **Post-edit regenerated hOCR** still uses equal slices (edit phase goes
  through `page_store.blocks_to_hocr`). Follow-up: persist optional
  `line_bboxes` in the sidecar or rasterize `origin.pdf` there.
- **Per-line x-extents** tighten selection, but the renderer assumes each line
  starts at its bbox's left; indented lines are handled (x-extent starts at
  the indent).
- Performance is pure-Python (~1–2 s/page worst case for dense pages);
  acceptable next to multi-minute API calls, ensure not to regress
  expectations on very large blocks (bands capped at `max_lines=6` for the
  text-across-bands path).
