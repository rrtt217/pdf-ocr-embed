"""Page inventory completeness: a page counts as "done" only once its hOCR is
fully written.

ocrmypdf's built-in Tesseract streams ``<n>_ocr_hocr.hocr`` progressively
while it OCRs a page, so a bare file appearing in the folder is NOT a finished
page — parsing a half-written file is what spammed "hOCR parse failed: no
element found: line 12, column 0".  ``page_store.page_numbers`` therefore only
counts pages whose result is complete: a block sidecar, a closed hOCR
document, or ocrmypdf's per-page ``<n>_hocr.json`` completion marker (written
after ``generate_hocr`` returns; also how an intentionally empty null page is
recognized).
"""
from __future__ import annotations

from backend import page_store

_HOCR_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"\n'
    '    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
    '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
    "<title>OCR page</title>\n"
    '<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
    "</head>\n<body>\n"
)


def _truncated_hocr(tmp_path, page_no: int):
    """An hOCR file exactly as it looks WHILE tesseract is still writing it:
    header present, closing tags not yet reached (fails XML parsing at
    "no element found")."""
    f = tmp_path / f"{page_no:06d}_ocr_hocr.hocr"
    f.write_text(_HOCR_HEADER +
                 "<div class='ocr_page' title='bbox 0 0 1000 2000'>\n",
                 encoding="utf-8")
    return f


def _complete_hocr(tmp_path, page_no: int):
    f = tmp_path / f"{page_no:06d}_ocr_hocr.hocr"
    f.write_text(_HOCR_HEADER +
                 "<div class='ocr_page' title='bbox 0 0 1000 2000'>\n"
                 "</div>\n</body>\n</html>\n", encoding="utf-8")
    return f


def _marker(tmp_path, page_no: int):
    """ocrmypdf's per-page completion marker (written after generate_hocr)."""
    m = tmp_path / f"{page_no:06d}_hocr.json"
    m.write_text("{}", encoding="utf-8")
    return m


def test_mid_write_hocr_is_not_counted(tmp_path):
    _truncated_hocr(tmp_path, 7)
    assert page_store.page_numbers(tmp_path) == []


def test_complete_hocr_is_counted(tmp_path):
    _complete_hocr(tmp_path, 3)
    assert page_store.page_numbers(tmp_path) == [3]


def test_nonempty_hocr_with_stale_marker_is_not_counted(tmp_path):
    """A stale ``<n>_hocr.json`` from a PREVIOUS run must not count a page
    whose hOCR is currently being rewritten: a forced re-run truncates and
    streams the hOCR in place while the old marker is still on disk, so the
    closing-tag check (not the marker) decides for non-empty files."""
    _truncated_hocr(tmp_path, 11)
    _marker(tmp_path, 11)
    assert page_store.page_numbers(tmp_path) == []


def test_empty_null_hocr_with_marker_is_counted(tmp_path):
    """ocrmypdf's null page (timeout / empty page) is an EMPTY hOCR file that
    still carries the completion marker — it must count as a done page."""
    (tmp_path / "000012_ocr_hocr.hocr").write_text("", encoding="utf-8")
    _marker(tmp_path, 12)
    assert page_store.page_numbers(tmp_path) == [12]


def test_empty_hocr_without_marker_is_not_counted(tmp_path):
    """An empty file with no marker is 'tesseract just created the file' — not
    a result (would otherwise force an infinite retry loop's sibling: a page
    that can never finish)."""
    (tmp_path / "000013_ocr_hocr.hocr").write_text("", encoding="utf-8")
    assert page_store.page_numbers(tmp_path) == []


def test_sidecar_always_counts(tmp_path):
    (tmp_path / "000014_ocr_hocr.blocks.json").write_text("{}", encoding="utf-8")
    assert page_store.page_numbers(tmp_path) == [14]


def test_mixed_complete_incomplete(tmp_path):
    _complete_hocr(tmp_path, 1)
    _truncated_hocr(tmp_path, 2)
    (tmp_path / "000003_ocr_hocr.blocks.json").write_text("{}", encoding="utf-8")
    (tmp_path / "000004_ocr_hocr.hocr").write_text("", encoding="utf-8")
    _marker(tmp_path, 4)
    assert page_store.page_numbers(tmp_path) == [1, 3, 4]


def test_load_page_never_parses_mid_write_hocr(tmp_path):
    """The WebUI page list (load_page) must not attempt a full XML parse of a
    half-written file — it is excluded from the inventory entirely."""
    _truncated_hocr(tmp_path, 9)
    assert page_store.load_page(tmp_path, 9, persist_missing_sidecar=False) is None


def test_tesseract_line_confidence_is_0_to_1_fraction(tmp_path):
    """ocrmypdf's HocrParser converts the hOCR ``x_wconf`` (0-100) to a
    0.0-1.0 fraction; the derived block ``conf`` must stay on that scale —
    dividing again would collapse every block to 0% or 1% in the WebUI."""
    f = tmp_path / "000002_ocr_hocr.hocr"
    f.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<body>\n'
        "<div class='ocr_page' title='bbox 0 0 1000 2000; ppageno 1'>\n"
        " <p class='ocr_par' title='bbox 100 100 900 200'>\n"
        "  <span class='ocr_line' title='bbox 100 100 900 200'>\n"
        "   <span class='ocrx_word' title='bbox 100 100 300 200; x_wconf 58'>one</span>\n"
        "   <span class='ocrx_word' title='bbox 300 100 500 200; x_wconf 54'>two</span>\n"
        "   <span class='ocrx_word' title='bbox 600 100 900 200; x_wconf 33'>three</span>\n"
        "  </span>\n"
        " </p>\n"
        "</div>\n</body>\n</html>\n",
        encoding="utf-8")
    page, _ = page_store.hocr_to_page(f, page_no=2)
    assert page is not None
    assert len(page["blocks"]) == 1
    # (58 + 54 + 33) / 3 = 48.33… -> 0.48 on the 0..1 scale.
    assert page["blocks"][0]["conf"] == 0.48


# --- crash artifacts: every counted page must be loadable --------------------

_COMPLETE_HOCR = _HOCR_HEADER + (
    "<div class='ocr_page' title='bbox 0 0 1000 2000; ppageno 2'>\n"
    " <p class='ocr_par' title='bbox 100 100 900 200'>\n"
    "  <span class='ocr_line' title='bbox 100 100 900 200'>\n"
    "   <span class='ocrx_word' title='bbox 100 100 300 200'>hi</span>\n"
    "  </span>\n"
    " </p>\n"
    "</div>\n</body>\n</html>\n"
)
_SIDECAR = ('{"page": {"page_index": 2, "width": 1000, "height": 2000, '
            '"blocks": []}, "dpi": 300}')


def _write(tmp_path, page_no: int, *, hocr: str | None, sidecar: str | None):
    if hocr is not None:
        (tmp_path / f"{page_no:06d}_ocr_hocr.hocr").write_text(
            hocr, encoding="utf-8")
    if sidecar is not None:
        (tmp_path / f"{page_no:06d}_ocr_hocr.blocks.json").write_text(
            sidecar, encoding="utf-8")


def test_a_truncated_sidecar_still_counts_when_the_hocr_is_complete(tmp_path):
    """The hOCR is the reproducible source: a damaged sidecar is rebuilt."""
    _write(tmp_path, 3, hocr=_COMPLETE_HOCR, sidecar=_SIDECAR[: len(_SIDECAR) // 2])

    assert page_store.page_numbers(tmp_path) == [3]
    page = page_store.load_page(tmp_path, 3)      # repaired from the hOCR
    assert page is not None and page["page_index"] == 2
    # ...and the repair was persisted, so the next load reads a valid sidecar.
    assert page_store.page_numbers(tmp_path) == [3]
    assert page_store._sidecar_complete(
        page_store.sidecar_path(tmp_path, 3)) is True


def test_a_truncated_hocr_is_not_rescued_by_its_sidecar(tmp_path):
    """A half-written hOCR must stay re-runnable, sidecar or not.

    The sidecar cannot vouch for the page: a stale one from an earlier run
    would mask a page that is being re-OCR'd right now (exactly the state a
    crash leaves behind).  So the page is NOT counted as done, and "retry
    remaining" re-runs it.
    """
    _write(tmp_path, 3, hocr=_COMPLETE_HOCR[: len(_COMPLETE_HOCR) // 2],
           sidecar=_SIDECAR)

    assert page_store.page_numbers(tmp_path) == []


def test_every_counted_page_is_loadable(tmp_path):
    """The invariant that keeps a page repairable: counted => readable.

    A page counted as done but unreadable is invisible in the editor AND
    skipped by every retry, so nothing could ever repair it.  ``load_page``
    may be MORE permissive than the inventory (a valid sidecar is still
    readable while its page waits to be re-OCR'd) — that direction is harmless.
    """
    _write(tmp_path, 1, hocr=_COMPLETE_HOCR, sidecar=_SIDECAR)
    _write(tmp_path, 2, hocr=_COMPLETE_HOCR,
           sidecar=_SIDECAR[: len(_SIDECAR) // 2])
    _write(tmp_path, 3, hocr=_COMPLETE_HOCR[: len(_COMPLETE_HOCR) // 2],
           sidecar=_SIDECAR)
    _write(tmp_path, 4, hocr=None, sidecar=_SIDECAR)
    _write(tmp_path, 5, hocr=None, sidecar=_SIDECAR[: len(_SIDECAR) // 2])

    counted = set(page_store.page_numbers(tmp_path))
    unreadable = {n for n in range(1, 6)
                  if page_store.load_page(tmp_path, n) is None}
    assert counted == {1, 2, 4}, counted
    assert counted.isdisjoint(unreadable), (
        f"counted as done but unreadable: {counted & unreadable}")


def test_a_sidecar_only_page_counts_when_it_is_fully_written(tmp_path):
    """Sidecar-only engines: the file is the result, but only when intact."""
    _write(tmp_path, 4, hocr=None, sidecar=_SIDECAR)
    assert page_store.page_numbers(tmp_path) == [4]

    _write(tmp_path, 5, hocr=None, sidecar=_SIDECAR[: len(_SIDECAR) // 2])
    _write(tmp_path, 6, hocr=None, sidecar="")
    assert page_store.page_numbers(tmp_path) == [4]
    # A truncated sidecar-only page has no result at all — and is not readable.
    assert page_store.load_page(tmp_path, 5) is None
    assert page_store.load_page(tmp_path, 6) is None


def test_a_complete_page_still_counts_normally(tmp_path):
    _write(tmp_path, 7, hocr=_COMPLETE_HOCR, sidecar=_SIDECAR)
    assert page_store.page_numbers(tmp_path) == [7]
    assert page_store.load_page(tmp_path, 7) is not None
