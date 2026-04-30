"""Platform headers + Checkrd-Version pinning.

Every control-plane request MUST carry ``X-Checkrd-SDK-*`` metadata so
operators running the Checkrd dashboard can answer questions like "what
fraction of the fleet is on SDK <0.3.0" or "are Python 3.9 callers
disproportionately seeing errors" without per-customer forensics. Match
pattern: OpenAI / Anthropic ``X-Stainless-*``.

Parallel test file to the JS SDK's ``tests/platform_headers.test.ts``.
When the header contract changes, both sides change.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from checkrd._platform import (
    _reset_platform_info_for_testing,
    default_control_headers,
    platform_headers,
    platform_info,
)
from checkrd._version import __version__


@pytest.fixture(autouse=True)
def _reset_snapshot() -> None:
    _reset_platform_info_for_testing()


class TestPlatformInfo:
    def test_lang_is_python(self) -> None:
        assert platform_info().lang == "python"

    def test_sdk_version_matches_package(self) -> None:
        # A regression here would cause a silent telemetry drift: clients
        # would look like they're on a different version than pip shows.
        assert platform_info().sdk_version == __version__

    def test_runtime_includes_cpython_or_pypy(self) -> None:
        assert platform_info().runtime in {
            "cpython",
            "pypy",
            "jython",
            "ironpython",
            "graalpy",
        }

    def test_runtime_version_is_semver_shaped(self) -> None:
        assert "." in platform_info().runtime_version

    def test_os_and_arch_are_non_empty(self) -> None:
        info = platform_info()
        assert info.os
        assert info.arch

    def test_snapshot_is_memoized(self) -> None:
        # Detection is cheap but not free — must be cached so the
        # telemetry hot path doesn't recompute on every batch.
        assert platform_info() is platform_info()

    def test_reset_helper_reruns_detection(self) -> None:
        first = platform_info()
        _reset_platform_info_for_testing()
        second = platform_info()
        assert first is not second
        assert first.sdk_version == second.sdk_version


class TestPlatformHeaders:
    def test_emits_all_six_headers(self) -> None:
        h = platform_headers()
        assert set(h.keys()) == {
            "X-Checkrd-SDK-Lang",
            "X-Checkrd-SDK-Version",
            "X-Checkrd-SDK-Runtime",
            "X-Checkrd-SDK-Runtime-Version",
            "X-Checkrd-SDK-OS",
            "X-Checkrd-SDK-Arch",
        }

    def test_values_are_non_empty_strings(self) -> None:
        # Ingestion cannot handle `None` / empty values — the header
        # family is an "always six strings" contract.
        for _, v in platform_headers().items():
            assert isinstance(v, str)
            assert v


class TestDefaultControlHeaders:
    def test_includes_platform_family(self) -> None:
        h = default_control_headers("ck_test_xyz")
        assert h["X-Checkrd-SDK-Lang"] == "python"
        assert h["X-Checkrd-SDK-Version"] == __version__

    def test_sets_api_key(self) -> None:
        assert default_control_headers("ck_live_abc")["X-API-Key"] == "ck_live_abc"

    def test_user_agent_matches_python_sdk(self) -> None:
        assert (
            default_control_headers("k")["User-Agent"]
            == f"checkrd-python/{__version__}"
        )

    def test_omits_checkrd_version_by_default(self) -> None:
        assert "Checkrd-Version" not in default_control_headers("k")

    def test_stamps_checkrd_version_when_set(self) -> None:
        assert (
            default_control_headers("k", api_version="2026-04-24")["Checkrd-Version"]
            == "2026-04-24"
        )

    def test_omits_idempotency_key_when_none(self) -> None:
        # Unlike the JS helper, the Python helper leaves Idempotency-Key
        # generation to callers — they manage a stable-per-retry-loop
        # UUID themselves. This matches how `_maybe_register_public_key`
        # generates the key once outside the retry loop.
        assert "Idempotency-Key" not in default_control_headers("k")

    def test_stamps_idempotency_key_when_provided(self) -> None:
        h = default_control_headers("k", idempotency_key="stable-uuid-123")
        assert h["Idempotency-Key"] == "stable-uuid-123"

    def test_empty_content_type_omits_the_header(self) -> None:
        # GET paths (SSE, state poll) pass ``content_type=""`` so the
        # Content-Type header is suppressed entirely rather than sent as
        # an empty or misleading value.
        assert "Content-Type" not in default_control_headers("k", content_type="")

    def test_default_content_type_is_json(self) -> None:
        assert default_control_headers("k")["Content-Type"] == "application/json"


class TestBatcherSendsPlatformHeaders:
    """Verify the batcher actually stamps the platform headers on the
    outbound POST — the helper is only valuable if every send site uses it.
    """

    @pytest.mark.requires_wasm
    def test_telemetry_post_includes_platform_headers(self) -> None:
        from tests.test_batcher import _make_batcher, sample_event

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        def capture(*args: Any, **kwargs: Any) -> Any:
            capture.req = args[0]  # type: ignore[attr-defined]
            return mock_resp

        batcher = _make_batcher()
        try:
            with patch("checkrd.batcher.urlopen", capture):
                batcher.enqueue(sample_event("req-platform"))
                batcher.flush()
        finally:
            batcher.stop()

        req = capture.req  # type: ignore[attr-defined]
        assert req.get_header("X-checkrd-sdk-lang") == "python"
        assert req.get_header("X-checkrd-sdk-version") == __version__
        assert req.get_header("User-agent") == f"checkrd-python/{__version__}"

    @pytest.mark.requires_wasm
    def test_telemetry_post_stamps_checkrd_version_when_configured(self) -> None:
        from tests.test_batcher import sample_event
        from checkrd.batcher import TelemetryBatcher
        from checkrd.engine import WasmEngine

        private, _ = WasmEngine.generate_keypair()
        engine = WasmEngine(
            policy_json='{"agent":"t","default":"allow","rules":[]}',
            agent_id="t",
            private_key_bytes=private,
        )
        batcher = TelemetryBatcher(
            base_url="http://localhost:8081",
            api_key="ck_test",
            engine=engine,
            signer_agent_id="550e8400-e29b-41d4-a716-446655440000",
            api_version="2026-04-24",
        )
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        def capture(*args: Any, **kwargs: Any) -> Any:
            capture.req = args[0]  # type: ignore[attr-defined]
            return mock_resp

        try:
            with patch("checkrd.batcher.urlopen", capture):
                batcher.enqueue(sample_event("req-version"))
                batcher.flush()
        finally:
            batcher.stop()

        req = capture.req  # type: ignore[attr-defined]
        assert req.get_header("Checkrd-version") == "2026-04-24"


def test_requires_wasm_marker_registered() -> None:
    # Sanity check that the `requires_wasm` marker used above is wired
    # up — same pattern as other test files in the suite.
    from tests.conftest import requires_wasm  # noqa: F401
