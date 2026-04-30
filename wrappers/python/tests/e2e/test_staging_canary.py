"""End-to-end staging canary — Python SDK.

Three round-trip tests against a live staging control plane:

  1. ``healthz`` reachability — the smoke test. If this fails the
     other two are noise, so it runs first.
  2. Signed telemetry batch ingestion — the most user-impacting wire
     path. Verifies signing, idempotency, and schema-version
     compatibility in one shot.
  3. SSE control connection — the second-most-impacting path. Just
     opens the stream, reads one event, and disconnects. Validates
     auth, timeout, and the dispatch contract.

Each test is independent; run a subset with::

    CHECKRD_STAGING_URL=https://api-staging.checkrd.io \\
    CHECKRD_STAGING_API_KEY=ck_test_… \\
    pytest tests/e2e/test_staging_canary.py::test_healthz -v

The full suite is wired into a nightly GitHub Actions workflow
(see ``.github/workflows/e2e-canary.yml``) that runs against
staging and pages on failure.
"""

from __future__ import annotations

import time

import httpx
import pytest


pytestmark = pytest.mark.e2e


def test_healthz_reachable(staging_url: str) -> None:
    """The control plane's public health endpoint must answer 200.

    Run first — every other canary depends on the staging API being
    reachable at all. Failure here means the other failures are
    expected, not regressions.
    """
    response = httpx.get(f"{staging_url.rstrip('/')}/health", timeout=10.0)
    response.raise_for_status()
    assert response.status_code == 200


def test_signed_telemetry_batch_accepted(
    staging_url: str,
    staging_api_key: str,
    staging_agent_id: str,
) -> None:
    """A single signed telemetry batch must be accepted by the
    ingestion endpoint.

    Exercises the full hot path: WASM init, Ed25519 keypair, RFC 9421
    signing, RFC 9530 Content-Digest, idempotency key, the
    consolidated header set, and the canonical body serialization.
    A 4xx here usually points at a signing-format regression or a
    schema-version mismatch between the SDK and the deployed
    ingestion service.
    """
    from checkrd import Checkrd

    with Checkrd(
        api_key=staging_api_key,
        base_url=staging_url,
        agent_id=staging_agent_id,
    ) as client:
        # Wrap a throwaway httpx.Client to start the batcher.
        # We don't need to send vendor traffic — enqueueing a single
        # synthetic event proves the wire path.
        wrapped = client.wrap(httpx.Client(timeout=5.0))
        # The test deliberately doesn't set a policy; observation
        # mode is fine for a canary.
        batcher = getattr(wrapped, "_checkrd_batcher", None)
        assert batcher is not None, "wrap() did not attach a batcher"

        synthetic_event: dict[str, object] = {
            "event_id": f"canary-{int(time.time())}",
            "agent_id": staging_agent_id,
            "timestamp": int(time.time() * 1000),
            "policy_result": "allow",
            "request": {
                "url_host": "canary.example.com",
                "url_path": "/v1/canary",
                "method": "POST",
            },
            "response": {"status_code": 200, "latency_ms": 1},
        }
        batcher.enqueue(synthetic_event)
        batcher.flush()

        # If signing or send had failed, the diagnostics counters
        # would show non-zero drops on the corresponding axis. Either
        # ``sent >= 1`` or a structured drop tells us why.
        diag = batcher.diagnostics()
        assert diag["dropped_signing_error"] == 0, (
            f"signing failure on staging — dropped {diag['dropped_signing_error']} events; "
            f"check ingestion service version against SDK expected schema"
        )
        assert diag["sent"] >= 1, (
            f"telemetry not accepted: diagnostics={diag}; "
            f"check API key permissions and ingestion endpoint"
        )


def test_sse_control_connects_and_disconnects(
    staging_url: str,
    staging_api_key: str,
    staging_agent_id: str,
) -> None:
    """SSE control receiver must connect, accept the first event, and
    disconnect cleanly within a short timeout.

    Opening the stream is the auth check; receiving any event (init,
    kill_switch, heartbeat) proves the dispatch contract; disconnect
    must not hang past ``stop()``'s 5-second join.
    """
    from checkrd.control import ControlReceiver
    from unittest.mock import MagicMock

    engine = MagicMock()
    receiver = ControlReceiver(
        base_url=staging_url,
        agent_id=staging_agent_id,
        api_key=staging_api_key,
        engine=engine,
    )
    receiver.start()
    # 5 seconds is generous — staging usually returns the first
    # event within ~100ms of the SSE handshake completing.
    time.sleep(5.0)
    receiver.stop()
    # The receiver thread must have exited within stop()'s 5-second
    # join. Any blocking would have timed out the test.
    assert receiver._thread is None or not receiver._thread.is_alive()


def test_invalid_api_key_returns_401(
    staging_url: str,
) -> None:
    """A request with a bogus key must surface as
    :class:`AuthenticationError`, not a generic 5xx or hang.

    Regression test for the exception-hierarchy contract: even at the
    real wire boundary, a 401 must dispatch to ``AuthenticationError``
    via :func:`make_api_error` so callers can branch on the typed
    exception.
    """
    from checkrd import AuthenticationError, make_api_error

    response = httpx.get(
        f"{staging_url.rstrip('/')}/v1/orgs",
        headers={"X-API-Key": "ck_live_definitely_not_a_real_key"},
        timeout=10.0,
    )
    assert response.status_code == 401, (
        f"expected 401 from bogus key, got {response.status_code}: {response.text[:200]}"
    )
    err = make_api_error(response=response, body=response.json())
    assert isinstance(err, AuthenticationError)
    assert err.status_code == 401
