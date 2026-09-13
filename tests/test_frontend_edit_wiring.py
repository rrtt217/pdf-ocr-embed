"""Wiring guard for the editor's save path (P0-1).

The WebUI keeps block edits in browser memory, so the ONLY thing that gets them
into the output PDF is ``app.js`` POSTing the edited page to
``/api/pages/{job}/{page_index}`` before the embed request.  That call was once
missing while the route itself existed and was covered by tests — a green suite
said nothing about the user's corrections being dropped.

There is no JS test runner here (the frontend deliberately has no build step),
so this is a deliberately COARSE source-level guard, not a behavioural test: it
fails only if the save call, its debounce, or the embed flush disappear.  The
behaviour itself is verified against a running server (edit a block, then read
the page back from the API).
"""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "app.js"


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """The source of ``function <name>`` up to the next top-level ``function``."""
    src = _source()
    start = re.search(rf"^(?:async )?function {re.escape(name)}\(", src, re.M)
    assert start, f"{name}() is gone from app.js"
    nxt = re.search(r"^(?:async )?function \w+\(", src[start.end():], re.M)
    return src[start.start():start.end() + (nxt.start() if nxt else len(src))]


def test_editor_posts_edited_pages_to_the_edit_route():
    """The save call itself: a POST to /api/pages/{job}/{page_index} carrying
    the page's blocks."""
    src = _source()
    assert re.search(r"api\(`/api/pages/\$\{[^}]+\}/\$\{[^}]+\}`,\s*\{\s*\n\s*method: \"POST\"", src), \
        "app.js no longer POSTs edited pages to /api/pages/{job}/{page} — " \
        "block edits would be silently dropped at embed time"
    assert "blocks: cloneBlocks(page.blocks)" in src, \
        "the save call must send the page's blocks"


def test_edit_save_is_debounced_and_flushes_before_embed():
    """An edit schedules a debounced save, and embed() flushes pending saves
    first (otherwise finalize renders the server's stale pages)."""
    assert "SAVE_DEBOUNCE_MS" in _source()
    assert "setTimeout(() => { saveDirtyPages(); }" in _source(), \
        "markDirty() must schedule a debounced saveDirtyPages()"

    embed_body = _function_body("embed")
    assert "await saveDirtyPages()" in embed_body, \
        "embed() must flush pending page edits before POSTing /api/embed"
    # ...and must not proceed when that flush failed.
    assert re.search(r"if \(!\(await saveDirtyPages\(\)\)\)", embed_body), \
        "embed() must bail out when the pending edits could not be saved"


def test_page_navigation_flushes_pending_edits():
    """Switching page/job must not throw away unsaved edits."""
    assert "await saveDirtyPages()" in _function_body("goToPage")
    assert "await saveDirtyPages()" in _function_body("selectJob")
    assert "await saveDirtyPages()" in _function_body("refreshSelectedPages")


def test_unload_guard_covers_unsaved_edits():
    """Editing a block and closing the tab used to lose the work silently: the
    beforeunload guard only asked whether an OCR job was running."""
    src = _source()
    guard = src[src.index('window.addEventListener("beforeunload"'):]
    guard = guard[:guard.index("});")]
    assert "dirtyPageCount()" in guard, \
        "the beforeunload guard must also fire for unsaved page edits"


def test_conf_controls_yield_to_the_block_filter_without_conf_data():
    """The unlimited engine reports no `conf`, so the low-confidence filter can
    never match: the pane must fall back to a filter that works (P0-4)."""
    src = _source()
    assert "BLOCK_FILTERS" in src and "matchesBlockFilter" in src
    render = _function_body("renderPage")
    assert "updateReviewControls()" in render, \
        "renderPage() must decide between the confidence controls and the filter"
