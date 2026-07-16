"""Transport integration tests for body/stream GenAI extraction + cost (P1-15).

Wires the httpx transport's opt-in body/stream token-usage extraction end to
end and proves it feeds cost metering — the gap that made ``settle_cost_into``
inert on the httpx path (it always got ``None`` usage). Covered:

  * NON-streaming vendor response, opt-in ON  -> the enqueued event carries the
    OTel ``gen_ai.usage.*`` counts + settled ``cost_usd_micros`` / status.
  * NON-streaming, opt-in OFF                 -> no usage, no settle (unchanged).
  * STREAMING SSE response, opt-in ON         -> the user receives the FULL,
    byte-identical stream lazily, AND a ``stream_completion`` event is enqueued
    with usage + cost from the terminal frame.
  * STREAMING abandoned mid-consumption       -> untallied, never estimated.
  * Parity against ``schemas/genai-fixtures/streaming/`` (shared with the JS SDK).

Sync and async are both exercised. A ``Mock(spec=WasmEngine)`` returns a known
``settle_usage`` result so the assertions are hermetic and fast; the real-WASM
settle arithmetic lives in ``tests/test_pricing.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

import httpx
import pytest

import checkrd
from checkrd.engine import EvalResult, WasmEngine
from checkrd.transports._httpx import CheckrdAsyncTransport, CheckrdTransport
from tests.conftest import requires_wasm

_SETTLE_RESULT: Dict[str, Any] = {
    "cost_usd_micros": 12_345,
    "currency": "USD",
    "pricing_bundle_version": 7,
    "pricing_status": "priced",
    "overflow": False,
    "sku_id": "openai-gpt-4o",
}


def _priced_engine(host: str = "api.openai.com", path: str = "/v1/chat/completions") -> Mock:
    """A mock engine that allows every request and prices any settled usage."""
    engine = Mock(spec=WasmEngine)
    engine.evaluate.return_value = EvalResult(
        allowed=True,
        deny_reason=None,
        telemetry_json=json.dumps({"request": {"url_host": host, "url_path": path}}),
        request_id="req-genai",
    )
    engine.get_active_pricing_version.return_value = 7
    engine.settle_usage.return_value = dict(_SETTLE_RESULT)
    return engine


def _json_transport(body: bytes, *, status: int = 200) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers={"content-type": "application/json"}, content=body
        )

    return httpx.MockTransport(handler)


def _sse_transport(chunks: List[bytes]) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(chunks)),
        )

    return httpx.MockTransport(handler)


def _async_sse_transport(chunks: List[bytes]) -> httpx.MockTransport:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(chunks)),
        )

    return httpx.MockTransport(handler)


def _async_json_transport(body: bytes) -> httpx.MockTransport:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    return httpx.MockTransport(handler)


_OPENAI_RESPONSE = json.dumps(
    {
        "model": "gpt-4o",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens_details": {"reasoning_tokens": 200},
        },
    }
).encode("utf-8")

_OPENAI_SSE = [
    b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":40,"completion_tokens":12}}\n\n',
    b"data: [DONE]\n\n",
]


def _stream_events(batcher: Mock) -> List[Dict[str, Any]]:
    events = [call.args[0] for call in batcher.enqueue.call_args_list]
    return [e for e in events if e.get("event_type") == "stream_completion"]


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


class TestNonStreamingExtraction:
    def test_sync_opt_in_on_extracts_usage_and_cost(self) -> None:
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdTransport(
            _json_transport(_OPENAI_RESPONSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        with httpx.Client(transport=transport, base_url="https://api.openai.com") as client:
            resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
            # The user still gets the untouched body — extraction pre-read it,
            # but httpx serves the cached content transparently.
            assert resp.json()["model"] == "gpt-4o"

        assert batcher.enqueue.call_count == 1
        event = batcher.enqueue.call_args[0][0]
        assert event["gen_ai.usage.input_tokens"] == 1000
        assert event["gen_ai.usage.output_tokens"] == 500
        assert event["gen_ai.usage.cache_read.input_tokens"] == 800
        assert event["gen_ai.usage.reasoning.output_tokens"] == 200
        assert event["gen_ai.response.model"] == "gpt-4o"
        engine.settle_usage.assert_called_once()
        assert event["cost_usd_micros"] == 12_345
        assert event["pricing_status"] == "priced"

    def test_sync_opt_in_off_no_usage_no_settle(self) -> None:
        """Opt-in OFF ⇒ behavior exactly as before: no body inspection, no cost."""
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdTransport(
            _json_transport(_OPENAI_RESPONSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=False,
        )
        with httpx.Client(transport=transport, base_url="https://api.openai.com") as client:
            client.post("/v1/chat/completions", json={"model": "gpt-4o"})

        event = batcher.enqueue.call_args[0][0]
        assert "gen_ai.usage.input_tokens" not in event
        assert "cost_usd_micros" not in event
        engine.settle_usage.assert_not_called()

    def test_non_genai_host_is_untouched(self) -> None:
        """A non-LLM host must not trigger a body read or usage even when on."""
        batcher = Mock()
        engine = _priced_engine(host="api.stripe.com", path="/v1/charges")
        transport = CheckrdTransport(
            _json_transport(b'{"id":"ch_1"}'),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        with httpx.Client(transport=transport, base_url="https://api.stripe.com") as client:
            client.post("/v1/charges", json={"amount": 100})
        event = batcher.enqueue.call_args[0][0]
        assert "gen_ai.usage.input_tokens" not in event
        engine.settle_usage.assert_not_called()

    def test_non_inference_path_on_genai_host_is_skipped(self) -> None:
        """A GenAI host but a non-inference path (e.g. a file download / model
        list) must NOT be body-read — nothing to price, and we won't buffer it."""
        batcher = Mock()
        engine = _priced_engine(host="api.openai.com", path="/v1/files/f-1/content")
        transport = CheckrdTransport(
            _json_transport(b"not json, a file"),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        with httpx.Client(transport=transport, base_url="https://api.openai.com") as client:
            client.get("/v1/files/f-1/content")
        event = batcher.enqueue.call_args[0][0]
        assert "gen_ai.usage.input_tokens" not in event
        engine.settle_usage.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_opt_in_on_extracts_usage_and_cost(self) -> None:
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdAsyncTransport(
            _async_json_transport(_OPENAI_RESPONSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="https://api.openai.com"
        ) as client:
            resp = await client.post("/v1/chat/completions", json={"model": "gpt-4o"})
            assert resp.json()["model"] == "gpt-4o"

        event = batcher.enqueue.call_args[0][0]
        assert event["gen_ai.usage.input_tokens"] == 1000
        assert event["cost_usd_micros"] == 12_345
        engine.settle_usage.assert_called_once()


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStreamingExtraction:
    def test_sync_stream_bytes_identical_and_usage_captured(self) -> None:
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdTransport(
            _sse_transport(_OPENAI_SSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        received = b""
        with httpx.Client(transport=transport, base_url="https://api.openai.com") as client:
            with client.stream(
                "POST", "/v1/chat/completions", json={"model": "gpt-4o", "stream": True}
            ) as resp:
                for chunk in resp.iter_bytes():
                    received += chunk

        # The consumer's stream is byte-for-byte the upstream, lazily.
        assert received == b"".join(_OPENAI_SSE)

        stream_events = _stream_events(batcher)
        assert len(stream_events) == 1
        event = stream_events[0]
        assert event["gen_ai.usage.input_tokens"] == 40
        assert event["gen_ai.usage.output_tokens"] == 12
        # Model comes from the request body (streaming responses omit it).
        assert event["gen_ai.request.model"] == "gpt-4o"
        engine.settle_usage.assert_called_once()
        assert event["cost_usd_micros"] == 12_345
        assert event["pricing_status"] == "priced"

    def test_sync_stream_lines_still_work(self) -> None:
        """``iter_lines`` (the shape vendor SDKs use) is unaffected by the tap."""
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdTransport(
            _sse_transport(_OPENAI_SSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        with httpx.Client(transport=transport, base_url="https://api.openai.com") as client:
            with client.stream("POST", "/v1/chat/completions") as resp:
                lines = [line for line in resp.iter_lines() if line]
        # 4 data chunks + DONE.
        assert lines[-1] == "data: [DONE]"
        assert _stream_events(batcher)[0]["gen_ai.usage.input_tokens"] == 40

    def test_sync_stream_opt_in_off_no_stream_event(self) -> None:
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdTransport(
            _sse_transport(_OPENAI_SSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=False,
        )
        received = b""
        with httpx.Client(transport=transport, base_url="https://api.openai.com") as client:
            with client.stream("POST", "/v1/chat/completions") as resp:
                for chunk in resp.iter_bytes():
                    received += chunk
        assert received == b"".join(_OPENAI_SSE)  # still identical
        assert _stream_events(batcher) == []  # but no usage tap installed
        engine.settle_usage.assert_not_called()

    def test_sync_stream_abandoned_is_untallied(self) -> None:
        """Aborting the stream before its usage frame ⇒ untallied, never priced."""
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdTransport(
            _sse_transport(_OPENAI_SSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        with httpx.Client(transport=transport, base_url="https://api.openai.com") as client:
            with client.stream("POST", "/v1/chat/completions") as resp:
                it = resp.iter_bytes()
                _ = next(it)  # consume the first content chunk, then bail

        stream_events = _stream_events(batcher)
        assert len(stream_events) == 1
        assert stream_events[0]["pricing_status"] == "untallied"
        assert "gen_ai.usage.input_tokens" not in stream_events[0]
        engine.settle_usage.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_stream_bytes_identical_and_usage_captured(self) -> None:
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdAsyncTransport(
            _async_sse_transport(_OPENAI_SSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        received = b""
        async with httpx.AsyncClient(
            transport=transport, base_url="https://api.openai.com"
        ) as client:
            async with client.stream("POST", "/v1/chat/completions") as resp:
                async for chunk in resp.aiter_bytes():
                    received += chunk

        assert received == b"".join(_OPENAI_SSE)
        stream_events = _stream_events(batcher)
        assert len(stream_events) == 1
        assert stream_events[0]["gen_ai.usage.input_tokens"] == 40
        assert stream_events[0]["cost_usd_micros"] == 12_345
        engine.settle_usage.assert_called_once()


# ---------------------------------------------------------------------------
# Parity: drive the shared streaming fixtures through the transport
# ---------------------------------------------------------------------------


def _streaming_fixtures_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "schemas" / "genai-fixtures" / "streaming"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("could not locate schemas/genai-fixtures/streaming")


_PROVIDER_ENDPOINT = {
    "openai": ("api.openai.com", "/v1/chat/completions"),
    "anthropic": ("api.anthropic.com", "/v1/messages"),
}


def _load_streaming_cases() -> List[tuple[str, Dict[str, Any]]]:
    cases: List[tuple[str, Dict[str, Any]]] = []
    for path in sorted(_streaming_fixtures_dir().glob("*.json")):
        for case in json.loads(path.read_text(encoding="utf-8")):
            cases.append((f"{path.stem}::{case['name']}", case))
    return cases


_STREAMING_CASES = _load_streaming_cases()


class TestStreamingFixtureParity:
    """The transport tap, driven with a fixture's raw SSE frames, must produce
    the same ``gen_ai.usage.*`` the JS SDK and the pure Python tap produce."""

    @pytest.mark.parametrize(
        "case_id,case", _STREAMING_CASES, ids=[c[0] for c in _STREAMING_CASES]
    )
    def test_fixture_through_transport(self, case_id: str, case: Dict[str, Any]) -> None:
        provider = case["provider"]
        host, path = _PROVIDER_ENDPOINT[provider]
        chunks = [f.encode("utf-8") for f in case["sse_frames"]]

        batcher, engine = Mock(), _priced_engine(host=host, path=path)
        transport = CheckrdTransport(
            _sse_transport(chunks),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        received = b""
        with httpx.Client(transport=transport, base_url=f"https://{host}") as client:
            with client.stream("POST", path) as resp:
                for chunk in resp.iter_bytes():
                    received += chunk
        # Byte fidelity holds for every fixture, including the abandoned ones.
        assert received == b"".join(chunks)

        event = _stream_events(batcher)[0]
        usage = {k: v for k, v in event.items() if k.startswith("gen_ai.usage.")}
        assert usage == case.get("expected_usage_attrs", {}), case_id
        if "expected_pricing_status" in case:
            assert event.get("pricing_status") == case["expected_pricing_status"], case_id


# ---------------------------------------------------------------------------
# End-to-end through checkrd.wrap() with the real WASM engine
# ---------------------------------------------------------------------------


class _CapturingSink:
    """Minimal ``TelemetrySink`` that records every enqueued event."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def enqueue(self, event: Dict[str, Any]) -> None:
        self.events.append(event)


_ALLOW_ALL = {"agent": "t", "mode": "enforce", "default": "allow", "rules": []}


@requires_wasm
class TestWrapPlumbing:
    """``checkrd.wrap(extract_genai_body_attrs=...)`` threads the flag to the
    transport and the real engine extracts usage from a live response."""

    def test_flag_reaches_transport(self) -> None:
        with httpx.Client(
            transport=_json_transport(_OPENAI_RESPONSE), base_url="https://api.openai.com"
        ) as client:
            checkrd.wrap(client, agent_id="t", policy=_ALLOW_ALL, extract_genai_body_attrs=True)
            assert client._transport._extract_genai_body_attrs is True  # type: ignore[attr-defined]

    def test_flag_defaults_off(self) -> None:
        with httpx.Client(
            transport=_json_transport(_OPENAI_RESPONSE), base_url="https://api.openai.com"
        ) as client:
            checkrd.wrap(client, agent_id="t", policy=_ALLOW_ALL)
            assert client._transport._extract_genai_body_attrs is False  # type: ignore[attr-defined]

    def test_real_engine_extracts_usage_end_to_end(self) -> None:
        """Real WASM engine + a capturing sink: the OTel usage attrs land on the
        telemetry event. (No pricing bundle is installed, so cost stays unset —
        the extraction wiring is what this asserts; test_pricing.py covers the
        settle arithmetic.)"""
        sink = _CapturingSink()
        with httpx.Client(
            transport=_json_transport(_OPENAI_RESPONSE), base_url="https://api.openai.com"
        ) as client:
            checkrd.wrap(
                client,
                agent_id="t",
                policy=_ALLOW_ALL,
                telemetry_sink=sink,
                extract_genai_body_attrs=True,
            )
            resp = client.post("/v1/chat/completions", json={"model": "gpt-4o"})
            assert resp.json()["model"] == "gpt-4o"

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event["gen_ai.usage.input_tokens"] == 1000
        assert event["gen_ai.usage.output_tokens"] == 500
        assert event["gen_ai.response.model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# Fail-open: an extraction fault must never break the user's request
# ---------------------------------------------------------------------------


class TestExtractionFailOpen:
    def test_sync_extraction_error_does_not_break_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the extractor to throw; the request must still succeed and the
        # base event must simply carry no usage/cost.
        monkeypatch.setattr(
            "checkrd.transports._httpx.extract_response_attrs",
            Mock(side_effect=RuntimeError("boom")),
        )
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdTransport(
            _json_transport(_OPENAI_RESPONSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        with httpx.Client(transport=transport, base_url="https://api.openai.com") as client:
            resp = client.post("/v1/chat/completions", json={"model": "gpt-4o"})
            assert resp.json()["model"] == "gpt-4o"  # user's call unaffected
        event = batcher.enqueue.call_args[0][0]
        assert "gen_ai.usage.input_tokens" not in event
        assert "cost_usd_micros" not in event

    @pytest.mark.asyncio
    async def test_async_extraction_error_does_not_break_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "checkrd.transports._httpx.extract_response_attrs",
            Mock(side_effect=RuntimeError("boom")),
        )
        batcher, engine = Mock(), _priced_engine()
        transport = CheckrdAsyncTransport(
            _async_json_transport(_OPENAI_RESPONSE),
            engine,
            batcher=batcher,
            cost_metering=True,
            extract_genai_body_attrs=True,
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="https://api.openai.com"
        ) as client:
            resp = await client.post("/v1/chat/completions", json={"model": "gpt-4o"})
            assert resp.json()["model"] == "gpt-4o"
        event = batcher.enqueue.call_args[0][0]
        assert "gen_ai.usage.input_tokens" not in event
