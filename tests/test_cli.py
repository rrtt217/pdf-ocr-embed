"""Tests for backend.cli (headless CLI).

Only pure/logic and subprocess smoke tests live here — no real OCR engine and
no network is required.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.cli import parse_pages

ROOT = Path(__file__).resolve().parents[1]
# Interpreter running the tests (the venv python when run inside .venv) —
# never hardcode a machine-specific absolute path here.
VENV_PY = sys.executable


# --- parse_pages -------------------------------------------------------------


def test_parse_pages_none_means_all():
    assert parse_pages(None, 10) == list(range(10))


def test_parse_pages_single():
    assert parse_pages("1", 5) == [0]
    assert parse_pages("5", 5) == [4]


def test_parse_pages_range():
    assert parse_pages("2-4", 6) == [1, 2, 3]


def test_parse_pages_mix():
    # 1-based, inclusive; preserves ascending, de-duplicated 0-based output.
    assert parse_pages("1,3,5-7", 10) == [0, 2, 4, 5, 6]


def test_parse_pages_open_ended_forward():
    assert parse_pages("3-", 6) == [2, 3, 4, 5]


def test_parse_pages_open_ended_backward():
    assert parse_pages("-3", 6) == [0, 1, 2]


def test_parse_pages_clamps_out_of_range():
    assert parse_pages("0,3,99", 5) == [0, 2, 4]
    # Fully out-of-range bounds clamp to the last page.
    assert parse_pages("100-200", 8) == [7]
    # A range that only partly overlaps clamps to the valid window.
    assert parse_pages("0-3", 5) == [0, 1, 2]
    assert parse_pages("4-9", 5) == [3, 4]


def test_parse_pages_dedupes():
    assert parse_pages("1-3,2,3", 5) == [0, 1, 2]


def test_parse_pages_empty_invalid():
    with pytest.raises(ValueError):
        parse_pages("", 5)
    with pytest.raises(ValueError):
        parse_pages("  ", 5)
    with pytest.raises(ValueError):
        parse_pages("1,,3", 5)


def test_parse_pages_reversed_range_raises():
    with pytest.raises(ValueError):
        parse_pages("5-2", 6)


# --- smoke tests --------------------------------------------------------------


def test_cli_module_imports_headless():
    # ``import backend.cli`` must work with no server running.
    import backend.cli  # noqa: F401


def test_cli_help_exits_zero_via_main():
    from backend.cli import main
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_cli_help_subprocess():
    result = subprocess.run(
        [VENV_PY, "-m", "backend.cli", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--adapter" in result.stdout
    assert "--pages" in result.stdout


def test_cli_adapter_list_no_network():
    result = subprocess.run(
        [VENV_PY, "-m", "backend.cli", "does_not_exist.pdf", "--adapter", "list"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    names = result.stdout.strip().splitlines()
    assert "unlimited" in names
    assert "tesseract" in names
