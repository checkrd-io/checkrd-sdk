"""Contract tests for the Instrumentor base class.

These tests use a purpose-built subclass that tracks setup/teardown
calls instead of patching a real library. That way the contract
(idempotency, thread safety, missing-target handling, exception
safety) is verified independently of any particular integration.

The OpenTelemetry instrumentation test suite has a near-identical
shape, and the contracts below mirror theirs. Keeping the API the
same minimizes the learning curve for developers coming from OTel.
"""

from __future__ import annotations

import threading
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from checkrd._state import _GlobalContext
from checkrd.integrations._base import Instrumentor


class _RecordingInstrumentor(Instrumentor):
    """Test double: counts _setup/_teardown calls and lets tests
    configure _setup to raise on demand."""

    _target_module_name: ClassVar[str] = ""  # skip the "target missing" check

    def __init__(self) -> None:
        super().__init__()
        self.setup_calls = 0
        self.teardown_calls = 0
        self.setup_should_raise: Exception | None = None
        self.teardown_should_raise: Exception | None = None
        self.last_context: _GlobalContext | None = None

    def _setup(self, context: _GlobalContext) -> None:
        self.setup_calls += 1
        self.last_context = context
        if self.setup_should_raise is not None:
            raise self.setup_should_raise

    def _teardown(self) -> None:
        self.teardown_calls += 1
        if self.teardown_should_raise is not None:
            raise self.teardown_should_raise


class _MissingTargetInstrumentor(Instrumentor):
    """A subclass that points at a module guaranteed not to exist."""

    _target_module_name: ClassVar[str] = "this_module_definitely_does_not_exist_xyz_123"

    def _setup(self, context: _GlobalContext) -> None:
        raise AssertionError("_setup must not be called when target is missing")

    def _teardown(self) -> None:
        raise AssertionError("_teardown must not be called")


@pytest.fixture
def fake_context() -> _GlobalContext:
    """Provide a MagicMock _GlobalContext for tests that don't need a
    real engine. Avoids touching global init state."""
    return MagicMock(spec=_GlobalContext)


# ============================================================
# Idempotency
# ============================================================


class TestIdempotency:
    def test_instrument_once_calls_setup(self, fake_context) -> None:
        inst = _RecordingInstrumentor()
        inst.instrument(context=fake_context)
        assert inst.setup_calls == 1
        assert inst.instrumented is True

    def test_instrument_twice_is_noop(self, fake_context) -> None:
        inst = _RecordingInstrumentor()
        inst.instrument(context=fake_context)
        inst.instrument(context=fake_context)
        assert inst.setup_calls == 1  # not 2

    def test_uninstrument_without_instrument_is_noop(self) -> None:
        inst = _RecordingInstrumentor()
        inst.uninstrument()  # safe
        assert inst.teardown_calls == 0
        assert inst.instrumented is False

    def test_uninstrument_twice_is_noop(self, fake_context) -> None:
        inst = _RecordingInstrumentor()
        inst.instrument(context=fake_context)
        inst.uninstrument()
        inst.uninstrument()
        assert inst.teardown_calls == 1

    def test_instrument_after_uninstrument_resets(self, fake_context) -> None:
        inst = _RecordingInstrumentor()
        inst.instrument(context=fake_context)
        inst.uninstrument()
        inst.instrument(context=fake_context)
        assert inst.setup_calls == 2
        assert inst.teardown_calls == 1
        assert inst.instrumented is True


# ============================================================
# Missing-target handling
# ============================================================


class TestMissingTarget:
    def test_missing_target_skips_setup(self, fake_context) -> None:
        inst = _MissingTargetInstrumentor()
        inst.instrument(context=fake_context)
        # The assertion in _setup would fire if we reached it.
        # Reaching here without raising is the pass condition.
        assert inst.instrumented is False

    def test_missing_target_debug_logged(
        self,
        fake_context,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        inst = _MissingTargetInstrumentor()
        with caplog.at_level(logging.DEBUG, logger="checkrd"):
            inst.instrument(context=fake_context)
        assert any(
            "not installed" in r.message for r in caplog.records
        ), "expected debug log about missing target"

    def test_missing_target_uninstrument_is_safe(self) -> None:
        inst = _MissingTargetInstrumentor()
        inst.uninstrument()  # no prior instrument() — should no-op


# ============================================================
# Exception safety
# ============================================================


class TestExceptionSafety:
    def test_setup_exception_leaves_uninstrumented(self, fake_context) -> None:
        inst = _RecordingInstrumentor()
        inst.setup_should_raise = RuntimeError("patch failed")
        with pytest.raises(RuntimeError, match="patch failed"):
            inst.instrument(context=fake_context)
        # Must be recoverable: a retry can succeed.
        assert inst.instrumented is False

    def test_retry_after_setup_failure(self, fake_context) -> None:
        inst = _RecordingInstrumentor()
        inst.setup_should_raise = RuntimeError("first attempt")
        with pytest.raises(RuntimeError):
            inst.instrument(context=fake_context)
        # Fix the underlying issue and retry.
        inst.setup_should_raise = None
        inst.instrument(context=fake_context)
        assert inst.instrumented is True
        assert inst.setup_calls == 2

    def test_teardown_exception_still_marks_uninstrumented(
        self,
        fake_context,
    ) -> None:
        # Teardown is best-effort — a buggy subclass should not sticky-
        # lock the instrumentor in the "instrumented" state.
        inst = _RecordingInstrumentor()
        inst.instrument(context=fake_context)
        inst.teardown_should_raise = RuntimeError("teardown failed")
        with pytest.raises(RuntimeError):
            inst.uninstrument()
        assert inst.instrumented is False


# ============================================================
# Context resolution
# ============================================================


class TestContextResolution:
    def test_explicit_context_used(self, fake_context) -> None:
        inst = _RecordingInstrumentor()
        inst.instrument(context=fake_context)
        assert inst.last_context is fake_context

    def test_missing_context_raises_init_error(self) -> None:
        # Without init() and without an explicit context, instrument()
        # should raise CheckrdInitError via get_context().
        from checkrd.exceptions import CheckrdInitError

        inst = _RecordingInstrumentor()
        with pytest.raises(CheckrdInitError, match="init"):
            inst.instrument()


# ============================================================
# Thread safety
# ============================================================


@pytest.mark.slow
@pytest.mark.xdist_group("serial")
class TestThreadSafety:
    """Lock-based serialization: concurrent instrument() calls must
    converge to a single setup. This is the property that lets users
    safely call checkrd.instrument() from multiple worker threads."""

    def test_concurrent_instrument_serializes(self, fake_context) -> None:
        inst = _RecordingInstrumentor()
        n_threads = 16
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            barrier.wait()
            inst.instrument(context=fake_context)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "worker hung"

        # Exactly one setup despite N concurrent calls.
        assert inst.setup_calls == 1
        assert inst.instrumented is True

    def test_concurrent_instrument_and_uninstrument(
        self, fake_context
    ) -> None:
        """Interleaved instrument/uninstrument calls must not deadlock
        and must leave the instrumentor in a consistent terminal state.
        We don't care whether the final state is instrumented or not —
        only that it's well-defined and thread-safe."""
        inst = _RecordingInstrumentor()
        stop = threading.Event()

        def instrument_loop() -> None:
            while not stop.is_set():
                inst.instrument(context=fake_context)

        def uninstrument_loop() -> None:
            while not stop.is_set():
                inst.uninstrument()

        threads = [
            threading.Thread(target=instrument_loop),
            threading.Thread(target=uninstrument_loop),
        ]
        for t in threads:
            t.start()

        import time

        time.sleep(0.1)  # let both workers race for a bit
        stop.set()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive()

        # Terminal state is consistent — bool on instrumented never corrupted.
        assert isinstance(inst.instrumented, bool)


# ============================================================
# Subclass contract
# ============================================================


class TestSubclassContract:
    def test_subclass_must_implement_setup(self, fake_context) -> None:
        class Incomplete(Instrumentor):
            _target_module_name = ""  # bypass target check

        inst = Incomplete()
        with pytest.raises(NotImplementedError, match="_setup"):
            inst.instrument(context=fake_context)

    def test_subclass_must_implement_teardown(self, fake_context) -> None:
        class HalfBaked(Instrumentor):
            _target_module_name = ""

            def _setup(self, context) -> None:
                pass  # minimal impl so instrument() succeeds

        inst = HalfBaked()
        inst.instrument(context=fake_context)
        with pytest.raises(NotImplementedError, match="_teardown"):
            inst.uninstrument()
