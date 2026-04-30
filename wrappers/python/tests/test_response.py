"""Tests for the response-wrapper Phase 2 hardening (consumed guard).

Mirrors the JS SDK's tests/response.test.ts. Pins the Anthropic-style
``Stream`` semantics: a streaming response can be consumed exactly
once; a second iteration attempt raises so callers don't get silent
zero-chunk reads.
"""

from __future__ import annotations

from typing import Iterator
from unittest.mock import Mock

import pytest

from checkrd._response import APIResponse, StreamingAPIResponse


def _make_response(
    *, status: int = 200, headers: dict[str, str] | None = None,
    body: bytes = b'{"ok": true}',
) -> Mock:
    """Build a duck-typed httpx.Response stub."""
    headers = headers or {"content-type": "application/json", "x-request-id": "req_abc"}
    response = Mock()
    response.status_code = status
    response.headers = headers
    response.read = Mock(return_value=body)
    return response


class TestAPIResponse:
    def test_exposes_status_headers_request_id(self) -> None:
        r = APIResponse[dict](
            _make_response(),
            parse=lambda b: {"raw": b.decode()},
        )
        assert r.status_code == 200
        assert r.headers["x-request-id"] == "req_abc"
        assert r.request_id == "req_abc"

    def test_parse_is_cached(self) -> None:
        calls = {"n": 0}

        def parse(b: bytes) -> dict:
            calls["n"] += 1
            return {"len": len(b)}

        r = APIResponse[dict](_make_response(), parse=parse)
        a = r.parse()
        b = r.parse()
        assert a is b
        assert calls["n"] == 1


class TestStreamingAPIResponseConsumedGuard:
    """Phase 2 — a streaming response can be consumed exactly once.

    Without this guard, calling ``iter_bytes()`` twice silently yields
    zero chunks (httpx exhausts the underlying body on first read).
    The new guard turns that footgun into a clear RuntimeError.
    """

    def _streaming(self) -> Mock:
        response = Mock()
        response.status_code = 200
        response.headers = {"content-type": "text/event-stream"}

        def iter_bytes(chunk_size: int | None = None) -> Iterator[bytes]:
            yield b"a"
            yield b"b"

        def iter_text(chunk_size: int | None = None) -> Iterator[str]:
            yield "a"
            yield "b"

        response.iter_bytes = iter_bytes
        response.iter_text = iter_text
        response.close = Mock()
        return response

    def test_starts_unconsumed(self) -> None:
        s = StreamingAPIResponse[bytes](self._streaming())
        assert s.consumed is False

    def test_marks_consumed_after_iter_bytes(self) -> None:
        s = StreamingAPIResponse[bytes](self._streaming())
        list(s.iter_bytes())
        assert s.consumed is True

    def test_second_iter_bytes_raises(self) -> None:
        s = StreamingAPIResponse[bytes](self._streaming())
        list(s.iter_bytes())
        with pytest.raises(RuntimeError, match="can only be consumed once"):
            list(s.iter_bytes())

    def test_iter_text_after_iter_bytes_raises(self) -> None:
        s = StreamingAPIResponse[bytes](self._streaming())
        list(s.iter_bytes())
        with pytest.raises(RuntimeError, match="can only be consumed once"):
            list(s.iter_text())

    def test_context_manager_closes_underlying(self) -> None:
        response = self._streaming()
        with StreamingAPIResponse[bytes](response):
            pass
        response.close.assert_called_once()
