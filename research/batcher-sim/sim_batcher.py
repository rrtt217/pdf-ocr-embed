"""Faithful simulation of the unlimited-ocr batcher under ocrmypdf's worker
model, to reproduce 'several full batches, then single-page dispatches'.

ocrmypdf's hOCR pipeline (with use_threads) runs one worker per page:
  _exec_page_hocr_sync(page):
      process_page()          # rasterize -> <n>_rasterize.png  (takes R_p)
                              # create_ocr_image() -> <n>_ocr.png (symlink when
                              # no masking; we model it as a real file)
      generate_hocr(<n>_ocr.png, <n>_ocr_hocr.hocr, ...)
          -> batcher.submit(page_idx, <n>_ocr.png)   # blocks
          -> writes hOCR on return

We use the REAL MultiPageBatcher from ocrmypdf_unlimited.batching and the REAL
pending-page scan (_pending_page_indices from unlimited_engine.py).
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # project root

from ocrmypdf_unlimited.batching import MultiPageBatcher  # noqa: E402

log = logging.getLogger("sim")


# --- replicated VERBATIM from unlimited_engine.py -----------------------------
from backend.page_store import hocr_path  # noqa: E402


def _page_index_from_name(name: str) -> int:
    try:
        return max(0, int(name.split("_", 1)[0]) - 1)
    except (ValueError, IndexError):
        return 0


def _pending_page_indices(work_dir: Path) -> set:
    work_dir = Path(work_dir)
    pending: set = set()
    for candidate in work_dir.glob("*_rasterize*.png"):
        idx = _page_index_from_name(candidate.name)
        if not hocr_path(work_dir, idx + 1).exists():
            pending.add(idx)
    return pending


# --- instrumentation ----------------------------------------------------------
events: list = []
events_lock = threading.Lock()


def record(evt: str):
    with events_lock:
        events.append((time.monotonic() - T0, evt))


# --- model knobs --------------------------------------------------------------
@dataclass
class Params:
    n_pages: int = 48
    workers: int = 8
    batch_size: int = 6
    timeout: float = 3.0
    raster_mean: float = 0.8      # seconds to rasterize one page
    raster_sigma: float = 0.25    # page-to-page spread (real: page size varies)
    slow_pages: tuple = ()        # (page_no_1based, factor) slow-page disruptions
    per_page_decode: float = 2.5  # seconds of model decode PER page in a batch
    decode_sigma: float = 0.5
    seed: int = 1
    # If True, model the API endpoint decoding pages sequentially -> a batch of
    # K pages takes ~K*per_page_decode (linear). If False, constant latency.
    batch_scales: bool = True
    drop_every: int = 0           # drop page's batch section every N-th page
    max_wait: float = 900.0       # follower wait budget before fallback
    single_decode: float = 10.0   # single-page fallback request time


def make_raster_times(p: Params) -> list:
    rng = random.Random(p.seed)
    times = []
    for n in range(p.n_pages):
        t = rng.gauss(p.raster_mean, p.raster_sigma)
        t = max(0.12, t)
        for page_no, factor in p.slow_pages:
            if n + 1 == page_no:
                t *= factor
        times.append(t)
    return times


def decode_latency(p: Params, k: int) -> float:
    rng = random.Random(p.seed * 1000 + k)
    jitter = rng.gauss(0.0, p.decode_sigma)
    base = p.per_page_decode * k if p.batch_scales else p.per_page_decode
    return max(0.3, base + jitter)


# --- the run ------------------------------------------------------------------
def run_sim(p: Params) -> dict:
    global T0
    work_dir = Path(tempfile.mkdtemp(prefix="batcher_sim_")) / "hocr"
    work_dir.mkdir(parents=True)
    raster_times = make_raster_times(p)

    stats = {"dispatches": [], "event_count": 0}
    dispatch_lock = threading.Lock()

    def sender(paths):
        # model decode latency for this batch of K pages
        latency = decode_latency(p, len(paths))
        time.sleep(latency)
        sections = [f"<det>{Path(x).stem}</det>" for x in paths]
        # model a dropped section: every --drop-every'th page's section never
        # arrives (missing -> padded "" by split_multi_page_stream semantics)
        for i, x in enumerate(paths):
            page_no = int(Path(x).stem.split("_")[0])
            if p.drop_every and page_no % p.drop_every == 0:
                sections[i] = ""
        return sections

    batcher = MultiPageBatcher(
        batch_size=p.batch_size,
        timeout=p.timeout,
        sender=sender,
        pending_pages=_pending_page_indices,
        max_wait=p.max_wait,
    )

    # wrap _dispatch to record window sizes
    orig_dispatch = batcher._dispatch

    def logged_dispatch(window):
        with dispatch_lock:
            stats["dispatches"].append(
                (time.monotonic() - T0, len(window.pages),
                 sorted(pi + 1 for pi, _ in window.pages)))
        record(f"dispatch {len(window.pages)} pages {sorted(pi+1 for pi,_ in window.pages)}")
        orig_dispatch(window)

    batcher._dispatch = logged_dispatch

    # single-page fallback used when a batch section is empty (mirrors the
    # engine's _batch_recognize -> client.recognize path)
    def single_recognize(page: int):
        time.sleep(p.single_decode)
        return f"<det>single{page + 1}</det>"

    q = list(range(p.n_pages))
    q_lock = threading.Lock()
    finished = [0]
    fin_lock = threading.Lock()

    def worker(wid: int):
        while True:
            with q_lock:
                if not q:
                    return
                page = q.pop(0)
            # ocrmypdf: rasterize first (the PNG lands on disk only NOW)
            r = raster_times[page]
            time.sleep(r)
            rpng = work_dir / f"{page + 1:06d}_rasterize.png"
            opng = work_dir / f"{page + 1:06d}_ocr.png"
            rpng.write_bytes(b"\x89PNG\r\n\x1a\n")
            opng.write_bytes(b"\x89PNG\r\n\x1a\n")
            # enter the batcher (this is where the worker blocks on the batch)
            raw = batcher.submit(page, opng)
            # mirror the engine: an empty/missing section falls back to a
            # single-page request (no batch involved)
            if not raw.strip():
                raw = single_recognize(page)
            # on return the engine writes its hOCR
            hocr_path(work_dir, page + 1).write_text(f"hocr {raw}", encoding="utf-8")
            record(f"page {page + 1} done")
            with fin_lock:
                finished[0] += 1

    T0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(p.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with fin_lock:
        stats["finished"] = finished[0]
    stats["dispatches"].sort()
    return stats


def summarize(stats: dict) -> list:
    """Return [k, count] pairs: how many dispatches had k pages (in order)."""
    seq = [k for _, k, _ in stats["dispatches"]]
    return seq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=48)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--raster-mean", type=float, default=0.8)
    ap.add_argument("--raster-sigma", type=float, default=0.25)
    ap.add_argument("--decode-per-page", type=float, default=2.5)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--constant-latency", action="store_true")
    ap.add_argument("--slow-page", type=int, default=0,
                    help="1-based page whose rasterize is slowed by --slow-factor")
    ap.add_argument("--slow-factor", type=float, default=1.0)
    ap.add_argument("--drop-every", type=int, default=0,
                    help="drop the batch section for every N-th page")
    ap.add_argument("--max-wait", type=float, default=900.0)
    ap.add_argument("--single-decode", type=float, default=10.0)
    args = ap.parse_args()

    slow_pages = ((args.slow_page, args.slow_factor),) if args.slow_page else ()

    p = Params(
        n_pages=args.pages, workers=args.workers, batch_size=args.batch_size,
        timeout=args.timeout, raster_mean=args.raster_mean,
        raster_sigma=args.raster_sigma, per_page_decode=args.decode_per_page,
        seed=args.seed, batch_scales=not args.constant_latency,
        slow_pages=slow_pages, drop_every=args.drop_every,
        max_wait=args.max_wait, single_decode=args.single_decode,
    )
    p.decode_per_page = args.decode_per_page
    stats = run_sim(p)
    seq = summarize(stats)
    print(f"N={p.n_pages} W={p.workers} B={p.batch_size} "
          f"R~N({p.raster_mean},{p.raster_sigma}) "
          f"decode={p.decode_per_page}/pg → dispatch sizes: {seq}")
    per = {k: seq.count(k) for k in sorted(set(seq))}
    print("  per size:", per)
    print("  first 12 events:")
    for t, evt in events[:12]:
        print(f"    {t:6.2f}s {evt}")
    print("  last 8 dispatches:")
    for t, k, pages in stats["dispatches"][-8:]:
        print(f"    {t:6.2f}s  {k} pages {pages}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
