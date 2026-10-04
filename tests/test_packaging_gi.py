"""The frozen Linux app must ship the GI typelibs PyInstaller's hooks miss.

Real incident: the Actions artifact could not open a native window — PyGObject's
hooks collect typelibs per gi namespace, there is no hook for WebKit2, and the
frozen app cannot see the *system* typelibs of another distro (the fallback
search paths are compiled into the build host's girepository).  ``gi`` then
reported "Namespace WebKit2 not available" and the app quietly fell back to the
browser — the workflow still went green.

These are source-level guards (the spec only runs inside a PyInstaller build):

* the spec collects the WebKit2 typelib closure itself, in the 4.1-then-4.0
  order pywebview requires, and the tray's namespaces in ``tray_gtk``'s order;
* typelib ONLY — bundling the shared libraries is forbidden (libwebkit2gtk is a
  multiprocess stack; its helpers must be the system ones);
* the workflow hard-fails a Linux build whose bundle lacks the typelib, so the
  silent fallback can never ship as a green artifact again.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "packaging" / "pdf_ocr_embed.spec").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "desktop-build.yml").read_text(
    encoding="utf-8")


def test_spec_collects_the_window_typelibs():
    # Both versions, in pywebview's require() order, into the runtime's path.
    assert '("WebKit2", "4.1")' in SPEC
    assert '("WebKit2", "4.0")' in SPEC
    assert '"gi_typelibs"' in SPEC
    # The tray namespaces, in backend/tray_gtk.py's order.
    assert '("AyatanaAppIndicator3", "0.1")' in SPEC
    assert '("AppIndicator3", "0.1")' in SPEC
    # Dependencies are walked, not hardcoded (WebKit2 needs Soup/JavaScriptCore).
    assert "dependencies" in SPEC


def test_spec_collects_typelibs_only_never_libraries():
    """libwebkit2gtk's .so must stay a system dependency: bundling the library
    without its WebKitWebProcess helpers yields a blank webview.  The helper
    that collects libraries-as-binaries is ``get_gi_typelibs``/``collect_gi_libs``
    — the spec must not use them."""
    assert "get_gi_typelibs" not in SPEC
    assert "collect_gi_libs" not in SPEC
    # The only thing collected is the typelib file itself.
    entries = re.findall(r"found\.append\(\((.*?), \"gi_typelibs\"\)\)", SPEC)
    assert entries == ["info.typelib"]


def test_workflow_fails_a_bundle_without_the_window_typelib():
    check = re.search(
        r"Verify the bundle has the window.*?exit 1", WORKFLOW, re.DOTALL)
    assert check, "the workflow must verify the WebKit2 typelib and exit 1"
    body = check.group(0)
    assert "gi_typelibs/WebKit2-" in body
    # And it is Linux-only (the other platforms have no typelib at all).
    step = WORKFLOW[WORKFLOW.index("Verify the bundle has the window") - 300:]
    assert "runner.os == 'Linux'" in step


def test_workflow_tray_packages_cannot_abort_the_gtk_step():
    """apt with a nonexistent package would kill the whole best-effort step —
    including the pywebview install — so the tray packages are checked first."""
    step = re.search(r"Linux native window.*?pywebview\[gtk\]", WORKFLOW,
                     re.DOTALL).group(0)
    assert "apt-cache show" in step
    assert "gir1.2-webkit2-4.1" in step


def test_spec_drops_the_bundled_gui_runtime():
    """Real incident #2: the hook-collected Ubuntu-built glib in the bundle
    shadowed the system glib, and the system libgstreamer (loaded by the
    system WebKit) died on ``undefined symbol: g_sort_array`` — back to the
    browser.  The GUI runtime must come from the system (which has it, since
    it has WebKit); the spec drops it after Analysis, Linux only."""
    assert "_GUI_RUNTIME_PREFIXES" in SPEC
    for must_drop in ("libglib-2.0.so", "libgtk-3.so", "libpango",
                      "libcairo", "libgio-2.0.so", "libayatana-appindicator"):
        assert f'"{must_drop}"' in SPEC, must_drop
    body = SPEC[SPEC.index("def _drop_gui_runtime"):]
    assert 'sys.platform.startswith("linux")' in body      # not on win/mac
    assert "a.binaries = _drop_gui_runtime(a.binaries)" in SPEC
    # libgirepository must NOT be dropped: only _gi loads it, and it has to
    # match the bundled PyGObject build (distros may ship only 1.0 or 2.0).
    assert "libgirepository" not in SPEC[SPEC.index("_GUI_RUNTIME_PREFIXES"):
                                         SPEC.index("def _drop_gui_runtime")]


def test_gui_runtime_drop_spares_hash_suffixed_libraries():
    """Measured via the smoke test: the prefix list also matched PyInstaller's
    DEDUP-renamed copies (``libxcb-ad31f5a3.so.1.1.0`` — Pillow really does
    vendor libxcb), and dropping them took ``PIL.Image`` down with the whole
    OCR pipeline.  Hashed names must be spared BEFORE the prefix match."""
    body = SPEC[SPEC.index("def _drop_gui_runtime"):]
    hashed = body.index("_HASHED_SUFFIX.search")
    prefix = body.index("bare.startswith(")
    assert hashed < prefix
    assert "-[0-9a-f]{8}" in SPEC


def test_loose_library_drop_is_top_level_only():
    """The staged Tesseract ships its own closure under ``tesseract/lib/`` —
    and the loose top-level duplicates (libtesseract, libcurl, …) carry
    distro-only deps that leak the loader outside the bundle (measured:
    libevent via ldap, libudev via fido2, libselinux via krb5).  Dropping
    them by name must be TOP-LEVEL-only, or the drop kills the staged
    closure it is meant to duplicate."""
    body = SPEC[SPEC.index("def _drop_gui_runtime"):]
    guard = body.index('"/" not in dest')
    match = body.index("bare.startswith(")
    assert guard < match
    for loose in ("libtesseract", "libcurl", "libgnutls", "libkrb5"):
        assert f'"{loose}"' in SPEC
    # codecs PIL/PDF stacks rely on stay bundled
    table = SPEC[SPEC.index("_GUI_RUNTIME_PREFIXES"):
                 SPEC.index("_HASHED_SUFFIX")]
    for must_keep in ("libjpeg", "libpng", "libz.so"):
        assert f'"{must_keep}' not in table, must_keep


def test_workflow_fails_a_bundle_that_vendors_the_gui_runtime():
    check = re.search(r"Verify the bundle.*?::error::The bundle vendors.*?\n",
                      WORKFLOW, re.DOTALL)
    assert check, "the workflow must reject a bundle that still vendors GTK"
    assert "libglib-2.0.so" in check.group(0)
