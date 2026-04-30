"""Tests for the control-plane circuit breaker.

Mirrors `wrappers/javascript/tests/circuit_breaker.test.ts` so the
state-machine semantics match across runtimes.
"""

from __future__ import annotations

from checkrd._circuit_breaker import CircuitBreaker


class _MockClock:
    """Monotonic-style clock with a writable ``now`` field, for tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def test_starts_closed_and_allows() -> None:
    breaker = CircuitBreaker()
    assert breaker.allow() is True
    assert breaker.diagnostics().state == "closed"
    assert breaker.diagnostics().consecutive_failures == 0


def test_opens_after_threshold_failures() -> None:
    """Breaker tracks consecutive failures and opens at the threshold."""
    clock = _MockClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_after_secs=10, now=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.diagnostics().state == "closed"
    breaker.record_failure()  # third failure
    assert breaker.diagnostics().state == "open"
    assert breaker.allow() is False


def test_success_resets_failure_counter() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.diagnostics().consecutive_failures == 0
    assert breaker.diagnostics().state == "closed"


def test_open_to_half_open_after_reset_window() -> None:
    """After ``reset_after_secs`` the next ``allow()`` returns True
    and transitions the breaker to half-open for one probe."""
    clock = _MockClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_after_secs=30, now=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow() is False
    clock.advance(30.0)
    assert breaker.allow() is True
    assert breaker.diagnostics().state == "half_open"


def test_half_open_success_closes_circuit() -> None:
    clock = _MockClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_after_secs=10, now=clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(10.0)
    assert breaker.allow() is True  # transitions to half_open
    breaker.record_success()
    assert breaker.diagnostics().state == "closed"
    assert breaker.allow() is True


def test_half_open_failure_re_opens_circuit() -> None:
    """A single failure in half-open re-opens the breaker — we don't
    require N consecutive failures from half-open, the probe is the
    canary."""
    clock = _MockClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_after_secs=10, now=clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(10.0)
    assert breaker.allow() is True  # half_open
    breaker.record_failure()
    assert breaker.diagnostics().state == "open"


def test_reset_returns_to_initial_state() -> None:
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.diagnostics().state == "open"
    breaker.reset()
    assert breaker.diagnostics().state == "closed"
    assert breaker.diagnostics().consecutive_failures == 0


def test_diagnostics_snapshot_is_immutable() -> None:
    """``CircuitBreakerDiagnostics`` is a frozen dataclass — readers
    can't accidentally mutate it and corrupt breaker state."""
    breaker = CircuitBreaker()
    snap = breaker.diagnostics()
    try:
        snap.consecutive_failures = 99  # type: ignore[misc]
    except Exception:
        # FrozenInstanceError or AttributeError both acceptable.
        return
    raise AssertionError("diagnostics should be frozen")
