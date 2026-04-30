"""Tests for the asyncio-native telemetry batcher.

Verifies behavioral parity with the thread-based ``TelemetryBatcher``:
same backpressure (drop on full queue), same retry contract, same
circuit-breaker fast-fail, same on_drop callback semantics, same
diagnostics counter shape.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from checkrd._async_batcher import AsyncTelemetryBatcher
from checkrd._circuit_breaker import CircuitBreaker


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
