"""Body-derived GenAI semantic-convention extraction.

Extracts ``gen_ai.request.model``, ``gen_ai.response.model``,
``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``,
``gen_ai.request.stream``, plus the cache / reasoning detail counters
(``gen_ai.usage.cache_read.input_tokens``,
``gen_ai.usage.cache_creation.input_tokens``,
``gen_ai.usage.reasoning.output_tokens``) from request / response
bodies. Combined with the URL-derived attributes from
:mod:`checkrd._genai`, this gives downstream observability the full
OTel GenAI span set.

# Opt-in by default

Body parsing has structural PII implications. The fields we extract
(``model``, ``stream``, ``usage.*``) are metadata, not user content.
But we still have to PARSE the body to find them, which means
buffering the request body (the SDK already buffers up to 1 MB for
policy evaluation) and the response body (we additionally buffer
when this extraction is enabled).

Checkrd's "zero data processor" stance is structural: by default we
only emit attributes derivable from the URL. To enable body-derived
attributes the caller must explicitly opt in via
``extract_genai_body_attrs=True`` on :class:`Checkrd` /
:class:`AsyncCheckrd`, or via the env var
``CHECKRD_EXTRACT_GENAI_BODY=1``. The opt-in is per-process — we
never fall back to "extract for vendors X but not Y" because that
would be a footgun.

# The inclusion-rule invariant (billing-critical)

OTel GenAI semconv treats the detail counters as *subsets* of the
totals (``input_tokens`` / ``output_tokens``). The core's
``settle_usage`` (crates/core) relies on it: ``fresh_input = input -
cache_read - cache_creation`` is billed at the fresh-input rate,
cache-read at the cache-read rate, cache-creation at the cache-write
rate. So every extractor here normalizes a provider's native counts
so that, in the emitted attributes::

    cache_read.input_tokens + cache_creation.input_tokens <= input_tokens
    reasoning.output_tokens                                <= output_tokens

Providers differ in whether their native counts are already
inclusive — the per-provider notes below document each normalization.
The language-neutral contract lives in
``schemas/genai-fixtures/*.json`` (the JS extractor tests against the
same fixtures); keep both in lockstep.

# Vendor coverage

  - **OpenAI** (``openai`` / ``azure.openai``) —
    ``api.openai.com/v1/chat/completions`` and any OpenAI-compatible
    shape. ``prompt_tokens`` / ``completion_tokens`` are already
    inclusive totals (cached ⊆ prompt, reasoning ⊆ completion), so
    no normalization.
  - **Anthropic** (``anthropic``) —
    ``api.anthropic.com/v1/messages``. ``input_tokens`` *excludes*
    cache, so the extractor adds cache_read + cache_creation back in
    to make the emitted input total inclusive.
  - **Gemini / Vertex** (``google.gemini`` / ``google.vertex_ai``) —
    ``usageMetadata.*``. For the standard Gemini API
    ``candidatesTokenCount`` is inclusive of thoughts, so no
    normalization (version-sensitive — see the fixtures README).
  - **Cohere** (``cohere``) — prefers ``meta.billed_units`` (what the
    customer is charged) over ``meta.tokens`` (includes uncharged
    internal tokens). No cache counters. Response carries no model.
  - **Bedrock** (``aws.bedrock``) — token counts come from the
    ``x-amzn-bedrock-*-token-count`` response *headers* (authoritative,
    string-valued); the Anthropic-shaped body is ignored for counts.
    The request body is Anthropic-shaped (model at the top level).

Extending coverage is one new function per shape — see
``_extract_openai_request`` for the template.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

if TYPE_CHECKING:
    from checkrd.engine import WasmEngine

logger = logging.getLogger("checkrd")


# Generous cap on the bytes we will parse. Keeps a hostile vendor
# response from exhausting host memory and matches the 1 MB request-
# body inspection limit the SDK already enforces.
_MAX_BODY_BYTES = 1_048_576


def extract_request_attrs(
    provider: Optional[str],
    body: Optional[bytes],
) -> Dict[str, Any]:
    """Extract OTel ``gen_ai.request.*`` attrs from a request body.

    Args:
        provider: The OTel ``gen_ai.provider.name`` (from
            :func:`checkrd._genai.detect_provider`). When ``None`` or
            unknown, returns an empty dict — no guessing across
            unrelated provider shapes.
        body: Raw request body bytes, or ``None`` if the request had
            no body. Caller is responsible for ensuring this is a
            buffered copy (not a stream).

    Returns:
        Subset of OTel GenAI attribute names → values. Empty when
        the body can't be parsed or the provider is unknown.
    """
    parsed = _parse_body(body)
    if parsed is None:
        return {}

    if provider == "openai" or provider == "azure.openai":
        return _extract_openai_request(parsed)
    if provider == "anthropic" or provider == "aws.bedrock":
        # Bedrock request bodies are Anthropic-shaped (model at the
        # top level), so the request extraction is identical.
        return _extract_anthropic_request(parsed)
    if provider == "google.gemini" or provider == "google.vertex_ai":
        return _extract_gemini_request(parsed)
    if provider == "cohere":
        return _extract_cohere_request(parsed)
    return {}


def extract_response_attrs(
    provider: Optional[str],
    body: Optional[bytes],
    headers: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Extract OTel ``gen_ai.response.*`` and ``gen_ai.usage.*`` attrs.

    Args:
        provider: The OTel ``gen_ai.provider.name``.
        body: Raw response body bytes, or ``None``.
        headers: Response headers. Only Bedrock reads token counts
            from headers (``x-amzn-bedrock-*-token-count``); other
            providers ignore this argument. Defaults to ``None`` for
            backward compatibility.

    Returns:
        Subset of OTel GenAI attribute names → values. Empty when the
        body can't be parsed (or the provider is unknown), never an
        exception.
    """
    # Bedrock's token counts live in the response headers, not the
    # body — handle it before the body parse so a missing/oversize
    # body doesn't suppress header-derived counts.
    if provider == "aws.bedrock":
        return _extract_bedrock_response(headers)

    parsed = _parse_body(body)
    if parsed is None:
        return {}

    if provider == "openai" or provider == "azure.openai":
        return _extract_openai_response(parsed)
    if provider == "anthropic":
        return _extract_anthropic_response(parsed)
    if provider == "google.gemini" or provider == "google.vertex_ai":
        return _extract_gemini_response(parsed)
    if provider == "cohere":
        return _extract_cohere_response(parsed)
    return {}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_body(body: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """Parse a JSON-object body, enforcing the 1 MB cap.

    Returns the parsed dict, or ``None`` when the body is empty, over
    the cap, not valid UTF-8 JSON, or not a JSON object. Callers treat
    ``None`` as "emit nothing" — the never-throw contract lives here.
    """
    if not body:
        return None
    if len(body) > _MAX_BODY_BYTES:
        return None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _as_int(value: Any) -> Optional[int]:
    """Return ``value`` iff it is a non-bool int, else ``None``.

    JSON booleans are ``bool`` (a subclass of ``int``) and must not be
    coerced into token counts; floats like ``1.5`` are skipped too —
    counts are skipped, never rounded.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


# ---------------------------------------------------------------------------
# OpenAI / Azure OpenAI
# ---------------------------------------------------------------------------


def _extract_openai_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI request shape:
    ``{"model": "...", "messages": [...], "stream": bool}``.
    """
    attrs: Dict[str, Any] = {}
    model = body.get("model")
    if isinstance(model, str) and model:
        attrs["gen_ai.request.model"] = model
    stream = body.get("stream")
    if isinstance(stream, bool):
        attrs["gen_ai.request.stream"] = stream
    return attrs


def _extract_openai_response(body: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI non-streaming response shape::

        {"model": "...", "usage": {
            "prompt_tokens", "completion_tokens",
            "prompt_tokens_details": {"cached_tokens"},
            "completion_tokens_details": {"reasoning_tokens"}}}

    ``prompt_tokens`` / ``completion_tokens`` are already inclusive
    totals, so the detail counters are emitted as-is (cached ⊆ prompt,
    reasoning ⊆ completion) with no normalization.

    Streaming responses don't go through this path — the streaming
    extractor lives in the transport layer where we see SSE chunks.
    """
    attrs: Dict[str, Any] = {}
    model = body.get("model")
    if isinstance(model, str) and model:
        attrs["gen_ai.response.model"] = model
    usage = body.get("usage")
    if isinstance(usage, dict):
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
    return attrs


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _extract_anthropic_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic request shape:
    ``{"model": "...", "messages": [...], "stream": bool}``.

    Anthropic-on-Bedrock requests share this shape (model at the top
    level), so the Bedrock request extractor reuses this function.
    """
    attrs: Dict[str, Any] = {}
    model = body.get("model")
    if isinstance(model, str) and model:
        attrs["gen_ai.request.model"] = model
    stream = body.get("stream")
    if isinstance(stream, bool):
        attrs["gen_ai.request.stream"] = stream
    return attrs


def _extract_anthropic_response(body: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic non-streaming response shape::

        {"model": "...", "usage": {
            "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens"}}

    Billing-critical normalization: Anthropic's ``input_tokens``
    *excludes* the cache counters, so the emitted ``input_tokens`` is
    ``input_tokens + cache_read + cache_creation`` (cache terms default
    to 0 when absent). This makes the detail counters subsets of the
    total, which is what the core ``settle_usage`` nets back out.
    """
    attrs: Dict[str, Any] = {}
    model = body.get("model")
    if isinstance(model, str) and model:
        attrs["gen_ai.response.model"] = model
    usage = body.get("usage")
    if isinstance(usage, dict):
        input_tokens = _as_int(usage.get("input_tokens"))
        cache_read = _as_int(usage.get("cache_read_input_tokens"))
        cache_creation = _as_int(usage.get("cache_creation_input_tokens"))

        if input_tokens is not None:
            # Sum cache back into the total so detail <= total. Cache
            # terms contribute 0 when absent / non-int.
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

        output_tokens = _as_int(usage.get("output_tokens"))
        if output_tokens is not None:
            attrs["gen_ai.usage.output_tokens"] = output_tokens
    return attrs


# ---------------------------------------------------------------------------
# Gemini / Vertex AI
# ---------------------------------------------------------------------------


def _extract_gemini_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini ``generateContent`` request shape.

    The model is part of the URL, not the body, for the standard
    Gemini API, so there is no ``gen_ai.request.model`` to pull here.
    ``stream`` is conveyed by the ``streamGenerateContent`` URL suffix
    rather than a body flag. The request body therefore yields no
    body-derived attributes; the function exists for symmetry and to
    keep the never-throw contract uniform across providers.
    """
    return {}


def _extract_gemini_response(body: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini / Vertex response shape::

        {"modelVersion": "...", "usageMetadata": {
            "promptTokenCount", "candidatesTokenCount",
            "cachedContentTokenCount", "thoughtsTokenCount"}}

    For the standard Gemini API ``candidatesTokenCount`` is inclusive
    of ``thoughtsTokenCount`` (so reasoning ⊆ output) and
    ``cachedContentTokenCount`` is a subset of ``promptTokenCount`` —
    no normalization. Vertex AI uses the same shape (this branch
    handles both). This version-sensitive assumption is pinned by the
    fixtures README.
    """
    attrs: Dict[str, Any] = {}
    model = body.get("modelVersion")
    if isinstance(model, str) and model:
        attrs["gen_ai.response.model"] = model
    usage = body.get("usageMetadata")
    if isinstance(usage, dict):
        prompt = _as_int(usage.get("promptTokenCount"))
        if prompt is not None:
            attrs["gen_ai.usage.input_tokens"] = prompt
        candidates = _as_int(usage.get("candidatesTokenCount"))
        if candidates is not None:
            attrs["gen_ai.usage.output_tokens"] = candidates
        cached = _as_int(usage.get("cachedContentTokenCount"))
        if cached is not None:
            attrs["gen_ai.usage.cache_read.input_tokens"] = cached
        thoughts = _as_int(usage.get("thoughtsTokenCount"))
        if thoughts is not None:
            attrs["gen_ai.usage.reasoning.output_tokens"] = thoughts
    return attrs


# ---------------------------------------------------------------------------
# Cohere
# ---------------------------------------------------------------------------


def _extract_cohere_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Cohere v2 chat request shape:
    ``{"model": "...", "messages": [...], "stream": bool}``.

    The Cohere v2 chat *response* carries no model in ``meta``, so the
    request model is the only source of ``gen_ai.request.model`` —
    extracted here, mirroring the OpenAI request extractor.
    """
    attrs: Dict[str, Any] = {}
    model = body.get("model")
    if isinstance(model, str) and model:
        attrs["gen_ai.request.model"] = model
    stream = body.get("stream")
    if isinstance(stream, bool):
        attrs["gen_ai.request.stream"] = stream
    return attrs


def _extract_cohere_response(body: Dict[str, Any]) -> Dict[str, Any]:
    """Cohere v2 chat response shape::

        {"meta": {
            "billed_units": {"input_tokens", "output_tokens"},
            "tokens":       {"input_tokens", "output_tokens"}}}

    Prefer ``billed_units`` (what the customer is charged) over
    ``tokens`` (includes uncharged internal tokens) so the emitted
    counts reconcile with the Cohere invoice. Falls back to ``tokens``
    when ``billed_units`` is absent. No cache counters, and the
    response carries no model.
    """
    attrs: Dict[str, Any] = {}
    meta = body.get("meta")
    if not isinstance(meta, dict):
        return attrs
    billed = meta.get("billed_units")
    source = billed if isinstance(billed, dict) else meta.get("tokens")
    if isinstance(source, dict):
        input_tokens = _as_int(source.get("input_tokens"))
        if input_tokens is not None:
            attrs["gen_ai.usage.input_tokens"] = input_tokens
        output_tokens = _as_int(source.get("output_tokens"))
        if output_tokens is not None:
            attrs["gen_ai.usage.output_tokens"] = output_tokens
    return attrs


# ---------------------------------------------------------------------------
# AWS Bedrock
# ---------------------------------------------------------------------------

_BEDROCK_INPUT_HEADER = "x-amzn-bedrock-input-token-count"
_BEDROCK_OUTPUT_HEADER = "x-amzn-bedrock-output-token-count"


def _extract_bedrock_response(
    headers: Optional[Mapping[str, str]],
) -> Dict[str, Any]:
    """Bedrock token counts come from the response headers, not the body.

    ``x-amzn-bedrock-input-token-count`` /
    ``x-amzn-bedrock-output-token-count`` are authoritative,
    string-valued, and matched case-insensitively (HTTP header names
    are case-insensitive per RFC 9110). Non-numeric values are skipped,
    never coerced or raised on. The Anthropic-shaped body is ignored
    for token counts.
    """
    attrs: Dict[str, Any] = {}
    if not headers:
        return attrs
    # Case-insensitive lookup over an arbitrary mapping.
    lowered: Dict[str, str] = {}
    for key, value in headers.items():
        if isinstance(key, str) and isinstance(value, str):
            lowered[key.lower()] = value

    input_tokens = _parse_header_int(lowered.get(_BEDROCK_INPUT_HEADER))
    if input_tokens is not None:
        attrs["gen_ai.usage.input_tokens"] = input_tokens
    output_tokens = _parse_header_int(lowered.get(_BEDROCK_OUTPUT_HEADER))
    if output_tokens is not None:
        attrs["gen_ai.usage.output_tokens"] = output_tokens
    return attrs


def _parse_header_int(value: Optional[str]) -> Optional[int]:
    """Parse a header value as a base-10 int, or ``None`` if not numeric.

    Header values are always strings on the wire; ``"n/a"`` and other
    non-digit strings yield ``None`` (skipped, not coerced).
    """
    if value is None:
        return None
    text = value.strip()
    # ``str.isdigit`` rejects signs, decimals, and whitespace-only —
    # exactly the non-numeric inputs the fixtures expect skipped.
    if not text.isdigit():
        return None
    try:
        return int(text)
    except ValueError:  # pragma: no cover — isdigit already guards this
        return None


# ---------------------------------------------------------------------------
# Cost metering (M-12): gen_ai attrs → UsageInput
# ---------------------------------------------------------------------------


def _first_int(attrs: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    """First key in ``keys`` whose value is a non-bool int, else ``None``."""
    for key in keys:
        v = _as_int(attrs.get(key))
        if v is not None:
            return v
    return None


def _first_str(attrs: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    """First key in ``keys`` whose value is a non-empty str, else ``None``."""
    for key in keys:
        v = attrs.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def build_usage_input(event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Build the core ``UsageInput`` from a telemetry event, or ``None`` when
    the event carries no token usage to price.

    A FAITHFUL MIRROR of the JS ``buildUsageInput`` (``batcher.ts``): both SDKs
    read BOTH the dotted OTel keys (``gen_ai.usage.input_tokens`` — stamped by
    the URL / body / streaming extractors on the transport path) AND the flat
    wire keys (``gen_ai_input_tokens`` — stamped by the framework adapters,
    LangChain / OpenAI-Agents) so an event from ANY source prices identically
    across the two runtimes. Reading only the dotted keys would silently price
    adapter LLM calls at $0 in Python while JS billed them — a cross-SDK money
    divergence. Verified by the ``build_usage_input`` dump-diff in
    ``[[project_genai_parity_fixtures]]``.

    Returns ``None`` when neither an input nor an output token count is present
    (``0`` is a real value, distinct from absent) so the caller skips the settle
    FFI for a non-GenAI event rather than stamping a spurious ``unpriced_model``
    — matching the JS ``null`` contract. Present fields only; the core fills the
    rest via serde defaults. Only reads well-known keys; never raises.

    The inclusion-rule invariant the extractors maintain (``cache_read +
    cache_creation <= input_tokens``, ``reasoning <= output_tokens``) is exactly
    what the core nets back out when it bills fresh input, cache-read, and
    cache-write at their respective rates — the detail counters pass straight
    through.
    """
    input_tokens = _first_int(event, ("gen_ai.usage.input_tokens", "gen_ai_input_tokens"))
    output_tokens = _first_int(event, ("gen_ai.usage.output_tokens", "gen_ai_output_tokens"))
    if input_tokens is None and output_tokens is None:
        return None

    usage: Dict[str, Any] = {}
    provider = _first_str(event, ("gen_ai.provider.name", "gen_ai_system"))
    if provider is not None:
        usage["provider"] = provider
    # Prefer the response model (what actually served the call) over the request
    # model, matching how the core resolves the priced SKU.
    model = _first_str(
        event, ("gen_ai.response.model", "gen_ai.request.model", "gen_ai_model")
    )
    if model is not None:
        usage["model"] = model
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    cache_read = _first_int(event, ("gen_ai.usage.cache_read.input_tokens",))
    if cache_read is not None:
        usage["cache_read_tokens"] = cache_read
    cache_creation = _first_int(event, ("gen_ai.usage.cache_creation.input_tokens",))
    if cache_creation is not None:
        usage["cache_creation_tokens"] = cache_creation
    reasoning = _first_int(event, ("gen_ai.usage.reasoning.output_tokens",))
    if reasoning is not None:
        usage["reasoning_tokens"] = reasoning
    return usage


def settle_cost_into(
    telemetry: Dict[str, Any],
    request_id: str,
    engine: "WasmEngine",
) -> None:
    """Settle the call's usage and stamp the cost fields onto ``telemetry``.

    This lives here — next to :func:`build_usage_input`, in a module with no
    transport / httpx dependency — so BOTH cost-metering call sites can reach it
    without an import cycle:

    - the httpx transport (``transports/_httpx._enrich_telemetry``), which
      settles for vendor-SDK and hand-rolled-httpx LLM calls, and
    - the framework adapters (LangChain, OpenAI-Agents), which build their
      telemetry by hand and enqueue it directly, bypassing the transport. Before
      this was factored out the adapters had no way to settle, so adapter LLM
      spend was billed at $0 in Python while the JS SDK — which settles in its
      common batcher funnel — billed it. This is the cross-SDK money divergence
      the helper closes.

    **Thread safety.** ``engine`` is a non-thread-safe wasmtime ``Store``; this
    function MUST run on the same thread that owns the engine. The transport and
    both adapters already call ``engine.evaluate(...)`` synchronously on their
    calling thread, so invoking this immediately before ``enqueue`` keeps the
    settle on that same thread. It must NOT be moved onto the telemetry
    batcher's background thread.

    Gated by an active pricing bundle: when ``get_active_pricing_version()`` is
    0 no price table is installed, so we leave the cost fields unset rather than
    stamp a ``"disabled"`` figure on every event (the dashboard treats absent
    cost fields and an explicit ``"disabled"`` identically, and skipping the FFI
    call entirely keeps the hot path free when metering is configured-on but no
    bundle has arrived yet).

    Reads the dotted ``gen_ai.*`` attributes (stamped by the transport's URL /
    body / streaming extractors) AND the flat ``gen_ai_*`` wire keys (stamped by
    the framework adapters) via :func:`build_usage_input`, calls
    ``settle_usage``, and writes the flat cost fields the telemetry-event schema
    carries. Never raises — a metering failure must never break the host's
    request, so any unexpected error is swallowed with a debug log (the
    ``@_no_throw`` posture, inline).
    """
    try:
        active_version = int(engine.get_active_pricing_version())
    except Exception:
        logger.debug("checkrd: get_active_pricing_version failed; skipping settle", exc_info=True)
        return
    if active_version <= 0:
        # No pricing bundle installed — metering not active for this event.
        return

    # Reads both the dotted (transport/body/streaming) and flat (framework
    # adapter) gen_ai keys; returns None when the event carries no token usage,
    # in which case there is nothing to price.
    usage = build_usage_input(telemetry)
    if usage is None:
        return
    try:
        settle = engine.settle_usage(request_id, json.dumps(usage))
    except Exception:
        logger.debug("checkrd: settle_usage failed; cost fields left unset", exc_info=True)
        return

    # Stamp the flat cost fields onto the event (TelemetryEventInput schema).
    cost = settle.get("cost_usd_micros")
    if isinstance(cost, int):
        telemetry["cost_usd_micros"] = cost
    currency = settle.get("currency")
    if isinstance(currency, str):
        telemetry["currency"] = currency
    version = settle.get("pricing_bundle_version")
    if isinstance(version, int):
        telemetry["pricing_bundle_version"] = version
    status = settle.get("pricing_status")
    if isinstance(status, str):
        telemetry["pricing_status"] = status


__all__ = [
    "extract_request_attrs",
    "extract_response_attrs",
    "build_usage_input",
    "settle_cost_into",
]
