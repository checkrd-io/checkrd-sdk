"""Integration tests for checkrd.wrap() and checkrd.wrap_async()."""

from __future__ import annotations

import httpx
import pytest

from checkrd import CheckrdInitError, CheckrdPolicyDenied, wrap, wrap_async
from tests.conftest import ALLOW_ALL_POLICY, SAMPLE_POLICY, requires_wasm


def _mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@requires_wasm
class TestWrap:
    def test_returns_same_client(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrapped = wrap(client, agent_id="test", policy=ALLOW_ALL_POLICY)
            assert wrapped is client

    def test_allowed_request(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=SAMPLE_POLICY)

            response = client.get("https://api.stripe.com/v1/charges")
            assert response.status_code == 200
            assert response.json() == {"ok": True}

    def test_denied_request_raises(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=SAMPLE_POLICY)

            with pytest.raises(CheckrdPolicyDenied, match="block-deletes"):
                client.delete("https://api.stripe.com/v1/charges")

    def test_default_deny_unknown_host(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=SAMPLE_POLICY)

            with pytest.raises(CheckrdPolicyDenied, match="default policy"):
                client.get("https://unknown.com/api")

    def test_invalid_policy_raises_init_error(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            with pytest.raises(CheckrdInitError):
                wrap(client, agent_id="test", policy={"not": "a valid policy"})

    def test_disabled_via_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKRD_DISABLED", "1")
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            # wrap() should return the client unchanged -- no policy needed
            wrapped = wrap(client, agent_id="test", policy=ALLOW_ALL_POLICY)
            assert wrapped is client
            # Requests pass through without policy evaluation
            response = client.delete("https://anything.com/dangerous")
            assert response.status_code == 200

    def test_disabled_accepts_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKRD_DISABLED", "true")
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=ALLOW_ALL_POLICY)
            response = client.delete("https://anything.com/dangerous")
            assert response.status_code == 200

    def test_not_disabled_by_default(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=SAMPLE_POLICY)
            # Should still enforce policy
            with pytest.raises(CheckrdPolicyDenied):
                client.delete("https://api.stripe.com/v1/charges")

    def test_dry_run_logs_but_allows(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=SAMPLE_POLICY, enforce=False)

            # This would normally be denied, but dry-run lets it through
            response = client.delete("https://api.stripe.com/v1/charges")
            assert response.status_code == 200

    def test_policy_as_dict(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=ALLOW_ALL_POLICY)

            response = client.get("https://anything.com/any/path")
            assert response.status_code == 200


@requires_wasm
class TestWrapAsync:
    # Async clients are wrapped in `async with` so they close cleanly. Without
    # this, pytest-xdist workers can emit "Task was destroyed but it is pending"
    # warnings when the GC runs across event-loop boundaries.

    @pytest.mark.asyncio
    async def test_allowed_request(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap_async(client, agent_id="test", policy=SAMPLE_POLICY)

            response = await client.get("https://api.stripe.com/v1/charges")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_denied_request_raises(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap_async(client, agent_id="test", policy=SAMPLE_POLICY)

            with pytest.raises(CheckrdPolicyDenied):
                await client.delete("https://api.stripe.com/v1/charges")


# ============================================================
# Production identity (Tier 1): wrap() with from_env / from_file / from_bytes
# ============================================================


@requires_wasm
class TestWrapWithProductionIdentity:
    """Verify wrap() accepts the production LocalIdentity constructors and
    that telemetry is signed with the explicitly-provided key."""

    def test_wrap_with_env_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64

        from checkrd import LocalIdentity
        from checkrd.engine import WasmEngine

        private, _ = WasmEngine.generate_keypair()
        monkeypatch.setenv("CHECKRD_AGENT_KEY", base64.b64encode(private).decode())
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrapped = wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                identity=LocalIdentity.from_env(),
            )
            # Wrap succeeded without auto-generating a dev key
            assert wrapped is client

            response = client.get("https://api.stripe.com/v1/charges")
            assert response.status_code == 200

    def test_wrap_with_file_identity(self, tmp_path) -> None:
        from checkrd import LocalIdentity
        from checkrd.engine import WasmEngine

        private, public = WasmEngine.generate_keypair()
        key_file = tmp_path / "identity.key"
        key_file.write_bytes(private + public)
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                identity=LocalIdentity.from_file(key_file),
            )
            response = client.get("https://api.stripe.com/v1/charges")
            assert response.status_code == 200

    def test_wrap_with_bytes_identity(self) -> None:
        from checkrd import LocalIdentity
        from checkrd.engine import WasmEngine

        private, _ = WasmEngine.generate_keypair()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                identity=LocalIdentity.from_bytes(private),
            )
            response = client.get("https://api.stripe.com/v1/charges")
            assert response.status_code == 200

    def test_explicit_identity_does_not_create_default_key_file(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Critical: when an explicit identity is provided, the default
        # ~/.checkrd/identity.key path must NOT be touched. This is the
        # property that makes production deployments not depend on a
        # writable home directory.
        import base64

        from checkrd import LocalIdentity
        from checkrd.engine import WasmEngine

        # Redirect default key path into a fresh tmp dir
        monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
        default_key = tmp_path / "identity.key"

        private, _ = WasmEngine.generate_keypair()
        monkeypatch.setenv("CHECKRD_AGENT_KEY", base64.b64encode(private).decode())
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                identity=LocalIdentity.from_env(),
            )

            # The dev key file must NOT have been auto-created
            assert not default_key.exists(), \
            "explicit identity must not trigger auto-generation"


# ============================================================
# Tier 3: wrap() with custom telemetry sink and file watchers
# ============================================================


@requires_wasm
class TestWrapWithCustomSink:
    def test_wrap_with_json_file_sink_writes_events(self, tmp_path) -> None:
        import json

        from checkrd.sinks import JsonFileSink

        sink_path = tmp_path / "events.jsonl"
        sink = JsonFileSink(sink_path)
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                telemetry_sink=sink,
            )

            client.get("https://api.stripe.com/v1/charges")
            client.get("https://api.stripe.com/v1/customers")

            # Stop the sink to flush
            sink.stop()

            lines = sink_path.read_text().strip().splitlines()
            assert len(lines) == 2
            for line in lines:
                event = json.loads(line)
                assert event.get("policy_result") == "allowed"

    def test_wrap_with_logging_sink(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from checkrd.sinks import LoggingSink

        sink = LoggingSink()
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                telemetry_sink=sink,
            )

            with caplog.at_level(logging.INFO, logger="checkrd.telemetry"):
                client.get("https://api.stripe.com/v1/charges")

            records = [r for r in caplog.records if r.name == "checkrd.telemetry"]
            assert len(records) >= 1

    def test_explicit_sink_overrides_control_plane_batcher(
        self, tmp_path
    ) -> None:
        # When both control plane creds AND an explicit sink are provided,
        # the explicit sink wins (it's the customer's documented override).
        import json

        from checkrd.sinks import JsonFileSink

        sink_path = tmp_path / "events.jsonl"
        sink = JsonFileSink(sink_path)
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                telemetry_sink=sink,
                control_plane_url="http://localhost:1",  # would normally create batcher
                api_key="ck_test_xxx",
            )

            client.get("https://api.stripe.com/v1/charges")
            sink.stop()

            # Events went to the explicit sink, not the would-be batcher
            lines = sink_path.read_text().strip().splitlines()
            assert len(lines) == 1
            event = json.loads(lines[0])
            assert event["policy_result"] == "allowed"

    def test_no_sink_no_control_plane_uses_logging_only(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=ALLOW_ALL_POLICY)
            response = client.get("https://api.stripe.com/v1/charges")
            assert response.status_code == 200


@requires_wasm
class TestWrapWithFileWatchers:
    def test_policy_watch_starts_watcher(self, tmp_path) -> None:
        from checkrd.watchers import PolicyFileWatcher

        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(
            """
agent: test-agent
default: allow
rules: []
"""
        )
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=policy_file,
                policy_watch=True,
                policy_watch_interval_secs=0.1,
            )

            watchers = getattr(client, "_checkrd_watchers", [])
            assert any(isinstance(w, PolicyFileWatcher) for w in watchers)
            # Cleanup
            for w in watchers:
                w.stop()

    def test_killswitch_file_starts_watcher(self, tmp_path) -> None:
        from checkrd.watchers import KillSwitchFileWatcher

        sentinel = tmp_path / "killswitch"
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                killswitch_file=sentinel,
                killswitch_poll_interval_secs=0.1,
            )

            watchers = getattr(client, "_checkrd_watchers", [])
            assert any(isinstance(w, KillSwitchFileWatcher) for w in watchers)
            for w in watchers:
                w.stop()

    def test_no_watchers_by_default(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(client, agent_id="test", policy=ALLOW_ALL_POLICY)
            watchers = getattr(client, "_checkrd_watchers", [])
            assert watchers == []

    def test_policy_watch_with_dict_policy_does_not_spawn_watcher(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrap(
                client,
                agent_id="test",
                policy=ALLOW_ALL_POLICY,
                policy_watch=True,
            )
            watchers = getattr(client, "_checkrd_watchers", [])
            assert watchers == []
