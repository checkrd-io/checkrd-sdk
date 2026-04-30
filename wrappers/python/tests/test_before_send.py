"""Tests for the ``before_send`` telemetry-event mutation hook.

Sentry-pattern hook: the SDK invokes ``before_send(event, hint)``
once per ``enqueue`` call right before the event lands in the
batcher's queue. Returning the (possibly mutated) event ships it;
returning ``None`` drops it; raising logs and drops.

The hook is the only mutation surface on the telemetry pipeline.
Read-only hooks (``OnAllowHook`` / ``OnDenyHook``) stay; this adds
the operator-controlled drop-or-rewrite path.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from checkrd.batcher import TelemetryBatcher
from checkrd.engine import WasmEngine
from tests.conftest import requires_wasm


def _engine() -> WasmEngine:
    private, _public = WasmEngine.generate_keypair()
    return WasmEngine(
        policy_json='{"agent":"test","default":"allow","rules":[]}',
        agent_id="test",
        private_key_bytes=private,
    )


pytestmark = requires_wasm


def _make_batcher(*, before_send: Any = None) -> TelemetryBatcher:
    return TelemetryBatcher(
        base_url="http://localhost:8081",
        api_key="ck_test",
        engine=_engine(),
        signer_agent_id="550e8400-e29b-41d4-a716-446655440000",
        before_send=before_send,
        flush_interval_secs=60.0,  # never auto-flush in tests
    )


class TestBeforeSendHook:
    def test_invokes_with_event_and_hint(self) -> None:
        captured: list[tuple[dict[str, Any], dict[str, object]]] = []

        def hook(event: dict[str, Any], hint: dict[str, object]) -> dict[str, Any]:
            captured.append((event.copy(), hint.copy()))
            return event

        b = _make_batcher(before_send=hook)
        try:
            b.enqueue({"event_type": "request_evaluation", "url": "x"})
            assert len(captured) == 1
            event, hint = captured[0]
            assert event["url"] == "x"
            assert hint["agent_id"] == "550e8400-e29b-41d4-a716-446655440000"
            assert hint["event_kind"] == "request_evaluation"
        finally:
            b.stop()

    def test_returning_mutated_event_ships_the_mutation(self) -> None:
        # Redact the URL — common pattern for operators who don't
        # want raw URLs in their telemetry.
        def redact(event: dict[str, Any], _hint: dict[str, object]) -> dict[str, Any]:
            event = dict(event)
            event["url"] = "[redacted]"
            return event

        b = _make_batcher(before_send=redact)
        try:
            b.enqueue({"event_type": "test", "url": "https://secret.example/x"})
            assert b.pending_count == 1
        finally:
            b.stop()

    def test_returning_none_drops_the_event(self) -> None:
        b = _make_batcher(
            before_send=lambda _e, _h: None,  # type: ignore[arg-type, return-value]
        )
        try:
            b.enqueue({"event_type": "test"})
            # Operator-intended drop → no counter increments.
            diag = b.diagnostics()
            assert diag["sent"] == 0
            assert diag["dropped_backpressure"] == 0
            assert diag["dropped_send_error"] == 0
            assert diag["dropped_signing_error"] == 0
            assert diag["pending"] == 0
        finally:
            b.stop()

    def test_raising_drops_event_and_logs(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        def buggy(_e: dict[str, Any], _h: dict[str, object]) -> dict[str, Any]:
            raise RuntimeError("hook crashed")

        b = _make_batcher(before_send=buggy)
        try:
            with caplog.at_level(logging.ERROR, logger="checkrd"):
                b.enqueue({"event_type": "test"})
            assert b.pending_count == 0
            assert any(
                "before_send hook raised" in r.getMessage() for r in caplog.records
            )
        finally:
            b.stop()

    def test_no_hook_is_passthrough(self) -> None:
        b = _make_batcher(before_send=None)
        try:
            b.enqueue({"event_type": "request_evaluation"})
            assert b.pending_count == 1
        finally:
            b.stop()

    def test_hint_event_kind_falls_back_when_event_type_missing(self) -> None:
        captured: list[dict[str, object]] = []

        def hook(e: dict[str, Any], h: dict[str, object]) -> dict[str, Any]:
            captured.append(h.copy())
            return e

        b = _make_batcher(before_send=hook)
        try:
            b.enqueue({"event_type": "stream_completion"})
            b.enqueue({"event_type": "request_evaluation"})
            b.enqueue({"url": "x"})  # no event_type
            kinds = [h["event_kind"] for h in captured]
            assert kinds == [
                "stream_completion",
                "request_evaluation",
                "request_evaluation",
            ]
        finally:
            b.stop()

    @patch("checkrd.batcher.urlopen")
    def test_drop_via_hook_does_not_show_in_send_counters(
        self, mock_urlopen: MagicMock,
    ) -> None:
        # Sentry semantic: operator drops are not failures. Confirm
        # they don't pollute the dashboard counters that page on-call.
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        b = _make_batcher(before_send=lambda _e, _h: None)  # type: ignore[arg-type, return-value]
        try:
            for _ in range(10):
                b.enqueue({"event_type": "test"})
            b.flush()  # nothing to flush
            diag = b.diagnostics()
            assert diag["sent"] == 0
            assert diag["pending"] == 0
            assert diag["dropped_backpressure"] == 0
            assert diag["dropped_send_error"] == 0
            # urlopen should have NEVER been called — all 10 events
            # were dropped before reaching the queue.
            mock_urlopen.assert_not_called()
        finally:
            b.stop()
