"""Tests for the plugin's HTTP retry + rate limiter (ocrmypdf_unlimited).

All tests are network-free: httpx.MockTransport stands in for the wire, and
the backoff/retry sleeps are injected (fake ``sleep``) so the suite stays fast.
The retry helpers now live in the standalone plugin (the app's copy was
removed); they are exercised through the plugin's own module.
"""
from __future__ import annotations

import threading
import time

import httpx
import pytest

from ocrmypdf_unlimited.http_retry import (
    RateLimiter,
    _backoff_delay,
    post_json_with_retry,
    should_retry,
)


# --- should_retry truth table ---------------------------------------------

@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 599])
def test_should_retry_true_for_transient(status):
    assert should_retry(status, None, "POST") is True


@pytest.mark.parametrize("status", [200, 201, 204, 400, 401, 403, 404, 422])
def test_should_retry_false_for_ok_and_4xx(status):
    assert should_retry(status, None, "POST") is False


def test_should_retry_transport_error():
    assert should_retry(None, httpx.ConnectTimeout("boom"), "POST") is True
    assert should_retry(None, httpx.NetworkError("boom"), "POST") is True
    assert should_retry(None, httpx.ReadTimeout("boom"), "POST") is True


def test_should_retry_non_transport_exception():
    assert should_retry(None, ValueError("boom"), "POST") is False


# --- backoff math ---------------------------------------------------------

def test_backoff_delay_respects_caps():
    # Full-width jitter => 0 <= delay < cap, cap grows by base*2**attempt.
    for attempt in range(5):
        delay = _backoff_delay(attempt, base_delay=1.0, max_delay=30.0)
        cap = min(1.0 * (2 ** attempt), 30.0)
        assert 0.0 <= delay < cap


def test_backoff_delay_hits_max():
    delay = _backoff_delay(10, base_delay=1.0, max_delay=30.0)
    assert 0.0 <= delay < 30.0


# --- post_json_with_retry: retry-then-succeed -----------------------------

def _make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"ok": True})

    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)

    client = _make_client(handler)
    resp = post_json_with_retry(
        client, "http://x/chat/completions", json={"a": 1},
        headers={"Authorization": "Bearer x"},
        max_retries=3, base_delay=0.001, max_delay=0.1,
        sleep=fake_sleep,
    )
    assert calls["n"] == 3
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(sleeps) == 2  # one backoff between each failed attempt


def test_429_honors_retry_after():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2.5"},
                                  json={"error": "rate limited"})
        return httpx.Response(200, json={"ok": True})

    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)

    client = _make_client(handler)
    resp = post_json_with_retry(
        client, "http://x/chat/completions", json={"a": 1},
        headers={}, max_retries=2, base_delay=30.0, max_delay=120.0,
        sleep=fake_sleep,
    )
    assert resp.status_code == 200
    # The Retry-After header must take precedence over the 30s backoff.
    assert sleeps == [2.5]


def test_retries_exhausted_raises_on_status():
    """Persistent retryable status must surface as an error, not be swallowed."""
    def handler(request):
        return httpx.Response(503, json={"error": "still down"})

    client = _make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        post_json_with_retry(
            client, "http://x/chat/completions", json={}, headers={},
            max_retries=2, base_delay=0.001, max_delay=0.01,
            sleep=lambda sec: None,
        )


def test_retries_exhausted_raises_on_transport():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = _make_client(handler)
    with pytest.raises(RuntimeError, match="failed after 2 retries"):
        post_json_with_retry(
            client, "http://x/chat/completions", json={}, headers={},
            max_retries=2, base_delay=0.001, max_delay=0.01,
            sleep=lambda sec: None,
        )


def test_non_retryable_status_returned_immediately():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _make_client(handler)
    resp = post_json_with_retry(
        client, "http://x/chat/completions", json={}, headers={},
        max_retries=3, sleep=lambda sec: None,
    )
    assert resp.status_code == 401


def test_max_retries_zero_does_not_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    client = _make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        post_json_with_retry(
            client, "http://x/chat/completions", json={}, headers={},
            max_retries=0, sleep=lambda sec: None,
        )
    assert calls["n"] == 1


# --- RateLimiter ----------------------------------------------------------

def test_rate_limiter_never_issues_faster_than_interval():
    rps = 100.0
    limiter = RateLimiter(rps)  # min_interval = 0.01s
    assert limiter.enabled is True

    limiter.acquire()
    t0 = time.monotonic()
    limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.01 - 1e-3


def test_rate_limiter_disabled_when_rps_zero():
    assert RateLimiter(0).enabled is False
    assert RateLimiter(-5).enabled is False
    assert RateLimiter(0).min_interval is None


def test_rate_limiter_thread_safe():
    """A shared limiter never lets two threads through closer than the interval."""
    rps = 500.0  # min_interval = 0.002s
    limiter = RateLimiter(rps)
    timestamps = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        limiter.acquire()
        with lock:
            timestamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    timestamps.sort()
    interval = 1.0 / rps
    for a, b in zip(timestamps, timestamps[1:]):
        assert (b - a) >= interval - 1e-3


def test_rate_limiter_context_manager():
    limiter = RateLimiter(20)
    with limiter as l:
        assert l is limiter
    assert limiter.enabled is True
