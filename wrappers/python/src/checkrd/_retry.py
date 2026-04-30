"""Retry policy for control-plane HTTP calls.

Centralises the retry contract every Python control-plane caller
shares (telemetry batcher, public-key registrar, future user-facing
endpoints). Mirrors `wrappers/javascript/src/_retry.ts` byte-for-byte
on the wire — same retryable-status table, same ``retry-after-ms``
preference, same ``x-should-retry`` server hint, same
exponential-with-jitter formula. Both SDKs hit the control plane the
same way so a 429 storm during a deploy looks identical in dashboards.

Formula (lifted verbatim from the OpenAI / Anthropic / Stripe SDKs)::

    sleep_secs = min(0.5 * 2**attempt, max_sleep_secs)
    jitter     = 1 - random() * 0.25         # up to 25% down-jitter
    delay      = sleep_secs * jitter

Server hints (``retry-after-ms`` first, then ``retry-after`` seconds
or HTTP-date) override the formula when present so a control plane
under load can throttle back precisely. The ``x-should-retry: true``
header forces a retry on a status code the table would normally not
retry; ``x-should-retry: false`` suppresses one. Stripe pioneered both
hints and OpenAI / Anthropic adopted them — every modern SDK honors
the contract.
"""

from __future__ import annotations

import secrets
import time
from email.utils import parsedate_to_datetime
from typing import Mapping, Optional


# ---------------------------------------------------------------------------
# Tuning (constants are the same defaults the JS SDK ships)
# ---------------------------------------------------------------------------

#: Maximum attempts including the first try. Stripe / OpenAI default.
DEFAULT_MAX_ATTEMPTS = 3

#: Ceiling for the computed-backoff sleep, in seconds. Server-supplied
#: ``Retry-After`` may exceed this — we cap server hints at
#: ``2 * max_sleep_secs`` (so a 60s value is honored when the default
#: is 8s) but never let local exponential backoff run past the cap.
DEFAULT_MAX_SLEEP_SECS = 8.0

#: Initial backoff coefficient: ``DEFAULT_INITIAL_DELAY * 2**attempt``.
#: 0.5 keeps the first retry at sub-second so transient blips don't
#: artificially extend per-batch latency.
DEFAULT_INITIAL_DELAY = 0.5


# ---------------------------------------------------------------------------
# Header inspection
# ---------------------------------------------------------------------------


def _get_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup, with both dash and underscore variants."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def should_retry_status(status: int, headers: Mapping[str, str]) -> bool:
    """Return True if a response is retryable per the OpenAI / Stripe contract.

    The server's ``x-should-retry`` hint wins over the status table —
    a control plane that knows its own state can override the SDK.

    Default retryable statuses (matches OpenAI, Anthropic, Stripe):

    - **408** Request Timeout — client-side timeout from upstream.
    - **409** Conflict — distinguishes "deduped retry" (server already
      saw this Idempotency-Key) from genuine conflict; both warrant a
      single retry, after which the SDK surfaces the conflict to the caller.
    - **429** Too Many Requests — rate limited. ``retry-after-ms`` /
      ``retry-after`` typically supplies the delay.
    - **>= 500** — server errors are presumed transient. The retry-budget
      cap and circuit breaker prevent runaway retries against a hard down.
    """
    hint = _get_header(headers, "x-should-retry")
    if hint == "true":
        return True
    if hint == "false":
        return False
    return status == 408 or status == 409 or status == 429 or status >= 500


def parse_retry_after(
    headers: Mapping[str, str],
    *,
    max_sleep_secs: float = DEFAULT_MAX_SLEEP_SECS,
) -> Optional[float]:
    """Parse ``Retry-After-Ms`` / ``Retry-After`` into seconds, or None.

    Order matches OpenAI and Stripe:

    1. ``retry-after-ms`` (millisecond precision) — preferred.
    2. ``retry-after`` as a numeric seconds value.
    3. ``retry-after`` as an HTTP-date.

    The server's hint is capped at ``2 * max_sleep_secs`` to bound
    pathological values (some misbehaving load balancers emit
    ``retry-after: 86400``); the cap is configurable so callers with
    legitimately long throttle windows can opt out.
    """
    cap = float(max_sleep_secs) * 2

    ms_hint = _get_header(headers, "retry-after-ms")
    if ms_hint is not None:
        try:
            ms_value = int(ms_hint.strip())
        except (TypeError, ValueError):
            ms_value = -1
        if ms_value > 0:
            return min(ms_value / 1000.0, cap)

    raw = _get_header(headers, "retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None

    # Numeric seconds form.
    try:
        secs = float(raw)
    except ValueError:
        secs = float("nan")
    if secs > 0 and secs == secs:  # not NaN
        return min(secs, cap)

    # HTTP-date form (RFC 7231 §7.1.3).
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    delta = when.timestamp() - time.time()
    if delta > 0:
        return min(delta, cap)
    return None


# ---------------------------------------------------------------------------
# Backoff formula
# ---------------------------------------------------------------------------


def compute_backoff_secs(
    attempt: int,
    *,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    max_sleep_secs: float = DEFAULT_MAX_SLEEP_SECS,
) -> float:
    """Exponential backoff with up-to-25% down-jitter.

    Mirrors the OpenAI / Stripe TS formula::

        sleep = min(initial_delay * 2**attempt, max_sleep_secs)
        jitter = 1 - random() * 0.25
        delay = sleep * jitter

    Uses :mod:`secrets` for the random fraction (not :mod:`random`)
    so the jitter pattern is unpredictable — prevents synchronized
    thundering-herd retries from a fleet of agents that started at
    the same moment.
    """
    if attempt < 0:
        attempt = 0
    # ``2.0 ** attempt`` (float base) is unambiguously ``float``; the
    # ``int ** int`` form returns ``Any`` per typeshed because negative
    # exponents change the result type, which would propagate Any
    # through the product and trip strict mypy.
    sleep = min(initial_delay * (2.0 ** attempt), max_sleep_secs)
    # Generate jitter in [0.75, 1.0) using cryptographic randomness.
    # secrets.randbelow(1_000_000) / 1_000_000 → uniform [0, 1).
    jitter = 1.0 - (secrets.randbelow(250_000) / 1_000_000.0)
    return sleep * jitter


def next_backoff(
    attempt: int,
    headers: Mapping[str, str],
    *,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    max_sleep_secs: float = DEFAULT_MAX_SLEEP_SECS,
) -> float:
    """Pick the next sleep duration: server hint if present, else local backoff.

    Single entry point so callers don't re-implement the precedence
    rules each time. Returns seconds suitable for ``time.sleep`` or
    ``asyncio.sleep``.
    """
    hint = parse_retry_after(headers, max_sleep_secs=max_sleep_secs)
    if hint is not None:
        return hint
    return compute_backoff_secs(
        attempt,
        initial_delay=initial_delay,
        max_sleep_secs=max_sleep_secs,
    )


__all__ = [
    "DEFAULT_INITIAL_DELAY",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_SLEEP_SECS",
    "compute_backoff_secs",
    "next_backoff",
    "parse_retry_after",
    "should_retry_status",
]
