# Why the unlimited-ocr batcher degrades from full batches to single-page dispatches

Research worktree: `research/batcher-dispatch` (branch `research/batcher-dispatch`,
based on `origin/main` @ `085d374`). No production code was changed.

> Location note: at `origin/main` (`085d374`) the batcher lives at
> `backend/ocrmypad/batching.py`. The repo's main tree is mid-refactor to a
> standalone plugin and the LIVE `:8000` server runs
> `ocrmypdf_unlimited/batching.py` + `ocrmypdf_unlimited/engine.py` — verified
> byte-for-byte the same scheduling logic (same `_pending_page_indices` glob,
> same `is_expired`/`_flush_due`, same `max_wait = read_timeout + 300`,
> `READ_TIMEOUT_MIN=900`, `READ_TIMEOUT_PER_TOKEN=0.08`, 3 s window default).
> All findings below apply to the current/live code.

## TL;DR

The batcher forms a "full" batch only when **≥ `ocr_batch_size` pages arrive
at the engine within `ocr_batch_timeout` (3 s) of each other**. That only
happens while ocrmypdf's worker threads are rasterizing in **lockstep**. At
run start every worker starts at the same instant, so the first ~7 batches are
full — then rasterization durations diverge, workers get absorbed into
long-running windows, and any page whose batch section is lost guts the
staging pool for 15-40 min. Once arrival gaps exceed the 3 s window, every
window flushes partial — and because single-page responses release workers one
at a time, the sparse-arrival regime is self-sustaining: **the rest of the run
dispatches single/partial batches**. Verified against a real 224-page live run.

## Live evidence (job `work/d73ec48df13a`, 224-page book, `ocr_batch_size=6`, 16 workers)

Reconstructed per-page hOCR write times (a batch response writes all its pages'
hOCR within ~1 s, so same-second writes == one batch landing):

* 224 pages rasterized, 223 hOCR written (page 187 never completed; job stuck).
* Of 223 pages, **96 (43 %) were written as single-page clusters**; only
  **9 (24 %) landed in a true 6-page batch**.
* The 9 full batches are almost all in the first 59 pages
  (`[6-11],[12-17],[18-23],[24-29],[34-39],[48-53],[54-59]`), i.e. the run-start
  lockstep phase. Later only two lucky ones (`[112-117]`, `[149-154]`).
* The very first ~24 pages were all rasterized within **one second**
  (17:28:51–52) — the startup burst that makes the early batches full.
* Several pages were stuck 15–75 min and finished late via single-page fallback
  (p1-4 → +17 min, p5 → +68 min, p64-69 → +15-45 min, p89 → +37 min,
  p186-191 → +31-34 min, p187 never). Their workers were absent from staging
  for the whole stall.
* Overall throughput stayed ~5-6 pages/min the whole run — only **batching
  quality** collapsed, not speed.

## Where the single dispatches come from (code)

`backend/ocrmypad/batching.py` (unchanged; commit 085d374 added the logging in
`_BatchWindow.add`/`_dispatch` that makes this observable).

A window dispatches with fewer than `batch_size` pages through three paths:

1. **`_flush_due` "no pages pending outside" race (batching.py `_register` →
   `_flush_due`, lines 163-170).** On every arrival the batcher asks
   `pending_pages` (the engine's `_pending_page_indices`,
   `unlimited_engine.py` lines 167-182): pages whose `*_rasterize*.png` is on
   disk AND whose hOCR is not yet written. Pages whose worker is **still
   rasterizing have no PNG yet, so they are invisible to this scan.** When the
   arriving page finds `pending - staged == {}` it elects **itself** as leader
   and dispatches a **1-page batch**. Fires hardest right after a batch lands:
   all its workers are released together, everyone re-rasterizes at once, and
   the first one to finish sees an empty staging pool.

2. **The 3 s window timeout (`window.is_expired()`, default from
   `ocr_batch_timeout_ms=3000`).** A partially-filled window flushes whatever
   it has at `timeout + 0.1 s`. Real rasterization takes seconds to tens of
   seconds per page (page-to-page variance is huge), so arrivals are rarely
   packed 6-within-3 s after the startup burst.

3. **Lost batch section → per-page fallback (`max_wait` + `recognize`).**
   `recognize_multi`/`split_multi_page_stream` pads a missing `<PAGE>` section
   with `""` (`parser.py` 116-141); `_batch_recognize`
   (`unlimited_engine.py` 224-246) then waits up to `max_wait =
   _batch_max_wait(...)` ≈ 983+300 s ≈ **~21 min** and falls back to a
   single-page `recognize` whose own read timeout is ~22 min. A page whose
   section was dropped (hosted endpoint flake, truncation, split misalignment)
   ties up one ocrmypdf worker for **15-40+ min** without producing any new
   arrivals.

## Why "several full batches" first, then "suddenly" singles

1. **Startup lockstep.** At t=0 all 16 workers rasterize pages 1-16
   simultaneously (live: all PNGs in the same second). The first arrivals find
   the other 15 pages' PNGs pending, windows fill to 6, dispatch — ~7 full
   batches for the first ~59 pages.

2. **Phase-lock loss.** After each batch landing the released workers re-rasterize
   their next pages, but page rasterize cost now varies (denser/complex pages),
   so completions stop landing within 3 s of each other. The
   empty-pending race (path 1) makes the *first* arrival after each landing
   dispatch solo; the 3 s timeout (path 2) flushes the rest small.

3. **Self-sustaining sparse regime.** A solo/partial response frees exactly one
   worker at a time, so arrival gaps stay > 3 s, so the next window also
   flushes small. The system never re-clusters. (Simulation W=8/B=6 with a
   longer per-page decode reproduces this: `[1, 6, 1, 1, 1, 1, 1, 1, 1, 4, ...]`.)

4. **Stuck pages accelerate/pin the collapse.** Each dropped section removes a
   worker from staging for ~21 min (+ up to ~22 min fallback). With 16 workers,
   the live run had ~5-8 stuck simultaneously around 18:01-18:09 (pages 64-69,
   89, ...); the staging pool shrank to ~10-11, after which no batch ≥ 5 ever
   formed again (in the live run the last full batch was at 18:08:29, pages
   149-154; the remaining ~70 pages were single/partial).

## Simulation harness

`research/batcher-sim/sim_batcher.py` — faithful model of ocrmypdf's worker
model (W workers each rasterize then `generate_hocr`, shared page queue),
drives the **real** `MultiPageBatcher` and the real `_pending_page_indices`
scan, with configurable raster/spread, per-page decode latency, dropped
sections, `max_wait` and single-page fallback.

Observed regimes:

| W | B | raster σ | decode/page | dispatch sizes (selected) |
|---|---|---|---|---|
| 16 | 6 | 0.25 | 3 s | `[1,6,6,3,1,4,6,6,4,6,6,4,5,6,5,6,6,4,6,5]` — healthy-ish |
| 8 | 6 | 0.25 | 3 s | `[1,6,1,1,1,1,1,6,2,2,1,6,...]` — full batch, singles, re-cluster |
| 6 | 6 | 1.2 | 3 s | `[1,5,1,1,1,6,1,5,1,1,6,...]` — every wave leads with a solo |
| 8 | 6 | 0.25 | 6 s | `[1,6,1,1,1,1,1,1,1,4,4,4,4,4,4,4,4,2]` — collapses after the 1st batch |

The per-wave leading **solo** in every config is the `pending_pages` race
(path 1); the long runs of small windows come from the 3 s timeout (path 2).

## Notes on possible improvements (research only, not applied)

* The `pending_pages` "flush when nothing pending outside" heuristic races with
  in-progress rasterization. A scan result of "no pages pending" is only
  trustworthy after a grace period equal to max(observed rasterize time).
* The 3 s window is shorter than realistic arrival spread; increasing
  `ocr_batch_timeout_ms` (or making it scale with rasterize cost) would let
  partial windows accumulate before flushing.
* A dropped section strands a worker for ~21 min (`max_wait`) before the
  single-page fallback; reducing `max_wait` bounds the damage, and the fallback
  itself should arguably rejoin the batch path rather than go single.

## Fix applied (main tree, `ocrmypdf_unlimited/batching.py`)

The first two issues were implemented in the main tree (do NOT re-apply the
"possible improvements" blindly — they are superseded):

* **Path 1 (leading solo) removed.** A window is now flushed ONLY when it is
  full or its timeout expired. The `pending_pages` "empty outside -> flush"
  fast path is gone, because an empty scan races with pages whose workers are
  still rasterizing (no PNG on disk). The run tail simply pays one `timeout`.
* **Path 2 (partial windows) bounded-fixed.** `MultiPageBatcher` gains
  `grace` (default = `timeout`) and `max_extensions` (default 3). When the
  backstop timer fires and the window is partial but the scan shows
  rasterized-but-unstaged pages OUTSIDE it, the window is held for `grace` —
  those pages are provably about to `submit()`, so late siblings join one
  batch. The hold is bounded (a phantom pending page cannot hold forever), and
  the re-armed timer fires at `deadline + 0.1 s` (not a fixed interval) so a
  held window always flushes instead of stranding until `max_wait`.
* Path 3 (stranded workers on dropped sections / hung requests) is unchanged:
  it is rare and self-healing; see the engine fallback in
  `ocrmypdf_unlimited/engine.py::_batch_recognize`.

Validation (same sim, same configs; run
`research/batcher-sim/sim_batcher.py` from the main tree):

| config | OLD dispatch sizes | NEW dispatch sizes |
|---|---|---|
| N48 W8 B6 σ0.25 decode0.4 | `[1,6,3,6,3,6,3,6,3,5,4,2]` | `[6,6,5,6,6,6,6,6,1]` |
| N30 W6 B6 σ1.2 decode0.5 | `[1,5,1,1,1,6,1,5,1,1,6,…]` (research table) | `[6,6,5,5,5,3]` |
| N96 W16 B6 σ0.25 decode0.5 | — (live run: 43 % singles, 24 % full) | `[6,6,4,6,6,6,6,6,6,6,6,6,6,6,6,5,3]` (14×full) |

Regression tests: `tests/test_batching.py` (the old "flush on empty pending"
tests were updated to the timeout-flush semantics; new tests cover the bounded
hold, late-sibling joining, and the unit-level `_maybe_extend` rules).
