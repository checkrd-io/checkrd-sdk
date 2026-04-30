"""Tests for debug mode (CHECKRD_DEBUG / debug=True)."""

from __future__ import annotations

import logging

import httpx
import pytest

from checkrd._settings import ENV_DEBUG, resolve
from checkrd.testing import MockEngine, mock_wrap


def _mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


class TestDebugSettings:
    def test_debug_false_by_default(self) -> None:
        s = resolve(env={})
        assert s.debug is False

    def test_debug_true_via_kwarg(self) -> None:
        s = resolve(debug=True, env={})
        assert s.debug is True

    def test_debug_via_env_var(self) -> None:
        s = resolve(env={ENV_DEBUG: "1"})
        assert s.debug is True

    def test_explicit_true_overrides_env_false(self) -> None:
        s = resolve(debug=True, env={ENV_DEBUG: "0"})
        assert s.debug is True


class TestTransportDebugLogging:
    def test_debug_log_on_allowed_request(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="allow")

            with caplog.at_level(logging.DEBUG, logger="checkrd"):
                client.get("https://api.example.com/resource")

            debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
            assert any("ALLOWED" in m for m in debug_messages)
            assert any("api.example.com" in m for m in debug_messages)

    def test_debug_log_on_denied_request(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="deny")

            with caplog.at_level(logging.DEBUG, logger="checkrd"):
                from checkrd.exceptions import CheckrdPolicyDenied

                with pytest.raises(CheckrdPolicyDenied):
                    client.get("https://api.example.com/resource")

            debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
            assert any("DENIED" in m for m in debug_messages)

    def test_debug_log_includes_timing(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with httpx.Client(transport=httpx.MockTransport(_mock_handler)) as client:
            mock_wrap(client, default="allow")

            with caplog.at_level(logging.DEBUG, logger="checkrd"):
                client.get("https://api.example.com/resource")

            debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
            assert any("us" in m for m in debug_messages)


class TestMockEngineTrace:
    def test_last_trace_captures_rule_match(self) -> None:
        engine = MockEngine(
            default="allow",
            rules=[
                {"name": "block-deletes", "deny": {"method": ["DELETE"], "url": "*"}},
            ],
        )
        engine.evaluate(
            request_id="r1", method="DELETE", url="https://example.com",
            headers=[], body=None, timestamp="", timestamp_ms=0,
        )
        trace = engine.last_trace
        assert any("block-deletes" in line for line in trace)
        assert any("MATCH" in line for line in trace)

    def test_last_trace_captures_skip(self) -> None:
        engine = MockEngine(
            default="allow",
            rules=[
                {"name": "block-deletes", "deny": {"method": ["DELETE"], "url": "*"}},
            ],
        )
        engine.evaluate(
            request_id="r1", method="GET", url="https://example.com",
            headers=[], body=None, timestamp="", timestamp_ms=0,
        )
        trace = engine.last_trace
        assert any("skip" in line for line in trace)

    def test_last_trace_captures_default_verdict(self) -> None:
        # When rules exist but none match, the default fallback is traced.
        engine = MockEngine(
            default="deny",
            rules=[{"name": "allow-post", "allow": {"method": ["POST"], "url": "*"}}],
        )
        engine.evaluate(
            request_id="r1", method="GET", url="https://example.com",
            headers=[], body=None, timestamp="", timestamp_ms=0,
        )
        trace = engine.last_trace
        assert any("default" in line.lower() for line in trace)

    def test_last_trace_is_empty_before_eval(self) -> None:
        engine = MockEngine()
        assert engine.last_trace == []
