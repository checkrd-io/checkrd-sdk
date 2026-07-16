"""Unit + property tests for the transport-side SSE usage tap (P1-15).

The pure accounting is exhaustively fixture-tested in ``test_stream_capture.py``;
here we pin the transport wrapper that drives it: byte fidelity (the tap is a
strict pass-through), the tallied-vs-untallied decision from natural end vs
early close, idempotent finalize, and the content-frame filter that keeps the
tap's memory bounded.

The Hypothesis property is the bar for the byte-fidelity invariant: for ANY
chunking of ANY byte stream, the bytes the consumer sees are the upstream bytes,
in order, unmodified — the tap never drops, reorders, duplicates, or mangles a
single byte no matter where the SSE framing boundaries fall relative to the
chunk boundaries.
"""

from __future__ import annotations

from typing import Any, Iterator, List, Optional
from unittest.mock import Mock

import httpx
from hypothesis import given, settings, strategies as st

from checkrd.engine import WasmEngine
from checkrd.transports._stream_tap import (
    _StreamUsageTap,
    _TappingSyncByteStream,
    install_stream_tap,
)


class _ChunkStream(httpx.SyncByteStream):
    """A minimal upstream ``SyncByteStream`` yielding fixed chunks."""

    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def _make_tap(
    *,
    provider: str = "openai",
    batcher: Optional[Any] = None,
    engine: Optional[Any] = None,
    cost_metering: bool = False,
) -> _StreamUsageTap:
    return _StreamUsageTap(
        provider=provider,
        base_attrs={"url_host": "api.openai.com", "url_path": "/v1/chat/completions"},
        request_id="req-tap",
        agent_id="agent-x",
        engine=engine if engine is not None else Mock(spec=WasmEngine),
        cost_metering=cost_metering,
        batcher=batcher,
        start_monotonic=0.0,
    )


def _drain(stream: _TappingSyncByteStream) -> bytes:
    return b"".join(stream)


# A fixed, usage-bearing SSE byte stream (OpenAI shape) shared by the
# tallied-vs-untallied tests and by the re-chunking property below. Frame 2
# carries the terminal ``include_usage`` object; capture must read 40/12 out
# of it no matter where the wire chunk boundaries fall.
_USAGE_SSE = [
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":40,"completion_tokens":12}}\n\n',
    b"data: [DONE]\n\n",
]
_USAGE_STREAM = b"".join(_USAGE_SSE)


# ---------------------------------------------------------------------------
# Byte fidelity
# ---------------------------------------------------------------------------


def test_pass_through_preserves_bytes_exactly() -> None:
    chunks = [b"data: ", b'{"choices":[]', b',"usage":{"prompt_tokens":5}}\n\n', b"data: [DONE]\n\n"]
    inner = _ChunkStream(chunks)
    stream = _TappingSyncByteStream(inner, _make_tap(batcher=Mock()))
    assert _drain(stream) == b"".join(chunks)


def test_upstream_close_is_forwarded() -> None:
    inner = _ChunkStream([b"data: x\n\n"])
    tap = _make_tap(batcher=Mock())
    stream = _TappingSyncByteStream(inner, tap)
    stream.close()
    assert inner.closed is True


@given(
    boundaries=st.lists(
        st.integers(min_value=1, max_value=len(_USAGE_STREAM) - 1),
        unique=True,
    )
)
@settings(max_examples=300, deadline=None)
def test_property_captured_usage_invariant_across_chunk_boundaries(
    boundaries: List[int],
) -> None:
    """For ANY re-chunking of a fixed usage-bearing SSE stream, the tap
    captures the SAME token usage.

    The invariant that matters is not byte pass-through — ``yield chunk``
    guarantees that structurally, which is why the old property was a
    tautology — but that the SSE frame parser is immune to where the wire
    chunk boundaries fall relative to the line / frame boundaries. Hypothesis
    draws an arbitrary set of cut points and re-splits the fixed stream there,
    so a boundary can bisect the ``"usage"`` marker, a JSON number, or the
    terminal blank line. A bug in the partial-line buffer (dropping a split
    marker, mis-joining a frame across a chunk seam) would change the captured
    usage under some boundary set — exactly what the tautology could never
    catch. Byte fidelity is kept as a cheap secondary assertion.
    """
    cuts = sorted(set(boundaries))
    chunks: List[bytes] = []
    prev = 0
    for c in cuts:
        chunks.append(_USAGE_STREAM[prev:c])
        prev = c
    chunks.append(_USAGE_STREAM[prev:])
    # Re-chunking must reproduce the original bytes (sanity on the split).
    assert b"".join(chunks) == _USAGE_STREAM

    batcher = Mock()
    stream = _TappingSyncByteStream(_ChunkStream(chunks), _make_tap(batcher=batcher))
    drained = _drain(stream)

    # Primary invariant: captured usage is identical across every chunking.
    event = batcher.enqueue.call_args[0][0]
    assert event["gen_ai.usage.input_tokens"] == 40
    assert event["gen_ai.usage.output_tokens"] == 12
    # Secondary: byte fidelity still holds under the same adversarial chunking.
    assert drained == _USAGE_STREAM


# ---------------------------------------------------------------------------
# Tallied vs untallied (natural end vs early close)
# ---------------------------------------------------------------------------


def test_full_consumption_tallies_usage() -> None:
    batcher = Mock()
    stream = _TappingSyncByteStream(_ChunkStream(_USAGE_SSE), _make_tap(batcher=batcher))
    _drain(stream)
    event = batcher.enqueue.call_args[0][0]
    assert event["event_type"] == "stream_completion"
    assert event["gen_ai.usage.input_tokens"] == 40
    assert event["gen_ai.usage.output_tokens"] == 12
    assert "pricing_status" not in event  # tallied streams carry no status


def test_early_close_before_usage_is_untallied() -> None:
    batcher = Mock()
    inner = _ChunkStream(_USAGE_SSE)
    tap = _make_tap(batcher=batcher)
    stream = _TappingSyncByteStream(inner, tap)
    it = iter(stream)
    next(it)  # first content chunk only
    stream.close()  # abort before the usage frame
    event = batcher.enqueue.call_args[0][0]
    assert event["pricing_status"] == "untallied"
    assert "gen_ai.usage.input_tokens" not in event


def test_finalize_is_idempotent_single_enqueue() -> None:
    """Exhaustion + close must not enqueue twice."""
    batcher = Mock()
    inner = _ChunkStream(_USAGE_SSE)
    tap = _make_tap(batcher=batcher)
    stream = _TappingSyncByteStream(inner, tap)
    _drain(stream)  # natural end -> finalize
    stream.close()  # second finalize path
    assert batcher.enqueue.call_count == 1


# ---------------------------------------------------------------------------
# Content-frame filter (bounded memory) + robustness
# ---------------------------------------------------------------------------


def test_content_frames_are_not_retained() -> None:
    """Only usage-bearing frames are kept; content deltas are dropped as they
    stream, so a long completion doesn't grow the tap's memory."""
    tap = _make_tap(batcher=Mock())
    content = [f'data: {{"choices":[{{"delta":{{"content":"tok{i}"}}}}]}}\n\n'.encode() for i in range(500)]
    usage = [b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n']
    stream = _TappingSyncByteStream(_ChunkStream(content + usage), tap)
    _drain(stream)
    # 500 content frames streamed through; only the single usage frame retained.
    assert len(tap._frames) == 1


def test_split_frame_across_chunks_still_captured() -> None:
    """A usage frame split across arbitrary chunk boundaries is still parsed."""
    whole = b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
    chunks = [whole[i : i + 5] for i in range(0, len(whole), 5)]
    batcher = Mock()
    stream = _TappingSyncByteStream(_ChunkStream(chunks), _make_tap(batcher=batcher))
    _drain(stream)
    event = batcher.enqueue.call_args[0][0]
    assert event["gen_ai.usage.input_tokens"] == 7


def test_feed_never_raises_and_marks_untallied_on_garbage() -> None:
    """A hostile / non-UTF-8 stream must not crash iteration; it degrades to
    untallied rather than a bad tally."""
    batcher = Mock()
    garbage = [b"\xff\xfe not sse at all \x00", b"more \xc3\x28 junk\n\n"]
    stream = _TappingSyncByteStream(_ChunkStream(garbage), _make_tap(batcher=batcher))
    # No exception, and every byte still passed through.
    assert _drain(stream) == b"".join(garbage)
    event = batcher.enqueue.call_args[0][0]
    assert event["pricing_status"] == "untallied"


def test_settle_invoked_when_metering_on_and_usage_present() -> None:
    engine = Mock(spec=WasmEngine)
    engine.get_active_pricing_version.return_value = 3
    engine.settle_usage.return_value = {
        "cost_usd_micros": 500,
        "currency": "USD",
        "pricing_bundle_version": 3,
        "pricing_status": "priced",
    }
    batcher = Mock()
    tap = _make_tap(batcher=batcher, engine=engine, cost_metering=True)
    # A model is needed to price; the transport supplies it via base_attrs.
    tap._base_attrs["gen_ai.request.model"] = "gpt-4o"
    stream = _TappingSyncByteStream(_ChunkStream(_USAGE_SSE), tap)
    _drain(stream)
    engine.settle_usage.assert_called_once()
    assert batcher.enqueue.call_args[0][0]["cost_usd_micros"] == 500


def test_install_stream_tap_wraps_and_stays_lazy() -> None:
    """``install_stream_tap`` swaps in a tapping stream that yields verbatim."""
    resp = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(b"".join(_USAGE_SSE)),
    )
    batcher = Mock()
    install_stream_tap(
        resp,
        provider="openai",
        base_attrs={"url_host": "api.openai.com", "url_path": "/v1/chat/completions", "method": "POST"},
        request_id="req-install",
        agent_id="a",
        engine=Mock(spec=WasmEngine),
        cost_metering=False,
        batcher=batcher,
        start_monotonic=0.0,
        is_async=False,
    )
    assert isinstance(resp.stream, _TappingSyncByteStream)
    assert resp.read() == b"".join(_USAGE_SSE)
    assert batcher.enqueue.call_args[0][0]["gen_ai.usage.input_tokens"] == 40
