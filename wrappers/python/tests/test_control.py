"""Tests for checkrd.control -- ControlReceiver SSE client with polling fallback."""

from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional
from unittest.mock import Mock, patch

import httpx
import pytest

from checkrd.control import AuthError, ControlReceiver, _INITIAL_BACKOFF, _MAX_BACKOFF
from tests.conftest import wait_for
from checkrd.engine import WasmEngine
from checkrd.exceptions import PolicySignatureError


# ============================================================
# Test helpers -- mock SSE events
# ============================================================


@dataclass
class FakeSSE:
    """Mimics httpx_sse.ServerSentEvent."""

    event: str = "message"
    data: str = ""
    id: str = ""
    retry: Optional[int] = None


def make_mock_engine() -> Mock:
    """Create a mock WasmEngine for control receiver tests."""
    engine = Mock(spec=WasmEngine)
    return engine


@contextmanager
def fake_sse_source(events: list[FakeSSE]) -> Iterator[Mock]:
    """Context manager that mimics httpx_sse.connect_sse().

    Yields a mock EventSource whose iter_sse() returns the given events.
    """
    source = Mock()
    source.iter_sse.return_value = iter(events)
    yield source


def make_receiver(engine: Optional[Mock] = None) -> ControlReceiver:
    """Create a ControlReceiver with defaults for testing."""
    return ControlReceiver(
        base_url="http://localhost:8080",
        agent_id="test-agent-id",
        api_key="ck_test_fake",
        engine=engine or make_mock_engine(),
    )


# ============================================================
# Signed policy envelope helpers
# ============================================================
#
# Strong-from-the-ground-up: there is NO unsigned policy distribution path,
# so every test that exercises ``policy_updated`` events must construct a
# real DSSE envelope. We sign here with PyCA cryptography (independent
# Ed25519 implementation) to mirror the cross-implementation interop
# pattern in test_policy_signing.py — the engine is mocked at the call
# boundary so the signature doesn't actually have to verify, but the
# envelope structure has to be wire-format-correct.

_PERMISSIVE_POLICY: dict[str, Any] = {
    "agent": "test",
    "default": "allow",
    "rules": [],
}


def _build_dsse_envelope_for_test(
    payload_dict: dict[str, Any],
    keyid: str = "test-cp",
    payload_type: str = "application/vnd.checkrd.policy-bundle+yaml",
) -> dict[str, Any]:
    """Build a DSSE envelope wrapping a PolicyBundle payload.

    The engine is mocked in these tests so the signature bytes are never
    verified — we just need a structurally-valid envelope so ``_apply_policy_update``
    will pass it to ``engine.reload_policy_signed``. Real signature
    interop is tested in test_policy_signing.py.
    """
    bundle = {
        "schema_version": 1,
        "version": 1,
        "signed_at": int(time.time()),
        "policy": payload_dict,
    }
    payload_bytes = json.dumps(bundle).encode()
    # The signature bytes don't need to verify — the engine is mocked. We
    # use a fixed 64-byte sentinel so the envelope is well-formed JSON
    # with a base64-encoded sig field of the right length.
    fake_sig = b"\x00" * 64
    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(payload_bytes).decode(),
        "signatures": [
            {
                "keyid": keyid,
                "sig": base64.b64encode(fake_sig).decode(),
            }
        ],
    }


# ============================================================
# Event handling tests (unit -- no threads)
# ============================================================


class TestEventHandling:
    def test_kill_switch_on_event(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="kill_switch", data=json.dumps({"active": True}))
        receiver._handle_event(sse)

        engine.set_kill_switch.assert_called_once_with(True)

    def test_kill_switch_off_event(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="kill_switch", data=json.dumps({"active": False}))
        receiver._handle_event(sse)

        engine.set_kill_switch.assert_called_once_with(False)

    def test_init_event_applies_kill_switch_on(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(
            event="init",
            data=json.dumps({"kill_switch_active": True, "active_policy_hash": None}),
        )
        receiver._handle_event(sse)

        engine.set_kill_switch.assert_called_once_with(True)

    def test_init_event_applies_kill_switch_off(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(
            event="init",
            data=json.dumps({"kill_switch_active": False, "active_policy_hash": "sha256:abc"}),
        )
        receiver._handle_event(sse)

        engine.set_kill_switch.assert_called_once_with(False)

    def test_policy_updated_event_reloads_engine(self) -> None:
        """A policy_updated SSE event with a signed envelope must be passed
        to the engine via reload_policy_signed.

        Strong-from-the-ground-up: there is no unsigned policy distribution
        path. The event MUST carry a DSSE envelope; the SDK constructs the
        trust list from CHECKRD_POLICY_TRUST_OVERRIDE_JSON / compile-time
        pinning and forwards both to the WASM core's reload_policy_signed.
        """
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        envelope = _build_dsse_envelope_for_test(_PERMISSIVE_POLICY)
        sse = FakeSSE(
            event="policy_updated",
            data=json.dumps(
                {
                    "version": 3,
                    "hash": "sha256:abc",
                    "policy_envelope": envelope,
                }
            ),
        )
        receiver._handle_event(sse)

        engine.reload_policy_signed.assert_called_once()
        # Verify the envelope was forwarded as JSON, alongside a trust list
        # and a (now, max_age_secs) tuple.
        call_args = engine.reload_policy_signed.call_args[0]
        envelope_arg = json.loads(call_args[0])
        assert envelope_arg["payloadType"] == "application/vnd.checkrd.policy-bundle+yaml"
        assert "signatures" in envelope_arg
        # Trust list is JSON-encoded; just confirm it parses.
        trusted_arg = json.loads(call_args[1])
        assert isinstance(trusted_arg, list)
        # Third arg is now (Unix seconds), fourth is max_age_secs.
        assert isinstance(call_args[2], int)
        assert call_args[3] == 86_400

    def test_policy_updated_event_without_envelope_is_dropped(self) -> None:
        """Strong-from-the-ground-up: an SSE policy_updated event missing
        the required policy_envelope field must be dropped with a structured
        warning. There is no fallback to unsigned distribution."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(
            event="policy_updated",
            data=json.dumps(
                {
                    "version": 3,
                    "hash": "sha256:abc",
                    # Note: no policy_envelope -- represents an old control plane
                    # or a tampered event.
                }
            ),
        )
        receiver._handle_event(sse)

        engine.reload_policy_signed.assert_not_called()
        engine.reload_policy.assert_not_called()

    def test_heartbeat_event_ignored(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="message", data="heartbeat")
        receiver._handle_event(sse)

        engine.set_kill_switch.assert_not_called()
        engine.reload_policy.assert_not_called()

    def test_unknown_event_type_ignored(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="some_future_event", data="{}")
        receiver._handle_event(sse)

        engine.set_kill_switch.assert_not_called()
        engine.reload_policy.assert_not_called()

    def test_malformed_json_does_not_crash(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="kill_switch", data="not json {{{")
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_not_called()

    def test_missing_field_does_not_crash(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        # kill_switch event without "active" field
        sse = FakeSSE(event="kill_switch", data=json.dumps({"wrong_field": True}))
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_not_called()


# ============================================================
# SSE connection tests (mocked httpx_sse)
# ============================================================


class TestSSEConnection:
    @patch("checkrd.control.httpx_sse.connect_sse")
    @patch("checkrd.control.httpx.Client")
    def test_run_sse_processes_events(self, mock_client_cls: Mock, mock_connect: Mock) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        events = [
            FakeSSE(event="init", data=json.dumps({"kill_switch_active": False})),
            FakeSSE(event="kill_switch", data=json.dumps({"active": True})),
        ]

        source = Mock()
        source.iter_sse.return_value = iter(events)
        source.__enter__ = Mock(return_value=source)
        source.__exit__ = Mock(return_value=False)
        mock_connect.return_value = source

        client_instance = Mock()
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)
        mock_client_cls.return_value = client_instance

        receiver._run_sse()

        assert engine.set_kill_switch.call_count == 2
        engine.set_kill_switch.assert_any_call(False)  # init
        engine.set_kill_switch.assert_any_call(True)  # kill_switch event


# ============================================================
# Polling fallback tests
# ============================================================


class TestPollingFallback:
    @patch("checkrd.control.httpx.Client")
    def test_poll_applies_kill_switch(self, mock_client_cls: Mock) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        mock_resp = Mock()
        mock_resp.json.return_value = {"kill_switch_active": True, "active_policy_hash": None}
        mock_resp.raise_for_status = Mock()

        client_instance = Mock()
        client_instance.get.return_value = mock_resp
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)
        mock_client_cls.return_value = client_instance

        receiver._poll_once()

        engine.set_kill_switch.assert_called_once_with(True)

    @patch("checkrd.control.httpx.Client")
    def test_poll_applies_kill_switch_off(self, mock_client_cls: Mock) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        mock_resp = Mock()
        mock_resp.json.return_value = {"kill_switch_active": False, "active_policy_hash": "h"}
        mock_resp.raise_for_status = Mock()

        client_instance = Mock()
        client_instance.get.return_value = mock_resp
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)
        mock_client_cls.return_value = client_instance

        receiver._poll_once()

        engine.set_kill_switch.assert_called_once_with(False)

    @patch("checkrd.control.httpx.Client")
    def test_poll_failure_does_not_crash(self, mock_client_cls: Mock) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        client_instance = Mock()
        client_instance.get.side_effect = httpx.ConnectError("connection refused")
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)
        mock_client_cls.return_value = client_instance

        # Should not raise -- poll_once is called inside the run_loop's exception handler
        with pytest.raises(httpx.ConnectError):
            receiver._poll_once()


# ============================================================
# Thread lifecycle tests
# ============================================================


class TestLifecycle:
    @staticmethod
    def _blocking_run_loop(receiver: ControlReceiver) -> None:
        """A fake _run_loop that blocks until stop() is called."""
        receiver._stop.wait()

    def test_start_creates_daemon_thread(self) -> None:
        receiver = make_receiver()

        with patch.object(receiver, "_run_loop", lambda: self._blocking_run_loop(receiver)):
            receiver.start()
            try:
                assert receiver._thread is not None
                assert receiver._thread.daemon is True
                assert receiver._thread.is_alive()
            finally:
                receiver.stop()

    def test_stop_joins_thread(self) -> None:
        receiver = make_receiver()

        with patch.object(receiver, "_run_loop", lambda: self._blocking_run_loop(receiver)):
            receiver.start()
            thread = receiver._thread
            assert thread is not None
            assert thread.is_alive()
            receiver.stop()

            assert receiver._thread is None
            assert not thread.is_alive()

    def test_start_is_idempotent(self) -> None:
        receiver = make_receiver()

        with patch.object(receiver, "_run_loop", lambda: self._blocking_run_loop(receiver)):
            receiver.start()
            first_thread = receiver._thread
            receiver.start()  # should not create a second thread
            assert receiver._thread is first_thread
            receiver.stop()

    def test_run_loop_reconnects_on_sse_failure(self) -> None:
        """Verify the run loop retries after SSE connection failure."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)
        call_count = 0

        def fake_run_sse() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                receiver._stop.set()  # stop after 2 attempts
            raise ConnectionError("test disconnect")

        with (
            patch.object(receiver, "_run_sse", side_effect=fake_run_sse),
            patch.object(receiver, "_poll_once"),
        ):
            # Patch _stop.wait to not actually sleep
            original_wait = receiver._stop.wait
            receiver._stop.wait = lambda timeout=None: original_wait(timeout=0.01)  # type: ignore[assignment]
            receiver._run_loop()

        assert call_count >= 2, f"expected at least 2 SSE attempts, got {call_count}"

    def test_exponential_backoff_caps_at_max(self) -> None:
        """Verify backoff doubles and caps at _MAX_BACKOFF."""
        receiver = make_receiver()
        waits: list[float] = []
        attempt = 0

        def fake_run_sse() -> None:
            nonlocal attempt
            attempt += 1
            if attempt > 8:
                receiver._stop.set()
            raise ConnectionError("fail")

        def capture_wait(timeout: Optional[float] = None) -> bool:
            if timeout is not None:
                waits.append(timeout)
            return receiver._stop.is_set()

        with (
            patch.object(receiver, "_run_sse", side_effect=fake_run_sse),
            patch.object(receiver, "_poll_once"),
            patch.object(receiver._stop, "wait", side_effect=capture_wait),
        ):
            receiver._run_loop()

        # Verify exponential: 1, 2, 4, 8, 16, 32, 60, 60
        assert len(waits) >= 6
        assert waits[0] == _INITIAL_BACKOFF
        assert waits[1] == _INITIAL_BACKOFF * 2
        assert waits[2] == _INITIAL_BACKOFF * 4
        # Later entries should cap at _MAX_BACKOFF
        assert all(w <= _MAX_BACKOFF for w in waits)

    @pytest.mark.slow
    def test_backoff_resets_on_successful_connect(self) -> None:
        """After a successful SSE session, backoff should reset to initial."""
        receiver = make_receiver()
        waits: list[float] = []
        attempt = 0

        def fake_run_sse() -> None:
            nonlocal attempt
            attempt += 1
            if attempt == 3:
                # Succeed (no exception) -- this resets backoff
                return
            if attempt > 4:
                receiver._stop.set()
            raise ConnectionError("fail")

        def capture_wait(timeout: Optional[float] = None) -> bool:
            if timeout is not None:
                waits.append(timeout)
            return receiver._stop.is_set()

        with (
            patch.object(receiver, "_run_sse", side_effect=fake_run_sse),
            patch.object(receiver, "_poll_once"),
            patch.object(receiver._stop, "wait", side_effect=capture_wait),
        ):
            receiver._run_loop()

        # After success on attempt 3, the next failure (attempt 4) should use initial backoff
        # waits[0] = 1 (fail 1), waits[1] = 2 (fail 2), then success resets, waits[2] = 1 (fail 4)
        assert len(waits) >= 3
        assert waits[0] == _INITIAL_BACKOFF
        assert waits[1] == _INITIAL_BACKOFF * 2
        assert waits[2] == _INITIAL_BACKOFF  # reset after success

    def test_poll_called_on_sse_failure(self) -> None:
        """Between reconnection attempts, _poll_once should be called."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)
        poll_count = 0

        def fake_run_sse() -> None:
            raise ConnectionError("fail")

        def fake_poll() -> None:
            nonlocal poll_count
            poll_count += 1

        def stop_after_two(timeout: Optional[float] = None) -> bool:
            if poll_count >= 2:
                receiver._stop.set()
            return receiver._stop.is_set()

        with (
            patch.object(receiver, "_run_sse", side_effect=fake_run_sse),
            patch.object(receiver, "_poll_once", side_effect=fake_poll),
            patch.object(receiver._stop, "wait", side_effect=stop_after_two),
        ):
            receiver._run_loop()

        assert poll_count >= 2, f"expected at least 2 poll calls, got {poll_count}"


# ============================================================
# Integration with wrap()
# ============================================================


class TestWrapIntegration:
    @patch("checkrd.control.ControlReceiver.start")
    @patch("checkrd.control.ControlReceiver.__init__", return_value=None)
    @patch("checkrd._create_engine_from_json")
    def test_wrap_starts_control_receiver(
        self, mock_create: Mock, mock_init: Mock, mock_start: Mock
    ) -> None:
        mock_create.return_value = make_mock_engine()
        from checkrd import wrap
        with httpx.Client() as client:
            try:
                wrap(
                    client,
                    agent_id="test",
                    policy={"agent": "test", "default": "allow", "rules": []},
                    control_plane_url="http://localhost:8080",
                    api_key="ck_test_fake",
                )
                mock_init.assert_called_once()
                mock_start.assert_called_once()
                assert hasattr(client, "_checkrd_control")
            finally:
                client.close()

    @patch("checkrd._create_engine_from_json")
    def test_wrap_without_control_params_no_receiver(self, mock_create: Mock) -> None:
        mock_create.return_value = make_mock_engine()
        from checkrd import wrap
        with httpx.Client() as client:
            try:
                wrap(client, agent_id="test", policy={"agent": "test", "default": "allow", "rules": []})
                assert not hasattr(client, "_checkrd_control")
            finally:
                client.close()


# ============================================================
# Edge cases and error handling
# ============================================================


class TestEdgeCases:
    def test_policy_updated_with_malformed_envelope_does_not_crash(self) -> None:
        """A non-dict / structurally bad envelope must not crash the receiver
        — the engine call raises and is logged as a structured warning, the
        previous policy is left in place."""
        engine = make_mock_engine()
        engine.reload_policy_signed.side_effect = PolicySignatureError(-1)  # parse error
        receiver = make_receiver(engine)

        sse = FakeSSE(
            event="policy_updated",
            data=json.dumps(
                {
                    "version": 1,
                    "hash": "h",
                    "policy_envelope": {"this": "is not a real envelope"},
                }
            ),
        )
        receiver._handle_event(sse)  # should not raise

        engine.reload_policy_signed.assert_called_once()

    def test_policy_updated_with_signature_rejection_does_not_crash(self) -> None:
        """When the WASM core rejects a signed envelope (bad signature, stale
        bundle, version replay, etc.), the receiver must catch
        PolicySignatureError, leave the previous policy in place, and log a
        structured warning."""
        engine = make_mock_engine()
        engine.reload_policy_signed.side_effect = PolicySignatureError(-5)  # signature_invalid
        receiver = make_receiver(engine)

        envelope = _build_dsse_envelope_for_test(_PERMISSIVE_POLICY)
        sse = FakeSSE(
            event="policy_updated",
            data=json.dumps(
                {
                    "version": 1,
                    "hash": "h",
                    "policy_envelope": envelope,
                }
            ),
        )
        receiver._handle_event(sse)  # should not raise

        engine.reload_policy_signed.assert_called_once()

    def test_auth_error_stops_run_loop_permanently(self) -> None:
        receiver = make_receiver()

        def fake_run_sse() -> None:
            raise AuthError("401 Unauthorized")

        poll_count = 0

        def fake_poll() -> None:
            nonlocal poll_count
            poll_count += 1

        with (
            patch.object(receiver, "_run_sse", side_effect=fake_run_sse),
            patch.object(receiver, "_poll_once", side_effect=fake_poll),
        ):
            receiver._run_loop()

        # AuthError should stop immediately -- no polling, no retry
        assert poll_count == 0

    def test_url_construction(self) -> None:
        receiver = ControlReceiver(
            base_url="https://api.checkrd.io/",  # trailing slash
            agent_id="agent-123",
            api_key="ck_live_test",
            engine=make_mock_engine(),
        )
        # Verify trailing slash stripped
        assert receiver._base_url == "https://api.checkrd.io"

    @patch("checkrd.control.httpx_sse.connect_sse")
    @patch("checkrd.control.httpx.Client")
    def test_api_key_header_sent(self, mock_client_cls: Mock, mock_connect: Mock) -> None:
        receiver = ControlReceiver(
            base_url="http://localhost:8080",
            agent_id="agent-xyz",
            api_key="ck_test_mykey123",
            engine=make_mock_engine(),
        )

        # Make connect_sse raise after checking the call args
        source = Mock()
        source.response = Mock(status_code=200)
        source.iter_sse.return_value = iter([])  # empty stream
        source.__enter__ = Mock(return_value=source)
        source.__exit__ = Mock(return_value=False)
        mock_connect.return_value = source

        client_instance = Mock()
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)
        mock_client_cls.return_value = client_instance

        receiver._run_sse()

        # Verify connect_sse was called with the correct URL and headers
        mock_connect.assert_called_once()
        call_args = mock_connect.call_args
        assert call_args[0][1] == "GET"
        assert call_args[0][2] == "http://localhost:8080/v1/agents/agent-xyz/control"
        assert call_args[1]["headers"]["X-API-Key"] == "ck_test_mykey123"

    @patch("checkrd.control.httpx.Client")
    def test_poll_url_and_headers(self, mock_client_cls: Mock) -> None:
        receiver = ControlReceiver(
            base_url="http://localhost:8080",
            agent_id="agent-xyz",
            api_key="ck_test_mykey123",
            engine=make_mock_engine(),
        )

        mock_resp = Mock()
        mock_resp.json.return_value = {"kill_switch_active": False}
        mock_resp.raise_for_status = Mock()

        client_instance = Mock()
        client_instance.get.return_value = mock_resp
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)
        mock_client_cls.return_value = client_instance

        receiver._poll_once()

        client_instance.get.assert_called_once()
        call_args = client_instance.get.call_args
        assert call_args[0][0] == "http://localhost:8080/v1/agents/agent-xyz/control/state"
        assert call_args[1]["headers"]["X-API-Key"] == "ck_test_mykey123"


# ============================================================
# Malformed SSE event recovery
# ============================================================


class TestSSEEventSizeLimit:
    """Oversized SSE events are rejected before json.loads() to prevent OOM."""

    def test_event_under_limit_is_processed(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="kill_switch", data=json.dumps({"active": True}))
        receiver._handle_event(sse)
        engine.set_kill_switch.assert_called_once_with(True)

    def test_event_over_limit_is_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        # Construct a payload just over 10 MB
        oversized_data = "x" * (10 * 1024 * 1024 + 1)
        sse = FakeSSE(event="kill_switch", data=oversized_data)

        with caplog.at_level("WARNING", logger="checkrd"):
            receiver._handle_event(sse)

        engine.set_kill_switch.assert_not_called()
        assert any("too large" in r.message for r in caplog.records)

    def test_event_at_exact_limit_is_processed(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        # Build a valid JSON payload that's exactly at the limit
        # (the actual data doesn't need to be valid JSON for the size check —
        # but we test that it reaches json.loads by using valid JSON)
        payload = json.dumps({"active": True})
        sse = FakeSSE(event="kill_switch", data=payload)
        assert len(payload) < 10 * 1024 * 1024  # sanity check
        receiver._handle_event(sse)
        engine.set_kill_switch.assert_called_once()

    def test_oversized_policy_update_is_dropped(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        oversized = "y" * (10 * 1024 * 1024 + 1)
        sse = FakeSSE(event="policy_updated", data=oversized)
        receiver._handle_event(sse)  # should not raise or OOM
        engine.reload_policy_signed.assert_not_called()


class TestMalformedEventRecovery:
    """Malformed or unexpected SSE events must be handled without crashing."""

    def test_invalid_json_in_kill_switch(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="kill_switch", data="not valid json{{{")
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_not_called()

    def test_invalid_json_in_init(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="init", data="[broken")
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_not_called()

    def test_invalid_json_in_policy_updated(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="policy_updated", data="{invalid json}")
        receiver._handle_event(sse)  # should not raise

        engine.reload_policy_signed.assert_not_called()

    def test_missing_active_field_in_kill_switch(self) -> None:
        """kill_switch event with missing 'active' field raises KeyError, caught."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="kill_switch", data=json.dumps({"wrong_field": True}))
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_not_called()

    def test_init_with_missing_kill_switch_field_defaults_false(self) -> None:
        """init event with missing kill_switch_active defaults to False."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="init", data=json.dumps({}))
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_called_once_with(False)

    def test_policy_updated_without_envelope_logs_warning(self) -> None:
        """policy_updated with missing policy_envelope is rejected."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(
            event="policy_updated",
            data=json.dumps({"version": 1, "hash": "h"}),  # no policy_envelope
        )
        receiver._handle_event(sse)  # should not raise

        engine.reload_policy_signed.assert_not_called()

    def test_unknown_event_type_ignored(self) -> None:
        """Unknown event types are silently ignored."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="heartbeat", data="")
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_not_called()
        engine.reload_policy_signed.assert_not_called()

    def test_empty_data_in_kill_switch(self) -> None:
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="kill_switch", data="")
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_not_called()

    def test_null_json_in_init(self) -> None:
        """JSON null is not a dict, should be handled."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="init", data="null")
        receiver._handle_event(sse)  # should not raise

    def test_array_json_in_kill_switch(self) -> None:
        """JSON array instead of object should be caught."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        sse = FakeSSE(event="kill_switch", data="[1, 2, 3]")
        receiver._handle_event(sse)  # should not raise

        engine.set_kill_switch.assert_not_called()


# ============================================================
# End-to-end: mock SSE server -> ControlReceiver -> engine
# ============================================================


class TestEndToEnd:
    def test_e2e_sse_events_reach_engine(self) -> None:
        """Simulate a full SSE session: init -> kill_switch -> policy_updated.

        This test mocks at the httpx/httpx_sse boundary (not at _run_sse level),
        so it exercises the full _run_sse -> _handle_event -> engine path.

        Strong-from-the-ground-up: the policy_updated event carries a
        DSSE-signed envelope, NOT yaml_content. The engine call goes
        through reload_policy_signed; the unsigned reload_policy path
        no longer exists.
        """
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        envelope = _build_dsse_envelope_for_test(_PERMISSIVE_POLICY)
        events = [
            FakeSSE(
                event="init",
                data=json.dumps(
                    {
                        "kill_switch_active": False,
                        "active_policy_hash": None,
                    }
                ),
            ),
            FakeSSE(event="kill_switch", data=json.dumps({"active": True})),
            FakeSSE(
                event="policy_updated",
                data=json.dumps(
                    {
                        "version": 2,
                        "hash": "sha256:def456",
                        "policy_envelope": envelope,
                    }
                ),
            ),
            FakeSSE(event="kill_switch", data=json.dumps({"active": False})),
        ]

        source = Mock()
        source.response = Mock(status_code=200)
        source.iter_sse.return_value = iter(events)
        source.__enter__ = Mock(return_value=source)
        source.__exit__ = Mock(return_value=False)

        client_instance = Mock()
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)

        with (
            patch("checkrd.control.httpx.Client", return_value=client_instance),
            patch("checkrd.control.httpx_sse.connect_sse", return_value=source),
        ):
            receiver._run_sse()

        # Verify all engine calls happened in order
        assert engine.set_kill_switch.call_count == 3
        assert engine.reload_policy_signed.call_count == 1
        # Legacy unsigned path is gone — must NOT have been touched.
        engine.reload_policy.assert_not_called()

        ks_calls = [c[0][0] for c in engine.set_kill_switch.call_args_list]
        assert ks_calls == [False, True, False]  # init, on, off

        # Verify the signed envelope was forwarded to the verifier as JSON.
        envelope_arg = json.loads(engine.reload_policy_signed.call_args[0][0])
        assert envelope_arg["payloadType"] == "application/vnd.checkrd.policy-bundle+yaml"
        assert "signatures" in envelope_arg

    def test_e2e_sse_disconnect_then_poll_fallback(self) -> None:
        """Simulate SSE failure -> poll fallback -> reconnect with backoff.

        Verifies the full run_loop flow: SSE fails, poll succeeds, backoff applied.
        """
        engine = make_mock_engine()
        receiver = make_receiver(engine)
        attempt = 0

        def fake_run_sse() -> None:
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise ConnectionError("server gone")
            # Second attempt: succeed then exit cleanly
            receiver._stop.set()

        poll_resp = {"kill_switch_active": True, "active_policy_hash": "h"}

        mock_resp = Mock()
        mock_resp.json.return_value = poll_resp
        mock_resp.raise_for_status = Mock()

        client_instance = Mock()
        client_instance.get.return_value = mock_resp
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)

        original_wait = receiver._stop.wait

        with (
            patch.object(receiver, "_run_sse", side_effect=fake_run_sse),
            patch("checkrd.control.httpx.Client", return_value=client_instance),
            patch.object(
                receiver._stop, "wait", side_effect=lambda timeout=None: original_wait(timeout=0.01)
            ),
        ):
            receiver._run_loop()

        # Poll should have applied the kill switch during fallback
        engine.set_kill_switch.assert_called_with(True)
        assert attempt == 2  # SSE was retried

    @pytest.mark.slow
    def test_e2e_threaded_start_stop(self) -> None:
        """Start receiver in a real thread, send events, stop cleanly."""
        engine = make_mock_engine()
        receiver = make_receiver(engine)

        events = [
            FakeSSE(event="init", data=json.dumps({"kill_switch_active": True})),
        ]

        source = Mock()
        source.response = Mock(status_code=200)

        def iter_then_block() -> Iterator[FakeSSE]:
            yield from events
            # Block until stop is called (simulating long-lived SSE connection)
            receiver._stop.wait()

        source.iter_sse = iter_then_block
        source.__enter__ = Mock(return_value=source)
        source.__exit__ = Mock(return_value=False)

        client_instance = Mock()
        client_instance.__enter__ = Mock(return_value=client_instance)
        client_instance.__exit__ = Mock(return_value=False)

        with (
            patch("checkrd.control.httpx.Client", return_value=client_instance),
            patch("checkrd.control.httpx_sse.connect_sse", return_value=source),
        ):
            receiver.start()

            wait_for(lambda: engine.set_kill_switch.called)
            engine.set_kill_switch.assert_called_with(True)

            receiver.stop()

        assert receiver._thread is None


# ============================================================
# Persistence: cross-restart rollback defense
# ============================================================
#
# These tests cover the wiring between the ControlReceiver, the
# WasmEngine FFI exports (reload_policy_signed / get_active_policy_version),
# and the on-disk state file. The persistence primitive itself is tested
# in test_policy_state.py; here we verify the receiver actually CALLS
# it on start() and on successful policy installs.
#
# Industry pattern (OPA bundle services / TUF clients): on startup, the
# wrapper re-installs the last verified envelope from disk via
# ``reload_policy_signed``. The same FFI path used for live updates
# re-verifies signatures, freshness, and trust-list match — a tampered
# or stale persisted bundle is rejected and the SDK falls through to a
# fresh server fetch.


class TestPolicyVersionPersistence:
    def test_start_restores_persisted_bundle_via_engine(self, tmp_path: Any) -> None:
        """When ``policy_state.json`` contains a verified envelope from
        the last process, ``start()`` must hand it to
        ``engine.reload_policy_signed`` BEFORE the SSE thread launches.
        Without this, the engine has no rules until SSE init lands and
        every request in the bootstrap window falls through to default-
        allow."""
        from checkrd._policy_state import persist_state

        envelope = _build_dsse_envelope_for_test(_PERMISSIVE_POLICY)
        envelope_json = json.dumps(envelope)
        hash_hex = "a" * 64
        state_path = tmp_path / "policy_state.json"
        persist_state(42, hash_hex, envelope_json, path=state_path)

        engine = make_mock_engine()
        with patch("checkrd._policy_state._default_state_path", return_value=state_path):
            receiver = make_receiver(engine)
            with patch.object(receiver, "_run_loop", lambda: receiver._stop.wait()):
                receiver.start()
                try:
                    engine.reload_policy_signed.assert_called_once()
                    args, _kwargs = engine.reload_policy_signed.call_args
                    assert args[0] == envelope_json
                    # Cache must be primed so the SSE init's identical
                    # bundle short-circuits as a no-op rather than
                    # re-installing.
                    assert receiver._last_installed_hash == hash_hex
                finally:
                    receiver.stop()

    def test_start_does_nothing_when_no_persisted_state(self, tmp_path: Any) -> None:
        """A fresh SDK install with no state file must NOT call
        reload_policy_signed at startup — there's nothing to install
        from disk; the engine waits for SSE init."""
        engine = make_mock_engine()
        empty_state = tmp_path / "policy_state.json"  # never written

        with patch("checkrd._policy_state._default_state_path", return_value=empty_state):
            receiver = make_receiver(engine)
            with patch.object(receiver, "_run_loop", lambda: receiver._stop.wait()):
                receiver.start()
                try:
                    engine.reload_policy_signed.assert_not_called()
                finally:
                    receiver.stop()

    def test_start_swallows_restore_failure(self, tmp_path: Any) -> None:
        """If the persisted bundle fails verification on restore (stale
        ``signed_at``, rotated trust list, tampered file), the receiver
        must log and continue. The engine stays empty until SSE delivers
        a fresh bundle — same posture as a brand-new install."""
        from checkrd._policy_state import persist_state

        envelope_json = json.dumps(_build_dsse_envelope_for_test(_PERMISSIVE_POLICY))
        state_path = tmp_path / "policy_state.json"
        persist_state(42, "b" * 64, envelope_json, path=state_path)

        engine = make_mock_engine()
        engine.reload_policy_signed.side_effect = PolicySignatureError(-3)

        with patch("checkrd._policy_state._default_state_path", return_value=state_path):
            receiver = make_receiver(engine)
            with patch.object(receiver, "_run_loop", lambda: receiver._stop.wait()):
                receiver.start()  # must not raise
                try:
                    engine.reload_policy_signed.assert_called_once()
                    # Cache stays empty so the next SSE/poll bundle
                    # actually goes through the FFI rather than
                    # short-circuiting against a stale hash.
                    assert receiver._last_installed_hash is None
                finally:
                    receiver.stop()

    def test_successful_install_persists_engine_version(self, tmp_path: Any) -> None:
        """After a successful signed install, the receiver must read the
        active version from the engine (NOT from the SSE event payload)
        and write the (version, hash, envelope) triple to disk so the
        next restart can re-install via the OPA-pattern restore path."""
        from checkrd._policy_state import load_persisted_state

        state_path = tmp_path / "policy_state.json"
        valid_hash = "c" * 64

        engine = make_mock_engine()
        engine.get_active_policy_version.return_value = 17
        engine.reload_policy_signed.return_value = None  # success

        with patch("checkrd._policy_state._default_state_path", return_value=state_path):
            receiver = make_receiver(engine)
            envelope = _build_dsse_envelope_for_test(_PERMISSIVE_POLICY)
            sse = FakeSSE(
                event="policy_updated",
                data=json.dumps({
                    "version": 17,
                    "hash": valid_hash,
                    "policy_envelope": envelope,
                }),
            )
            receiver._handle_event(sse)

        # The persisted file must contain the version from the engine,
        # not from the wire payload — proves we're the source of truth.
        engine.get_active_policy_version.assert_called_once()
        version, persisted_hash, persisted_env = load_persisted_state(state_path)
        assert version == 17
        assert persisted_hash == valid_hash
        # The envelope is canonicalized via json.dumps before the FFI
        # call; assert it round-trips to the same shape we sent in.
        assert json.loads(persisted_env) == envelope

    def test_failed_install_does_not_persist(self, tmp_path: Any) -> None:
        """A rejected install (bad signature, stale, etc.) must NOT
        write to disk — the previous state stays valid and the failed
        envelope's ``version`` must never become persisted state."""
        from checkrd._policy_state import load_persisted_state, persist_state

        state_path = tmp_path / "policy_state.json"
        baseline_hash = "d" * 64
        baseline_env = json.dumps(_build_dsse_envelope_for_test(_PERMISSIVE_POLICY))
        persist_state(10, baseline_hash, baseline_env, path=state_path)

        engine = make_mock_engine()
        engine.reload_policy_signed.side_effect = PolicySignatureError(-5)

        with patch("checkrd._policy_state._default_state_path", return_value=state_path):
            receiver = make_receiver(engine)
            envelope = _build_dsse_envelope_for_test(_PERMISSIVE_POLICY)
            sse = FakeSSE(
                event="policy_updated",
                data=json.dumps({
                    "version": 999,  # would-be rollback
                    "hash": "e" * 64,
                    "policy_envelope": envelope,
                }),
            )
            receiver._handle_event(sse)

        # The on-disk state must be unchanged.
        engine.get_active_policy_version.assert_not_called()
        assert load_persisted_state(state_path) == (10, baseline_hash, baseline_env)

    def test_persist_failure_does_not_break_install(self, tmp_path: Any) -> None:
        """If the disk write fails, the install is still considered
        successful — the in-process monotonic check still applies. We
        log a warning but the receiver does NOT raise."""
        engine = make_mock_engine()
        engine.get_active_policy_version.return_value = 5
        engine.reload_policy_signed.return_value = None

        with patch(
            "checkrd.control.persist_state",
            side_effect=OSError("disk full"),
        ):
            receiver = make_receiver(engine)
            envelope = _build_dsse_envelope_for_test(_PERMISSIVE_POLICY)
            sse = FakeSSE(
                event="policy_updated",
                data=json.dumps({
                    "version": 5,
                    "hash": "f" * 64,
                    "policy_envelope": envelope,
                }),
            )
            receiver._handle_event(sse)  # must not raise

        engine.reload_policy_signed.assert_called_once()
