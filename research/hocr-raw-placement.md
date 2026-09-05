# Research: where should "raw" (pre-normalization) content ride inside hOCR?

> Question: if the OCR phase stops normalizing text at parse time (so blocks
> carry the engine's raw output) and normalization moves into a separate
> OCRmyPDF plugin / the embed path, where can the hOCR carry the raw content so
> that (a) OCRmyPDF's hOCR→PDF renderer ignores it and (b) our tooling can read
> it back?

Spec: [hOCR 1.2](https://kba.github.io/hocr-spec/1.2/)
Verified empirically against **ocrmypdf 17.11** with a real page
(`work/b9d6a4a4f794`, page 45) end-to-end through `_hocr_to_ocr_pdf`.

## Recommendation

Attach the raw content as an **engine property on the `ocr_par` title** —
one `ocr_par` = one block in this codebase:

```html
<p class="ocr_par" title="bbox 651 3080 2147 3432; x_kind equation; x_raw Tl9rID0gXHN1bV97az0wfV57XGluZnR5fSBLX2sgXHRpbWVzIDJear0=">
  <span class="ocr_line" title="bbox ..."><span class="ocrx_word" title="bbox ...">(N)_n ...</span></span>
</p>
```

* `x_raw` — base64url(UTF-8) of the block's raw (pre-normalization) text.
* `x_kind equation|table|text|...` — the block kind. **Needed alongside raw**:
  hOCR line classes (`ocr_line`/`ocr_header`/`ocr_footer`/`ocr_caption`) lose
  the `equation`/`table` distinction the normalizer dispatches on
  (`text_norm.normalize_engine_text`), and a raw HTML table fragment must not
  be fed through the `latex_to_plain` path.
* Declare the capabilities in the head per §6.2:
  `<meta name='ocr-capabilities' content='ocrp_x_raw ocrp_x_kind ...'/>`.

## Spec evidence (hOCR 1.2)

| Spec section | What it says |
|---|---|
| §2.2.2 "property" | Property names must be from §4 **or begin with `x_` to denote implementation-specific extensions** — `x_raw`/`x_kind` are the canonical extension names. |
| §2.4 ABNF | `property-value = ( ascii-word / delimited-string ) *( whitespace ( ascii-word / delimited-string ) )`; `ascii-word` = printable ASCII **without space and semicolon**; separator between pairs is `space* ';' space*` (exactly what tesseract-style titles already use). Raw CJK/spaces/newlines must therefore be **encoded**: base64url fits `ascii-word` exactly; percent-encoding fits too (more inspectable, `whitespace = +%20` so spaces must be `%20`). |
| §2.2.3 + §6.2 | Presence of custom properties "must be explicitly stated as a capability" via the `ocr-capabilities` metadata field, property capabilities declared with the `ocrp_...` prefix. |
| §4.20 `x_source` | Spec precedent for source-referencing properties (`property-value = 1* delimited-string`, Allowed on `ocr_page`) — but its semantics are "document source pointer", not per-block content; abusing it for raw would be wrong, hence a new `x_raw`. |
| §3.1.4 `ocr_line` | `ocr_line` allowed properties are a closed list (baseline, hardbreak, x_font, x_fsize, x_bboxes) — raw belongs at **par** (block) level anyway, matching our one-par-per-block emission in both `ocrmypdf_unlimited.parser.blocks_to_hocr` and `backend.page_store.blocks_to_hocr`. |

## OCRmyPDF 17.11 — what the embed path actually reads (verified)

`hocrtransform/hocr_parser.py` only consumes:

* element **class** (whitelist per level: `ocr_page`, `ocr_par`, line classes
  `{ocr_line, ocr_header, ocr_footer, ocr_caption, ocr_textfloat}`, `ocrx_word`);
* **title** properties via regex search for known keys only
  (`bbox`, `baseline`, `textangle`, `x_wconf`, `x_fsize`, `x_font`, `ppageno`,
  `scan_res`) — unknown keys are silently ignored;
* `dir` / `lang` attributes;
* the **full text subtree of each `ocrx_word`** (`_get_element_text`
  concatenates all descendant text + tails — the only channel that reaches the
  rendered PDF).

Empirical results (real page, injected on all 21 pars):

| Test | Result |
|---|---|
| `HocrParser.parse()` on modified hOCR | ok, 83 word texts **identical** to original |
| `backend.page_store.hocr_to_page` derivation | page dict **identical** |
| Full `_hocr_to_ocr_pdf` embed render (origin.pdf graft) | extracted PDF text **identical**, raw/base64 **absent** from the text layer |

## Alternatives considered

| Placement | Verdict |
|---|---|
| **`x_raw` title property on `ocr_par`** | ✅ recommended — spec's own extension mechanism; empirically invisible to embed |
| `data-raw="..."` XML attribute | works empirically (parser reads only class/title/dir/lang), values stay human-readable (XML-escaped only) — but not a spec extension point; fine as an internal fallback reader |
| Custom child element (`<span class="ocrx_raw">` inside the line, outside words) | empirically invisible to embed, but breaks the spec's line content model and sits one refactor away from the **`ocrx_word`-subtree hazard** (any raw inside a word's subtree leaks into the rendered PDF) |
| HTML comments (`<!-- raw: ... -->`) | ❌ `ElementTree` drops comments — not round-trip safe for any ET-based consumer |
| Custom line class (`<span class="ocrx_rawline">`) | ❌ dropped by the parser's line-class whitelist — content would vanish from the embed |
| `meta` in head / `x_source` on `ocr_page` | page-level only, no per-block granularity |

## Fit with the overall design

* Once normalization moves out of parse time, the block sidecar's `text` **is**
  the raw content; the hOCR `x_raw`/`x_kind` copy makes the hOCR
  **self-contained**, so the standalone normalize plugin can be a pure
  hOCR-in/hOCR-out postprocessor (works in any ocrmypdf workflow, not just this
  app) and tesseract-derived pages keep working unchanged.
* Consumers read it back with the same regex style ocrmypdf uses:
  `re.search(r'x_raw ([A-Za-z0-9_=-]+)', title)` + `base64.urlsafe_b64decode`.
  Producers must XML-escape the title as usual (`html.escape(..., quote=True)`
  — base64url output is already attribute-safe).
* Edit path: `blocks_to_hocr` re-emits `x_kind`/`x_raw` from the sidecar when
  regenerating a page's hOCR after edits, so the properties survive the
  WebUI's edit round-trip.
