"""MultiPageBatcher: windowing, leader election, failure/cancel fallback."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from backend.ocrmypad.batching import MultiPageBatcher, BatchCancelled, BatchTimeout


def _batcher(sender, pending, timeout=10.0, batch_size=2, cancel=None,
             max_wait=10.0) -> MultiPageBatcher:
    return MultiPageBatcher(
        batch_size=batch_size,
        timeout=timeout,
        sender=sender,
        pending_pages=lambda wd: set(pending),
        is_cancelled=cancel or (lambda: False),
        max_wait=max_wait,
    )


def _run_concurrent(batcher, jobs):
    """Run jobs in threads; return {idx: result} and any raised exceptions."""
    results, errors = {}, {}
    barrier = threading.Barrier(len(jobs))

    def work(idx, path):
        barrier.wait(timeout=5)
        try:
            results[idx] = batcher.submit(idx, path)
        except BaseException as exc:  # noqa: BLE001 - test harness
            errors[idx] = exc

    threads = [threading.Thread(target=work, args=(i, Path(f"/tmp/p{i}.png")))
               for i in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    return results, errors


def test_concurrent_pages_form_one_batch_in_staged_order():
    calls = []

    def sender(paths):
        # The leader only dispatches after the window is full, so both pages
        # are already staged when sender runs.
        calls.append(list(paths))
        # Section per page, keyed by the page number in the file name, so the
        # assertion is independent of thread registration order.
        return [f"S{Path(p).stem[1]}" for p in paths]

    batcher = _batcher(sender, pending={0, 1})
    results, errors = _run_concurrent(batcher, jobs=[0, 1])
    assert errors == {}
    assert results == {0: "S0", 1: "S1"}
    assert len(calls) == 1 and len(calls[0]) == 2


def test_pending_excludes_already_staged_pages():
    # Both pages are rasterized; page 1 joins while page 0 is staged.  The
    # pending scan must NOT count the staged page 0, or the window would wait
    # for its own member (the stale-pending bug) and time out.
    calls = []

    def sender(paths):
        calls.append(list(paths))
        return [f"S{Path(p).stem[1]}" for p in paths]

    batcher = _batcher(sender, pending={0, 1})
    results, errors = _run_concurrent(batcher, jobs=[0, 1])
    assert errors == {}
    assert results == {0: "S0", 1: "S1"}
    assert len(calls) == 1


def test_serial_pages_each_get_their_own_batch():
    calls = []

    def sender(paths):
        calls.append(list(paths))
        return [f"P{paths[0].name}"]

    # Serial arrivals: nothing else is pending, so each lone page is flushed by
    # the window timeout (no premature "empty pending -> flush" fast path —
    # that raced with still-rasterizing pages).
    batcher = _batcher(sender, pending=set(), batch_size=4, timeout=0.2,
                       max_wait=5.0)
    assert batcher.submit(0, Path("/tmp/p0.png")) == "Pp0.png"
    assert batcher.submit(1, Path("/tmp/p1.png")) == "Pp1.png"
    assert len(calls) == 2 and all(len(c) == 1 for c in calls)
    assert calls[0] == [Path("/tmp/p0.png")] and calls[1] == [Path("/tmp/p1.png")]


def test_sender_failure_degrades_to_batch_timeout():
    def sender(paths):
        raise ConnectionError("boom")

    batcher = _batcher(sender, pending={0, 1})
    results, errors = _run_concurrent(batcher, jobs=[0, 1])
    assert results == {}
    assert all(isinstance(e, BatchTimeout) for e in errors.values())


def test_missing_sections_handed_back_as_empty_strings():
    calls = []

    def sender(paths):
        calls.append(list(paths))
        return ["S0"]  # only one section for a 2-page window

    batcher = _batcher(sender, pending={0, 1})
    results, errors = _run_concurrent(batcher, jobs=[0, 1])
    # One page gets the single section; the page that got none raises
    # BatchTimeout so the engine falls back to a per-page request.
    assert len(results) == 1 and list(results.values()) == ["S0"]
    assert len(errors) == 1 and isinstance(next(iter(errors.values())), BatchTimeout)


def test_cancel_while_waiting_raises_batch_cancelled():
    state = {"cancelled": False}
    results, errors = {}, {}

    def cancel():
        return state["cancelled"]

    batcher = _batcher(sender=lambda paths: ["S0", "S1"], pending={0, 9},
                       batch_size=4, cancel=cancel, max_wait=15.0)

    def work(idx, path):
        try:
            results[idx] = batcher.submit(idx, path)
        except BaseException as exc:  # noqa: BLE001 - test harness
            errors[idx] = exc

    threads = [threading.Thread(target=work, args=(i, Path(f"/tmp/p{i}.png")))
               for i in (0, 1)]
    for t in threads:
        t.start()
    time.sleep(0.3)  # let the pages reach the wait state (window never flushes)
    state["cancelled"] = True
    for t in threads:
        t.join(5)
    # Both waiters detect the cancel flag and raise without the sender's result.
    assert results == {}
    assert all(isinstance(e, BatchCancelled) for e in errors.values())


def test_partial_window_flushed_by_backstop_timer():
    calls = []

    def sender(paths):
        calls.append(list(paths))
        return ["S0"]

    # nothing else is pending -> the lone page is flushed by the backstop timer
    # (the empty-pending fast path is gone, so the timer is the only flusher).
    batcher = _batcher(sender, pending=set(), timeout=0.2, batch_size=4,
                       max_wait=5.0)
    assert batcher.submit(0, Path("/tmp/p0.png")) == "S0"
    assert len(calls) == 1 and calls[0] == [Path("/tmp/p0.png")]


def test_partial_window_holds_while_pages_are_pending_outside(monkeypatch):
    """Anti-collapse fix: a partial window is NOT flushed at expiry while the
    scan shows rasterized-but-unstaged pages outside it — those pages are about
    to submit(), so a bounded hold lets them join one batch."""
    calls = []

    def sender(paths):
        calls.append(list(paths))
        return [f"S{Path(p).stem[1]}" for p in paths]

    batcher = MultiPageBatcher(
        batch_size=4, timeout=0.15, sender=sender,
        pending_pages=lambda wd: set(), max_wait=5.0,
        grace=0.4, max_extensions=2,
    )

    def work(args, out):
        try:
            out.append((args, batcher.submit(*args)))
        except BaseException as exc:  # noqa: BLE001 - test harness
            out.append((args, exc))

    results = []
    t0 = threading.Thread(
        target=work, args=((0, Path("/tmp/hocr/p0.png")), results), daemon=True)
    t0.start()

    # wait until page 0 is staged, then let page 1's PNG appear as pending
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with batcher._lock:
            w = batcher._window
        if w is not None and len(w.pages) == 1:
            break
        time.sleep(0.01)
    else:
        pytest.fail("page 0 never staged")
    monkeypatch.setattr(batcher, "pending_pages", lambda wd: {0, 1})

    # let the window expire and be held (page 1 provably pending), then join
    time.sleep(0.3)
    work((1, Path("/tmp/hocr/p1.png")), results)
    t0.join(5)

    assert len(results) == 2
    assert all(not isinstance(r, BaseException) for _, r in results)
    assert len(calls) == 1 and len(calls[0]) == 2  # ONE batch, both pages


def test_hold_is_bounded_when_pending_page_never_arrives():
    """A stuck/phantom pending page cannot hold the window forever: after
    max_extensions holds the window flushes with what it has."""
    calls = []

    def sender(paths):
        calls.append(list(paths))
        return [f"S{Path(p).stem[1]}" for p in paths]

    batcher = MultiPageBatcher(
        batch_size=4, timeout=0.1, sender=sender,
        pending_pages=lambda wd: {1}, max_wait=5.0,  # page 1 never submits
        grace=0.1, max_extensions=2,
    )
    t_start = time.monotonic()
    assert batcher.submit(0, Path("/tmp/hocr/p0.png")) == "S0"
    elapsed = time.monotonic() - t_start
    assert len(calls) == 1 and calls[0] == [Path("/tmp/hocr/p0.png")]
    assert elapsed < 2.0  # bounded well below an unbounded hold


def test_maybe_extend_only_fires_for_confirmably_pending_pages():
    """Unit-level: holds happen only when the window is expired, has hold
    budget left, AND the scan shows rasterized-but-unstaged pages outside."""
    from ocrmypdf_unlimited.batching import _BatchWindow

    batcher = MultiPageBatcher(
        batch_size=4, timeout=1.0, sender=lambda ps: ["S"] * len(ps),
        pending_pages=lambda wd: {0}, grace=0.5, max_extensions=2,
    )
    w = _BatchWindow(4, 1.0)
    w.work_dir = Path("/tmp/hocr")
    w.add(0, Path("/tmp/hocr/p0.png"))  # staged page 0

    # 1) not expired -> no hold
    w.deadline = time.monotonic() + 10
    assert batcher._maybe_extend(w) is False

    # 2) expired but NOTHING pending outside (only the staged page) -> flush
    w.deadline = time.monotonic() - 1
    assert batcher._maybe_extend(w) is False

    # 3) expired + page 3 pending outside -> hold (deadline pushed forward)
    batcher.pending_pages = lambda wd: {0, 3}
    old_deadline = w.deadline
    assert batcher._maybe_extend(w) is True
    assert w.extensions == 1 and w.deadline > old_deadline

    # 4) bounded: no more holds once max_extensions is reached
    w.extensions = 2
    assert batcher._maybe_extend(w) is False


# --- batch-window logging ----------------------------------------------------

def test_batch_window_logs_pages_on_each_update(caplog):
    """Every page join logs the growing window with its 1-based page numbers;
    the flush logs the final composition being sent."""
    calls = []

    def sender(paths):
        calls.append(list(paths))
        return ["S0", "S1"]

    with caplog.at_level(logging.INFO, logger="ocrmypdf_unlimited.batching"):
        batcher = _batcher(sender, pending={0, 1})
        results, errors = _run_concurrent(batcher, jobs=[0, 1])
    assert errors == {}
    assert len(calls) == 1

    staged = [r.message for r in caplog.records if "staged" in r.message]
    dispatched = [r.message for r in caplog.records if "dispatching" in r.message]
    # Each page join is one "update": both pages log the window as it grows.
    assert len(staged) == 2
    assert "staged 1/2" in staged[0]
    # The final update and the dispatch both show every staged page (1-based).
    assert "staged 2/2 page(s) — pages [1, 2]" in staged[-1]
    assert "dispatching 2 page(s) — pages [1, 2]" in dispatched[-1]


def test_serial_windows_each_log_their_own_pages(caplog):
    """Serial pages form separate windows; each window's log names only its
    own page."""

    def sender(paths):
        return [f"P{paths[0].name}"]

    with caplog.at_level(logging.INFO, logger="ocrmypdf_unlimited.batching"):
        batcher = _batcher(sender, pending=set(), batch_size=4, timeout=0.2,
                           max_wait=5.0)
        assert batcher.submit(0, Path("/tmp/p0.png")) == "Pp0.png"
        assert batcher.submit(1, Path("/tmp/p1.png")) == "Pp1.png"

    staged = [r.message for r in caplog.records if "staged" in r.message]
    dispatched = [r.message for r in caplog.records if "dispatching" in r.message]
    assert len(staged) == 2 and all("staged 1/4" in m for m in staged)
    assert [m for m in dispatched if "pages [1]" in m]
    assert [m for m in dispatched if "pages [2]" in m]
