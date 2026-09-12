# Why the app used to hang on exit (and how to prove it is fixed)

The app could be asked to quit and then never leave: uvicorn logged
`Finished server process`, and the process sat there forever (a `Ctrl-C` in the
terminal killed it, which is why the symptom looked like "needs a second
Ctrl-C").  Two independent causes, both fixed in this change set.

## Cause 1 — a non-daemon OCR worker is joined at interpreter exit

`POST /api/ocr/upload` started the OCR phase with

```python
loop.run_in_executor(None, ocr_service.run_ocr, job_id, options)
```

The default executor's threads are **non-daemon**.  At interpreter exit Python
runs `threading._shutdown()`, which joins every non-daemon thread — so the
process outlives `main` until that OCR run finishes, however long that is:

```
Exception ignored on threading shutdown:
  File ".../concurrent/futures/thread.py", line 31, in _python_exit
    t.join()
  File ".../threading.py", line 1133, in join
    self._os_thread_handle.join(timeout)
KeyboardInterrupt
```

The same applies to anyio's worker threads (which handle `def` endpoints).
Fix: **every** OCR run goes through `ocr_service.start_job()`, which uses a
daemon thread and tracks it in a registry that shutdown can cancel and wait
for.  A daemon thread can never hold the interpreter open.

## Cause 2 — uvicorn waited forever for the open SSE stream

`EmbeddedServer.stop()` relies on uvicorn's graceful drain, and uvicorn's
default `timeout_graceful_shutdown` is `None` — "wait for every connection to
close".  The WebUI holds one `EventSource` per running job open for the whole
run, so "wait for the connections" meant "wait for the OCR job".

Measured before the fix: `stop()` still had not returned after 30 s, and the
watchdog force-exit only fired at its 10 s deadline.  Fix: a finite
`timeout_graceful_shutdown` (`server.DEFAULT_GRACEFUL_SHUTDOWN`) plus a
force-exit watchdog, and the SSE generators now end themselves when a quit is
requested.

## Cause 3 — the app does not own the signal handler in `uvicorn backend.main:app`

`uvicorn backend.main:app` is a documented way to run the server, and it is the
one path where none of the above helps: uvicorn installs its own SIGINT
handler, never calls `backend.main.run()`, and therefore knows nothing about
the OCR job behind the HTTP server.  The user-visible symptom:

```
INFO:     Finished server process [302390]
Exception ignored on threading shutdown:
  File ".../concurrent/futures/thread.py", line 31, in _python_exit
    t.join()
KeyboardInterrupt:            <- only THIS second Ctrl-C ends the process
```

Two separate mistakes were made here, and both had to be fixed:

1. **The wrong thread kind.**  The first fix assumed executor workers are
   non-daemon (true on 3.12 and earlier, and what the traceback suggests).
   On this interpreter (CPython 3.14) they are **daemon** threads — and 3.13+
   `threading._shutdown` waits for the thread STATE of daemon threads too.
   A watchdog that only looked at non-daemon threads therefore found nothing
   and never armed.  It now considers every thread except the main one.
2. **The wrong hook ordering.**  `atexit.register` is not enough:
   `concurrent.futures.thread` registers its join hook through
   ``threading._register_atexit`, whose list runs inside
   ``threading._shutdown`` and in REVERSE order.  Our hook must be registered
   *after* that one (``concurrent.futures`` is imported first on purpose) or
   the blocking join runs before the watchdog is ever reached.

Clearing ``concurrent.futures.thread._threads_queues`` — the obvious "skip the
join" trick — does **not** work and was removed again: the wait that hangs is
the C-level ``_thread._shutdown()``, which that registry has no influence on.
Only a hard exit beats it.

So the app now wires two things into its own lifespan
(``backend.main._wire_exit_safety``), which work under ANY launcher:

* ``shutdown.chain_signal_handler`` — wraps the handler uvicorn installed, so
  the OCR jobs get their cancel flag before uvicorn drains (uvicorn stays in
  charge of the shutdown; we only add what it cannot know);
* ``shutdown.watchdog_on_exit`` — when the main thread is done and workers are
  still alive, force the exit after ``EXIT_JOIN_GRACE`` (5 s).  A process with
  nothing left to wait for exits normally, so an idle server is never killed
  by it.

Measured after the fix, all with the OCR phase blocked mid-page:

| entry point | signal | before | after |
| --- | --- | --- | --- |
| `uvicorn backend.main:app` | 1x Ctrl-C | hangs, needs a 2nd | **5.2 s** |
| `python -m backend.main` | 1x Ctrl-C | hangs, needs a 2nd | 5.3 s |
| `desktop.py` (Quit button) | HTTP | hangs | 5.1 s |


* `backend/shutdown.py` — one bounded teardown for every entry point:
  `stop_jobs()` (cancel flag first, then a bounded wait), `stop_server()`
  (which also arms the hard-exit deadline), `exit_soon()` /
  `force_exit_now()` (`os._exit` backstop), `make_signal_handler()` (first
  signal graceful, second immediate).
* `desktop.py` — SIGINT/SIGTERM handling, a window-quit watcher, and the
  shutdown hook that the WebUI Quit button reaches.
* `backend/main.run()` — the plain server (`python -m backend.main`) now has
  the same signal handling; it previously had none.
* `page_store.clear_cancel()` — a retry after a stop used to abort on its first
  page, because the `cancel` flag from the previous stop was still on disk.

## The guards added on top

* `backend/shutdown.py` — one bounded teardown for every entry point:
  `stop_jobs()` (cancel flag first, then a bounded wait), `stop_server()`
  (which also arms the hard-exit deadline), `exit_soon()` /
  `force_exit_now()` (`os._exit` backstop), `make_signal_handler()` (first
  signal graceful, second immediate), `chain_signal_handler()` (add the OCR
  cancel to a handler we do not own) and `watchdog_on_exit()` /
  `register_exit_cleanup()` (bound the interpreter's wait for workers).
* `desktop.py` — SIGINT/SIGTERM handling, a window-quit watcher, and the
  shutdown hook that the WebUI Quit button reaches.
* `backend/main.run()` — the plain server (`python -m backend.main`) now has
  the same signal handling; it previously had none.
* `backend/main.lifespan` — the launcher-independent wiring above, so even a
  bare `uvicorn backend.main:app` exits on the first signal.
* `page_store.clear_cancel()` — a retry after a stop used to abort on its first
  page, because the `cancel` flag from the previous stop was still on disk.

## What an "hour-long page" does (and does not) cost

A single page can legitimately be in flight for ~1 h with the default plugin
settings: the OCR client's read timeout is `max(900 s, max_tokens * 0.08)` —
15 min for the default budget — and `max_retries = 3` allows four attempts,
plus backoff (`ocrmypdf_unlimited/client.py`, `http_retry.py`).  The engine
checks the `cancel` flag **before** each page (`engine.generate_hocr`), so
neither "stop" nor "quit" can interrupt a request that is already running:
pressing stop during that page does nothing until it returns, and the UI only
ever promises "stopping after the current page".

What the exit path guarantees in that situation (pinned by
`tests/test_exit_shutdown.py::test_a_page_that_takes_an_hour_never_delays_the_exit`):

* **Quitting still takes seconds.**  The worker is a daemon thread on purpose:
  `stop_jobs()` waits `JOB_STOP_WAIT` (5 s) for it and then simply leaves it
  behind, the server stops, the process exits.  Nothing anywhere waits on the
  in-flight page — the 20 s `EXIT_DEADLINE` is a backstop, not the mechanism.
* **Finished pages are kept.**  Pages are written as they complete, so a quit
  during page 224 of 224 keeps pages 1..223 on disk, and the job comes back as
  `stopped` with `pages_done = 223` (plus its `embedded_path` if it had already
  been finalized).  "Retry remaining" then re-runs *only* the missing page(s) —
  it does not restart the book.
* **The abandoned page is simply not written.**  Nothing half-finished is ever
  recorded as a result: a page's hOCR/sidecar appear only once its request
  returned.

So the cost is one page of work, not the document — and the same holds for a
per-page *failure*: `run_ocr` keeps the completed pages, marks the job
`stopped`/`error`, and the retry flow resumes at the pages without a result.
Verified against the 224-page job that triggered this investigation:
interrupted at 223/224, it restored as `stopped 223/224`, exposed 223 editable
pages, and a retry targeted only the missing one.

## Resuming, audited (and the bug that audit found)

`tests/test_job_resume.py` drives real entry points with a plugin-shaped fake
that actually writes hOCR + sidecars, then kills or stops them and checks what
the NEXT start makes of the work folder.  Three interruption shapes:

| interruption | on disk | restored as |
| --- | --- | --- |
| graceful stop (cancel flag) | N complete pages | `stopped`, `pages_done=N`, retry runs only the rest |
| SIGKILL between the hOCR and sidecar write | page N: hOCR complete, sidecar truncated | page N is COMPLETE (rebuilt from the hOCR) |
| SIGKILL while the hOCR was streaming | page N: hOCR truncated (no `</html>`), sidecar maybe complete | page N is NOT counted, retry re-OCRs it |

The audit found a real defect in the second row: `load_page` claimed to fall
back to the hOCR when a sidecar is unusable, but it actually returned `None` as
soon as the sidecar existed and failed to parse.  A page in that state was
counted as done by `page_numbers` (so no retry would ever touch it) while being
invisible to the editor (so the user could not fix it either) — and the
finalize step would have embedded that page with **no text layer**.  Fixed:
a damaged sidecar is now rebuilt from the hOCR (`page_store.load_page`).

Tightening that also pinned the inventory rule the module already documented
but did not fully enforce: **the hOCR decides whenever it exists**.  A page
whose hOCR is half-written must stay re-runnable even if a sidecar is present
(that is exactly how ocrmypdf's Tesseract streams a page, and a crash can
leave a *stale* sidecar next to a re-run in progress); a sidecar is the
completion signal only for a page with no hOCR at all (sidecar-only engines).
The invariant that keeps a page repairable is one-directional and is now
asserted in the tests: **every page counted as done must be loadable** —
`load_page` may be more permissive, never less.

## The harnesses here

Each runs a REAL entry point in a child process with OCRmyPDF patched to block
(so a "long run" needs no provider), starts a job over HTTP, then quits and
times the exit.  They recreate `_tmp/` themselves (gitignored).

```bash
.venv/bin/python research/exit-hang/repro_quit.py             # Quit button
.venv/bin/python research/exit-hang/repro_quit.py --with-sse  # + open SSE
.venv/bin/python research/exit-hang/repro_quit.py --signal    # SIGTERM
.venv/bin/python research/exit-hang/repro_ctrlc.py            # python -m backend.main
.venv/bin/python research/exit-hang/repro_ctrlc.py --signal=SIGTERM
.venv/bin/python research/exit-hang/repro_uvicorn.py          # uvicorn backend.main:app
```

Healthy output is `RESULT: exited after ~5s (rc=0)`; the 5 s is the synthetic
`time.sleep(600)` OCR body, which of course ignores the cancel flag — a real
engine stops at its current page boundary.  `repro_uvicorn.py` says explicitly
when a SECOND signal was needed, which is the symptom Cause 3 describes.  The
automated versions of all of this live in `tests/test_exit_shutdown.py`.
