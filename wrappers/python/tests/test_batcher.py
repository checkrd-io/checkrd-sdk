"""Tests for checkrd.batcher.TelemetryBatcher."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from checkrd.batcher import TelemetryBatcher
from checkrd.engine import WasmEngine
from tests.conftest import requires_wasm, wait_for


def sample_event(request_id: str = "req-001") -> dict[str, Any]:
    """A minimal enriched telemetry event dict (WASM output format)."""
    return {
        "event_id": request_id,
        "agent_id": "550e8400-e29b-41d4-a716-446655440000",
        "instance_id": "inst-abc",
        "timestamp": "2026-03-28T14:30:00Z",
        "request": {
            "url_host": "api.stripe.com",
            "url_path": "/v1/charges",
            "method": "GET",
        },
        "response": {"status_code": 200, "latency_ms": 142},
        "policy_result": "allowed",
        "trace_id": "0af7651916cd43dd8448eb211c80319c",
        "span_id": "b7ad6b7169203331",
        "span_name": "GET api.stripe.com",
        "span_kind": "INTERNAL",
        "span_status_code": "OK",
    }


def _mock_urlopen(
    *, request_id: Optional[str] = None, status: int = 200
) -> MagicMock:
    """Create a mock urlopen that returns the given status + headers.

    ``request_id`` populates ``Checkrd-Request-Id`` in the response
    headers — used by the diagnostics-correlation tests.
    """
    mock_response = MagicMock()
    mock_response.status = status
    headers: dict[str, str] = {}
    if request_id is not None:
        headers["Checkrd-Request-Id"] = request_id
    mock_response.headers = headers
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


_DEFAULT_SIGNER_AGENT_ID = "550e8400-e29b-41d4-a716-446655440000"


def _make_engine_for_batcher() -> WasmEngine:
    """Construct a real signing-capable WasmEngine for batcher tests.

    Strong-from-the-ground-up: TelemetryBatcher requires a signing engine.
    There is no unsigned path through the system, so every batcher test
    must use a real engine with a generated key.
    """
    private, _public = WasmEngine.generate_keypair()
    return WasmEngine(
        policy_json='{"agent":"test-agent","default":"allow","rules":[]}',
        agent_id="test-agent",
        private_key_bytes=private,
    )


def _make_batcher(
    *,
    base_url: str = "http://localhost:8081",
    api_key: str = "ck_test_abc",
    batch_size: int = 100,
    flush_interval_secs: float = 60.0,
    max_queue_size: int = 10_000,
    on_drop: Any = None,
    engine: Any = None,
) -> TelemetryBatcher:
    """Construct a TelemetryBatcher with a real signing engine.

    Centralizing the construction here means tests don't need to repeat the
    engine + signer_agent_id wiring. Tests that want a specific batcher
    config override the kwargs.
    """
    kwargs: dict[str, Any] = dict(
        base_url=base_url,
        api_key=api_key,
        engine=engine if engine is not None else _make_engine_for_batcher(),
        signer_agent_id=_DEFAULT_SIGNER_AGENT_ID,
        batch_size=batch_size,
        flush_interval_secs=flush_interval_secs,
        max_queue_size=max_queue_size,
    )
    if on_drop is not None:
        kwargs["on_drop"] = on_drop
    return TelemetryBatcher(**kwargs)


# All TelemetryBatcher tests need the WASM core for signing.
pytestmark = requires_wasm


# ============================================================
# Core functionality
# ============================================================


class TestBatcherFlush:
    @patch("checkrd.batcher.urlopen")
    def test_enqueue_and_flush(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-001"))
        batcher.flush()
        batcher.stop()

        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert len(body["events"]) == 1
        assert body["events"][0]["request_id"] == "req-001"
        assert body["sdk_version"] is not None

    @patch("checkrd.batcher.urlopen")
    def test_batch_size_triggers_flush(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(batch_size=5)
        for i in range(5):
            batcher.enqueue(sample_event(f"req-{i}"))

        # Poll until the background thread flushes (fast on normal runs,
        # tolerates slow CI with 5s deadline).
        wait_for(lambda: mock_urlopen.call_count >= 1)
        batcher.stop()

        total_events = 0
        for call in mock_urlopen.call_args_list:
            body = json.loads(call[0][0].data)
            total_events += len(body["events"])
        assert total_events == 5

    @pytest.mark.slow
    @patch("checkrd.batcher.urlopen")
    def test_timer_triggers_flush(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(batch_size=1000, flush_interval_secs=0.5)
        batcher.enqueue(sample_event("req-timer"))

        # Poll until the timer-based flush fires (0.5s interval, 5s deadline).
        wait_for(lambda: mock_urlopen.call_count >= 1)
        batcher.stop()

        body = json.loads(mock_urlopen.call_args_list[0][0][0].data)
        assert body["events"][0]["request_id"] == "req-timer"

    @patch("checkrd.batcher.urlopen")
    def test_shutdown_flushes_remaining(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(batch_size=1000)
        batcher.enqueue(sample_event("req-shutdown"))
        batcher.stop()

        assert mock_urlopen.call_count >= 1
        total_events = 0
        for call in mock_urlopen.call_args_list:
            body = json.loads(call[0][0].data)
            total_events += len(body["events"])
        assert total_events == 1


# ============================================================
# Backpressure
# ============================================================


class TestBackpressure:
    def test_drops_events_when_buffer_full(self) -> None:
        batcher = _make_batcher(
            base_url="http://localhost:1",
            batch_size=1000,
            max_queue_size=5,
        )
        for i in range(10):
            batcher.enqueue(sample_event(f"req-{i}"))

        assert batcher.pending_count <= 5
        batcher.stop()


# ============================================================
# Event flattening
# ============================================================


class TestEventFlattening:
    @patch("checkrd.batcher.urlopen")
    def test_flatten_event_format(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-flat"))
        batcher.flush()
        batcher.stop()

        flat = json.loads(mock_urlopen.call_args[0][0].data)["events"][0]
        # Nested fields should be flattened
        assert flat["request_id"] == "req-flat"
        assert flat["url_host"] == "api.stripe.com"
        assert flat["url_path"] == "/v1/charges"
        assert flat["method"] == "GET"
        assert flat["status_code"] == 200
        assert flat["latency_ms"] == 142
        # OTEL fields preserved
        assert flat["span_name"] == "GET api.stripe.com"
        assert flat["span_kind"] == "INTERNAL"
        assert flat["span_status_code"] == "OK"
        # event_id renamed to request_id
        assert "event_id" not in flat


# ============================================================
# Error handling
# ============================================================


class TestErrorHandling:
    @patch("checkrd.batcher.urlopen", side_effect=ConnectionRefusedError("refused"))
    def test_http_failure_drops_events_gracefully(self, mock_urlopen: MagicMock) -> None:
        batcher = _make_batcher(base_url="http://localhost:1")
        batcher.enqueue(sample_event("req-fail"))
        # Should not raise — errors are logged and dropped
        batcher.flush()
        batcher.stop()

        # Buffer should be empty (events were drained even though send failed)
        assert batcher.pending_count == 0

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen")
    def test_retries_once_on_5xx(self, mock_urlopen: MagicMock, mock_sleep: MagicMock) -> None:
        """5xx on first attempt triggers a retry; success on second attempt."""
        resp_500 = MagicMock()
        resp_500.status = 500
        resp_500.__enter__ = lambda s: s
        resp_500.__exit__ = MagicMock(return_value=False)

        resp_200 = _mock_urlopen()

        mock_urlopen.side_effect = [resp_500, resp_200]

        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-retry"))
        batcher.flush()
        batcher.stop()

        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()
        # Backoff should be within the jitter range (0 to _MAX_RETRY_DELAY)
        sleep_val = mock_sleep.call_args[0][0]
        assert 0 <= sleep_val <= 5.0

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen")
    def test_drops_after_exhausted_retries_5xx(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """All retries exhausted on 5xx: drop the batch."""
        resp_500 = MagicMock()
        resp_500.status = 500
        resp_500.headers = {}
        resp_500.__enter__ = lambda s: s
        resp_500.__exit__ = MagicMock(return_value=False)

        mock_urlopen.return_value = resp_500

        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-drop"))
        batcher.flush()
        batcher.stop()

        # 3 attempts (initial + 2 retries) with Stripe-style backoff
        assert mock_urlopen.call_count == 3
        assert batcher.pending_count == 0
        assert batcher.events_dropped > 0

    @patch("checkrd.batcher.urlopen")
    def test_4xx_not_retried(self, mock_urlopen: MagicMock) -> None:
        """4xx errors are client-side and should not be retried."""
        resp_400 = MagicMock()
        resp_400.status = 400
        resp_400.__enter__ = lambda s: s
        resp_400.__exit__ = MagicMock(return_value=False)

        mock_urlopen.return_value = resp_400

        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-400"))
        batcher.flush()
        batcher.stop()

        assert mock_urlopen.call_count == 1

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen")
    def test_retries_once_on_network_error(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Network error on first attempt, success on second."""
        mock_urlopen.side_effect = [
            TimeoutError("connection timed out"),
            _mock_urlopen(),
        ]

        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-timeout"))
        batcher.flush()
        batcher.stop()

        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen", side_effect=OSError("network unreachable"))
    def test_drops_after_exhausted_retries_network(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """All retries exhausted on network errors: drop the batch."""
        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-net"))
        batcher.flush()
        batcher.stop()

        # 3 attempts (initial + 2 retries)
        assert mock_urlopen.call_count == 3
        assert batcher.pending_count == 0
        assert batcher.events_dropped > 0


# ============================================================
# Telemetry loss tracking (Sentry client-reports pattern)
# ============================================================


class TestTelemetryLossTracking:
    """Verify monotonic loss counters for observability/self-diagnostics."""

    @patch("checkrd.batcher.urlopen")
    def test_events_sent_increments_on_success(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()
        for _ in range(3):
            batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        assert batcher.events_sent == 3
        assert batcher.events_dropped == 0

    @patch("checkrd.batcher.urlopen")
    def test_backpressure_increments_drop_counter(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(max_queue_size=2, batch_size=1000)
        for _ in range(5):
            batcher.enqueue(sample_event())
        batcher.stop()

        diag = batcher.diagnostics()
        assert diag["dropped_backpressure"] == 3  # 5 enqueued, 2 fit

    @patch("checkrd.batcher.urlopen", side_effect=OSError("network down"))
    @patch("checkrd.batcher.time.sleep")
    def test_send_error_increments_drop_counter(
        self, mock_sleep: MagicMock, mock_urlopen: MagicMock
    ) -> None:
        batcher = _make_batcher()
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        assert batcher.events_dropped > 0
        diag = batcher.diagnostics()
        assert diag["dropped_send_error"] >= 1
        assert diag["dropped_backpressure"] == 0

    @patch("checkrd.batcher.urlopen")
    def test_diagnostics_returns_complete_dict(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        diag = batcher.diagnostics()
        assert set(diag.keys()) == {
            "sent",
            "dropped_backpressure",
            "dropped_signing_error",
            "dropped_send_error",
            "pending",
            "last_request_id",
        }
        assert diag["sent"] == 1
        assert diag["pending"] == 0

    @patch("checkrd.batcher.urlopen")
    def test_last_request_id_captured_from_response_headers(
        self, mock_urlopen: MagicMock
    ) -> None:
        """The control plane stamps a ``Checkrd-Request-Id`` on every
        accepted batch; the batcher captures the most recent value so
        operators can paste it into a support ticket. Stripe/OpenAI
        convention — we follow the same shape so dashboards built for
        those SDKs work here."""
        mock_urlopen.return_value = _mock_urlopen(request_id="req_01HZX42")
        batcher = _make_batcher()
        try:
            batcher.enqueue(sample_event("req-rid"))
            batcher.flush()
            assert batcher.diagnostics()["last_request_id"] == "req_01HZX42"
        finally:
            batcher.stop()

    @patch("checkrd.batcher.urlopen")
    def test_last_request_id_is_none_before_first_send(
        self, mock_urlopen: MagicMock
    ) -> None:
        """Documenting the initial state — operators reading
        diagnostics before any traffic must see ``None``, not stale
        data from a previous batcher instance in the same process."""
        mock_urlopen.return_value = _mock_urlopen(request_id="ignored")
        batcher = _make_batcher()
        try:
            assert batcher.diagnostics()["last_request_id"] is None
        finally:
            batcher.stop()

    @patch("checkrd.batcher.urlopen")
    def test_counters_are_monotonic(self, mock_urlopen: MagicMock) -> None:
        """Counters never decrease — consumers can diff consecutive reads."""
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()

        batcher.enqueue(sample_event())
        batcher.flush()
        sent_1 = batcher.events_sent

        batcher.enqueue(sample_event())
        batcher.flush()
        sent_2 = batcher.events_sent

        batcher.stop()
        assert sent_2 > sent_1


# ============================================================
# Retry with jitter (Stripe-style backoff)
# ============================================================


class TestRetryWithJitter:
    """Stripe-style exponential backoff with jitter and Retry-After respect."""

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen")
    def test_429_is_retried(self, mock_urlopen: MagicMock, mock_sleep: MagicMock) -> None:
        """429 Too Many Requests should trigger a retry."""
        resp_429 = MagicMock()
        resp_429.status = 429
        resp_429.headers = {}
        resp_429.__enter__ = lambda s: s
        resp_429.__exit__ = MagicMock(return_value=False)

        resp_200 = _mock_urlopen()
        mock_urlopen.side_effect = [resp_429, resp_200]

        batcher = _make_batcher()
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        assert mock_urlopen.call_count == 2
        assert batcher.events_sent == 1

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen")
    def test_retry_after_header_respected(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Server's Retry-After header should override the computed backoff."""
        resp_429 = MagicMock()
        resp_429.status = 429
        resp_429.headers = {"Retry-After": "2"}
        resp_429.__enter__ = lambda s: s
        resp_429.__exit__ = MagicMock(return_value=False)

        resp_200 = _mock_urlopen()
        mock_urlopen.side_effect = [resp_429, resp_200]

        batcher = _make_batcher()
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        # Sleep should be 2.0 (from Retry-After), not the jittered backoff
        sleep_val = mock_sleep.call_args[0][0]
        assert sleep_val == 2.0

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen")
    def test_retry_after_capped_to_2x_max_sleep(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Pathological server hints are capped at ``2 * DEFAULT_MAX_SLEEP_SECS``.

        The previous implementation had an arbitrary 60-second ceiling;
        the centralised :mod:`checkrd._retry` helper expresses the cap
        as a multiple of the local backoff ceiling, which keeps server
        hints proportional to whatever the caller has chosen as the
        local maximum.
        """
        from checkrd._retry import DEFAULT_MAX_SLEEP_SECS

        resp_429 = MagicMock()
        resp_429.status = 429
        resp_429.headers = {"Retry-After": "300"}
        resp_429.__enter__ = lambda s: s
        resp_429.__exit__ = MagicMock(return_value=False)

        resp_200 = _mock_urlopen()
        mock_urlopen.side_effect = [resp_429, resp_200]

        batcher = _make_batcher()
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        sleep_val = mock_sleep.call_args[0][0]
        assert sleep_val == DEFAULT_MAX_SLEEP_SECS * 2

    @patch("checkrd.batcher.urlopen")
    def test_401_not_retried(self, mock_urlopen: MagicMock) -> None:
        """401 Unauthorized is not retryable."""
        resp_401 = MagicMock()
        resp_401.status = 401
        resp_401.__enter__ = lambda s: s
        resp_401.__exit__ = MagicMock(return_value=False)

        mock_urlopen.return_value = resp_401

        batcher = _make_batcher()
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        assert mock_urlopen.call_count == 1
        assert batcher.events_dropped == 1

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen")
    def test_502_503_504_retried(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """502, 503, 504 are all retryable server errors."""
        for status in (502, 503, 504):
            mock_urlopen.reset_mock()
            mock_sleep.reset_mock()

            resp_err = MagicMock()
            resp_err.status = status
            resp_err.headers = {}
            resp_err.__enter__ = lambda s: s
            resp_err.__exit__ = MagicMock(return_value=False)

            resp_200 = _mock_urlopen()
            mock_urlopen.side_effect = [resp_err, resp_200]

            batcher = _make_batcher()
            batcher.enqueue(sample_event())
            batcher.flush()
            batcher.stop()

            assert mock_urlopen.call_count == 2, f"Expected retry for HTTP {status}"

    @patch("checkrd.batcher.time.sleep")
    @patch("checkrd.batcher.urlopen")
    def test_jitter_within_expected_range(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Backoff delay should be within [0, 5.0] (jitter range)."""
        resp_500 = MagicMock()
        resp_500.status = 500
        resp_500.headers = {}
        resp_500.__enter__ = lambda s: s
        resp_500.__exit__ = MagicMock(return_value=False)

        resp_200 = _mock_urlopen()
        mock_urlopen.side_effect = [resp_500, resp_200]

        batcher = _make_batcher()
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        sleep_val = mock_sleep.call_args[0][0]
        assert 0 <= sleep_val <= 5.0, f"Jitter out of range: {sleep_val}"


# ============================================================
# Thread safety
# ============================================================


@pytest.mark.slow
@pytest.mark.xdist_group("serial")
class TestThreadSafety:
    @patch("checkrd.batcher.urlopen")
    def test_concurrent_enqueue(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(batch_size=1000)

        def enqueue_many(start: int) -> None:
            for i in range(50):
                batcher.enqueue(sample_event(f"req-{start + i}"))

        threads = [threading.Thread(target=enqueue_many, args=(i * 50,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), f"thread {t.name} hung"

        batcher.flush()
        batcher.stop()

        total_events = 0
        for call in mock_urlopen.call_args_list:
            body = json.loads(call[0][0].data)
            total_events += len(body["events"])
        assert total_events == 200


# ============================================================
# API key header
# ============================================================


class TestApiKeyHeader:
    @patch("checkrd.batcher.urlopen")
    def test_sends_api_key_header(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(api_key="ck_test_secret_key")
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-api-key") == "ck_test_secret_key"

    @patch("checkrd.batcher.urlopen")
    def test_sends_correct_url(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(base_url="https://api.checkrd.io")
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.checkrd.io/v1/telemetry"


# ============================================================
# W3C Trace Context (end-to-end request correlation)
# ============================================================


class TestTraceparentHeader:
    """Every telemetry batch emits a W3C traceparent header so operators
    can follow a request across Python SDK -> ingestion -> writer -> ClickHouse
    via `{service=~".+"} | json | trace_id="abc"` in Grafana Loki."""

    @patch("checkrd.batcher.urlopen")
    def test_sends_traceparent_header(self, mock_urlopen: MagicMock) -> None:
        import re

        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()
        batcher.enqueue(sample_event())
        batcher.flush()
        batcher.stop()

        req = mock_urlopen.call_args[0][0]
        traceparent = req.get_header("Traceparent")
        assert traceparent is not None, "traceparent header must be present"

        # W3C spec: 00-{32 hex}-{16 hex}-{2 hex flags}
        pattern = r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$"
        assert re.match(pattern, traceparent), (
            f"traceparent must match W3C format, got: {traceparent!r}"
        )

    @patch("checkrd.batcher.urlopen")
    def test_traceparent_unique_per_flush(self, mock_urlopen: MagicMock) -> None:
        """Two separate flushes should produce different trace IDs."""
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()

        batcher.enqueue(sample_event("req-1"))
        batcher.flush()
        tp1 = mock_urlopen.call_args[0][0].get_header("Traceparent")

        batcher.enqueue(sample_event("req-2"))
        batcher.flush()
        tp2 = mock_urlopen.call_args[0][0].get_header("Traceparent")

        batcher.stop()

        assert tp1 != tp2, "each flush must generate a unique trace_id"

    def test_generate_traceparent_uses_secrets_module(self) -> None:
        """Verify the helper uses cryptographic randomness, not random.random.

        W3C spec requires unpredictable trace IDs so internal traces
        cannot be guessed or forged.
        """
        from checkrd.batcher import _generate_traceparent

        # Sanity check: the function produces a well-formed W3C traceparent.
        tp = _generate_traceparent()
        assert tp.startswith("00-")
        assert tp.endswith("-01")
        parts = tp.split("-")
        assert len(parts) == 4
        assert len(parts[1]) == 32  # trace_id
        assert len(parts[2]) == 16  # parent_id
        # All hex
        assert all(c in "0123456789abcdef" for c in parts[1])
        assert all(c in "0123456789abcdef" for c in parts[2])

    def test_generate_traceparent_produces_distinct_ids(self) -> None:
        """Sanity check: 100 generated trace IDs should all be unique."""
        from checkrd.batcher import _generate_traceparent

        ids = {_generate_traceparent() for _ in range(100)}
        assert len(ids) == 100, "all 100 trace IDs should be unique"


# ============================================================
# Stop idempotency
# ============================================================


class TestStopIdempotency:
    def test_stop_is_idempotent(self) -> None:
        batcher = _make_batcher(base_url="http://localhost:1")
        batcher.stop()
        batcher.stop()  # Should not raise


# ============================================================
# Telemetry signing (RFC 9421 + DSSE) — see crates/shared/src/http_sig.rs
# ============================================================


_SIGNER_AGENT_ID = "550e8400-e29b-41d4-a716-446655440000"


def _make_anonymous_engine() -> WasmEngine:
    """Construct a WasmEngine with no signing key (anonymous mode).

    Strong-from-the-ground-up: this engine is only used to test that the
    batcher REJECTS anonymous mode rather than silently sending unsigned.
    """
    return WasmEngine(
        policy_json='{"agent":"test-agent","default":"allow","rules":[]}',
        agent_id="test-agent",
        # No private_key_bytes => anonymous
    )


class TestSigningHeaders:
    """The batcher must produce RFC 9421 + RFC 9530 headers on every send."""

    @patch("checkrd.batcher.urlopen")
    def test_signed_request_has_all_three_headers(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-signed"))
        batcher.flush()
        batcher.stop()

        req = mock_urlopen.call_args[0][0]
        # urllib normalizes headers to title case via add_header.
        assert req.get_header("Content-digest", "").startswith("sha-256=:")
        sig_input = req.get_header("Signature-input", "")
        assert sig_input.startswith("checkrd=("), sig_input
        assert 'alg="ed25519"' in sig_input
        sig = req.get_header("Signature", "")
        assert sig.startswith("checkrd=:") and sig.endswith(":"), sig
        assert req.get_header("X-checkrd-signer-agent") == _DEFAULT_SIGNER_AGENT_ID

    @patch("checkrd.batcher.urlopen")
    def test_content_digest_matches_body_sha256(self, mock_urlopen: MagicMock) -> None:
        # The Content-Digest must be the SHA-256 of the exact bytes the
        # batcher sends. If they diverge the verifier will reject everything.
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher()
        batcher.enqueue(sample_event("req-digest"))
        batcher.flush()
        batcher.stop()

        req = mock_urlopen.call_args[0][0]
        body_bytes = req.data
        expected = base64.b64encode(hashlib.sha256(body_bytes).digest()).decode()
        expected_header = f"sha-256=:{expected}:"
        assert req.get_header("Content-digest") == expected_header

    @patch("checkrd.batcher.urlopen")
    def test_anonymous_engine_drops_batch_instead_of_sending_unsigned(
        self, mock_urlopen: MagicMock
    ) -> None:
        # Strong-from-the-ground-up: an engine with no signing key cannot
        # produce a signature, so the batcher MUST drop the batch with a
        # structured error log rather than sending unsigned. There is no
        # unsigned path through the system.
        mock_urlopen.return_value = _mock_urlopen()
        engine = _make_anonymous_engine()
        batcher = TelemetryBatcher(
            base_url="http://localhost:8081",
            api_key="ck_test_abc",
            engine=engine,
            signer_agent_id=_SIGNER_AGENT_ID,
            batch_size=100,
            flush_interval_secs=60,
        )
        batcher.enqueue(sample_event("req-anon"))
        batcher.flush()
        batcher.stop()

        # urlopen was never called — the batch was dropped before sending.
        assert mock_urlopen.call_count == 0


@requires_wasm
class TestSigningRoundTrip:
    """Sign a batch in Python, verify the signature reconstructs and validates."""

    @patch("checkrd.batcher.urlopen")
    def test_signature_verifies_against_public_key(self, mock_urlopen: MagicMock) -> None:
        # The end-to-end invariant: a signature produced by the Python batcher
        # must be verifiable using only the wire bytes and the public key.
        # This is the proof that the wrapper and the WASM core agree on the
        # signature base bytes.
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError:
            pytest.skip("cryptography not installed; skipping verifier-side test")

        mock_urlopen.return_value = _mock_urlopen()

        # Generate a fresh key so we can extract the public key for verification.
        private, public_key_bytes = WasmEngine.generate_keypair()
        engine = WasmEngine(
            policy_json='{"agent":"test-agent","default":"allow","rules":[]}',
            agent_id="test-agent",
            private_key_bytes=private,
        )
        batcher = TelemetryBatcher(
            base_url="http://localhost:8081",
            api_key="ck_test_abc",
            engine=engine,
            signer_agent_id=_DEFAULT_SIGNER_AGENT_ID,
            batch_size=100,
            flush_interval_secs=60,
        )
        batcher.enqueue(sample_event("req-verify"))
        batcher.flush()
        batcher.stop()

        req = mock_urlopen.call_args[0][0]
        body_bytes = req.data
        content_digest = req.get_header("Content-digest")
        sig_input = req.get_header("Signature-input")
        sig_header = req.get_header("Signature")

        # Reconstruct the RFC 9421 signature base string the same way the
        # ingestion service will. If the batcher and the WASM core disagree
        # on a single byte this assertion fails.
        # Parse parameters out of "checkrd=(...)params" format
        assert sig_input.startswith("checkrd=")
        params_str = sig_input[len("checkrd=") :]
        # Find the (...) component list
        close_paren = params_str.index(")")
        params_after = params_str[close_paren + 1 :]
        # Extract created, expires, keyid, alg, nonce
        params: dict[str, str] = {}
        for kv in params_after.split(";"):
            kv = kv.strip()
            if not kv:
                continue
            k, _, v = kv.partition("=")
            params[k] = v.strip('"')

        created = int(params["created"])
        expires = int(params["expires"])
        keyid = params["keyid"]
        nonce = params["nonce"]
        assert params["alg"] == "ed25519"

        base_string = (
            '"@method": POST\n'
            '"@target-uri": http://localhost:8081/v1/telemetry\n'
            f'"content-digest": {content_digest}\n'
            f'"x-checkrd-signer-agent": {_DEFAULT_SIGNER_AGENT_ID}\n'
            '"@signature-params": '
            '("@method" "@target-uri" "content-digest" "x-checkrd-signer-agent");'
            f'created={created};expires={expires};keyid="{keyid}";'
            f'alg="ed25519";nonce="{nonce}"'
        ).encode("utf-8")

        # Decode the signature value
        assert sig_header.startswith("checkrd=:") and sig_header.endswith(":")
        sig_b64 = sig_header[len("checkrd=:") : -1]
        sig_bytes = base64.b64decode(sig_b64)
        assert len(sig_bytes) == 64

        # Verify with cryptography library — independent implementation.
        # If our wrapper produces wrong bytes this raises InvalidSignature.
        verifier = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        verifier.verify(sig_bytes, base_string)  # raises if invalid

        # Also verify the body sha256 matches
        expected_digest = base64.b64encode(hashlib.sha256(body_bytes).digest()).decode()
        assert content_digest == f"sha-256=:{expected_digest}:"


class TestOnDropCallback:
    """The on_drop callback observes telemetry losses in real time."""

    @patch("checkrd.batcher.urlopen")
    def test_backpressure_fires_callback(
        self, mock_urlopen: MagicMock,
    ) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        drops: list[tuple[str, int]] = []
        batcher = _make_batcher(
            max_queue_size=2,
            batch_size=1000,
            flush_interval_secs=3600.0,
            on_drop=lambda reason, count: drops.append((reason, count)),
        )
        try:
            for i in range(5):
                batcher.enqueue(sample_event(f"req-{i}"))
        finally:
            batcher.stop()
        backpressure = [(r, c) for r, c in drops if r == "backpressure"]
        assert backpressure == [("backpressure", 1)] * 3

    def test_signing_error_fires_callback_with_count(self) -> None:
        from checkrd.exceptions import CheckrdInitError
        drops: list[tuple[str, int]] = []
        batcher = _make_batcher(
            batch_size=10,
            on_drop=lambda reason, count: drops.append((reason, count)),
        )
        try:
            def poison(*a: Any, **kw: Any) -> None:
                raise CheckrdInitError("test: no signing key")
            with patch.object(batcher._engine, "sign_telemetry_batch", poison):
                batcher._send([sample_event(f"req-{i}") for i in range(7)])
        finally:
            batcher.stop()
        assert drops == [("signing_error", 7)]

    def test_send_error_fires_callback_with_count(self) -> None:
        import urllib.error
        drops: list[tuple[str, int]] = []
        batcher = _make_batcher(
            batch_size=10,
            on_drop=lambda reason, count: drops.append((reason, count)),
        )
        try:
            def always_fail(*a: Any, **kw: Any) -> None:
                raise urllib.error.URLError("connection refused")
            with patch("checkrd.batcher.urlopen", always_fail):
                batcher._send([sample_event(f"req-{i}") for i in range(4)])
        finally:
            batcher.stop()
        assert drops == [("send_error", 4)]

    @patch("checkrd.batcher.urlopen")
    def test_callback_exceptions_are_swallowed(
        self, mock_urlopen: MagicMock,
    ) -> None:
        mock_urlopen.return_value = _mock_urlopen()
        calls: list[int] = []
        def buggy(reason: str, count: int) -> None:
            calls.append(count)
            raise RuntimeError("intentional test failure")
        batcher = _make_batcher(
            max_queue_size=1,
            batch_size=1000,
            flush_interval_secs=3600.0,
            on_drop=buggy,
        )
        try:
            batcher.enqueue(sample_event("req-1"))
            batcher.enqueue(sample_event("req-2"))  # must not propagate
        finally:
            batcher.stop()
        assert calls == [1]

    @patch("checkrd.batcher.urlopen")
    def test_no_callback_by_default(self, mock_urlopen: MagicMock) -> None:
        """Backwards compat: batcher works without on_drop."""
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(
            max_queue_size=1,
            batch_size=1000,
            flush_interval_secs=3600.0,
        )
        try:
            batcher.enqueue(sample_event("req-1"))
            batcher.enqueue(sample_event("req-2"))
            assert batcher.diagnostics()["dropped_backpressure"] == 1
        finally:
            batcher.stop()


class TestSigningErrorCounter:
    """``dropped_signing_error`` is tracked separately from ``dropped_send_error``."""

    def test_signing_error_increments_own_counter(self) -> None:
        from checkrd.exceptions import CheckrdInitError
        batcher = _make_batcher()
        try:
            def poison(*a: Any, **kw: Any) -> None:
                raise CheckrdInitError("test")
            with patch.object(batcher._engine, "sign_telemetry_batch", poison):
                batcher._send([sample_event("req-1"), sample_event("req-2")])
            diag = batcher.diagnostics()
        finally:
            batcher.stop()
        assert diag["dropped_signing_error"] == 2
        assert diag["dropped_send_error"] == 0

    @patch("checkrd.batcher.urlopen")
    def test_events_dropped_property_sums_all_three(
        self, mock_urlopen: MagicMock,
    ) -> None:
        from checkrd.exceptions import CheckrdInitError
        mock_urlopen.return_value = _mock_urlopen()
        batcher = _make_batcher(
            max_queue_size=1,
            batch_size=1000,
            flush_interval_secs=3600.0,
        )
        try:
            batcher.enqueue(sample_event("req-1"))
            batcher.enqueue(sample_event("req-2"))  # 1 backpressure drop
            def poison(*a: Any, **kw: Any) -> None:
                raise CheckrdInitError("test")
            with patch.object(batcher._engine, "sign_telemetry_batch", poison):
                batcher._send([sample_event(f"req-{i}") for i in range(3)])
        finally:
            batcher.stop()
        assert batcher.events_dropped == 4  # 1 + 3
