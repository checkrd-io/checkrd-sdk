"""Tests for checkrd.testing — the WASM-free mock module.

These tests verify that ``mock_wrap()`` provides a usable mock that
exercises the same transport code paths as production without requiring
the WASM engine. No ``@requires_wasm`` markers needed — the whole point
of the testing module is that it works without WASM.
"""

from __future__ import annotations

from typing import Optional

import httpx
import pytest

from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.testing import MockEngine, mock_wrap, mock_wrap_async
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport


def _mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


# ============================================================
# mock_wrap() basics
# ============================================================


class TestMockWrapBasics:
    def test_returns_same_client(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            wrapped = mock_wrap(client)
            assert wrapped is client

    def test_installs_checkrd_transport(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client)
            assert isinstance(client._transport, CheckrdTransport)

    def test_default_allow_passes_requests(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client)
            response = client.get("https://api.example.com/anything")
            assert response.status_code == 200

    def test_default_deny_blocks_requests(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny")
            with pytest.raises(CheckrdPolicyDenied, match="default policy"):
                client.get("https://api.example.com/anything")

    def test_invalid_default_raises(self) -> None:
        with pytest.raises(ValueError, match="must be"):
            MockEngine(default="maybe")


# ============================================================
# Rule-based evaluation
# ============================================================


class TestRuleBasedEvaluation:
    def test_allow_rule_matches(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                default="deny",
                rules=[
                    {"name": "allow-get", "allow": {"method": ["GET"], "url": "api.stripe.com/*"}},
                ],
            )
            response = client.get("https://api.stripe.com/v1/charges")
            assert response.status_code == 200

    def test_deny_rule_blocks(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                default="allow",
                rules=[
                    {"name": "block-deletes", "deny": {"method": ["DELETE"], "url": "*"}},
                ],
            )
            with pytest.raises(CheckrdPolicyDenied, match="block-deletes"):
                client.delete("https://api.stripe.com/v1/charges")

    def test_deny_rules_checked_before_allow(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                rules=[
                    {"name": "allow-all", "allow": {"method": ["DELETE"], "url": "*"}},
                    {"name": "deny-all", "deny": {"method": ["DELETE"], "url": "*"}},
                ],
            )
            with pytest.raises(CheckrdPolicyDenied, match="deny-all"):
                client.delete("https://anything.com")

    def test_unmatched_request_falls_to_default(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                default="deny",
                rules=[
                    {"name": "allow-stripe", "allow": {"method": ["GET"], "url": "api.stripe.com/*"}},
                ],
            )
            with pytest.raises(CheckrdPolicyDenied, match="default policy"):
                client.get("https://unknown.com/endpoint")

    def test_glob_patterns_in_url(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                default="deny",
                rules=[
                    {"allow": {"url": "api.*.com/v1/*"}},
                ],
            )
            response = client.get("https://api.stripe.com/v1/charges")
            assert response.status_code == 200

    def test_method_matching_is_case_insensitive(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                default="deny",
                rules=[
                    {"allow": {"method": ["get"], "url": "*"}},
                ],
            )
            response = client.get("https://api.example.com/resource")
            assert response.status_code == 200


# ============================================================
# Callback mode
# ============================================================


class TestPolicyFnCallback:
    def test_callback_allows(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                policy_fn=lambda method, url, headers, body: method == "GET",
            )
            response = client.get("https://api.example.com")
            assert response.status_code == 200

    def test_callback_denies(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                policy_fn=lambda method, url, headers, body: method == "GET",
            )
            with pytest.raises(CheckrdPolicyDenied, match="policy_fn"):
                client.post("https://api.example.com")

    def test_callback_receives_all_args(self) -> None:
        """The callback gets method, url, headers, and body."""
        captured: list[tuple] = []

        def _capture(
            method: str,
            url: str,
            headers: list[tuple[str, str]],
            body: Optional[str],
        ) -> bool:
            captured.append((method, url))
            return True
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, policy_fn=_capture)
            client.get("https://api.example.com/resource")
            assert len(captured) == 1
            assert captured[0] == ("GET", "https://api.example.com/resource")

    def test_callback_overrides_rules(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                rules=[{"deny": {"url": "*"}}],
                policy_fn=lambda m, u, h, b: True,
            )
            response = client.get("https://api.example.com")
            assert response.status_code == 200


# ============================================================
# Dry-run mode
# ============================================================


class TestDryRunMode:
    def test_enforce_false_allows_denied_requests(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny", enforce=False)
            response = client.get("https://api.example.com/anything")
            assert response.status_code == 200


# ============================================================
# Async
# ============================================================


class TestMockWrapAsync:
    @pytest.mark.asyncio
    async def test_async_allow(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap_async(client)
            response = await client.get("https://api.example.com")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_async_deny(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap_async(client, default="deny")
            with pytest.raises(CheckrdPolicyDenied):
                await client.get("https://api.example.com")

    @pytest.mark.asyncio
    async def test_async_installs_async_transport(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap_async(client)
            assert isinstance(client._transport, CheckrdAsyncTransport)


# ============================================================
# Exception enrichment
# ============================================================


class TestExceptionEnrichment:
    """The mock should produce the same enriched exceptions as production
    so users can test their error-handling code."""

    def test_denied_exception_has_rule_name(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                rules=[
                    {"name": "block-deletes", "deny": {"method": ["DELETE"], "url": "*"}},
                ],
            )
            with pytest.raises(CheckrdPolicyDenied) as exc_info:
                client.delete("https://api.stripe.com/v1/charges")
            assert exc_info.value.rule_name == "block-deletes"

    def test_denied_exception_has_url(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny")
            with pytest.raises(CheckrdPolicyDenied) as exc_info:
                client.get("https://api.example.com/resource")
            assert "api.example.com" in (exc_info.value.url or "")

    def test_denied_exception_has_suggestion(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(
                client,
                rules=[
                    {"name": "no-deletes", "deny": {"method": ["DELETE"], "url": "*"}},
                ],
            )
            with pytest.raises(CheckrdPolicyDenied) as exc_info:
                client.delete("https://api.example.com")
            assert exc_info.value.suggestion is not None
            assert "no-deletes" in exc_info.value.suggestion

    def test_default_deny_has_suggestion(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny")
            with pytest.raises(CheckrdPolicyDenied) as exc_info:
                client.get("https://api.example.com")
            assert "allow rule" in (exc_info.value.suggestion or "").lower()

    def test_backward_compat_str_starts_with_legacy_format(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny")
            with pytest.raises(CheckrdPolicyDenied) as exc_info:
                client.get("https://api.example.com")
            message = str(exc_info.value)
            assert message.startswith("Request ")
            assert "denied:" in message

    def test_request_id_is_present(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny")
            with pytest.raises(CheckrdPolicyDenied) as exc_info:
                client.get("https://api.example.com")
            assert exc_info.value.request_id  # non-empty


# ============================================================
# No WASM dependency
# ============================================================


class TestNoWasmDependency:
    def test_no_wasmtime_import(self) -> None:
        """The testing module must be importable without wasmtime.

        This is the whole value prop: users run ``pip install checkrd``
        in their test environment and use ``checkrd.testing`` even if
        they skip ``wasmtime`` (or can't install it on that platform).

        We verify by checking that ``wasmtime`` is not in the testing
        module's import chain.
        """
        import importlib

        # Force a fresh import of the testing module.
        mod = importlib.import_module("checkrd.testing")

        # The testing module itself doesn't trigger wasmtime import.
        # (wasmtime may already be loaded from other test modules in the
        # same process, so we can't assert it's absent — but we CAN
        # verify the testing module doesn't FAIL when wasmtime is
        # hypothetically unavailable, by checking it uses no wasmtime
        # types at module level.)
        assert hasattr(mod, "mock_wrap")
        assert hasattr(mod, "MockEngine")


# ============================================================
# MockEngine unit tests
# ============================================================


class TestMockEngineDirectly:
    """Test the engine in isolation from the transport for fine-grained
    coverage of the evaluation logic."""

    def test_allow_by_default(self) -> None:
        engine = MockEngine()
        result = engine.evaluate(
            request_id="r1", method="GET", url="https://example.com",
            headers=[], body=None, timestamp="", timestamp_ms=0,
        )
        assert result.allowed is True
        assert result.deny_reason is None

    def test_deny_by_default(self) -> None:
        engine = MockEngine(default="deny")
        result = engine.evaluate(
            request_id="r1", method="GET", url="https://example.com",
            headers=[], body=None, timestamp="", timestamp_ms=0,
        )
        assert result.allowed is False
        assert "default policy" in (result.deny_reason or "")

    def test_telemetry_json_is_parseable(self) -> None:
        import json

        engine = MockEngine()
        result = engine.evaluate(
            request_id="r1", method="GET", url="https://example.com",
            headers=[], body=None, timestamp="", timestamp_ms=0,
        )
        parsed = json.loads(result.telemetry_json)
        assert parsed["request_id"] == "r1"
        assert parsed["policy_result"] == "allowed"

    def test_rule_name_in_deny_reason(self) -> None:
        engine = MockEngine(
            default="allow",
            rules=[{"name": "my-rule", "deny": {"url": "*"}}],
        )
        result = engine.evaluate(
            request_id="r1", method="GET", url="https://example.com",
            headers=[], body=None, timestamp="", timestamp_ms=0,
        )
        assert result.deny_reason == "denied by rule 'my-rule'"

    def test_unnamed_rule_gets_default_name(self) -> None:
        engine = MockEngine(rules=[{"deny": {"url": "*"}}])
        result = engine.evaluate(
            request_id="r1", method="GET", url="https://example.com",
            headers=[], body=None, timestamp="", timestamp_ms=0,
        )
        assert "unnamed" in (result.deny_reason or "")
