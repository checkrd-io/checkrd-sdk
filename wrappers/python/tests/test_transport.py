"""Tests for checkrd.transports._httpx."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from checkrd.engine import EvalResult, WasmEngine
from checkrd.exceptions import CheckrdPolicyDenied
from checkrd.transports._httpx import (
    CheckrdAsyncTransport,
    CheckrdTransport,
    _SENSITIVE_HEADER_NAMES,
    _build_eval_kwargs,
    _compute_span_status,
    _parse_traceparent,
    _sanitize_headers_for_hooks,
)


def _mock_engine(
    allowed: bool = True,
    deny_reason: str | None = None,
) -> Mock:
    """Factory for custom mock engines. For common cases, use conftest fixtures."""
    engine = Mock(spec=WasmEngine)
    engine.evaluate.return_value = EvalResult(
        allowed=allowed,
        deny_reason=deny_reason,
        telemetry_json="{}",
        request_id="req-001",
    )
    return engine


class TestCheckrdTransport:
    def test_allowed_request_forwards(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        request = httpx.Request("GET", "https://example.com/api")
        response = checkrd.handle_request(request)

        assert response.status_code == 200
        mock_transport.handle_request.assert_called_once()
        mock_engine_allowed.evaluate.assert_called_once()

    def test_denied_request_raises(self, mock_transport: Mock) -> None:
        engine = _mock_engine(allowed=False, deny_reason="blocked by rule 'no-deletes'")
        checkrd = CheckrdTransport(mock_transport, engine)

        request = httpx.Request("DELETE", "https://example.com/api")
        with pytest.raises(CheckrdPolicyDenied, match="no-deletes") as exc_info:
            checkrd.handle_request(request)

        assert exc_info.value.request_id == "req-001"
        mock_transport.handle_request.assert_not_called()

    def test_dry_run_logs_but_forwards(
        self, mock_engine_denied: Mock, mock_transport: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_denied, enforce=False)

        request = httpx.Request("DELETE", "https://example.com/api")
        with caplog.at_level("WARNING", logger="checkrd"):
            response = checkrd.handle_request(request)

        assert response.status_code == 200
        mock_transport.handle_request.assert_called_once()
        assert any("dry-run" in r.message for r in caplog.records)

    def test_user_agent_appended(self, mock_engine_allowed: Mock, mock_transport: Mock) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        request = httpx.Request("GET", "https://example.com/api")
        checkrd.handle_request(request)

        ua = request.headers.get("user-agent", "")
        assert "Checkrd-Python/" in ua

    def test_user_agent_preserves_existing(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        request = httpx.Request(
            "GET",
            "https://example.com/api",
            headers={"User-Agent": "MyAgent/1.0"},
        )
        checkrd.handle_request(request)

        ua = request.headers.get("user-agent", "")
        assert "MyAgent/1.0" in ua
        assert "Checkrd-Python/" in ua

    def test_close_delegates(self, mock_engine_allowed: Mock, mock_transport: Mock) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        checkrd.close()
        mock_transport.close.assert_called_once()

    def test_context_manager(self, mock_engine_allowed: Mock, mock_transport: Mock) -> None:
        with CheckrdTransport(mock_transport, mock_engine_allowed) as checkrd:
            assert isinstance(checkrd, CheckrdTransport)
        mock_transport.close.assert_called_once()

    def test_large_body_skipped_in_permissive_mode(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Permissive mode: oversize bodies pass through with body=None.

        Strict mode (the default) denies outright — see
        tests/test_oversize_body.py for that contract."""
        checkrd = CheckrdTransport(
            mock_transport, mock_engine_allowed, security_mode="permissive",
        )

        large_body = b"x" * (1_048_576 + 1)  # 1 byte over limit
        request = httpx.Request("POST", "https://example.com/api", content=large_body)
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args
        assert call_kwargs.kwargs.get("body") is None or call_kwargs[1].get("body") is None

    def test_large_body_denied_in_strict_mode(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Regression guard: strict must DENY, never silently skip. Silent
        skipping is a body-matcher bypass vector (just pad the payload)."""
        from checkrd.exceptions import CheckrdPolicyDenied

        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)  # default: strict

        large_body = b"x" * (1_048_576 + 1)
        request = httpx.Request("POST", "https://example.com/api", content=large_body)
        with pytest.raises(CheckrdPolicyDenied, match="body exceeds"):
            checkrd.handle_request(request)

        # Engine was NOT invoked — short-circuit before WASM eval.
        mock_engine_allowed.evaluate.assert_not_called()

    def test_telemetry_logged_on_allow(
        self, mock_engine_allowed: Mock, mock_transport: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        with caplog.at_level("INFO", logger="checkrd"):
            checkrd.handle_request(httpx.Request("GET", "https://example.com/api"))

        assert any("allowed" in r.message for r in caplog.records)

    def test_binary_body_treated_as_empty_string(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Body that can't be decoded as UTF-8 is passed as empty string (not None).

        None means 'no body at all' (GET/HEAD). Empty string means 'body exists
        but is undecodable', which the WASM engine uses to fail-closed on body matchers.
        """
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        request = httpx.Request("POST", "https://example.com/api", content=b"\x80\x81\x82")
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args
        body = call_kwargs.kwargs.get("body") if call_kwargs.kwargs else call_kwargs[1].get("body")
        assert body == "", f"expected empty string for undecodable body, got {body!r}"

    def test_telemetry_logged_on_deny(
        self, mock_transport: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = _mock_engine(allowed=False, deny_reason="denied")
        checkrd = CheckrdTransport(mock_transport, engine)

        with caplog.at_level("WARNING", logger="checkrd"), pytest.raises(CheckrdPolicyDenied):
            checkrd.handle_request(httpx.Request("DELETE", "https://example.com/api"))

        assert any("denied" in r.message for r in caplog.records)


class TestCheckrdAsyncTransport:
    @pytest.mark.asyncio
    async def test_allowed_request_forwards(
        self, mock_engine_allowed: Mock, mock_async_transport: Mock
    ) -> None:
        checkrd = CheckrdAsyncTransport(mock_async_transport, mock_engine_allowed)

        request = httpx.Request("GET", "https://example.com/api")
        response = await checkrd.handle_async_request(request)

        assert response.status_code == 200
        mock_async_transport.handle_async_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_denied_request_raises(self, mock_async_transport: Mock) -> None:
        engine = _mock_engine(allowed=False, deny_reason="blocked")
        checkrd = CheckrdAsyncTransport(mock_async_transport, engine)

        request = httpx.Request("DELETE", "https://example.com/api")
        with pytest.raises(CheckrdPolicyDenied, match="blocked"):
            await checkrd.handle_async_request(request)

        mock_async_transport.handle_async_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_logs_but_forwards(
        self, mock_engine_denied: Mock, mock_async_transport: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        checkrd = CheckrdAsyncTransport(mock_async_transport, mock_engine_denied, enforce=False)

        request = httpx.Request("DELETE", "https://example.com/api")
        with caplog.at_level("WARNING", logger="checkrd"):
            response = await checkrd.handle_async_request(request)

        assert response.status_code == 200
        mock_async_transport.handle_async_request.assert_called_once()
        assert any("dry-run" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_aclose_delegates(
        self, mock_engine_allowed: Mock, mock_async_transport: Mock
    ) -> None:
        checkrd = CheckrdAsyncTransport(mock_async_transport, mock_engine_allowed)

        await checkrd.aclose()
        mock_async_transport.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_telemetry_logged_on_allow(
        self,
        mock_engine_allowed: Mock,
        mock_async_transport: Mock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        checkrd = CheckrdAsyncTransport(mock_async_transport, mock_engine_allowed)

        with caplog.at_level("INFO", logger="checkrd"):
            await checkrd.handle_async_request(httpx.Request("GET", "https://example.com/api"))

        assert any("allowed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_telemetry_logged_on_deny(
        self, mock_async_transport: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = _mock_engine(allowed=False, deny_reason="denied")
        checkrd = CheckrdAsyncTransport(mock_async_transport, engine)

        with caplog.at_level("WARNING", logger="checkrd"), pytest.raises(CheckrdPolicyDenied):
            await checkrd.handle_async_request(httpx.Request("DELETE", "https://example.com/api"))

        assert any("denied" in r.message for r in caplog.records)


class TestTraceContext:
    def test_parse_traceparent_valid(self) -> None:
        trace_id = "0af7651916cd43dd8448eb211c80319c"
        parent_id = "b7ad6b7169203331"
        headers = [("traceparent", f"00-{trace_id}-{parent_id}-01")]
        tid, pid = _parse_traceparent(headers)
        assert tid == trace_id
        assert pid == parent_id

    def test_parse_traceparent_missing(self) -> None:
        tid, pid = _parse_traceparent([("content-type", "application/json")])
        assert tid is None
        assert pid is None

    def test_parse_traceparent_malformed(self) -> None:
        headers = [("traceparent", "garbage-data")]
        tid, pid = _parse_traceparent(headers)
        assert tid is None
        assert pid is None

    def test_parse_traceparent_wrong_length(self) -> None:
        headers = [("traceparent", "00-abcd-ef01-01")]
        tid, pid = _parse_traceparent(headers)
        assert tid is None
        assert pid is None

    def test_parse_traceparent_non_hex(self) -> None:
        headers = [("traceparent", "00-ZZZZ651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")]
        tid, pid = _parse_traceparent(headers)
        assert tid is None
        assert pid is None

    def test_build_eval_kwargs_generates_trace_context(self) -> None:
        request = httpx.Request("GET", "https://example.com/api")
        kwargs = _build_eval_kwargs(request)

        assert "trace_id" in kwargs
        assert "span_id" in kwargs
        assert len(kwargs["trace_id"]) == 32
        assert len(kwargs["span_id"]) == 16
        assert kwargs["parent_span_id"] is None

    def test_build_eval_kwargs_extracts_traceparent(self) -> None:
        trace_id = "0af7651916cd43dd8448eb211c80319c"
        parent_id = "b7ad6b7169203331"
        request = httpx.Request(
            "GET",
            "https://example.com/api",
            headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
        )
        kwargs = _build_eval_kwargs(request)

        assert kwargs["trace_id"] == trace_id
        assert kwargs["parent_span_id"] == parent_id
        assert len(kwargs["span_id"]) == 16
        # span_id should be freshly generated, not the parent
        assert kwargs["span_id"] != parent_id

    def test_build_eval_kwargs_ignores_bad_traceparent(self) -> None:
        request = httpx.Request(
            "GET",
            "https://example.com/api",
            headers={"traceparent": "invalid"},
        )
        kwargs = _build_eval_kwargs(request)

        assert len(kwargs["trace_id"]) == 32
        assert len(kwargs["span_id"]) == 16
        assert kwargs["parent_span_id"] is None

    def test_transport_passes_trace_context_to_engine(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Verify CheckrdTransport passes trace_id/span_id/parent_span_id
        through to the engine.evaluate() call."""
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        trace_id = "0af7651916cd43dd8448eb211c80319c"
        parent_id = "b7ad6b7169203331"
        request = httpx.Request(
            "GET",
            "https://example.com/api",
            headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
        )
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["trace_id"] == trace_id
        assert call_kwargs["parent_span_id"] == parent_id
        assert len(call_kwargs["span_id"]) == 16
        assert call_kwargs["span_id"] != parent_id  # freshly generated

    def test_transport_generates_trace_when_no_traceparent(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Without a traceparent header, trace_id and span_id are generated,
        parent_span_id is None."""
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        request = httpx.Request("GET", "https://example.com/api")
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert len(call_kwargs["trace_id"]) == 32
        assert all(c in "0123456789abcdef" for c in call_kwargs["trace_id"])
        assert len(call_kwargs["span_id"]) == 16
        assert all(c in "0123456789abcdef" for c in call_kwargs["span_id"])
        assert call_kwargs["parent_span_id"] is None

    def test_each_request_gets_unique_span_id(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Two consecutive requests must get different span_ids."""
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)

        checkrd.handle_request(httpx.Request("GET", "https://example.com/a"))
        span_1 = mock_engine_allowed.evaluate.call_args.kwargs["span_id"]

        checkrd.handle_request(httpx.Request("GET", "https://example.com/b"))
        span_2 = mock_engine_allowed.evaluate.call_args.kwargs["span_id"]

        assert span_1 != span_2


class TestComputeSpanStatus:
    """OTEL span status derivation: _compute_span_status(allowed, deny_reason, status_code)."""

    def test_allowed_2xx_is_ok(self) -> None:
        code, msg = _compute_span_status(True, None, 200)
        assert code == "OK"
        assert msg is None

    def test_allowed_301_is_ok(self) -> None:
        code, msg = _compute_span_status(True, None, 301)
        assert code == "OK"
        assert msg is None

    def test_allowed_5xx_is_error(self) -> None:
        code, msg = _compute_span_status(True, None, 500)
        assert code == "ERROR"
        assert "500" in (msg or "")

    def test_allowed_502_is_error(self) -> None:
        code, msg = _compute_span_status(True, None, 502)
        assert code == "ERROR"
        assert "502" in (msg or "")

    def test_allowed_4xx_is_unset(self) -> None:
        code, msg = _compute_span_status(True, None, 404)
        assert code == "UNSET"
        assert msg is None

    def test_allowed_no_response_is_unset(self) -> None:
        code, msg = _compute_span_status(True, None, None)
        assert code == "UNSET"
        assert msg is None

    def test_denied_is_unset_with_reason(self) -> None:
        code, msg = _compute_span_status(False, "blocked by policy", None)
        assert code == "UNSET"
        assert msg == "blocked by policy"

    def test_denied_without_reason(self) -> None:
        code, msg = _compute_span_status(False, None, None)
        assert code == "UNSET"
        assert msg is None


class TestHttpMethods:
    """Verify every HTTP method flows through the transport correctly."""

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def test_allowed_request_forwards_all_methods(
        self, mock_engine_allowed: Mock, mock_transport: Mock, method: str
    ) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)
        request = httpx.Request(method, "https://example.com/api")
        response = checkrd.handle_request(request)

        assert response.status_code == 200
        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["method"] == method

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def test_denied_request_raises_all_methods(
        self, mock_transport: Mock, method: str
    ) -> None:
        engine = _mock_engine(allowed=False, deny_reason="blocked by policy")
        checkrd = CheckrdTransport(mock_transport, engine)

        request = httpx.Request(method, "https://example.com/api")
        with pytest.raises(CheckrdPolicyDenied):
            checkrd.handle_request(request)

        mock_transport.handle_request.assert_not_called()

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    @pytest.mark.asyncio
    async def test_async_allowed_request_forwards_all_methods(
        self, mock_engine_allowed: Mock, mock_async_transport: Mock, method: str
    ) -> None:
        checkrd = CheckrdAsyncTransport(mock_async_transport, mock_engine_allowed)
        request = httpx.Request(method, "https://example.com/api")
        response = await checkrd.handle_async_request(request)

        assert response.status_code == 200
        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["method"] == method


class TestBodyHandling:
    """Verify request body extraction for policy evaluation."""

    def test_json_body_passed_to_engine(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)
        body = b'{"amount": 5000, "currency": "usd"}'
        request = httpx.Request("POST", "https://api.stripe.com/v1/charges", content=body)
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] == '{"amount": 5000, "currency": "usd"}'

    def test_form_body_passed_to_engine(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)
        body = b"name=test&value=123"
        request = httpx.Request("POST", "https://example.com/form", content=body)
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] == "name=test&value=123"

    def test_empty_body_is_none(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """GET with no body should pass body=None (not empty string)."""
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)
        request = httpx.Request("GET", "https://example.com/api")
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] is None

    def test_body_at_exact_limit_passed(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Body exactly at 1MB limit should still be passed."""
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)
        body = b"x" * 1_048_576  # exactly 1MB
        request = httpx.Request("POST", "https://example.com/api", content=body)
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] == "x" * 1_048_576

    def test_body_over_limit_is_none_in_permissive_mode(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Permissive mode: over-limit body passes through as None.

        Strict mode (the default) DENIES oversize bodies — see
        tests/test_oversize_body.py for the strict contract."""
        checkrd = CheckrdTransport(
            mock_transport, mock_engine_allowed, security_mode="permissive",
        )
        body = b"x" * (1_048_576 + 1)
        request = httpx.Request("POST", "https://example.com/api", content=body)
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] is None

    def test_non_utf8_body_is_empty_string(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """Binary body is passed as empty string, signaling 'exists but unparseable'."""
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)
        request = httpx.Request("POST", "https://example.com/api", content=b"\x80\x81\xff\xfe")
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] == ""

    def test_utf8_multibyte_body_preserved(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """UTF-8 multibyte characters (emoji, CJK) should round-trip correctly."""
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed)
        body_str = '{"name": "テスト", "emoji": "🚀"}'
        request = httpx.Request(
            "POST", "https://example.com/api", content=body_str.encode("utf-8")
        )
        checkrd.handle_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] == body_str

    @pytest.mark.asyncio
    async def test_async_json_body_passed_to_engine(
        self, mock_engine_allowed: Mock, mock_async_transport: Mock
    ) -> None:
        checkrd = CheckrdAsyncTransport(mock_async_transport, mock_engine_allowed)
        body = b'{"key": "value"}'
        request = httpx.Request("POST", "https://example.com/api", content=body)
        await checkrd.handle_async_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] == '{"key": "value"}'

    @pytest.mark.asyncio
    async def test_async_body_over_limit_is_none_in_permissive_mode(
        self, mock_engine_allowed: Mock, mock_async_transport: Mock
    ) -> None:
        """Async permissive mode mirrors sync — over-limit body → None.
        Strict mode denies (covered in tests/test_oversize_body.py)."""
        checkrd = CheckrdAsyncTransport(
            mock_async_transport, mock_engine_allowed,
            security_mode="permissive",
        )
        body = b"y" * (1_048_576 + 1)
        request = httpx.Request("POST", "https://example.com/api", content=body)
        await checkrd.handle_async_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] is None

    @pytest.mark.asyncio
    async def test_async_non_utf8_body_is_empty_string(
        self, mock_engine_allowed: Mock, mock_async_transport: Mock
    ) -> None:
        checkrd = CheckrdAsyncTransport(mock_async_transport, mock_engine_allowed)
        request = httpx.Request("POST", "https://example.com/api", content=b"\xff\xfe\xfd")
        await checkrd.handle_async_request(request)

        call_kwargs = mock_engine_allowed.evaluate.call_args.kwargs
        assert call_kwargs["body"] == ""


class TestBuildEvalKwargsBodyExtraction:
    """Unit tests for _build_eval_kwargs body extraction logic."""

    def test_post_with_json(self) -> None:
        request = httpx.Request(
            "POST", "https://example.com/api", content=b'{"a": 1}'
        )
        kwargs = _build_eval_kwargs(request)
        assert kwargs["body"] == '{"a": 1}'
        assert kwargs["method"] == "POST"

    def test_get_no_body(self) -> None:
        request = httpx.Request("GET", "https://example.com/api")
        kwargs = _build_eval_kwargs(request)
        assert kwargs["body"] is None

    def test_delete_no_body(self) -> None:
        request = httpx.Request("DELETE", "https://example.com/api/123")
        kwargs = _build_eval_kwargs(request)
        assert kwargs["body"] is None

    def test_put_with_body(self) -> None:
        request = httpx.Request(
            "PUT", "https://example.com/api/123", content=b'{"updated": true}'
        )
        kwargs = _build_eval_kwargs(request)
        assert kwargs["body"] == '{"updated": true}'

    def test_patch_with_body(self) -> None:
        request = httpx.Request(
            "PATCH", "https://example.com/api/123", content=b'{"field": "new"}'
        )
        kwargs = _build_eval_kwargs(request)
        assert kwargs["body"] == '{"field": "new"}'

    def test_body_exactly_at_limit(self) -> None:
        request = httpx.Request(
            "POST", "https://example.com/api", content=b"z" * 1_048_576
        )
        kwargs = _build_eval_kwargs(request)
        assert kwargs["body"] is not None
        assert len(kwargs["body"]) == 1_048_576

    def test_body_one_over_limit(self) -> None:
        request = httpx.Request(
            "POST", "https://example.com/api", content=b"z" * 1_048_577
        )
        kwargs = _build_eval_kwargs(request)
        assert kwargs["body"] is None

    def test_binary_body_returns_empty_string(self) -> None:
        request = httpx.Request(
            "POST", "https://example.com/api", content=bytes(range(128, 256))
        )
        kwargs = _build_eval_kwargs(request)
        assert kwargs["body"] == ""

    def test_timestamp_fields_present(self) -> None:
        request = httpx.Request("GET", "https://example.com/api")
        kwargs = _build_eval_kwargs(request)
        assert "timestamp" in kwargs
        assert "timestamp_ms" in kwargs
        assert isinstance(kwargs["timestamp_ms"], int)
        assert kwargs["timestamp"].endswith("Z")

    def test_url_preserved_with_query_params(self) -> None:
        request = httpx.Request("GET", "https://example.com/api?page=2&limit=10")
        kwargs = _build_eval_kwargs(request)
        assert "page=2" in kwargs["url"]
        assert "limit=10" in kwargs["url"]

    def test_headers_as_list_of_tuples(self) -> None:
        request = httpx.Request(
            "GET",
            "https://example.com/api",
            headers={"Authorization": "Bearer tok", "Accept": "application/json"},
        )
        kwargs = _build_eval_kwargs(request)
        header_dict = dict(kwargs["headers"])
        assert header_dict["authorization"] == "Bearer tok"
        assert header_dict["accept"] == "application/json"


class TestHeaderSanitization:
    """Verify credential-bearing headers are stripped before passing to user hooks.

    This is a security-critical test class. AI SDK instrumentors patch httpx
    transports that carry third-party API keys (Authorization: Bearer sk-...,
    X-API-Key: sk-ant-...). These MUST NOT leak to user-provided hook callbacks.
    The WASM engine still receives full headers (sandboxed, no I/O).
    """

    def test_authorization_header_stripped(self) -> None:
        headers = [("Authorization", "Bearer sk-proj-abc123secret")]
        assert _sanitize_headers_for_hooks(headers) == []

    def test_x_api_key_header_stripped(self) -> None:
        """Anthropic SDK uses X-API-Key for authentication."""
        headers = [("X-API-Key", "sk-ant-api-secret")]
        assert _sanitize_headers_for_hooks(headers) == []

    def test_api_key_header_stripped(self) -> None:
        """Some providers use Api-Key (Azure OpenAI)."""
        headers = [("Api-Key", "azure-secret-key")]
        assert _sanitize_headers_for_hooks(headers) == []

    def test_cookie_header_stripped(self) -> None:
        headers = [("Cookie", "session=secret-token")]
        assert _sanitize_headers_for_hooks(headers) == []

    def test_set_cookie_header_stripped(self) -> None:
        headers = [("Set-Cookie", "session=secret; HttpOnly")]
        assert _sanitize_headers_for_hooks(headers) == []

    def test_proxy_authorization_stripped(self) -> None:
        headers = [("Proxy-Authorization", "Basic dXNlcjpwYXNz")]
        assert _sanitize_headers_for_hooks(headers) == []

    def test_checkrd_api_key_stripped(self) -> None:
        headers = [("X-Checkrd-Api-Key", "ck_live_secret")]
        assert _sanitize_headers_for_hooks(headers) == []

    def test_safe_headers_preserved(self) -> None:
        headers = [
            ("Content-Type", "application/json"),
            ("User-Agent", "python-httpx/0.27"),
            ("Accept", "application/json"),
            ("X-Request-Id", "req-123"),
        ]
        result = _sanitize_headers_for_hooks(headers)
        assert len(result) == 4
        assert dict(result)["Content-Type"] == "application/json"

    def test_case_insensitive_matching(self) -> None:
        """HTTP headers are case-insensitive (RFC 9110 Section 5.1)."""
        headers = [
            ("AUTHORIZATION", "Bearer secret"),
            ("authorization", "Bearer secret"),
            ("Authorization", "Bearer secret"),
            ("x-api-key", "secret"),
            ("X-API-KEY", "secret"),
        ]
        assert _sanitize_headers_for_hooks(headers) == []

    def test_mixed_sensitive_and_safe(self) -> None:
        """Realistic header set from an OpenAI SDK call."""
        headers = [
            ("authorization", "Bearer sk-proj-abc123"),
            ("content-type", "application/json"),
            ("user-agent", "OpenAI/Python 1.30.0"),
            ("x-request-id", "req-abc"),
            ("cookie", "session=xyz"),
        ]
        result = _sanitize_headers_for_hooks(headers)
        assert len(result) == 3
        keys = {k for k, _ in result}
        assert "authorization" not in keys
        assert "cookie" not in keys

    def test_sensitive_header_names_is_frozen(self) -> None:
        """The blocklist must be immutable to prevent runtime modification."""
        assert isinstance(_SENSITIVE_HEADER_NAMES, frozenset)

    def test_engine_receives_full_headers_hooks_receive_sanitized(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """The WASM engine (sandboxed) gets all headers for policy matching,
        but hook callbacks get sanitized headers."""
        received_hook_headers: list[list[tuple[str, str]]] = []

        def capture_hook(event: Any) -> None:
            received_hook_headers.append(event.headers)

        checkrd = CheckrdTransport(
            mock_transport,
            mock_engine_allowed,
            on_allow=capture_hook,
        )
        request = httpx.Request(
            "GET",
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": "Bearer sk-proj-secret",
                "Content-Type": "application/json",
            },
        )
        checkrd.handle_request(request)

        # Engine sees the full Authorization header (for policy matching)
        engine_headers = dict(mock_engine_allowed.evaluate.call_args.kwargs["headers"])
        assert "authorization" in engine_headers

        # Hook does NOT see the Authorization header
        assert len(received_hook_headers) == 1
        hook_header_keys = {k for k, _ in received_hook_headers[0]}
        assert "authorization" not in hook_header_keys
        assert "content-type" in hook_header_keys

    def test_on_deny_hook_receives_sanitized_headers(
        self, mock_transport: Mock
    ) -> None:
        """on_deny hook also gets sanitized headers."""
        received_headers: list[list[tuple[str, str]]] = []

        def capture_deny(event: Any) -> None:
            received_headers.append(event.headers)

        engine = _mock_engine(allowed=False, deny_reason="blocked")
        checkrd = CheckrdTransport(
            mock_transport,
            engine,
            enforce=False,  # dry-run so request proceeds
            on_deny=capture_deny,
        )
        request = httpx.Request(
            "DELETE",
            "https://example.com/api",
            headers={"Authorization": "Bearer secret", "X-Custom": "safe"},
        )
        checkrd.handle_request(request)

        assert len(received_headers) == 1
        hook_keys = {k for k, _ in received_headers[0]}
        assert "authorization" not in hook_keys
        assert "x-custom" in hook_keys

    def test_before_request_hook_receives_sanitized_headers(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        """before_request hook gets sanitized headers too."""
        received_headers: list[list[tuple[str, str]]] = []

        def capture_before(event: Any) -> Any:
            received_headers.append(event.headers)
            return event

        checkrd = CheckrdTransport(
            mock_transport,
            mock_engine_allowed,
            before_request=capture_before,
        )
        request = httpx.Request(
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers={"X-API-Key": "sk-ant-secret", "Accept": "application/json"},
        )
        checkrd.handle_request(request)

        assert len(received_headers) == 1
        hook_keys = {k for k, _ in received_headers[0]}
        assert "x-api-key" not in hook_keys
        assert "accept" in hook_keys


class TestTransportBatcherIntegration:
    """Verify the transport enqueues telemetry events to the batcher."""

    def test_allowed_request_enqueues_to_batcher(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        batcher = Mock()
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed, batcher=batcher)

        checkrd.handle_request(httpx.Request("GET", "https://example.com/api"))

        batcher.enqueue.assert_called_once()
        event = batcher.enqueue.call_args[0][0]
        assert isinstance(event, dict)

    def test_denied_request_enqueues_to_batcher(self, mock_transport: Mock) -> None:
        engine = _mock_engine(allowed=False, deny_reason="blocked")
        batcher = Mock()
        checkrd = CheckrdTransport(mock_transport, engine, batcher=batcher, enforce=False)

        checkrd.handle_request(httpx.Request("DELETE", "https://example.com/api"))

        # Denied events are enqueued (for denied telemetry logging)
        assert batcher.enqueue.call_count >= 1

    def test_no_batcher_does_not_error(
        self, mock_engine_allowed: Mock, mock_transport: Mock
    ) -> None:
        checkrd = CheckrdTransport(mock_transport, mock_engine_allowed, batcher=None)

        response = checkrd.handle_request(httpx.Request("GET", "https://example.com/api"))
        assert response.status_code == 200
