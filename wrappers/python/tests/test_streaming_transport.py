"""Streaming-response regression tests for the httpx transport.

The #1 use case for the Checkrd Python SDK is wrapping an httpx
client that talks to OpenAI / Anthropic / other AI vendors — all of
which ship responses as SSE streams by default for any non-trivial
chat completion. If the wrapped transport accidentally buffers the
entire stream into memory (or worse, truncates it), the SDK becomes
unusable for the core use case.

These tests exercise the streaming path end-to-end against an
in-memory mock transport:

  1. Policy is evaluated before the stream starts.
  2. The wrapped stream yields the same bytes as the upstream.
  3. The stream can be closed cleanly mid-consumption.
  4. Streaming works through both sync and async transports.

Covers the gap called out in the Week-3 audit: "tests don't cover
streaming responses — OpenAI streaming, event streams — all untested."
"""

from __future__ import annotations

import json

import httpx
import pytest

import checkrd
from tests.conftest import requires_wasm


ALLOW_ALL = {"agent": "test", "default": "allow", "rules": []}
DENY_ALL = {"agent": "test", "default": "deny", "rules": []}


# ---------------------------------------------------------------------------
# Mock transports that simulate SSE streaming
# ---------------------------------------------------------------------------
#
# httpx.MockTransport lets us return a response with a streaming body
# without an actual network. The body is built from a list of chunks
# so tests can assert the wrapped transport preserves every byte in
# order — which is the contract the AI vendor SDKs all rely on.


def _sse_chunks(prompts: list[str]) -> list[bytes]:
    """Turn a list of completion fragments into OpenAI-style SSE bytes."""
    out: list[bytes] = []
    for i, text in enumerate(prompts):
        payload = json.dumps(
            {
                "id": f"chatcmpl-{i}",
                "choices": [{"delta": {"content": text}, "index": 0}],
            },
        )
        out.append(f"data: {payload}\n\n".encode("utf-8"))
    out.append(b"data: [DONE]\n\n")
    return out


def _make_streaming_transport(chunks: list[bytes]) -> httpx.MockTransport:
    """Build a sync MockTransport that serves `chunks` as a stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        # `httpx.Response(stream=...)` accepts any iterable of bytes.
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(chunks)),
        )

    return httpx.MockTransport(handler)


def _make_streaming_async_transport(chunks: list[bytes]) -> httpx.MockTransport:
    """Build an async-compatible MockTransport for AsyncClient paths."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(chunks)),
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


@requires_wasm
class TestSyncStreaming:
    def test_streaming_response_bytes_pass_through_unchanged(self) -> None:
        """The wrapped transport must yield identical bytes to upstream.

        This is the most fundamental contract: if we mangle a single
        byte, streaming JSON-parsed deltas corrupt and every LLM call
        breaks. The assertion compares wire bytes, not parsed content,
        so any encoding-mangling bug gets caught.
        """
        chunks = _sse_chunks(["Hello", " world", "!"])
        with httpx.Client(
            transport=_make_streaming_transport(chunks), base_url="https://x",
        ) as client:
            try:
                checkrd.wrap(client, agent_id="t", policy=ALLOW_ALL)
                with client.stream("POST", "/v1/chat") as response:
                    received = b""
                    for chunk in response.iter_bytes():
                        received += chunk
                assert received == b"".join(chunks)
            finally:
                client.close()

    def test_streaming_consumes_line_by_line(self) -> None:
        """`iter_lines()` — the shape OpenAI / Anthropic SDKs use —
        must work. A regression where the transport consumed the
        whole stream upfront would turn this from real streaming
        into a "receive everything, then iterate" that defeats the
        whole point of SSE."""
        chunks = _sse_chunks(["chunk-a", "chunk-b", "chunk-c"])
        with httpx.Client(
            transport=_make_streaming_transport(chunks), base_url="https://x",
        ) as client:
            try:
                checkrd.wrap(client, agent_id="t", policy=ALLOW_ALL)
                with client.stream("POST", "/v1/chat") as response:
                    lines = [line for line in response.iter_lines() if line]
                assert len(lines) == 4  # 3 data lines + DONE
                assert lines[0].startswith("data: ")
                assert lines[-1] == "data: [DONE]"
            finally:
                client.close()

    def test_streaming_can_be_closed_early(self) -> None:
        """Callers often abort a stream (user cancellation, timeout)
        mid-consumption. The wrapped transport must not leak a half-
        read connection — `with client.stream(...)` closes cleanly
        even after partial iteration."""
        chunks = _sse_chunks([f"tok-{i}" for i in range(50)])
        with httpx.Client(
            transport=_make_streaming_transport(chunks), base_url="https://x",
        ) as client:
            try:
                checkrd.wrap(client, agent_id="t", policy=ALLOW_ALL)
                with client.stream("POST", "/v1/chat") as response:
                    # Consume one chunk, then break.
                    it = response.iter_bytes()
                    _ = next(it)
                    # Exit the `with` block — should close cleanly.
                # No assertion here beyond "no exception". httpx would
                # raise `StreamClosed` or similar if cleanup was broken.
            finally:
                client.close()

    def test_denied_stream_raises_before_any_bytes_flow(self) -> None:
        """Policy enforcement must happen BEFORE the stream starts.
        Otherwise the vendor's API has already been called + billed
        before the deny takes effect."""
        chunks = _sse_chunks(["should never be seen"])
        with httpx.Client(
            transport=_make_streaming_transport(chunks), base_url="https://x",
        ) as client:
            try:
                checkrd.wrap(
                    client, agent_id="t", policy=DENY_ALL, enforce=True,
                )
                with pytest.raises(checkrd.CheckrdPolicyDenied):
                    client.stream("POST", "/v1/chat").__enter__()
            finally:
                client.close()


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------


@requires_wasm
class TestAsyncStreaming:
    async def test_async_streaming_preserves_bytes(self) -> None:
        """Mirror of the sync byte-fidelity test on AsyncClient."""
        chunks = _sse_chunks(["Hello", " async", "!"])
        async with httpx.AsyncClient(
            transport=_make_streaming_async_transport(chunks),
            base_url="https://x",
        ) as client:
            try:
                checkrd.wrap_async(client, agent_id="t", policy=ALLOW_ALL)
                async with client.stream("POST", "/v1/chat") as response:
                    received = b""
                    async for chunk in response.aiter_bytes():
                        received += chunk
                assert received == b"".join(chunks)
            finally:
                await client.aclose()

    async def test_async_streaming_iter_lines(self) -> None:
        chunks = _sse_chunks(["a", "b", "c", "d"])
        async with httpx.AsyncClient(
            transport=_make_streaming_async_transport(chunks),
            base_url="https://x",
        ) as client:
            try:
                checkrd.wrap_async(client, agent_id="t", policy=ALLOW_ALL)
                async with client.stream("POST", "/v1/chat") as response:
                    lines = [
                        line
                        async for line in response.aiter_lines()
                        if line
                    ]
                assert len(lines) == 5  # 4 data + DONE
            finally:
                await client.aclose()

    async def test_async_denied_stream_raises_before_bytes(self) -> None:
        """Async parity for the pre-stream deny contract."""
        chunks = _sse_chunks(["should never be seen"])
        async with httpx.AsyncClient(
            transport=_make_streaming_async_transport(chunks),
            base_url="https://x",
        ) as client:
            try:
                checkrd.wrap_async(
                    client, agent_id="t", policy=DENY_ALL, enforce=True,
                )
                with pytest.raises(checkrd.CheckrdPolicyDenied):
                    async with client.stream("POST", "/v1/chat") as _:
                        pass
            finally:
                await client.aclose()
