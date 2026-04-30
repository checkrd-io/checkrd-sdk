"""OpenTelemetry GenAI semantic conventions extraction.

Maps an outbound LLM API call's URL to the OTel GenAI attribute set
defined at
https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/gen-ai-spans.md.

Mirrors `wrappers/javascript/src/_genai.ts` exactly so both SDKs emit
identical labels — every dashboard, alert, and runbook works against
either runtime without re-mapping.

Why URL-only (not body-derived):
  - The transport sees the request URL on every call without buffering
    the body. ``gen_ai.provider.name`` and ``gen_ai.operation.name``
    are the two attributes downstream tooling needs first; both come
    from the URL.
  - Other attributes (``gen_ai.request.model``,
    ``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``)
    require parsing request / response bodies. Those are added in a
    future revision behind an explicit opt-in to keep the PII surface
    bounded — Checkrd's "zero data processor" stance is structural,
    not best-effort.
  - The OTel spec marks ``gen_ai.provider.name`` and
    ``gen_ai.operation.name`` as required / recommended for
    GenAI spans; everything else is opportunistic.

Stable across the seven vendor integrations the SDK ships
(`integrations/_openai.py`, `_anthropic.py`, `_cohere.py`,
`_groq.py`, `_mistralai.py`, `_together.py`, `_google_genai.py`) plus
AWS Bedrock and Google Vertex AI for users wiring the httpx
transport directly.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple


# --------------------------------------------------------------------------
# Provider mapping (host → OTel ``gen_ai.provider.name``)
# --------------------------------------------------------------------------
#
# Matches both the exact-host case (``api.openai.com``) and the suffix
# case for hosts that include a region segment (``…amazonaws.com``).
# Suffix matches are checked AFTER exact matches so a vendor with both
# patterns (e.g. ``api.cohere.com`` exact, ``cohere-prod-….run.app``
# suffix) routes to the same provider name.
_PROVIDER_EXACT: Dict[str, str] = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "api.cohere.com": "cohere",
    "api.cohere.ai": "cohere",
    "api.groq.com": "groq",
    "api.mistral.ai": "mistral.ai",
    "api.together.xyz": "together.ai",
    "api.together.ai": "together.ai",
    "generativelanguage.googleapis.com": "google.gemini",
    "api.perplexity.ai": "perplexity",
    "api.fireworks.ai": "fireworks",
    "api.x.ai": "xai",
}

# Suffix patterns. For Azure OpenAI a clean ``endswith`` works because
# the deployment name is the leading segment. AWS Bedrock and Google
# Vertex AI use regional segments embedded in the middle of the host
# (``bedrock-runtime.us-east-1.amazonaws.com``,
# ``us-central1-aiplatform.googleapis.com``), so a plain endswith would
# match S3 / DynamoDB / unrelated GCP APIs. We instead require a
# substring AND a suffix together — the substring nails the service,
# the suffix nails the cloud.
_PROVIDER_SUFFIX: Tuple[Tuple[str, str], ...] = (
    # Azure OpenAI deployments (e.g. ``my-deployment.openai.azure.com``).
    (".openai.azure.com", "azure.openai"),
)

# (substring, required_suffix, provider). The substring must appear
# anywhere in the host; the suffix is anchored at the end. Both must
# match — neither is sufficient on its own.
_PROVIDER_SUBSTRING_SUFFIX: Tuple[Tuple[str, str, str], ...] = (
    # AWS Bedrock regional endpoints (``bedrock-runtime.{region}.amazonaws.com``).
    ("bedrock-runtime.", ".amazonaws.com", "aws.bedrock"),
    # Pre-runtime Bedrock control-plane endpoints
    # (``bedrock.{region}.amazonaws.com``). Matched after the runtime
    # rule so the more specific pattern wins for inference traffic.
    ("bedrock.", ".amazonaws.com", "aws.bedrock"),
    # Google Vertex AI (``{region}-aiplatform.googleapis.com``).
    ("aiplatform.", ".googleapis.com", "google.vertex_ai"),
)


# --------------------------------------------------------------------------
# Operation mapping (path → OTel ``gen_ai.operation.name``)
# --------------------------------------------------------------------------
#
# Substring matches against the URL path. Order matters: the more
# specific patterns come first so ``/v1/chat/completions`` resolves to
# ``chat`` rather than the broader ``completions`` → ``text_completion``.
_OPERATION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # OpenAI / Anthropic / Cohere / etc.
    ("/chat/completions", "chat"),
    ("/messages", "chat"),                # Anthropic
    ("/converse", "chat"),                # AWS Bedrock Converse API
    ("/responses", "chat"),               # OpenAI Responses API (2025+)
    # Embeddings
    ("/embeddings", "embeddings"),
    ("/embed", "embeddings"),
    # Plain (legacy) text completion
    ("/completions", "text_completion"),
    # Google Gemini / Vertex (matched lower-cased — patterns must too).
    (":generatecontent", "generate_content"),
    (":streamgeneratecontent", "generate_content"),
    # Image / audio
    ("/images/generations", "image.generation"),
    ("/audio/speech", "audio.speech"),
    ("/audio/transcriptions", "audio.transcription"),
    # Tool / function endpoints (operation name follows the OTel
    # ``execute_tool`` convention).
    ("/tools/", "execute_tool"),
)


def detect_provider(url_host: str) -> Optional[str]:
    """Return the OTel ``gen_ai.provider.name`` for a given URL host.

    Returns ``None`` when the host doesn't match any known provider —
    callers should omit ``gen_ai.provider.name`` entirely in that case
    rather than emit ``"unknown"`` (the OTel spec treats absence and
    the literal string ``"unknown"`` differently for billing /
    aggregation queries).

    Case-insensitive: hosts are lower-cased before lookup.
    """
    if not url_host:
        return None
    host = url_host.lower().strip()
    if host in _PROVIDER_EXACT:
        return _PROVIDER_EXACT[host]
    for suffix, provider in _PROVIDER_SUFFIX:
        if host.endswith(suffix):
            return provider
    for substring, suffix, provider in _PROVIDER_SUBSTRING_SUFFIX:
        if substring in host and host.endswith(suffix):
            return provider
    return None


def detect_operation(url_path: str) -> Optional[str]:
    """Return the OTel ``gen_ai.operation.name`` for a given URL path.

    Returns ``None`` for paths that don't look like a GenAI endpoint —
    same omit-vs-unknown rationale as :func:`detect_provider`.
    """
    if not url_path:
        return None
    path = url_path.lower()
    for pattern, op in _OPERATION_PATTERNS:
        if pattern in path:
            return op
    return None


def attributes_for_url(
    url_host: str,
    url_path: str,
) -> Dict[str, str]:
    """Build the OTel GenAI attribute dict for a given URL.

    Returns a dict containing only the attributes that could be
    confidently derived — no ``"unknown"`` placeholders. Caller merges
    the result into the telemetry event payload::

        event.update(attributes_for_url(host, path))
        # event["gen_ai.provider.name"] = "openai"
        # event["gen_ai.operation.name"] = "chat"

    Empty dict means "no GenAI attributes inferred" — the call is
    probably not an LLM request, or the SDK's mapping table needs
    updating for a new vendor.
    """
    attrs: Dict[str, str] = {}
    provider = detect_provider(url_host)
    if provider is not None:
        attrs["gen_ai.provider.name"] = provider
    operation = detect_operation(url_path)
    if operation is not None:
        attrs["gen_ai.operation.name"] = operation
    return attrs
