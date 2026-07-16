"""Lazy SSE stream tap for GenAI token-usage capture on the httpx transport.

This is the transport-side half of the streaming usage tap (P1-15). The pure,
fixture-tested accounting lives in :mod:`checkrd._stream_capture`
(``capture_stream_usage``); this module does the transport-specific job that
module's docstring explicitly delegates: *"splitting the raw byte stream on the
SSE blank-line (``\\n\\n``) boundary and deciding ``complete``, then feeding the
frames"*.

# What it does

``install_stream_tap`` wraps an ``httpx.Response.stream`` (sync or async) with a
pass-through byte stream that:

  1. **Yields every chunk to the consumer unchanged, lazily.** The user's
     ``for chunk in response.iter_bytes()`` / ``async for`` sees identical bytes
     in identical order — the tap never buffers, reorders, or swallows content.
  2. **Skims usage frames as bytes flow by.** It splits on SSE line/blank-line
     boundaries and retains ONLY the handful of frames that carry token usage
     (every such frame contains the substring ``"usage"``); content-delta frames
     are dropped immediately, never accumulated. Memory stays bounded.
  3. **On stream end, finalizes:** runs ``capture_stream_usage`` over the
     retained frames, settles the cost in-WASM (when metering is on), and
     enqueues a single ``stream_completion`` telemetry event carrying the OTel
     ``gen_ai.usage.*`` counts + ``cost_usd_micros`` / ``pricing_status``.

# Abandonment

If the consumer aborts the stream before it reaches its terminal usage frame
(``with client.stream(...)`` exited early, client disconnect), ``complete`` is
``False`` and ``capture_stream_usage`` returns an empty usage with
``pricing_status="untallied"`` — the engine never estimates a partial stream.

# Fail-open

Every step is wrapped so a tap failure can NEVER break the user's real API call
or their stream iteration. Extraction errors are logged at DEBUG and the stream
continues; a broken tap degrades to "no usage event", never to a broken stream.

# PII safety

Only token COUNTS and the model name (both operational metadata) are ever read
or emitted — never prompt/completion content. The retained frames are the
provider's out-of-band usage/metadata frames, and the emitted event carries the
same PII-safe ``url_host`` / ``url_path`` the base decision event uses, never a
raw URL or body.

# Thread safety

``settle_cost_into`` runs on whatever thread/loop drives the consumer's stream
iteration. ``WasmEngine`` serializes every FFI call behind an internal lock, so
this is safe even if the consumer iterates on a different thread than the one
that issued the request.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

import httpx

from checkrd._genai_body import settle_cost_into
from checkrd._stream_capture import StreamCaptureResult, capture_stream_usage
from checkrd.engine import WasmEngine

logger = logging.getLogger("checkrd")

#: Providers whose streaming SSE shape :func:`capture_stream_usage` understands.
#: Others (Gemini/Cohere/etc.) have no streaming fixture yet, so the transport
#: leaves their streams un-tapped (today's behavior) rather than emit untallied
#: noise. Mirrors the JS ``vendorForUrl`` openai/anthropic gate.
STREAM_CAPTURE_PROVIDERS = frozenset({"openai", "azure.openai", "anthropic"})

#: Cheap byte-marker present in every usage-bearing frame (OpenAI's
#: ``include_usage`` terminal chunk; Anthropic's ``message_start`` /
#: ``message_delta``). Content-delta frames never carry a top-level ``usage``
#: object, so filtering on this drops the bulk of the stream without buffering
#: it. A rare false positive (a model that emits the literal token ``usage`` in
#: its text) is harmless: :func:`capture_stream_usage` re-parses each retained
#: frame and ignores any without a real usage object.
_USAGE_MARKER = b'"usage"'

#: Hard ceiling on retained-frame bytes plus the partial-line buffer. Bounds a
#: hostile/pathological upstream (usage-marker spam, a newline-less flood) to a
#: logged abandonment (``untallied``) instead of unbounded memory. Mirrors the
#: per-stream cap in the JS tap (``MAX_STREAM_EVENT_BYTES``).
_MAX_TAP_BYTES = 4 * 1024 * 1024


class _StreamUsageTap:
    """Stateful accumulator driven chunk-by-chunk by the wrapping byte stream.

    Retains only usage-bearing SSE frames; on :meth:`finalize` runs the pure
    ``capture_stream_usage`` → ``settle_cost_into`` → ``enqueue`` pipeline
    exactly once (idempotent).
    """

    def __init__(
        self,
        *,
        provider: str,
        base_attrs: Dict[str, Any],
        request_id: str,
        agent_id: str,
        engine: WasmEngine,
        cost_metering: bool,
        batcher: Optional[Any],
        start_monotonic: float,
    ) -> None:
        self._provider = provider
        self._base_attrs = base_attrs
        self._request_id = request_id
        self._agent_id = agent_id
        self._engine = engine
        self._cost_metering = cost_metering
        self._batcher = batcher
        self._start_monotonic = start_monotonic
        self._line_buf = bytearray()
        self._cur_frame_lines: List[bytes] = []
        self._frames: List[str] = []
        self._retained_bytes = 0
        self._complete = False
        self._overflow = False
        self._finalized = False

    def feed(self, chunk: bytes) -> None:
        """Fold one wire chunk into the SSE line/frame accumulator.

        Never raises: any parse fault marks the stream abandoned (untallied)
        rather than propagating into the consumer's iteration.
        """
        if self._finalized or self._overflow or not chunk:
            return
        try:
            self._line_buf.extend(chunk)
            while True:
                nl = self._line_buf.find(b"\n")
                if nl == -1:
                    # No complete line yet. Guard against a newline-less flood.
                    if len(self._line_buf) > _MAX_TAP_BYTES:
                        self._overflow = True
                        self._line_buf.clear()
                    break
                raw = bytes(self._line_buf[:nl])
                del self._line_buf[: nl + 1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                if raw == b"":
                    self._dispatch_frame()
                else:
                    self._cur_frame_lines.append(raw)
        except Exception:
            logger.debug("checkrd: stream tap feed failed", exc_info=True)
            self._overflow = True

    def _dispatch_frame(self) -> None:
        """Close the current SSE event; retain it only if it carries usage."""
        if not self._cur_frame_lines:
            return
        frame_bytes = b"\n".join(self._cur_frame_lines)
        self._cur_frame_lines = []
        if _USAGE_MARKER not in frame_bytes:
            # Content frame — drop, never buffer.
            return
        self._retained_bytes += len(frame_bytes)
        if self._retained_bytes > _MAX_TAP_BYTES:
            self._overflow = True
            self._frames.clear()
            return
        try:
            self._frames.append(frame_bytes.decode("utf-8") + "\n\n")
        except UnicodeDecodeError:
            # A non-UTF-8 payload can't be JSON usage; skip it.
            pass

    def mark_complete(self) -> None:
        """Record that the wrapped stream reached its natural end."""
        self._complete = True

    def finalize(self) -> None:
        """Capture → settle → enqueue, exactly once. Never raises."""
        if self._finalized:
            return
        self._finalized = True
        try:
            # Flush a trailing event not terminated by a blank line.
            if self._cur_frame_lines:
                self._dispatch_frame()
            complete = self._complete and not self._overflow
            result = capture_stream_usage(self._provider, self._frames, complete)
            event = self._build_event(result)
            # Settle only a tallied stream: an untallied result has no usage to
            # price, and settle_cost_into would no-op anyway. Gate on usage so
            # the settle FFI is skipped entirely for abandoned streams.
            if self._cost_metering and result.usage_attrs:
                settle_cost_into(event, self._request_id, self._engine)
            if self._batcher is not None:
                self._batcher.enqueue(event)
        except Exception:
            logger.debug("checkrd: stream usage tap finalize failed", exc_info=True)

    def _build_event(self, result: StreamCaptureResult) -> Dict[str, Any]:
        """Build the PII-safe ``stream_completion`` telemetry event."""
        now = datetime.now(timezone.utc)
        latency_ms = max(0, int((time.monotonic() - self._start_monotonic) * 1000))
        event: Dict[str, Any] = {
            "event_type": "stream_completion",
            "request_id": self._request_id,
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timestamp_ms": int(now.timestamp() * 1000),
            "latency_ms": latency_ms,
        }
        if self._agent_id:
            event["agent_id"] = self._agent_id
        # url_host / url_path / method + gen_ai.provider/operation/request.model.
        event.update(self._base_attrs)
        # Dotted gen_ai.usage.* counts from the terminal frame(s).
        event.update(result.usage_attrs)
        if result.pricing_status is not None:
            event["pricing_status"] = result.pricing_status
        return event


class _TappingSyncByteStream(httpx.SyncByteStream):
    """Sync ``response.stream`` wrapper that yields bytes verbatim while tapping.

    Subclasses ``httpx.SyncByteStream`` because ``httpx.Response.iter_raw`` /
    ``close`` assert ``isinstance(self.stream, SyncByteStream)`` — a duck-typed
    object would raise ``"Attempted to call a sync iterator on an async
    stream."``.
    """

    def __init__(self, inner: httpx.SyncByteStream, tap: _StreamUsageTap) -> None:
        self._inner = inner
        self._tap = tap

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._inner:
                self._tap.feed(chunk)
                yield chunk
            # Reached the natural end of the upstream stream.
            self._tap.mark_complete()
        finally:
            # Runs on exhaustion AND on early generator close (consumer break /
            # GC). Idempotent, so a later close() is a no-op.
            self._tap.finalize()

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self._tap.finalize()


class _TappingAsyncByteStream(httpx.AsyncByteStream):
    """Async ``response.stream`` wrapper — async analogue of the sync tap."""

    def __init__(self, inner: httpx.AsyncByteStream, tap: _StreamUsageTap) -> None:
        self._inner = inner
        self._tap = tap

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._inner:
                self._tap.feed(chunk)
                yield chunk
            self._tap.mark_complete()
        finally:
            self._tap.finalize()

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._tap.finalize()


def install_stream_tap(
    response: httpx.Response,
    *,
    provider: str,
    base_attrs: Dict[str, Any],
    request_id: str,
    agent_id: str,
    engine: WasmEngine,
    cost_metering: bool,
    batcher: Optional[Any],
    start_monotonic: float,
    is_async: bool,
) -> None:
    """Wrap ``response.stream`` in place with a lazy usage tap.

    The consumer's iteration is unchanged; a ``stream_completion`` event is
    enqueued when the stream ends. No-op-safe: any failure to install is
    swallowed so the response is returned un-tapped rather than broken.
    """
    try:
        tap = _StreamUsageTap(
            provider=provider,
            base_attrs=base_attrs,
            request_id=request_id,
            agent_id=agent_id,
            engine=engine,
            cost_metering=cost_metering,
            batcher=batcher,
            start_monotonic=start_monotonic,
        )
        inner = response.stream
        if is_async:
            response.stream = _TappingAsyncByteStream(inner, tap)  # type: ignore[arg-type]
        else:
            response.stream = _TappingSyncByteStream(inner, tap)  # type: ignore[arg-type]
    except Exception:
        logger.debug("checkrd: failed to install stream usage tap", exc_info=True)


__all__ = ["install_stream_tap"]
