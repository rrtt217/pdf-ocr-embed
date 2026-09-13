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


def test_no_control_that_cannot_affect_the_output():
    """The per-block font-size slider was removed because it could not do
    anything: `page_store.blocks_to_hocr()` ignores `font_scale` and the fpdf2
    renderer fits each word to its bbox (UI_IMPROVEMENTS.md §2.9 P0-1b).

    If a font-size control comes back, it must come back WITH the rendering
    support — otherwise the UI is again promising an effect it cannot deliver.
    The data field itself stays: an older sidecar's value must survive edits.
    """
    src = _source()
    assert "fs-row" not in src and "fontSize" not in src, \
        "a font-size control reappeared in the block editor — make font_scale " \
        "affect the embedded output first (or keep the control out)"
    assert "font_scale: 1.0" not in src, \
        "blocks must not be seeded with font_scale; only an existing value is kept"

    assert "function preserveFontScale(" in src
    assert "preserveFontScale(chosen[0], {" in _function_body("mergeSelected")
    assert "preserveFontScale(block, {" in _function_body("splitBlock")


def test_job_card_surfaces_live_run_activity():
    """A running card must answer "is it stuck?": how long since the last page
    finished, and which HTTP attempt the engine is on.  Activity events patch
    that one line in place — a full re-render every couple of seconds would
    fight the user's scrolling and selection."""
    src = _source()
    assert "STALL_WARN_SECONDS" in src, \
        "a run that produces nothing must be flagged as waiting on the endpoint"

    card = _function_body("jobCard")
    assert "fillActivityLine(" in card, \
        "the job card must render the activity line for a live run"

    apply_event = _function_body("applyJobEvent")
    assert 'msg.type === "activity"' in apply_event, \
        "applyJobEvent must handle the SSE activity event"
    assert "updateActivityLine(" in apply_event

    updater = _function_body("updateActivityLine")
    assert "getElementById" in updater, \
        "activity updates must patch the existing line, not re-render the list"

    css = (APP_JS.parent / "style.css").read_text(encoding="utf-8")
    assert ".job-activity" in css and ".job-activity.stalled" in css


def test_i18n_dictionaries_stay_in_sync_and_cover_every_call():
    """The UI is bilingual: a missing zh/en entry silently falls back to English
    (or to the raw key), which is exactly the kind of gap nobody notices until a
    user does.  Pure text checks — no JS runtime needed."""
    i18n = (APP_JS.parent / "i18n.js").read_text(encoding="utf-8")

    def keys_of(locale: str) -> set:
        start = i18n.index(f"\n  {locale}: {{")
        end = i18n.index("\n  },", start)
        body = i18n[start:end]
        return set(re.findall(r'^\s{4}"([^"]+)":', body, re.M))

    en, zh = keys_of("en"), keys_of("zh")
    assert en, "could not parse the en dictionary"
    assert en == zh, (
        f"i18n dictionaries out of sync — missing in zh: {sorted(en - zh)}; "
        f"missing in en: {sorted(zh - en)}")

    app = _source()
    used = set(re.findall(r'\bt\("([^"]+)"', app))
    # Two call sites build keys dynamically (`status.` + code, `cleanup.area.`).
    missing = {k for k in used if k not in en and not k.endswith(".")}
    assert not missing, f"t() keys with no translation: {sorted(missing)}"

    html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    declared = set(re.findall(
        r'data-i18n(?:-html|-title|-placeholder|-alt)?="([^"]+)"', html))
    assert not (declared - en), \
        f"markup keys with no translation: {sorted(declared - en)}"
