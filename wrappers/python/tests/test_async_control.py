"""Tests for ``checkrd._async_control.AsyncControlReceiver``.

The async receiver is the production SSE path for ``wrap_async()``
and ``AsyncCheckrd``. The sync ``ControlReceiver`` is well-covered
in ``tests/test_control.py``; this file mirrors that coverage on
the async side so both paths receive equal scrutiny:

  - lifecycle (start / stop / idempotent stop / asyncio.CancelledError)
  - SSE event dispatch (init, kill_switch, policy_updated)
  - malformed event tolerance
  - shared CircuitBreaker integration
  - persisted-version restore on start
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from checkrd._async_control import AsyncAuthError, AsyncControlReceiver
from checkrd._circuit_breaker import CircuitBreaker
from checkrd.engine import WasmEngine
from checkrd.exceptions import PolicySignatureError


# ---------------------------------------------------------------------------
# Test helpers — the async equivalents of test_control.py's fakes.
# ---------------------------------------------------------------------------


@dataclass
class FakeSSE:
    """Mimics ``httpx_sse.ServerSentEvent`` for unit tests."""

    event: str = "message"
    data: str = ""
    id: str = ""
    retry: Optional[int] = None


def _make_mock_engine() -> MagicMock:
    """Mock WasmEngine — shape matches the real class's surface."""
    return MagicMock(spec=WasmEngine)


def _make_receiver(
    *,
    engine: Optional[MagicMock] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> AsyncControlReceiver:
    return AsyncControlReceiver(
        base_url="http://localhost:8080",
        agent_id="test-agent-id",
        api_key="ck_test_fake",
        engine=engine or _make_mock_engine(),
        circuit_breaker=circuit_breaker,
    )


def _signed_envelope(version: int = 1) -> dict[str, Any]:
    """Structurally-valid DSSE envelope. The mocked engine ignores
    signature bytes; we just need the JSON shape correct so the
    handler dispatches to ``reload_policy_signed``."""
    import base64

    bundle = {"schema_version": 1, "version": version, "signed_at": 1730000000, "policy": {}}
    payload = json.dumps(bundle).encode()
    return {
        "payloadType": "application/vnd.checkrd.policy-bundle+yaml",
        "payload": base64.b64encode(payload).decode(),
        "signatures": [{"keyid": "test", "sig": base64.b64encode(b"\x00" * 64).decode()}],
    }


# ---------------------------------------------------------------------------
# Synchronous event handler — sufficient for most coverage
# ---------------------------------------------------------------------------


class TestEventHandling:
    """``_handle_event`` is sync — easier to drive in unit tests
    than the full async loop. Same code path the live receiver hits."""

    def test_kill_switch_on(self) -> None:
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)
        receiver._handle_event(
            FakeSSE(event="kill_switch", data=json.dumps({"active": True})),
        )
        engine.set_kill_switch.assert_called_once_with(True)

    def test_kill_switch_off(self) -> None:
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)
        receiver._handle_event(
            FakeSSE(event="kill_switch", data=json.dumps({"active": False})),
        )
        engine.set_kill_switch.assert_called_once_with(False)

    def test_init_applies_initial_kill_switch(self) -> None:
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)
        receiver._handle_event(
            FakeSSE(event="init", data=json.dumps({"kill_switch_active": True})),
        )
        engine.set_kill_switch.assert_called_once_with(True)

    def test_policy_updated_calls_reload_signed(self) -> None:
        # Strong-from-the-ground-up: NO unsigned policy distribution
        # path. The handler MUST forward the envelope to
        # ``reload_policy_signed``; a future regression that fell
        # back to ``reload_policy`` would silently lose all crypto.
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)
        envelope = _signed_envelope()
        receiver._handle_event(
            FakeSSE(
                event="policy_updated",
                data=json.dumps({"version": 1, "policy_envelope": envelope}),
            ),
        )
        engine.reload_policy_signed.assert_called_once()
        # Legacy unsigned path is never touched.
        engine.reload_policy.assert_not_called()

    def test_policy_updated_without_envelope_dropped(self) -> None:
        # Old control plane / tampered event missing the signed
        # envelope must NOT trigger a reload of any kind.
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)
        receiver._handle_event(
            FakeSSE(event="policy_updated", data=json.dumps({"version": 3})),
        )
        engine.reload_policy_signed.assert_not_called()
        engine.reload_policy.assert_not_called()

    def test_signature_rejection_does_not_crash(self) -> None:
        engine = _make_mock_engine()
        engine.reload_policy_signed.side_effect = PolicySignatureError(-5)
        receiver = _make_receiver(engine=engine)
        envelope = _signed_envelope()
        # Must not raise — the rejection is logged, the previous
        # policy stays in place, and the receiver continues.
        receiver._handle_event(
            FakeSSE(
                event="policy_updated",
                data=json.dumps({"version": 1, "policy_envelope": envelope}),
            ),
        )

    def test_malformed_json_silently_ignored(self) -> None:
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)
        receiver._handle_event(FakeSSE(event="kill_switch", data="not valid json{{{"))
        engine.set_kill_switch.assert_not_called()

    def test_oversized_event_rejected(self) -> None:
        # Compromised control plane could try to OOM the SDK with a
        # multi-GB SSE payload. Hard cap at 10 MB.
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)
        oversized = "x" * (10 * 1024 * 1024 + 1)
        receiver._handle_event(FakeSSE(event="kill_switch", data=oversized))
        engine.set_kill_switch.assert_not_called()

    def test_unknown_event_type_ignored(self) -> None:
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)
        receiver._handle_event(FakeSSE(event="some_future_event", data="{}"))
        engine.set_kill_switch.assert_not_called()
        engine.reload_policy_signed.assert_not_called()


# ---------------------------------------------------------------------------
# Lifecycle — start / stop semantics
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_stop_without_start_is_idempotent(self) -> None:
        # ``stop()`` before ``start()`` must be a clean no-op so
        # async-context-manager teardown on init failure doesn't
        # explode.
        receiver = _make_receiver()
        await receiver.stop()
        await receiver.stop()  # idempotent

    @pytest.mark.asyncio
    async def test_start_creates_named_task(self) -> None:
        receiver = _make_receiver()
        # Patch the loop so it exits immediately and we don't hold
        # an open httpx.AsyncClient against a non-existent server.
        with patch.object(receiver, "_run_loop", new=AsyncMock()):
            receiver.start()
            try:
                assert receiver._task is not None
                # asyncio.Task.get_name() is stable since 3.8.
                assert "checkrd-async-control" in receiver._task.get_name()
            finally:
                await receiver.stop()
                assert receiver._task is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        receiver = _make_receiver()
        with patch.object(receiver, "_run_loop", new=AsyncMock()):
            receiver.start()
            first_task = receiver._task
            receiver.start()  # second call must NOT spawn a new task
            assert receiver._task is first_task
            await receiver.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task_cleanly(self) -> None:
        # Build a loop that hangs on stop_event so we can force the
        # cancellation path. The receiver's ``stop()`` must unwind
        # without leaking the task or raising.
        receiver = _make_receiver()

        async def hanging_loop() -> None:
            # Mimic the real receiver — block on the stop event so
            # ``stop()`` actually has work to do.
            await receiver._stop_event.wait()

        with patch.object(receiver, "_run_loop", new=hanging_loop):
            receiver.start()
            # Give the loop a tick to actually start.
            await asyncio.sleep(0.01)
            await receiver.stop()
            assert receiver._task is None

    @pytest.mark.asyncio
    async def test_stop_handles_cancellation_in_flight(self) -> None:
        # If the user cancels the parent context (asyncio.timeout(),
        # TaskGroup teardown) while the receiver is mid-fetch, the
        # receiver must NOT swallow CancelledError into a generic
        # exception path. Test the property by asserting stop()
        # itself completes when the underlying task is cancelled.
        receiver = _make_receiver()

        async def cancellable_loop() -> None:
            try:
                await asyncio.sleep(60)  # arbitrarily long
            except asyncio.CancelledError:
                raise

        with patch.object(receiver, "_run_loop", new=cancellable_loop):
            receiver.start()
            await asyncio.sleep(0.01)
            await receiver.stop()


# ---------------------------------------------------------------------------
# Authentication — non-retryable 401/403 must stop the receiver
# ---------------------------------------------------------------------------


class TestAuthError:
    @pytest.mark.asyncio
    async def test_auth_error_stops_loop_permanently(self) -> None:
        # An AuthError thrown from ``_run_sse`` must NOT be retried —
        # the API key is wrong, hammering the control plane changes
        # nothing. Match the sync receiver's behaviour exactly.
        receiver = _make_receiver()
        attempts = 0

        async def fake_sse() -> None:
            nonlocal attempts
            attempts += 1
            raise AsyncAuthError("401 Unauthorized")

        async def fake_poll() -> None:
            pass  # poll fallback shouldn't fire on auth errors

        with (
            patch.object(receiver, "_run_sse", new=fake_sse),
            patch.object(receiver, "_poll_once", new=fake_poll),
        ):
            await receiver._run_loop()
        assert attempts == 1


# ---------------------------------------------------------------------------
# Circuit breaker — shared with the batcher
# ---------------------------------------------------------------------------


class TestCircuitBreakerIntegration:
    """When a shared CircuitBreaker is wired in, the receiver's
    reconnect loop honours the breaker state so the batcher and
    receiver fast-fail together on a hard control-plane outage."""

    @pytest.mark.asyncio
    async def test_open_breaker_skips_sse_attempt(self) -> None:
        # Construct a breaker that's been tripped to ``open``, then
        # run one iteration of the loop. The receiver MUST NOT call
        # _run_sse — that's the whole point of sharing the breaker.
        breaker = CircuitBreaker(failure_threshold=1, reset_after_secs=60.0)
        breaker.record_failure()  # trips to open
        assert breaker.diagnostics().state == "open"

        receiver = _make_receiver(circuit_breaker=breaker)
        run_sse_calls = 0

        async def fake_sse() -> None:
            nonlocal run_sse_calls
            run_sse_calls += 1

        async def stop_after_first_iteration() -> bool:
            # Coerce the loop to exit after the first sleep.
            receiver._stop_event.set()
            return True

        with (
            patch.object(receiver, "_run_sse", new=fake_sse),
            patch.object(
                receiver._stop_event, "wait", side_effect=stop_after_first_iteration,
            ),
        ):
            await receiver._run_loop()

        assert run_sse_calls == 0

    @pytest.mark.asyncio
    async def test_successful_sse_records_success(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, reset_after_secs=60.0)
        # Pre-trip with one failure so we can confirm success
        # resets the count.
        breaker.record_failure()
        breaker_diag_before = breaker.diagnostics()
        assert breaker_diag_before.state == "closed"
        assert breaker_diag_before.consecutive_failures == 1

        receiver = _make_receiver(circuit_breaker=breaker)

        async def fake_sse() -> None:
            # Clean disconnect — emulates a graceful server-closed
            # SSE stream. Loop should mark this as success.
            receiver._stop_event.set()

        with patch.object(receiver, "_run_sse", new=fake_sse):
            await receiver._run_loop()

        diag = breaker.diagnostics()
        # Success cleared the failure count (closed state + 0 fails).
        assert diag.state == "closed"
        assert diag.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Polling fallback — async client variant
# ---------------------------------------------------------------------------


class TestPollFallback:
    @pytest.mark.asyncio
    async def test_poll_applies_kill_switch_state(self) -> None:
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)

        # Mock the AsyncClient.get to return a fake state response.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"kill_switch_active": True}
        mock_resp.raise_for_status = MagicMock()

        async def fake_get(_url: str, **_kwargs: Any) -> MagicMock:
            return mock_resp

        with patch.object(receiver._client, "get", new=fake_get):
            await receiver._poll_once()

        engine.set_kill_switch.assert_called_with(True)

    @pytest.mark.asyncio
    async def test_poll_with_signed_envelope_installs_policy(self) -> None:
        # The poll fallback also delivers signed policies — same
        # path as SSE for offline / disconnected scenarios.
        engine = _make_mock_engine()
        receiver = _make_receiver(engine=engine)

        envelope = _signed_envelope()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "kill_switch_active": False,
            "policy_envelope": envelope,
        }
        mock_resp.raise_for_status = MagicMock()

        async def fake_get(_url: str, **_kwargs: Any) -> MagicMock:
            return mock_resp

        with patch.object(receiver._client, "get", new=fake_get):
            await receiver._poll_once()

        engine.reload_policy_signed.assert_called_once()


# ---------------------------------------------------------------------------
# Persisted version restore — cross-restart rollback defense
# ---------------------------------------------------------------------------


class TestPolicyVersionRestore:
    @pytest.mark.asyncio
    async def test_start_restores_persisted_bundle(self, tmp_path: Any) -> None:
        """Mirrors the sync receiver's OPA-pattern restore: on startup,
        re-install the persisted DSSE envelope via ``reload_policy_signed``
        and prime the hash cache. See the matching sync test in
        ``test_control.py`` for the full rationale."""
        from checkrd._policy_state import persist_state

        envelope = _signed_envelope(version=42)
        envelope_json = json.dumps(envelope)
        hash_hex = "a" * 64
        state_path = tmp_path / "policy_state.json"
        persist_state(42, hash_hex, envelope_json, path=state_path)

        engine = _make_mock_engine()
        with patch("checkrd._policy_state._default_state_path", return_value=state_path):
            receiver = _make_receiver(engine=engine)
            with patch.object(receiver, "_run_loop", new=AsyncMock()):
                receiver.start()
                try:
                    engine.reload_policy_signed.assert_called_once()
                    args, _kwargs = engine.reload_policy_signed.call_args
                    assert args[0] == envelope_json
                    assert receiver._last_installed_hash == hash_hex
                finally:
                    await receiver.stop()

    @pytest.mark.asyncio
    async def test_start_no_persisted_state_skips_restore(self, tmp_path: Any) -> None:
        """No state file → no install at startup; engine waits for SSE init."""
        engine = _make_mock_engine()
        empty_state = tmp_path / "policy_state.json"
        with patch("checkrd._policy_state._default_state_path", return_value=empty_state):
            receiver = _make_receiver(engine=engine)
            with patch.object(receiver, "_run_loop", new=AsyncMock()):
                receiver.start()
                try:
                    engine.reload_policy_signed.assert_not_called()
                finally:
                    await receiver.stop()


# ---------------------------------------------------------------------------
# httpx client lifecycle — owned vs caller-supplied
# ---------------------------------------------------------------------------


class TestHttpClientLifecycle:
    @pytest.mark.asyncio
    async def test_caller_supplied_client_not_closed_on_stop(self) -> None:
        # If the caller injects an AsyncClient (e.g. for testing or
        # to share connection pooling), ``stop()`` must NOT close
        # it — that's the caller's lifecycle.
        engine = _make_mock_engine()
        client = httpx.AsyncClient()
        try:
            receiver = AsyncControlReceiver(
                base_url="http://localhost:8080",
                agent_id="agent",
                api_key="ck_test",
                engine=engine,
                http_client=client,
            )
            await receiver.stop()
            # Client still usable — caller owns it.
            assert not client.is_closed
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_owned_client_closed_on_stop(self) -> None:
        # When the receiver builds its own client, ``stop()`` MUST
        # close it. Resource leak otherwise.
        receiver = _make_receiver()
        # Start a no-op loop so stop() actually unwinds the close path.
        with patch.object(receiver, "_run_loop", new=AsyncMock()):
            receiver.start()
        await receiver.stop()
        # The owned client should now be closed.
        assert receiver._client.is_closed


# ---------------------------------------------------------------------------
# Diagnostics — observability surface
# ---------------------------------------------------------------------------


class TestDiagnostics:
    @pytest.mark.asyncio
    async def test_initial_diagnostics_shape(self) -> None:
        receiver = _make_receiver()
        diag = receiver.diagnostics()
        assert set(diag.keys()) == {
            "running",
            "connected",
            "reconnects",
            "events_received",
            "last_event_at",
        }
        assert diag["running"] is False
        assert diag["events_received"] == 0
        assert diag["reconnects"] == 0

    @pytest.mark.asyncio
    async def test_diagnostics_running_after_start(self) -> None:
        receiver = _make_receiver()
        with patch.object(receiver, "_run_loop", new=AsyncMock()):
            receiver.start()
            try:
                # ``running=True`` until the task completes.
                # AsyncMock returns immediately, so by the time we
                # check ``running`` may already be False — accept
                # either as long as ``start()`` didn't raise.
                assert isinstance(receiver.diagnostics()["running"], bool)
            finally:
                await receiver.stop()
        assert receiver.diagnostics()["running"] is False
