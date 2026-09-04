"""Multi-page batch scheduling for the unlimited-ocr engine.

ocrmypdf runs each page's ``generate_hocr`` in its own worker thread
(``use_threads``, jobs-way concurrency), so pages arrive at the engine
concurrently.  The batcher groups pages that arrive in a short window into ONE
"Multi page parsing." request (``engine_client.recognize_multi``); the
``<PAGE>``-delimited response sections are handed back to each waiting worker
thread, which then writes its own page files.

Scheduling model (leader-follower, with a backstop timer):

* the first page to arrive opens a window and arms a flush timer;
* every thread that arrives joins the open window;
* the thread that fills the window — or finds it expired — becomes the
  leader: it sends the request while the other waiters block on the window
  condition;
* when the response lands, each page picks its own section off the result
  table and returns through ``submit()``.

Anti-collapse rules (see ``research/batcher-research.md`` — the batcher used
to degrade from full batches to single-page dispatches):

* a window is flushed ONLY when it is full or its timeout expired.  It is
  NEVER flushed "because no other pages look pending": the pending-page scan
  reads rasterized PNGs off disk, so pages whose workers are still
  rasterizing are invisible — flushing on an empty scan raced with them and
  produced a 1-page dispatch after every batch landing (the leading-solo bug);
* a partial window whose timeout has expired is HELD (bounded, ``grace`` x
  ``max_extensions``) while the scan shows rasterized-but-unstaged pages
  outside it: those pages are provably about to call ``submit()``, so a short
  hold lets them join one batch instead of flushing small.

Every failure path degrades gracefully: a waiter whose section never arrived
(batch failure, missing section, cancel, or a wait that outlives the request)
gets an exception so the engine can fall back to its per-page request — never
worse than running without batching.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

#: Pending page indices for one run, from the engine's view of its work folder
#: (pages rasterized but not yet written hOCR).  Receives a representative page
#: image path of the run (its parent is the work folder).  Provided by the engine.
PendingPagesFn = Callable[[Path], Set[int]]


class BatchCancelled(RuntimeError):
    """A job stop was requested while a page waited on its batch."""


class BatchTimeout(RuntimeError):
    """A page did not receive its batch section within the expected budget."""


class _BatchWindow:
    """One group of pages staged for a single multi-image request."""

    def __init__(self, batch_size: int, timeout: float) -> None:
        self.batch_size = max(1, int(batch_size))
        self.timeout = float(timeout)
        self.cv = threading.Condition()
        #: Staged pages in the exact order they will be sent: (page_index, path).
        self.pages: List[Tuple[int, Path]] = []
        #: Work folder (parent of the first staged image) — all pages of one
        #: ocrmypdf run share it; used by the engine's pending-page scan.
        self.work_dir: Optional[Path] = None
        #: page_index -> raw marker stream section.
        self.results: Dict[int, str] = {}
        self.failure: Optional[BaseException] = None
        #: open -> dispatching -> done
        self.phase: str = "open"
        self.deadline: float = time.monotonic() + self.timeout
        #: How many times this window has been held open past its deadline
        #: because more rasterized pages were still arriving (bounded).
        self.extensions: int = 0

    def add(self, page_index: int, input_file: Path) -> None:
        if self.work_dir is None:
            self.work_dir = Path(input_file).resolve().parent
        self.pages.append((page_index, input_file))
        # Logged on EVERY update: shows the batch window growing and the
        # 1-based page numbers staged in it so far.
        log.info("batch %s: staged %d/%d page(s) — pages [%s]",
                 self._label(), len(self.pages), self.batch_size,
                 self._pages_label())

    def _label(self) -> str:
        """Disambiguating window label for logs: the job id when the work
        folder convention is known (``work/<job_id>/hocr``), else a unique
        window id (anonymous / test runs)."""
        if self.work_dir is not None:
            return f"job/{Path(self.work_dir).resolve().parent.name}"
        return f"window/{id(self):x}"

    def _pages_label(self) -> str:
        """1-based page numbers staged so far, in ascending order."""
        return ", ".join(str(pi + 1) for pi, _ in sorted(self.pages))

    def is_full(self) -> bool:
        return len(self.pages) >= self.batch_size

    def is_expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def staged_indices(self) -> Set[int]:
        return {pi for pi, _ in self.pages}


class MultiPageBatcher:
    """Groups concurrent per-page engine calls into one multi-image request.

    Thread-safety: a single lock guards window selection and leader election;
    the window's own Condition guards result delivery.  The HTTP send happens
    outside any lock (one thread per window while the others wait).
    """

    def __init__(
        self,
        batch_size: int,
        timeout: float,
        sender: Callable[[List[Path]], List[str]],
        pending_pages: Optional[PendingPagesFn] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        max_wait: Optional[float] = None,
        grace: Optional[float] = None,
        max_extensions: int = 3,
    ) -> None:
        self.batch_size = max(1, int(batch_size))
        self.timeout = float(timeout)
        self.sender = sender
        self.pending_pages = pending_pages or (lambda: set())
        self.is_cancelled = is_cancelled or (lambda: False)
        #: How long a follower may wait for its window's response (the leader's
        #: HTTP call is bounded by its own timeouts + retries).
        self.max_wait = float(max_wait) if max_wait else 900.0
        #: A partial window whose deadline passed is held for ``grace`` (up to
        #: ``max_extensions`` times) while pages are provably still arriving,
        #: so late siblings join one batch instead of flushing small.
        self.grace = float(grace) if grace else self.timeout
        self.max_extensions = max(0, int(max_extensions or 0))
        self._lock = threading.Lock()
        self._window: Optional[_BatchWindow] = None
        self._timer: Optional[threading.Timer] = None

    def submit(self, page_index: int, input_file: Path) -> str:
        """Stage one page and block until its raw stream section is ready.

        Returns the raw marker stream for this page.  Raises on batch failure
        / wait timeout / cancel so the caller can fall back to a per-page
        request (or stop, on cancel).
        """
        if self.is_cancelled():
            raise BatchCancelled()
        window, is_leader = self._register(page_index, input_file)
        if is_leader:
            self._dispatch(window)
        return self._await_result(window, page_index)

    def _register(self, page_index: int, input_file: Path) -> Tuple[_BatchWindow, bool]:
        """Join the open window; become leader when a flush is due.

        Called under the guard of a private lock: either of "window full" or
        "window expired" elects the caller as leader for this window.
        """
        with self._lock:
            window = self._window
            if window is None or window.phase != "open":
                window = _BatchWindow(self.batch_size, self.timeout)
                self._window = window
                self._arm_timer(window)
            window.add(page_index, input_file)
            leader = self._flush_due(window)
            if leader:
                window.phase = "dispatching"
                self._cancel_timer()
            return window, leader

    def _pending_outside(self, window: _BatchWindow) -> Set[int]:
        """Pages that have rasterized (PNG on disk) but are not staged here.

        Those pages' worker threads are about to call ``submit()`` — the only
        *trustworthy* "more pages are coming" signal the batcher has (pages
        still being rasterized have no PNG yet and are invisible to the scan).
        """
        if window.work_dir is None:
            return set()
        return self.pending_pages(window.work_dir) - window.staged_indices()

    def _flush_due(self, window: _BatchWindow) -> bool:
        """True when this window should be dispatched now (lock held).

        Deliberately NOT "pending outside is empty": that scan races with
        pages whose workers are still rasterizing (invisible), which used to
        fire a 1-page dispatch after every batch landing.  The tail of a run
        simply pays one ``timeout``.
        """
        if window.is_full() or window.work_dir is None:
            return True
        return window.is_expired()

    def _maybe_extend(self, window: _BatchWindow) -> bool:
        """Hold a partial, expired window while more pages are provably coming.

        Called from the backstop timer only.  When the scan shows
        rasterized-but-unstaged pages outside this window, those pages are
        imminent ``submit()`` calls: extend the deadline by ``grace`` (at most
        ``max_extensions`` times) so they join this batch.  Bounded, so a page
        that never arrives cannot hold the window forever.

        Caller must already hold ``self._lock``.
        """
        if not window.is_expired() or window.extensions >= self.max_extensions:
            return False
        outside = self._pending_outside(window)
        if not outside:
            return False
        window.deadline = time.monotonic() + self.grace
        window.extensions += 1
        log.info("batch %s: holding window %.1fs (pending outside: [%s]) — "
                 "extension %d/%d",
                 window._label(), self.grace,
                 ", ".join(str(pi + 1) for pi in sorted(outside)),
                 window.extensions, self.max_extensions)
        return True

    def _dispatch(self, window: _BatchWindow) -> None:
        """Send the batch and hand each section to its page (no locks held)."""
        log.info("batch %s: dispatching %d page(s) — pages [%s]",
                 window._label(), len(window.pages), window._pages_label())
        try:
            sections = self.sender([path for _, path in window.pages])
        except BaseException as exc:  # noqa: BLE001 - degrade to per-page
            with window.cv:
                window.failure = exc
                window.phase = "done"
                window.cv.notify_all()
            return
        with window.cv:
            for idx, (page_index, _) in enumerate(window.pages):
                if idx < len(sections):
                    window.results[page_index] = sections[idx]
            window.phase = "done"
            window.cv.notify_all()

    def _await_result(self, window: _BatchWindow, page_index: int) -> str:
        """Block until this page's section is ready (or a reason to give up)."""
        deadline = time.monotonic() + self.max_wait
        with window.cv:
            while (page_index not in window.results
                   and window.failure is None and window.phase != "done"):
                if self.is_cancelled():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                window.cv.wait(timeout=min(remaining, 0.5))
        if page_index in window.results:
            return window.results[page_index]
        if self.is_cancelled():
            raise BatchCancelled()
        if window.failure is not None:
            raise BatchTimeout(
                f"page {page_index + 1}: batch request failed: {window.failure}")
        raise BatchTimeout(
            f"page {page_index + 1}: did not get its batch result in time")

    def _arm_timer(self, window: _BatchWindow) -> None:
        """Backstop flush: fire just after the window's current deadline.

        The delay is computed from ``window.deadline`` (not a fixed interval),
        so a re-armed timer after a hold (``_maybe_extend``) fires at the NEW
        deadline — never prematurely, which would lose the arm and strand the
        window until ``max_wait``.

        At expiry the window is either held (bounded, while pages are
        provably still arriving — see ``_maybe_extend``) or dispatched.

        Caller must already hold ``self._lock`` (it is invoked from
        ``_register``); the callback itself takes the lock when it runs.
        """
        def _on_timer() -> None:
            with self._lock:
                if self._window is not window or window.phase != "open":
                    return
                if window.is_full():
                    window.phase = "dispatching"
                    self._timer = None
                elif window.is_expired() and self._maybe_extend(window):
                    self._arm_timer(window)  # held: re-arm for the new deadline
                    return
                elif window.is_expired():
                    window.phase = "dispatching"
                    self._timer = None
                else:
                    self._arm_timer(window)  # fired early (jitter); re-arm
                    return
            self._dispatch(window)

        delay = max(0.05, (window.deadline - time.monotonic()) + 0.1)
        timer = threading.Timer(delay, _on_timer)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer(self) -> None:
        """Caller must already hold ``self._lock``."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
