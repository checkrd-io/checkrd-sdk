"""Body-derived GenAI semantic-convention extraction.

Extracts ``gen_ai.request.model``, ``gen_ai.response.model``,
``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``,
``gen_ai.request.stream`` from request / response bodies. Combined
with the URL-derived attributes from :mod:`checkrd._genai`, this
gives downstream observability the full OTel GenAI span set.

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

# Vendor coverage

Two formats today, both with high adoption:

  - **OpenAI** — ``api.openai.com/v1/chat/completions``, also matches
    Azure OpenAI and any provider that ships the OpenAI-compatible
    response shape (Together, Groq, Mistral with their OpenAI-compat
    endpoints, etc.).
  - **Anthropic** — ``api.anthropic.com/v1/messages``, also AWS
    Bedrock with the Anthropic-on-Bedrock shape.

Other providers ship distinct shapes (Vertex Gemini's
``usageMetadata.promptTokenCount``, Cohere's ``meta.tokens``).
Extending coverage is one new function per shape — see
``_extract_openai_request`` for the template.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


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
    if not body or provider is None:
        return {}
    if len(body) > _MAX_BODY_BYTES:
        return {}
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    if provider == "openai" or provider == "azure.openai":
        return _extract_openai_request(parsed)
    if provider == "anthropic" or provider == "aws.bedrock":
        return _extract_anthropic_request(parsed)
    return {}


def extract_response_attrs(
    provider: Optional[str],
    body: Optional[bytes],
) -> Dict[str, Any]:
    """Extract OTel ``gen_ai.response.*`` and ``gen_ai.usage.*`` attrs."""
    if not body or provider is None:
        return {}
    if len(body) > _MAX_BODY_BYTES:
        return {}
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    if provider == "openai" or provider == "azure.openai":
        return _extract_openai_response(parsed)
    if provider == "anthropic" or provider == "aws.bedrock":
        return _extract_anthropic_response(parsed)
    return {}


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
    """OpenAI non-streaming response shape:
    ``{"model": "...", "usage": {"prompt_tokens", "completion_tokens"}}``.

    Streaming responses don't go through this path — the streaming
    extractor lives in the transport layer where we see SSE chunks.
    """
    attrs: Dict[str, Any] = {}
    model = body.get("model")
    if isinstance(model, str) and model:
        attrs["gen_ai.response.model"] = model
    usage = body.get("usage")
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        if isinstance(prompt, int):
            attrs["gen_ai.usage.input_tokens"] = prompt
        completion = usage.get("completion_tokens")
        if isinstance(completion, int):
            attrs["gen_ai.usage.output_tokens"] = completion
    return attrs


# ---------------------------------------------------------------------------
# Anthropic / Anthropic-on-Bedrock
# ---------------------------------------------------------------------------


def _extract_anthropic_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic request shape:
    ``{"model": "...", "messages": [...], "stream": bool}``.

    Bedrock Converse uses a different envelope but ``model`` lives at
    the top level in both, so the extraction is identical.
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
    """Anthropic non-streaming response shape:
    ``{"model": "...", "usage": {"input_tokens", "output_tokens"}}``.
    """
    attrs: Dict[str, Any] = {}
    model = body.get("model")
    if isinstance(model, str) and model:
        attrs["gen_ai.response.model"] = model
    usage = body.get("usage")
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens")
        if isinstance(input_tokens, int):
            attrs["gen_ai.usage.input_tokens"] = input_tokens
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, int):
            attrs["gen_ai.usage.output_tokens"] = output_tokens
    return attrs


__all__ = [
    "extract_request_attrs",
    "extract_response_attrs",
]
