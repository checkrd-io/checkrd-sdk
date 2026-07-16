"""Tests for the asyncio-native telemetry batcher.

Verifies behavioral parity with the thread-based ``TelemetryBatcher``:
same backpressure (drop on full queue), same retry contract, same
circuit-breaker fast-fail, same on_drop callback semantics, same
diagnostics counter shape.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from checkrd._async_batcher import AsyncTelemetryBatcher
from checkrd._circuit_breaker import CircuitBreaker


async def _await_until(cond: Any, *, timeout: float = 5.0, poll: float = 0.01) -> None:
    """Async analogue of ``tests.conftest.wait_for``.

    Polls ``cond()`` without blocking the event loop (``await asyncio.sleep``
    rather than ``time.sleep``, which would stall the very worker task under
    test). Succeeds as soon as the condition is met.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(poll)
    raise AssertionError(f"condition not met within {timeout}s")


def _mock_engine() -> MagicMock:
    """An engine that always signs successfully."""
    engine = MagicMock()
    engine.sign_telemetry_batch.return_value = {
        "content_digest": "sha-256=:Xy=:",
        "signature_input": "sig1=()",
        "signature": "sig1=:abc:",
    }
    return engine


def _sample_event() -> dict[str, Any]:
    return {
        "event_id": "req-1",
        "agent_id": "test-agent",
        "request": {"url_host": "api.openai.com", "url_path": "/v1/chat/completions"},
    }


def _accepting_transport() -> httpx.MockTransport:
    """Returns 200 for every request — happy path."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": True})

    return httpx.MockTransport(handler)


def _rejecting_transport(status: int) -> httpx.MockTransport:
    """Returns the supplied status for every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "fail"}})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_enqueue_and_flush_sends_batch() -> None:
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            http_client=client,
        )
        batcher.enqueue(_sample_event())
        batcher.enqueue(_sample_event())
        await batcher.flush()
        assert batcher.events_sent == 2
        assert batcher.diagnostics()["sent"] == 2
        await batcher.stop()


@pytest.mark.asyncio
async def test_backpressure_drops_when_queue_full() -> None:
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            max_queue_size=2,
            http_client=client,
        )
        batcher.enqueue(_sample_event())
        batcher.enqueue(_sample_event())
        batcher.enqueue(_sample_event())  # dropped
        assert batcher.diagnostics()["dropped_backpressure"] == 1
        await batcher.stop()


@pytest.mark.asyncio
async def test_signing_error_drops_with_signing_error_label() -> None:
    """Engine returns ``None`` from sign — batch dropped, NOT sent unsigned."""
    engine = MagicMock()
    engine.sign_telemetry_batch.return_value = None  # signing unavailable
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=engine,
            signer_agent_id="agent",
            http_client=client,
        )
        batcher.enqueue(_sample_event())
        await batcher.flush()
        diag = batcher.diagnostics()
        assert diag["dropped_signing_error"] == 1
        assert diag["sent"] == 0
        await batcher.stop()


@pytest.mark.asyncio
async def test_circuit_breaker_fast_fails_when_open() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_after_secs=60)
    breaker.record_failure()  # opens the circuit
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            circuit_breaker=breaker,
            http_client=client,
        )
        batcher.enqueue(_sample_event())
        await batcher.flush()
        diag = batcher.diagnostics()
        # Fast-failed without hitting the network.
        assert diag["dropped_send_error"] == 1
        assert diag["sent"] == 0
        await batcher.stop()


@pytest.mark.asyncio
async def test_owns_client_lifecycle() -> None:
    """When the batcher creates its own httpx.AsyncClient it MUST close
    it on stop. When the caller supplies one, the batcher must NOT
    close it (the caller owns lifecycle)."""
    # Caller-supplied client — explicit `async with` makes the lifecycle
    # contract self-evident at the call site.
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            http_client=client,
        )
        await batcher.stop()
        # Client should still be usable inside the context manager.
        assert not client.is_closed
    # Caller's context manager closes the client on exit.


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            http_client=client,
        )
        await batcher.stop()
        await batcher.stop()  # must not raise


@pytest.mark.asyncio
async def test_diagnostics_shape_matches_sync_batcher() -> None:
    """Same five keys as ``TelemetryBatcher.diagnostics()`` so dashboards
    can read either runtime's batcher uniformly."""
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            http_client=client,
        )
        diag = batcher.diagnostics()
        assert set(diag.keys()) == {
            "sent",
            "dropped_backpressure",
            "dropped_signing_error",
            "dropped_send_error",
            "pending",
        }
        await batcher.stop()


@pytest.mark.asyncio
async def test_run_loop_survives_non_serializable_event() -> None:
    """The async worker task must survive a batch it can't serialize.

    Parity with ``TelemetryBatcher._run``: ``_send`` flattens + ``json.dumps``es
    + signs the batch before any of its own handlers run. A non-serializable
    event (a ``before_send`` that stamped a ``set``/``datetime``) would escape
    ``_run`` and kill the worker task, silently stopping ALL telemetry for the
    event loop's lifetime.
    """

    def inject_bad(event: dict[str, Any], _hint: dict[str, object]) -> dict[str, Any]:
        if event.get("event_id") == "req-poison":
            event = dict(event)
            event["latency_ms"] = {1, 2, 3}  # a set → not JSON serializable
        return event

    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            batch_size=1,  # each enqueue triggers the background loop to flush
            before_send=inject_bad,
            http_client=client,
        )
        batcher.start()
        task = batcher._task
        assert task is not None

        # Poison event → the background _run task drains it and _send raises
        # TypeError inside json.dumps. The guard must catch it and keep the
        # task running.
        batcher.enqueue({**_sample_event(), "event_id": "req-poison"})
        await _await_until(lambda: batcher.diagnostics()["dropped_send_error"] >= 1)
        assert not task.done()  # worker survived — same task still looping

        # The loop survived: a subsequent good event still sends.
        batcher.enqueue({**_sample_event(), "event_id": "req-good"})
        await _await_until(lambda: batcher.events_sent >= 1)

        await batcher.stop()


@pytest.mark.asyncio
async def test_background_flush_fires_without_explicit_start_or_stop() -> None:
    """The periodic flush loop must run on the async path with NO explicit
    start()/flush()/stop() from the caller.

    Regression: nothing ever called ``AsyncTelemetryBatcher.start()``, so the
    default async path (``use_async_batcher=True``) buffered telemetry in
    memory and shipped nothing until ``aclose()``. A long-running async
    service would accumulate to ``max_queue_bytes`` then silently drop. This
    drives a single enqueue below ``batch_size`` — so ONLY the flush interval
    can trigger a send — and asserts a background flush ships it, before any
    ``stop()`` / ``aclose()``.
    """
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            batch_size=100,  # large: the batch-size trigger can never fire here
            flush_interval_secs=0.05,  # only the interval can flush the one event
            http_client=client,
        )
        batcher.enqueue(_sample_event())
        # No start(), no flush(), no stop(): the loop must lazy-start on enqueue
        # and drain on flush_interval. ``_await_until`` polls with asyncio.sleep
        # (never time.sleep, which would stall the very worker task under test).
        await _await_until(lambda: batcher.events_sent >= 1, timeout=5.0)
        assert batcher.events_sent == 1
        assert batcher.pending_count == 0
        await batcher.stop()  # cleanup only — the assertion already passed


@pytest.mark.asyncio
async def test_stop_final_flush_survives_non_serializable_event() -> None:
    """``stop()``'s final flush must sit inside the same poison guard as
    ``_run``.

    A non-serializable value still buffered at shutdown (a ``set`` / a
    ``datetime`` stamped by ``before_send``) must be dropped + counted, never
    raised out of ``stop()`` / ``aclose()``. The event is buffered directly
    (bypassing ``enqueue``) so the background loop never drains it first — the
    final flush in ``stop()`` is the path under test.
    """
    async with httpx.AsyncClient(transport=_accepting_transport()) as client:
        batcher = AsyncTelemetryBatcher(
            base_url="https://api.checkrd.io",
            api_key="ck_test",
            engine=_mock_engine(),
            signer_agent_id="agent",
            http_client=client,
        )
        # A set is not JSON-serializable → ``_send`` raises TypeError inside
        # json.dumps. Buffer it directly so no ``_run`` task drains it before
        # ``stop()``'s final flush runs.
        batcher._buffer.append({**_sample_event(), "latency_ms": {1, 2, 3}})
        await batcher.stop()  # must NOT raise
        assert batcher.diagnostics()["dropped_send_error"] == 1
        assert batcher.events_sent == 0
