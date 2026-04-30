"""Regression tests for the centralised retry helpers in ``checkrd._retry``.

The helpers replace the old per-call-site retry loops in batcher.py and
the public-key registrar. Three properties under test:

  1. Pathological server hints (``Retry-After: 0``,
     ``Retry-After-Ms: 0``) must not crash the batcher worker thread.
  2. Sub-millisecond local backoff inputs must produce valid
     non-negative sleep durations.
  3. The server hint precedence (``retry-after-ms`` > ``retry-after``
     seconds > ``retry-after`` HTTP-date > local exponential) must
     match Stripe / OpenAI behavior.
"""

from __future__ import annotations

import time

from checkrd._retry import (
    DEFAULT_MAX_SLEEP_SECS,
    compute_backoff_secs,
    next_backoff,
    parse_retry_after,
    should_retry_status,
)


# ---------------------------------------------------------------------------
# Server hint parsing
# ---------------------------------------------------------------------------


def test_retry_after_zero_returns_none() -> None:
    """``Retry-After: 0`` is treated as "no hint" — local backoff applies.

    A literal zero is a sentinel some misbehaving load balancers emit;
    honoring it would produce a busy-loop. Returning ``None`` lets the
    fallback exponential backoff fire instead.
    """
    assert parse_retry_after({"Retry-After": "0"}) is None


def test_retry_after_ms_zero_returns_none() -> None:
    """Same guarantee for the millisecond variant."""
    assert parse_retry_after({"Retry-After-Ms": "0"}) is None


def test_retry_after_ms_takes_priority_over_seconds() -> None:
    """Stripe / OpenAI precedence: ``retry-after-ms`` wins."""
    headers = {"Retry-After": "5", "Retry-After-Ms": "750"}
    delay = parse_retry_after(headers, max_sleep_secs=10)
    assert delay is not None
    assert abs(delay - 0.75) < 1e-6


def test_retry_after_seconds_when_no_ms_header() -> None:
    delay = parse_retry_after({"Retry-After": "2"}, max_sleep_secs=10)
    assert delay is not None
    assert abs(delay - 2.0) < 1e-6


def test_retry_after_caps_pathological_value() -> None:
    """Server says wait an hour; we cap at ``2 * max_sleep_secs``."""
    delay = parse_retry_after({"Retry-After": "3600"}, max_sleep_secs=8)
    assert delay == 16.0  # 2 * 8


def test_retry_after_http_date_form() -> None:
    """RFC 7231 §7.1.3 HTTP-date form is honored."""
    future_ts = int(time.time()) + 3
    # Format: ``Sun, 06 Nov 1994 08:49:37 GMT``
    from email.utils import formatdate

    headers = {"Retry-After": formatdate(future_ts, usegmt=True)}
    delay = parse_retry_after(headers, max_sleep_secs=10)
    assert delay is not None
    # ~3s minus a few ms of test overhead.
    assert 2.0 <= delay <= 3.5


def test_retry_after_garbage_value_returns_none() -> None:
    """Unparseable headers fall through to local backoff."""
    assert parse_retry_after({"Retry-After": "not-a-number"}) is None


def test_no_retry_after_header_returns_none() -> None:
    assert parse_retry_after({}) is None


# ---------------------------------------------------------------------------
# Status table + x-should-retry override
# ---------------------------------------------------------------------------


def test_should_retry_default_table() -> None:
    """408 / 409 / 429 / 5xx are retryable; 4xx otherwise are not."""
    for status in (408, 409, 429, 500, 502, 503, 504):
        assert should_retry_status(status, {}), f"{status} should be retryable"
    for status in (400, 401, 403, 404, 422):
        assert not should_retry_status(status, {}), f"{status} should NOT retry"


def test_x_should_retry_true_overrides_table() -> None:
    """``x-should-retry: true`` forces a retry even on a normally-fatal 401."""
    assert should_retry_status(401, {"X-Should-Retry": "true"})


def test_x_should_retry_false_overrides_table() -> None:
    """``x-should-retry: false`` suppresses an otherwise-retryable 503."""
    assert not should_retry_status(503, {"X-Should-Retry": "false"})


def test_should_retry_is_case_insensitive() -> None:
    assert should_retry_status(401, {"x-should-retry": "true"})


# ---------------------------------------------------------------------------
# Backoff formula
# ---------------------------------------------------------------------------


def test_compute_backoff_grows_exponentially() -> None:
    """Each retry roughly doubles the base sleep, modulo jitter."""
    samples = [
        compute_backoff_secs(attempt, initial_delay=0.5, max_sleep_secs=8.0)
        for attempt in range(4)
    ]
    # Lower bounds are 0.75 * (initial_delay * 2**attempt).
    expected_lower = [0.5 * (2 ** a) * 0.75 for a in range(4)]
    for got, lo in zip(samples, expected_lower):
        assert got >= lo - 1e-6, f"{got} below floor {lo}"


def test_compute_backoff_caps_at_max() -> None:
    """Past the saturation point the cap holds (modulo jitter)."""
    delay = compute_backoff_secs(20, initial_delay=0.5, max_sleep_secs=8.0)
    assert delay <= 8.0


def test_compute_backoff_is_jittered() -> None:
    """Two calls at the same attempt should rarely return identical values.

    Jitter is up to 25% — the probability of two calls in a row landing
    on the same float is vanishingly small. If the assertion ever flakes
    we have bigger problems with :mod:`secrets`.
    """
    a = compute_backoff_secs(3)
    b = compute_backoff_secs(3)
    # At least one of the two pairs should differ.
    c = compute_backoff_secs(3)
    assert {a, b, c} != {a}, "jitter appears to be missing"


def test_next_backoff_prefers_server_hint() -> None:
    """When the server says wait, we wait that long — not the local formula."""
    delay = next_backoff(
        attempt=0,
        headers={"Retry-After-Ms": "1234"},
        max_sleep_secs=DEFAULT_MAX_SLEEP_SECS,
    )
    assert abs(delay - 1.234) < 1e-6


def test_next_backoff_falls_back_to_local() -> None:
    """No server hint → local exponential applies."""
    delay = next_backoff(attempt=0, headers={})
    # First retry sits in [0.375, 0.5] for the default initial_delay=0.5.
    assert 0.0 < delay <= 0.5
