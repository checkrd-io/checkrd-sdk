"""Circuit breaker for control-plane HTTP calls.

Mirrors `wrappers/javascript/src/_circuit_breaker.ts` byte-for-byte —
same three states (``closed`` / ``open`` / ``half_open``), same
defaults (5 consecutive failures opens; 30 s reset window). One
breaker instance can be shared across the batcher and the public-key
registrar so a single sustained outage of the control plane fast-fails
both call sites instead of every caller racing through its own retry
budget.

Why a circuit breaker matters:

  - Without one, a control-plane outage means every caller pays the
    full retry budget on every attempt. With ``DEFAULT_MAX_ATTEMPTS=3``
    and ``DEFAULT_MAX_SLEEP_SECS=8``, that's ~24 seconds of latency on
    every batcher flush during an outage — unacceptable in async hot
    paths.
  - Open → half-open → closed transitions let the breaker probe the
    control plane periodically; one successful probe closes the
    circuit. No background thread or polling required.
  - Per-instance state means tests can construct a breaker with tight
    thresholds and a mock clock.

Thread safety:
  Uses :class:`threading.Lock` because the batcher worker thread and
  the foreground caller can both call ``allow()`` / ``record_*`` from
  different threads. The lock is uncontended in steady state — only
  the failure-burst windows take it, so the cost is negligible.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Optional


#: Three states observable from diagnostics / logs. Closed is the
#: happy path; open means fast-fail; half_open means "let one probe
#: through to see if the control plane is back".
CircuitState = Literal["closed", "open", "half_open"]


@dataclass(frozen=True)
class CircuitBreakerDiagnostics:
    """Snapshot of breaker state for ``Checkrd.healthy()`` exposure.

    Mirrors the JS SDK's ``CircuitBreakerDiagnostics`` interface so a
    multi-runtime fleet exports identically-shaped fields.
    """

    state: CircuitState
    consecutive_failures: int
    opened_at: Optional[float]


class CircuitBreaker:
    """Three-state circuit breaker with thread-safe accounting.

    Wire ``allow()`` before each outbound attempt and
    ``record_success()`` / ``record_failure()`` after::

        if not breaker.allow():
            raise APIConnectionError(message="circuit open")
        try:
            response = make_request(...)
            breaker.record_success()
        except SomeFailure:
            breaker.record_failure()
            raise

    Defaults match the JS SDK so a fleet running both runtimes has
    identical breaker behavior; override per call site for tests.

    Args:
        failure_threshold: Consecutive failures before the circuit
            opens. Default 5 — tuned to ride out a momentary 5xx burst
            without cracking, but to fail fast on a sustained outage.
        reset_after_secs: Time the circuit stays open before admitting
            a probe. Default 30 s — short enough that a brief outage
            recovers quickly, long enough that we don't hammer a still-
            unhealthy backend.
        now: Optional clock override for tests.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_after_secs: float = 30.0,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_after_secs = reset_after_secs
        self._now = now if now is not None else time.monotonic
        self._lock = threading.Lock()
        self._state: CircuitState = "closed"
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None

    def allow(self) -> bool:
        """Return True if the next attempt should proceed.

        Transitions ``open → half_open`` automatically when the reset
        window has elapsed. The half-open probe is admitted; the
        outcome of that probe (recorded via :meth:`record_success` or
        :meth:`record_failure`) decides whether the circuit closes
        again or re-opens.
        """
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "half_open":
                return True
            # state == "open"
            if (
                self._opened_at is not None
                and self._now() - self._opened_at >= self._reset_after_secs
            ):
                self._state = "half_open"
                return True
            return False

    def record_success(self) -> None:
        """Mark the most recent attempt as successful.

        Closes the circuit and resets the failure counter. Safe to
        call even when already closed — the only state change is the
        counter reset, which is idempotent.
        """
        with self._lock:
            self._consecutive_failures = 0
            self._state = "closed"
            self._opened_at = None

    def record_failure(self) -> None:
        """Mark the most recent attempt as failed.

        Opens the circuit if either:
        - The half-open probe failed (one strike — back to open), or
        - The consecutive-failure count crosses the threshold.

        The "half-open probe failed" branch is the safety hatch for a
        control plane that flaps repeatedly: each probe failure
        re-arms the reset timer, so we don't probe in a tight loop.
        """
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._state == "half_open"
                or self._consecutive_failures >= self._failure_threshold
            ):
                self._state = "open"
                self._opened_at = self._now()

    def diagnostics(self) -> CircuitBreakerDiagnostics:
        """Snapshot of breaker state for monitoring and ``healthy()``."""
        with self._lock:
            return CircuitBreakerDiagnostics(
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                opened_at=self._opened_at,
            )

    def reset(self) -> None:
        """Force-close the circuit. Test-only — production callers should
        not need to invoke this; the half-open probe handles recovery."""
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._opened_at = None


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerDiagnostics",
    "CircuitState",
]
