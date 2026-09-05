"""OCRmyPDF plugin hooks for the unlimited-OCR engine.

This module is what makes ``ocrmypdf-unlimited`` a *real* OCRmyPDF plugin per
the [plugin documentation](https://ocrmypdf.readthedocs.io/en/latest/plugins.html):

* :func:`initialize` — verify required dependencies when the plugin is first
  loaded (the right place for "can this plugin even work?" checks; per-run
  validation lives in :func:`check_options`).
* :func:`add_options` — register the engine's own command-line and API
  arguments.  OCRmyPDF mirrors command-line arguments onto API calls, so the
  same ``--unlimited-*`` flags work as keyword arguments to
  ``ocrmypdf.api.ocr()`` / ``_pdf_to_hocr()`` (the values land on
  ``options.unlimited_*``).
* :func:`check_options` — validate the plugin's options before a run starts.

The hooks are also exported from the package ``__init__`` so the plugin loads
both as a script plugin (``--plugin ocrmypdf_unlimited``) and as a packaged /
entry-point plugin (``[project.entry-points."ocrmypdf"]``).
"""
from __future__ import annotations

import logging

from ocrmypdf import hookimpl
from ocrmypdf.exceptions import ExitCodeException

from ocrmypdf_unlimited import settings as unlimited_settings

log = logging.getLogger(__name__)

#: Hard invariant shared with the client: a single generation budget.
MAX_TOKENS_LIMIT = 32768


def _require_import(name: str, pip_name: str) -> None:
    """Raise ExitCodeException when an optional runtime dependency is absent."""
    try:
        __import__(name)
    except ImportError as exc:
        raise ExitCodeException(
            f"The ocrmypdf-unlimited plugin requires '{pip_name}' "
            f"(pip install {pip_name})."
        ) from exc


@hookimpl
def initialize(plugin_manager) -> None:
    """Check dependencies once, when this plugin is first loaded.

    Called from the main process only — it must not depend on run options; use
    :func:`check_options` for option-dependent problems.
    """
    _require_import("httpx", "httpx")
    _require_import("PIL", "Pillow")
    _require_import("pylatexenc", "pylatexenc")


@hookimpl
def add_options(parser):
    """Register the unlimited engine's own CLI/API arguments.

    These become ``options.unlimited_*`` on OcrOptions and are resolved by
    ``settings.effective()`` with the highest priority.  Every default is
    ``None`` so an absent flag never shadows lower-priority config layers
    (env / host-injected snapshot / client defaults).
    """
    group = parser.add_argument_group("Unlimited-OCR engine (ocrmypdf-unlimited)")
    group.add_argument(
        "--unlimited-api-key", dest="unlimited_api_key", default=None,
        help="API key for the OpenAI-compatible endpoint "
             "(fallback: OCR_UNLIMITED_API_KEY).")
    group.add_argument(
        "--unlimited-base-url", dest="unlimited_base_url", default=None,
        help="Base URL of the OpenAI-compatible endpoint, e.g. "
             "https://api.llm.ustc.edu.cn/v1 (fallback: OCR_UNLIMITED_BASE_URL).")
    group.add_argument(
        "--unlimited-model", dest="unlimited_model", default=None,
        help="Model name to call, e.g. unlimited-ocr "
             "(fallback: OCR_UNLIMITED_MODEL).")
    group.add_argument(
        "--unlimited-max-tokens", dest="unlimited_max_tokens", default=None,
        type=int, metavar="N",
        help="Per-generation output budget (must stay < 32768; default 16384).")
    group.add_argument(
        "--unlimited-batch-size", dest="unlimited_batch_size", default=None,
        type=int, metavar="N",
        help="Pages per multi-image request; 0 (default) disables batching.")
    group.add_argument(
        "--unlimited-batch-timeout-ms", dest="unlimited_batch_timeout_ms",
        default=None, type=int, metavar="MS",
        help="Batch window flush timeout in milliseconds (default 3000).")
    group.add_argument(
        "--unlimited-batch-per-page-tokens", dest="unlimited_batch_per_page_tokens",
        default=None, type=int, metavar="N",
        help="Per-page output budget inside a multi-page batch (default 2048).")
    group.add_argument(
        "--unlimited-max-retries", dest="unlimited_max_retries", default=None,
        type=int, metavar="N",
        help="HTTP retries after the first attempt (default 3).")
    group.add_argument(
        "--unlimited-retry-base-delay", dest="unlimited_retry_base_delay",
        default=None, type=float, metavar="SEC",
        help="Backoff base delay in seconds (default 1.0).")
    group.add_argument(
        "--unlimited-retry-max-delay", dest="unlimited_retry_max_delay",
        default=None, type=float, metavar="SEC",
        help="Backoff ceiling in seconds (default 30.0).")
    group.add_argument(
        "--unlimited-rate-limit-rps", dest="unlimited_rate_limit_rps",
        default=None, type=float, metavar="RPS",
        help="Cap API requests per second; 0 (default) disables limiting.")
    group.add_argument(
        "--unlimited-generate-raw", dest="unlimited_generate_raw",
        default=None, action="store_true",
        help="Keep the engine's raw (pre-normalization) marker content per "
             "block: the block sidecar gains a 'raw' field and the hOCR "
             "gains x_kind/x_raw engine properties on each ocr_par title "
             "(off by default).  The invisible text layer is unaffected.")


@hookimpl
def check_options(options) -> None:
    """Validate the unlimited engine's options before the run starts.

    Hard problems raise ``ExitCodeException`` (the run never starts); soft
    ones are logged as warnings.  Values absent from the CLI/API are simply
    not validated here — they fall back to env / host config.
    """
    cfg = unlimited_settings.from_options(options)

    max_tokens = cfg.get("max_tokens")
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            raise ExitCodeException(
                f"--unlimited-max-tokens must be an integer, got {max_tokens!r}"
            ) from None
        if not 1 <= max_tokens < MAX_TOKENS_LIMIT:
            raise ExitCodeException(
                f"--unlimited-max-tokens must be in 1..{MAX_TOKENS_LIMIT - 1} "
                f"(got {max_tokens}).")

    batch_size = cfg.get("ocr_batch_size")
    if batch_size is not None:
        try:
            batch_size = int(batch_size)
        except (TypeError, ValueError):
            raise ExitCodeException(
                f"--unlimited-batch-size must be an integer, got {batch_size!r}"
            ) from None
        if batch_size < 0:
            raise ExitCodeException(
                f"--unlimited-batch-size must be >= 0 (got {batch_size}).")

    per_page = cfg.get("ocr_batch_per_page_tokens")
    if per_page is not None:
        try:
            per_page = int(per_page)
        except (TypeError, ValueError):
            raise ExitCodeException(
                "--unlimited-batch-per-page-tokens must be an integer, "
                f"got {per_page!r}"
            ) from None
        if not 1 <= per_page < MAX_TOKENS_LIMIT:
            raise ExitCodeException(
                "--unlimited-batch-per-page-tokens must be in "
                f"1..{MAX_TOKENS_LIMIT - 1} (got {per_page}).")

    for dest in ("unlimited_retry_base_delay", "unlimited_retry_max_delay",
                 "unlimited_rate_limit_rps"):
        value = cfg.get(unlimited_settings._OPTION_KEYS[dest])  # noqa: SLF001
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ExitCodeException(
                f"--{dest.replace('_', '-')} must be a number, got {value!r}"
            ) from None
        if value < 0:
            raise ExitCodeException(
                f"--{dest.replace('_', '-')} must be >= 0 (got {value}).")

    if not cfg.get("api_key") and log.isEnabledFor(logging.WARNING):
        log.warning(
            "unlimited-OCR engine: no API key configured "
            "(--unlimited-api-key / OCR_UNLIMITED_API_KEY); "
            "the run will fail when a page is processed.")
