/**
 * Body-derived GenAI semantic-convention extraction.
 *
 * Mirrors `wrappers/python/src/checkrd/_genai_body.py` byte-for-byte
 * — the same vendor shapes, the same opt-in posture, the same
 * 1 MB cap. Both SDKs produce identical attribute names so dashboards
 * group across runtimes.
 *
 * # Opt-in by default
 *
 * Body parsing has structural PII implications. The fields we extract
 * (``model``, ``stream``, ``usage.*``) are metadata, not user content.
 * But we still have to PARSE the body to find them, which means
 * buffering the request body (the SDK already buffers up to 1 MB for
 * policy evaluation) and the response body (we additionally buffer
 * when this extraction is enabled).
 *
 * Checkrd's "zero data processor" stance is structural: by default we
 * only emit attributes derivable from the URL. To enable body-derived
 * attributes the caller must explicitly opt in via
 * ``extractGenaiBodyAttrs: true`` on ``Checkrd``, or via the env var
 * ``CHECKRD_EXTRACT_GENAI_BODY=1``.
 *
 * # Vendor coverage
 *
 *   - **OpenAI** + Azure OpenAI, also matches OpenAI-compatible
 *     endpoints (Together, Groq, Mistral compat).
 *   - **Anthropic** + Anthropic-on-Bedrock.
 *
 * Other providers (Vertex Gemini, Cohere) ship distinct shapes;
 * extending coverage is one new function per shape.
 */

const MAX_BODY_BYTES = 1_048_576;

/**
 * Extract OTel ``gen_ai.request.*`` attrs from a request body.
 *
 * Returns an empty object when the body is missing, the provider is
 * unknown, the body exceeds the size cap, or the JSON parse fails —
 * the telemetry path must never crash on hostile or truncated input.
 */
export function extractRequestAttrs(
  provider: string | undefined,
  body: Uint8Array | string | undefined,
): Record<string, string | boolean> {
  const parsed = safeParse(provider, body);
  if (parsed === null) return {};
  if (provider === "openai" || provider === "azure.openai") {
    return extractOpenAIRequest(parsed);
  }
  if (provider === "anthropic" || provider === "aws.bedrock") {
    return extractAnthropicRequest(parsed);
  }
  return {};
}

/** Extract OTel ``gen_ai.response.*`` and ``gen_ai.usage.*`` attrs. */
export function extractResponseAttrs(
  provider: string | undefined,
  body: Uint8Array | string | undefined,
): Record<string, string | number> {
  const parsed = safeParse(provider, body);
  if (parsed === null) return {};
  if (provider === "openai" || provider === "azure.openai") {
    return extractOpenAIResponse(parsed);
  }
  if (provider === "anthropic" || provider === "aws.bedrock") {
    return extractAnthropicResponse(parsed);
  }
  return {};
}

function safeParse(
  provider: string | undefined,
  body: Uint8Array | string | undefined,
): Record<string, unknown> | null {
  if (provider === undefined || body === undefined) return null;
  let text: string;
  if (typeof body === "string") {
    if (body.length === 0) return null;
    if (body.length > MAX_BODY_BYTES) return null;
    text = body;
  } else {
    if (body.byteLength === 0) return null;
    if (body.byteLength > MAX_BODY_BYTES) return null;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(body);
    } catch {
      // Non-UTF-8 input (e.g. an audio response) — bail out cleanly.
      return null;
    }
  }
  try {
    const obj: unknown = JSON.parse(text);
    if (obj === null || typeof obj !== "object" || Array.isArray(obj)) {
      return null;
    }
    return obj as Record<string, unknown>;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// OpenAI / Azure OpenAI
// ---------------------------------------------------------------------------

function extractOpenAIRequest(
  body: Record<string, unknown>,
): Record<string, string | boolean> {
  const attrs: Record<string, string | boolean> = {};
  if (typeof body.model === "string" && body.model) {
    attrs["gen_ai.request.model"] = body.model;
  }
  if (typeof body.stream === "boolean") {
    attrs["gen_ai.request.stream"] = body.stream;
  }
  return attrs;
}

function extractOpenAIResponse(
  body: Record<string, unknown>,
): Record<string, string | number> {
  const attrs: Record<string, string | number> = {};
  if (typeof body.model === "string" && body.model) {
    attrs["gen_ai.response.model"] = body.model;
  }
  const usage = body.usage;
  if (usage !== null && typeof usage === "object" && !Array.isArray(usage)) {
    const u = usage as Record<string, unknown>;
    if (typeof u.prompt_tokens === "number" && Number.isInteger(u.prompt_tokens)) {
      attrs["gen_ai.usage.input_tokens"] = u.prompt_tokens;
    }
    if (typeof u.completion_tokens === "number" && Number.isInteger(u.completion_tokens)) {
      attrs["gen_ai.usage.output_tokens"] = u.completion_tokens;
    }
  }
  return attrs;
}

// ---------------------------------------------------------------------------
// Anthropic / Anthropic-on-Bedrock
// ---------------------------------------------------------------------------

function extractAnthropicRequest(
  body: Record<string, unknown>,
): Record<string, string | boolean> {
  const attrs: Record<string, string | boolean> = {};
  if (typeof body.model === "string" && body.model) {
    attrs["gen_ai.request.model"] = body.model;
  }
  if (typeof body.stream === "boolean") {
    attrs["gen_ai.request.stream"] = body.stream;
  }
  return attrs;
}

function extractAnthropicResponse(
  body: Record<string, unknown>,
): Record<string, string | number> {
  const attrs: Record<string, string | number> = {};
  if (typeof body.model === "string" && body.model) {
    attrs["gen_ai.response.model"] = body.model;
  }
  const usage = body.usage;
  if (usage !== null && typeof usage === "object" && !Array.isArray(usage)) {
    const u = usage as Record<string, unknown>;
    if (typeof u.input_tokens === "number" && Number.isInteger(u.input_tokens)) {
      attrs["gen_ai.usage.input_tokens"] = u.input_tokens;
    }
    if (typeof u.output_tokens === "number" && Number.isInteger(u.output_tokens)) {
      attrs["gen_ai.usage.output_tokens"] = u.output_tokens;
    }
  }
  return attrs;
}
