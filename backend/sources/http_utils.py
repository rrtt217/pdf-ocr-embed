"""Retry + rate-limit helpers for HTTP-based OCR adapters.

Both the unlimited and generic OpenAI adapters previously made a single bare
``httpx`` call per page with no failure resilience.  This module centralises:

* ``should_retry`` — a pure decision helper for which failures are retryable.
* ``post_json_with_retry`` — wraps ``client.post(...)`` with exponential
  backoff + jitter, honouring ``Retry-After`` on 429.
* ``RateLimiter`` — a thread-safe min-interval limiter so API rate can be
  capped regardless of concurrency.

Redaction safety: this module never logs request bodies, prompts or API keys —
the adapter logs only ``url`` + ``model`` and the response status/latency.
"""
from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from typing import Callable, Optional

import httpx

log = logging.getLogger(__name__)

# Status codes treated as transient and worth retrying.  408/425/429 are
# client-triggered but recoverable; every 5xx is a server error we retry.
# 4xx auth/validation errors (400/401/403/404/...) are NOT retryable.
_RETRYABLE_STATUS = frozenset((408, 425, 429) + tuple(range(500, 600)))

# The errors httpx raises for a network-level failure (connection refused,
# timeout, DNS, ...).  We retry these, but never retry request-body corruptions.
_TRANSPORT_ERROR = httpx.TransportError


def should_retry(
    status_code: Optional[int],
    exception: Optional[BaseException],
    method: str = "POST",
) -> bool:
    """Decide whether a failed HTTP attempt is worth retrying.

    Args:
        status_code: The HTTP status of the response, or ``None`` when the
            failure was a raised exception (no response produced).
        exception: The exception raised by the transport, or ``None`` if we got
            a response back.
        method: The HTTP method used (informational; kept for symmetry with the
            signature and future idempotency checks).

    Returns:
        ``True`` for 429/408/425/5xx responses and transient transport/network
        errors; ``False`` for 4xx auth/validation errors and non-transport
        exceptions.
    """
    if exception is not None:
        return isinstance(exception, _TRANSPORT_ERROR)
    if status_code is None:
        return False
    return status_code in _RETRYABLE_STATUS


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    """Extract ``Retry-After`` seconds from the response, if present.

    Supports both an integer number of seconds and an HTTP-date value.
    Returns ``None`` when the header is absent or unparseable.
    """
    header = response.headers.get("Retry-After")
    if not header:
        return None
    header = header.strip()
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    # HTTP-date form (RFC 7231 §7.1.3).
    try:
        parsed = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return max(0.0, (parsed.timestamp() - time.time()))


def _backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Exponential backoff with full-width jitter: ``[0, min(base*2^a, max))``."""
    cap = min(base_delay * (2.0 ** attempt), max_delay)
    return random.uniform(0.0, cap)


def post_json_with_retry(
    client: httpx.Client,
    url: str,
    *,
    json: dict,
    headers: dict,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    rate_limiter: Optional["RateLimiter"] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """POST ``json`` to ``url`` with retry/backoff and optional rate limiting.

    The first attempt is made immediately; each subsequent attempt waits for the
    rate limiter (if any) to release a permit, then applies exponential backoff
    (honouring ``Retry-After`` on 429) before issuing the next request.

    Args:
        client: The ``httpx.Client`` to use.
        url: Target URL.
        json: JSON payload (never logged).
        headers: Request headers (never logged).
        max_retries: Number of retries *after* the initial attempt.  Default 3
            (so up to 4 total attempts).  Set 0 to disable retrying.
        base_delay: Base backoff delay in seconds.
        max_delay: Upper bound for the backoff delay in seconds.
        rate_limiter: Optional ``RateLimiter`` to throttle requests with.
        sleep: Injectable sleep for tests; defaults to ``time.sleep``.

    Returns:
        The final ``httpx.Response`` once a non-retryable status is returned.

    Raises:
        httpx.HTTPStatusError: When every attempt returned a retryable error
            status (mirrors the behavior the caller's ``raise_for_status``
            would otherwise produce for the final response).
        RuntimeError: When retries are exhausted due to repeated transport
            errors.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")

    for attempt in range(max_retries + 1):
        if rate_limiter is not None:
            rate_limiter.acquire()

        try:
            response = client.post(url, json=json, headers=headers)
        except _TRANSPORT_ERROR as exc:
            if attempt >= max_retries:
                raise RuntimeError(
                    f"POST {url} failed after {max_retries} retries "
                    f"(transport error): {exc}"
                ) from exc
            delay = _backoff_delay(attempt, base_delay, max_delay)
            log.debug("POST %s transport failure (attempt %d); retrying in %.2fs",
                      url, attempt + 1, delay)
            sleep(delay)
            continue

        if not should_retry(response.status_code, None, "POST"):
            return response

        if attempt >= max_retries:
            # Give up on this retryable status; surface it as the caller would
            # have seen from a single non-retryable request.
            log.warning("POST %s still failing after %d retries "
                        "(status %d); giving up", url, max_retries,
                        response.status_code)
            response.raise_for_status()
            raise RuntimeError(
                f"POST {url} failed after {max_retries} retries "
                f"(status {response.status_code})"
            )

        delay = _parse_retry_after(response)
        if delay is None:
            delay = _backoff_delay(attempt, base_delay, max_delay)
        log.debug("POST %s retryable status %d (attempt %d); retrying in %.2fs",
                  url, response.status_code, attempt + 1, delay)
        sleep(delay)

    raise RuntimeError(f"POST {url} failed after {max_retries} retries")  # pragma: no cover


class RateLimiter:
    """Thread-safe min-interval rate limiter (token-bucket-ish).

    Enforces that two permits are never issued closer than ``min_interval``
    seconds apart.  Safe to share across threads, so a single instance can cap
    API rate regardless of concurrency.
    """

    def __init__(
        self,
        calls_per_second: float,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """Construct a limiter for ``calls_per_second`` requests/sec.

        A ``calls_per_second <= 0`` (or NaN) disables limiting entirely.
        """
        self._sleep = sleep
        if calls_per_second and calls_per_second > 0:
            self.min_interval = 1.0 / float(calls_per_second)
        else:
            self.min_interval = None
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    @property
    def enabled(self) -> bool:
        return self.min_interval is not None

    @classmethod
    def from_requests_per_second(cls, rps: float) -> "RateLimiter":
        """Construct from a "requests per second" rate (float)."""
        return cls(rps)

    def acquire(self) -> None:
        """Block until a permit is available, then consume it."""
        if self.min_interval is None:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                self._sleep(self._next_allowed - now)
                now = self._next_allowed
            self._next_allowed = now + self.min_interval

    def __enter__(self) -> "RateLimiter":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> bool:
        return False
