"""Streaming terminal-frame GenAI usage capture (the streaming usage tap).

Non-streaming responses carry their token usage in a single JSON body,
which :mod:`checkrd._genai_body` parses. **Streaming** responses don't:
the usage is delivered out-of-band, in specific *terminal* / *metadata*
frames of a Server-Sent-Events (SSE) stream, while the bulk of the
stream is content deltas. This module is the pure, transport-independent
core of the tap that pulls usage out of those terminal frames.

It emits the **same OTel ``gen_ai.usage.*`` dotted attribute keys** as
the body extractor (``input_tokens``, ``output_tokens``,
``cache_read.input_tokens``, ``cache_creation.input_tokens``,
``reasoning.output_tokens``) and applies the **same inclusion-rule
normalization** (Anthropic's ``input_tokens`` excludes cache, so the
emitted input is the inclusive sum). Both SDKs test against the shared
fixtures in ``schemas/genai-fixtures/streaming/*.json``; Python and the
JS tap must agree frame-for-frame.

# Transport independence

This module sees no sockets, no ``httpx``, no SSE byte parsing. It takes
an already-framed, ordered iterable of SSE *event* strings — exactly the
``sse_frames`` the fixtures store — plus a ``complete`` flag the
transport sets when it observed the stream reach its natural end. The
M-12 transport wiring is responsible for splitting the raw byte stream on
the SSE blank-line (``\\n\\n``) boundary and deciding ``complete``; it
then feeds the frames here. Keeping the accounting pure makes it
exhaustively fixture-testable without a live LLM.

# We NEVER buffer content frames

The whole point of the tap is that it does **not** reconstruct the
model's output. It reads only the frames that carry usage metadata and
skips everything else:

  - **OpenAI** — only the final ``data:`` chunk that has an empty
    ``choices`` array and a populated ``usage`` object (present only when
    the caller set ``stream_options.include_usage``). Content chunks
    (non-empty ``choices`` with ``delta.content``) and the ``[DONE]``
    sentinel are skipped. ``prompt_tokens`` / ``completion_tokens`` are
    already inclusive totals (cached ⊆ prompt, reasoning ⊆ completion),
    so no normalization.

  - **Anthropic** — input usage (incl. ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``) lands in the ``message_start``
    event's ``message.usage``; cumulative output lands in the final
    ``message_delta`` event's ``usage.output_tokens``. ``content_block_*``
    deltas are skipped entirely. Inclusive normalization, identical to
    the body extractor: ``input_tokens = input + cache_read +
    cache_creation``.

# Abandonment ⇒ ``untallied`` (the engine MUST NOT estimate)

If the stream is abandoned before its terminal usage frame — the caller
sets ``complete=False`` (client disconnect / truncation), **or** the
frames simply never include a terminal usage frame (OpenAI: no usage
chunk seen; Anthropic: no ``message_delta`` carrying ``output_tokens``)
— the tap returns an **empty** ``usage_attrs`` and a ``pricing_status``
of ``"untallied"``. It never emits a partial or estimated count: a
half-finished stream's pre-flight reserve is released on settle-timeout
(TDD §4.2 / §5.3), not billed from a guess.

# Robustness

The tap never throws on a malformed frame. A frame that isn't valid SSE,
whose ``data:`` payload isn't JSON, or whose JSON is the wrong shape is
skipped; a later well-formed terminal frame is still captured. Token
counts use the same bool-rejecting :func:`checkrd._genai_body._as_int`
the body extractor uses, so JSON booleans / floats are never coerced
into counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# Single-source the bool-rejecting integer parse so the streaming tap and
# the body extractor agree on what counts as a token count (JSON booleans
# are `int` subclasses and must be rejected; floats are skipped, never
# rounded). Importing the private helper keeps the rule in exactly one
# place rather than re-implementing it sloppily.
from checkrd._genai_body import _as_int

# Marks an event whose usage could not be finalized from the observed
# frames. Mirrors the JS tap and the `expected_pricing_status` the
# `*_abandoned` fixtures pin. Downstream (M-12 / the core) reads this to
# release the pre-flight reserve instead of billing an estimate.
PRICING_STATUS_UNTALLIED = "untallied"

# Generous cap on the bytes of a single SSE frame the tap will parse.
# A frame larger than this is treated as malformed (skipped), bounding
# the work a hostile upstream can force per frame. Matches the spirit of
# the 1 MiB body cap in `_genai_body`. The transport enforces its own
# aggregate budget; this is the per-frame floor.
_MAX_FRAME_BYTES = 1_048_576


@dataclass
class StreamCaptureResult:
    """Outcome of capturing usage from one streaming response.

    Attributes:
        usage_attrs: OTel ``gen_ai.usage.*`` dotted attribute names →
            int counts, using the exact same key names as
            :mod:`checkrd._genai_body`. Empty when the stream was
            abandoned before a terminal usage frame.
        pricing_status: ``"untallied"`` when the stream ended without a
            terminal usage frame (``usage_attrs`` is then empty), else
            ``None`` (a normal, fully-tallied stream carries no explicit
            status — the usage attrs speak for themselves).
    """

    usage_attrs: Dict[str, Any] = field(default_factory=dict)
    pricing_status: Optional[str] = None


def capture_stream_usage(
    provider: Optional[str],
    sse_frames: Iterable[str],
    complete: bool,
) -> StreamCaptureResult:
    """Capture GenAI usage from an ordered iterable of SSE frame strings.

    Pure and transport-independent: the caller is responsible for having
    split the wire bytes into ordered SSE event frames (each a raw
    ``data: {...}\\n\\n`` for OpenAI, ``event: T\\ndata: {...}\\n\\n``
    for Anthropic) and for deciding ``complete``.

    Args:
        provider: The OTel ``gen_ai.provider.name`` (from
            :func:`checkrd._genai.detect_provider`). Only ``"openai"`` /
            ``"azure.openai"`` and ``"anthropic"`` carry streaming usage
            today; any other value (or ``None``) yields an untallied
            result — we never guess a usage shape across unrelated
            providers.
        sse_frames: Ordered SSE event frames, exactly as they came off
            the wire. Consumed lazily and exactly once; content frames
            are skipped, not buffered.
        complete: Whether the transport observed the stream reach its
            natural end (it saw the terminal frame / ``[DONE]`` /
            ``message_stop``). ``False`` forces an untallied result even
            if a usage frame was seen, because a stream the caller
            abandoned mid-flight must not be billed.

    Returns:
        A :class:`StreamCaptureResult`. On any abandonment path the
        ``usage_attrs`` is empty and ``pricing_status`` is
        ``"untallied"``; never an exception, even on malformed frames.
    """
    if provider == "openai" or provider == "azure.openai":
        usage = _capture_openai(sse_frames)
    elif provider == "anthropic":
        usage = _capture_anthropic(sse_frames)
    else:
        # Unknown / unsupported provider: drain nothing, tally nothing.
        # An unrecognized stream is abandoned-by-definition for billing.
        usage = None

    # Abandonment rule: the result is untallied if the caller flagged the
    # stream incomplete OR no terminal usage frame was ever observed. The
    # engine must not estimate — return empty usage + the explicit status.
    if not complete or usage is None:
        return StreamCaptureResult(
            usage_attrs={},
            pricing_status=PRICING_STATUS_UNTALLIED,
        )
    return StreamCaptureResult(usage_attrs=usage, pricing_status=None)


# ---------------------------------------------------------------------------
# SSE frame parsing (shared)
# ---------------------------------------------------------------------------


def _parse_frame(frame: str) -> Optional[Dict[str, Any]]:
    """Parse the JSON ``data:`` payload of one SSE frame, or ``None``.

    Handles both wire shapes uniformly:

      - OpenAI: ``data: {...}\\n\\n`` (no ``event:`` line).
      - Anthropic: ``event: T\\ndata: {...}\\n\\n``.

    Per the SSE spec a frame may carry multiple ``data:`` lines that are
    joined with ``\\n``; we honor that. The ``event:`` line is read
    separately by the Anthropic path via :func:`_event_name`, so it's
    ignored here. Returns ``None`` (skip this frame) when there is no
    ``data:`` payload, the payload is the ``[DONE]`` sentinel, the frame
    is over the per-frame byte cap, or the payload isn't a JSON object —
    the never-throw contract lives here.
    """
    if not isinstance(frame, str):
        return None
    # Cheap bound on hostile per-frame size before any splitting.
    if len(frame) > _MAX_FRAME_BYTES:
        return None

    data_parts: List[str] = []
    for raw_line in frame.split("\n"):
        # Per SSE, a trailing CR (CRLF line endings) is stripped.
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if line.startswith("data:"):
            # A single optional leading space after the colon is part of
            # the SSE framing and is removed; further spaces are payload.
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_parts.append(value)
    if not data_parts:
        return None

    payload = "\n".join(data_parts)
    # The OpenAI stream terminator carries no usage; skip it explicitly so
    # it is never mistaken for a JSON payload.
    if payload == "[DONE]":
        return None

    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _event_name(frame: str) -> Optional[str]:
    """Return the SSE ``event:`` field of a frame, or ``None`` if absent.

    Anthropic dispatches on this (``message_start`` vs ``message_delta``
    vs the content/stop events it skips). OpenAI frames have no
    ``event:`` line, so this returns ``None`` for them — which the
    OpenAI path never consults.
    """
    if not isinstance(frame, str):
        return None
    for raw_line in frame.split("\n"):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if line.startswith("event:"):
            name = line[6:]
            if name.startswith(" "):
                name = name[1:]
            return name
    return None


# ---------------------------------------------------------------------------
# OpenAI / Azure OpenAI
# ---------------------------------------------------------------------------


def _capture_openai(sse_frames: Iterable[str]) -> Optional[Dict[str, Any]]:
    """Scan OpenAI SSE frames for the ``include_usage`` terminal chunk.

    Usage rides on the final ``data:`` chunk, which has an empty
    ``choices`` array and a populated ``usage`` object (only emitted when
    the caller set ``stream_options.include_usage``). We read only that
    object and skip every content delta and the ``[DONE]`` sentinel — we
    never reconstruct the completion.

    ``prompt_tokens`` / ``completion_tokens`` are already inclusive
    totals, so the cache / reasoning detail counters are emitted as-is
    (cached ⊆ prompt, reasoning ⊆ completion) with no normalization.

    Returns the usage attrs dict if a usage chunk was seen, else ``None``
    (no usage frame ⇒ abandoned-for-billing, surfaced as untallied by the
    caller). The last usage chunk seen wins, matching the cumulative wire
    contract.
    """
    captured: Optional[Dict[str, Any]] = None
    for frame in sse_frames:
        parsed = _parse_frame(frame)
        if parsed is None:
            continue
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            # Content delta (or any non-usage chunk) — skip, don't buffer.
            continue

        attrs: Dict[str, Any] = {}
        prompt = _as_int(usage.get("prompt_tokens"))
        if prompt is not None:
            attrs["gen_ai.usage.input_tokens"] = prompt
        completion = _as_int(usage.get("completion_tokens"))
        if completion is not None:
            attrs["gen_ai.usage.output_tokens"] = completion

        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            # 0 is a valid, distinct-from-absent count — emit it.
            cached = _as_int(prompt_details.get("cached_tokens"))
            if cached is not None:
                attrs["gen_ai.usage.cache_read.input_tokens"] = cached

        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            reasoning = _as_int(completion_details.get("reasoning_tokens"))
            if reasoning is not None:
                attrs["gen_ai.usage.reasoning.output_tokens"] = reasoning

        # A usage chunk with no parseable counts still counts as "seen"
        # (the stream reached its usage frame); the last one wins.
        captured = attrs
    return captured


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _capture_anthropic(sse_frames: Iterable[str]) -> Optional[Dict[str, Any]]:
    """Scan Anthropic SSE frames for the ``message_start`` + final
    ``message_delta`` usage.

    Two frames carry usage; everything between them is content we skip:

      - ``message_start`` → ``message.usage`` has the input side, incl.
        ``cache_read_input_tokens`` / ``cache_creation_input_tokens``.
        Billing-critical normalization (identical to the body extractor):
        the emitted ``input_tokens`` is ``input + cache_read +
        cache_creation``, so the cache counters are subsets of the total
        that the core's ``settle_usage`` nets back out.
      - the final ``message_delta`` → ``usage.output_tokens`` is the
        cumulative output. The last one observed wins.

    A finalized output (a ``message_delta`` carrying ``output_tokens``)
    is the terminal usage frame: without it the stream is abandoned and
    we return ``None`` so the caller surfaces it as untallied — never a
    partial estimate from ``message_start`` alone.
    """
    saw_message_start = False
    input_tokens: Optional[int] = None
    cache_read: Optional[int] = None
    cache_creation: Optional[int] = None
    output_tokens: Optional[int] = None
    saw_output = False

    for frame in sse_frames:
        name = _event_name(frame)
        # Only the two usage-bearing event types are parsed; content_block_*
        # / message_stop / ping are skipped without touching their payload.
        if name != "message_start" and name != "message_delta":
            continue
        parsed = _parse_frame(frame)
        if parsed is None:
            continue

        if name == "message_start":
            saw_message_start = True
            message = parsed.get("message")
            if isinstance(message, dict):
                usage = message.get("usage")
                if isinstance(usage, dict):
                    input_tokens = _as_int(usage.get("input_tokens"))
                    cache_read = _as_int(usage.get("cache_read_input_tokens"))
                    cache_creation = _as_int(
                        usage.get("cache_creation_input_tokens")
                    )
        else:  # message_delta
            usage = parsed.get("usage")
            if isinstance(usage, dict):
                delta_output = _as_int(usage.get("output_tokens"))
                if delta_output is not None:
                    output_tokens = delta_output
                    saw_output = True

    # The terminal usage frame for Anthropic is the message_delta carrying
    # output_tokens. No finalized output ⇒ abandoned ⇒ untallied. We also
    # require a message_start so a stray delta alone can't tally.
    if not saw_output or not saw_message_start:
        return None

    attrs: Dict[str, Any] = {}
    if input_tokens is not None:
        # Sum cache back into the input total so the detail counters are
        # subsets of it (cache terms contribute 0 when absent / non-int).
        inclusive = input_tokens
        if cache_read is not None:
            inclusive += cache_read
        if cache_creation is not None:
            inclusive += cache_creation
        attrs["gen_ai.usage.input_tokens"] = inclusive
    if cache_read is not None:
        attrs["gen_ai.usage.cache_read.input_tokens"] = cache_read
    if cache_creation is not None:
        attrs["gen_ai.usage.cache_creation.input_tokens"] = cache_creation
    if output_tokens is not None:
        attrs["gen_ai.usage.output_tokens"] = output_tokens
    return attrs


__all__ = [
    "StreamCaptureResult",
    "capture_stream_usage",
    "PRICING_STATUS_UNTALLIED",
]
