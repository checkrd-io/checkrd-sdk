/**
 * Body-derived GenAI semantic-convention extraction.
 *
 * Mirrors `wrappers/python/src/checkrd/_genai_body.py` byte-for-byte
 * — the same vendor shapes, the same opt-in posture, the same
 * 1 MB cap. Both SDKs produce identical attribute names so dashboards
 * group across runtimes. The parity contract is pinned by the
 * language-neutral golden fixtures in `schemas/genai-fixtures/`,
 * which both extractors are tested against case-for-case.
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
 * # The inclusion-rule invariant (billing-critical)
 *
 * OTel GenAI semconv treats the cache / reasoning detail counters as
 * **subsets of the totals** (``gen_ai.usage.input_tokens`` /
 * ``output_tokens``). The core's ``settle_usage`` nets fresh input as
 * ``input − cache_read − cache_creation`` and bills each band at its
 * own rate, so every extractor MUST normalize a provider's native
 * counts so that, in the emitted attributes:
 *
 *   cache_read + cache_creation <= input   and   reasoning <= output
 *
 * Providers differ in whether their native counts are already
 * inclusive — OpenAI / Gemini are; Anthropic's ``input_tokens``
 * *excludes* cache, so we sum cache back in. See the per-provider
 * branches and the fixtures README for the worked Anthropic example.
 *
 * # Vendor coverage
 *
 *   - **OpenAI** + Azure OpenAI, also matches OpenAI-compatible
 *     endpoints (Together, Groq compat).
 *   - **Anthropic** (``api.anthropic.com/v1/messages``).
 *   - **Gemini / Vertex AI** (``usageMetadata`` shape).
 *   - **Cohere** (``meta.billed_units`` / ``meta.tokens``).
 *   - **Bedrock** — token counts come from the
 *     ``x-amzn-bedrock-*-token-count`` response headers; the
 *     Anthropic-on-Bedrock request body carries the model.
 */

const MAX_BODY_BYTES = 1_048_576;

// OTel GenAI attribute keys. Kept as named constants so a typo can't
// silently drift one SDK's wire key away from the other's.
const ATTR_REQUEST_MODEL = "gen_ai.request.model";
const ATTR_REQUEST_STREAM = "gen_ai.request.stream";
const ATTR_RESPONSE_MODEL = "gen_ai.response.model";
const ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens";
const ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens";
const ATTR_CACHE_READ = "gen_ai.usage.cache_read.input_tokens";
const ATTR_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens";
const ATTR_REASONING = "gen_ai.usage.reasoning.output_tokens";

/**
 * Response-attribute values. Token counts are numbers; the model name
 * is a string. (Request attrs additionally carry the boolean
 * ``stream`` flag.)
 */
export type ResponseAttrs = Record<string, string | number>;

/** Request-attribute values: model string + the boolean stream flag. */
export type RequestAttrs = Record<string, string | boolean>;

/**
 * Header bag accepted by {@link extractResponseAttrs}: either a plain
 * object (case-sensitive keys, looked up case-insensitively) or a
 * WHATWG ``Headers`` instance (already case-insensitive).
 */
export type HeaderBag = Record<string, string> | Headers | undefined;

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
): RequestAttrs {
  const parsed = safeParse(provider, body);
  if (parsed === null) return {};
  if (provider === "openai" || provider === "azure.openai") {
    return extractOpenAIRequest(parsed);
  }
  if (provider === "cohere") {
    // Cohere's chat request mirrors OpenAI's: model + stream at top level.
    return extractOpenAIRequest(parsed);
  }
  if (provider === "anthropic" || provider === "aws.bedrock") {
    // Anthropic and Anthropic-on-Bedrock both carry the model at the
    // top level, so the request extraction is identical.
    return extractAnthropicRequest(parsed);
  }
  return {};
}

/**
 * Extract OTel ``gen_ai.response.*`` and ``gen_ai.usage.*`` attrs.
 *
 * ``headers`` is only consulted for Bedrock, whose authoritative
 * token counts live in the ``x-amzn-bedrock-*-token-count`` response
 * headers rather than the body. Accepts a plain object (looked up
 * case-insensitively) or a WHATWG ``Headers`` instance; ignored for
 * every other provider.
 */
export function extractResponseAttrs(
  provider: string | undefined,
  body: Uint8Array | string | undefined,
  headers?: HeaderBag,
): ResponseAttrs {
  // Bedrock derives its token counts purely from response headers and
  // ignores the (Anthropic-shaped) body for counts, so it runs before
  // the body-parse short-circuit — a Bedrock response can legitimately
  // carry no body at all.
  if (provider === "aws.bedrock") {
    return extractBedrockResponse(headers);
  }
  const parsed = safeParse(provider, body);
  if (parsed === null) return {};
  if (provider === "openai" || provider === "azure.openai") {
    return extractOpenAIResponse(parsed);
  }
  if (provider === "anthropic") {
    return extractAnthropicResponse(parsed);
  }
  if (provider === "google.gemini" || provider === "google.vertex_ai") {
    return extractGeminiResponse(parsed);
  }
  if (provider === "cohere") {
    return extractCohereResponse(parsed);
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
// Shared helpers
// ---------------------------------------------------------------------------

/**
 * Read a nested ``Record`` value, returning ``null`` when the value is
 * absent, ``null``, a primitive, or an array. Mirrors the Python
 * ``isinstance(x, dict)`` guard used before reaching into ``usage`` /
 * ``meta`` sub-objects.
 */
function asObject(value: unknown): Record<string, unknown> | null {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return null;
}

/**
 * Return the value if it is a JSON integer, else ``undefined``.
 * Matches Python's ``isinstance(x, int)`` (and `bool` is excluded
 * because TS booleans are not ``number``). Non-integers (floats,
 * strings, NaN) are skipped, never coerced.
 */
function asInt(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isInteger(value)) {
    return value;
  }
  return undefined;
}

/**
 * Sum integer terms for the Anthropic inclusive-input normalization.
 * Missing / non-integer terms count as 0 so a partial usage object
 * still yields a sensible inclusive total.
 */
function intOrZero(value: unknown): number {
  return asInt(value) ?? 0;
}

// ---------------------------------------------------------------------------
// OpenAI / Azure OpenAI
// ---------------------------------------------------------------------------

function extractOpenAIRequest(body: Record<string, unknown>): RequestAttrs {
  const attrs: RequestAttrs = {};
  if (typeof body.model === "string" && body.model) {
    attrs[ATTR_REQUEST_MODEL] = body.model;
  }
  if (typeof body.stream === "boolean") {
    attrs[ATTR_REQUEST_STREAM] = body.stream;
  }
  return attrs;
}

function extractOpenAIResponse(body: Record<string, unknown>): ResponseAttrs {
  const attrs: ResponseAttrs = {};
  if (typeof body.model === "string" && body.model) {
    attrs[ATTR_RESPONSE_MODEL] = body.model;
  }
  const usage = asObject(body.usage);
  if (usage !== null) {
    // ``prompt_tokens`` / ``completion_tokens`` are already totals
    // (cached ⊆ prompt, reasoning ⊆ completion) — no normalization.
    const prompt = asInt(usage.prompt_tokens);
    if (prompt !== undefined) {
      attrs[ATTR_INPUT_TOKENS] = prompt;
    }
    const completion = asInt(usage.completion_tokens);
    if (completion !== undefined) {
      attrs[ATTR_OUTPUT_TOKENS] = completion;
    }
    const promptDetails = asObject(usage.prompt_tokens_details);
    if (promptDetails !== null) {
      // 0 is a valid count, distinct from absent — emit it.
      const cached = asInt(promptDetails.cached_tokens);
      if (cached !== undefined) {
        attrs[ATTR_CACHE_READ] = cached;
      }
    }
    const completionDetails = asObject(usage.completion_tokens_details);
    if (completionDetails !== null) {
      const reasoning = asInt(completionDetails.reasoning_tokens);
      if (reasoning !== undefined) {
        attrs[ATTR_REASONING] = reasoning;
      }
    }
  }
  return attrs;
}

// ---------------------------------------------------------------------------
// Anthropic
// ---------------------------------------------------------------------------

function extractAnthropicRequest(body: Record<string, unknown>): RequestAttrs {
  const attrs: RequestAttrs = {};
  if (typeof body.model === "string" && body.model) {
    attrs[ATTR_REQUEST_MODEL] = body.model;
  }
  if (typeof body.stream === "boolean") {
    attrs[ATTR_REQUEST_STREAM] = body.stream;
  }
  return attrs;
}

function extractAnthropicResponse(body: Record<string, unknown>): ResponseAttrs {
  const attrs: ResponseAttrs = {};
  if (typeof body.model === "string" && body.model) {
    attrs[ATTR_RESPONSE_MODEL] = body.model;
  }
  const usage = asObject(body.usage);
  if (usage !== null) {
    const cacheRead = asInt(usage.cache_read_input_tokens);
    const cacheCreation = asInt(usage.cache_creation_input_tokens);
    const rawInput = asInt(usage.input_tokens);
    if (rawInput !== undefined) {
      // Anthropic's input_tokens EXCLUDES cache; normalize to the
      // inclusive total so cache_read + cache_creation ≤ input and the
      // core's settle netting reproduces the invoice. Missing cache
      // terms count as 0.
      attrs[ATTR_INPUT_TOKENS] =
        rawInput + intOrZero(usage.cache_read_input_tokens) + intOrZero(usage.cache_creation_input_tokens);
    }
    const output = asInt(usage.output_tokens);
    if (output !== undefined) {
      attrs[ATTR_OUTPUT_TOKENS] = output;
    }
    if (cacheRead !== undefined) {
      attrs[ATTR_CACHE_READ] = cacheRead;
    }
    if (cacheCreation !== undefined) {
      attrs[ATTR_CACHE_CREATION] = cacheCreation;
    }
  }
  return attrs;
}

// ---------------------------------------------------------------------------
// Gemini / Vertex AI
// ---------------------------------------------------------------------------

function extractGeminiResponse(body: Record<string, unknown>): ResponseAttrs {
  const attrs: ResponseAttrs = {};
  // Model lives in ``modelVersion`` on the response (the request model
  // is URL-derived and out of the body-extractor's scope).
  if (typeof body.modelVersion === "string" && body.modelVersion) {
    attrs[ATTR_RESPONSE_MODEL] = body.modelVersion;
  }
  const usage = asObject(body.usageMetadata);
  if (usage !== null) {
    const promptTokens = asInt(usage.promptTokenCount);
    if (promptTokens !== undefined) {
      attrs[ATTR_INPUT_TOKENS] = promptTokens;
    }
    // candidatesTokenCount is inclusive of thoughts on the standard
    // Gemini API (and the Vertex deployments these fixtures pin), so
    // it is the output total as-is — do NOT add thoughts back in.
    const candidatesTokens = asInt(usage.candidatesTokenCount);
    if (candidatesTokens !== undefined) {
      attrs[ATTR_OUTPUT_TOKENS] = candidatesTokens;
    }
    const cached = asInt(usage.cachedContentTokenCount);
    if (cached !== undefined) {
      attrs[ATTR_CACHE_READ] = cached;
    }
    const thoughts = asInt(usage.thoughtsTokenCount);
    if (thoughts !== undefined) {
      attrs[ATTR_REASONING] = thoughts;
    }
  }
  return attrs;
}

// ---------------------------------------------------------------------------
// Cohere
// ---------------------------------------------------------------------------

function extractCohereResponse(body: Record<string, unknown>): ResponseAttrs {
  const attrs: ResponseAttrs = {};
  const meta = asObject(body.meta);
  if (meta !== null) {
    // Prefer ``billed_units`` (what the customer is charged) over
    // ``meta.tokens`` (includes uncharged internal tokens), so the
    // cost reconciles with the Cohere invoice.
    const billed = asObject(meta.billed_units);
    const tokens = asObject(meta.tokens);
    const source = billed ?? tokens;
    if (source !== null) {
      const input = asInt(source.input_tokens);
      if (input !== undefined) {
        attrs[ATTR_INPUT_TOKENS] = input;
      }
      const output = asInt(source.output_tokens);
      if (output !== undefined) {
        attrs[ATTR_OUTPUT_TOKENS] = output;
      }
    }
  }
  return attrs;
}

// ---------------------------------------------------------------------------
// Bedrock (response token counts from headers)
// ---------------------------------------------------------------------------

/**
 * Case-insensitive header lookup over either a plain object or a WHATWG
 * ``Headers`` instance. Returns ``undefined`` when the header is absent.
 */
function getHeader(headers: HeaderBag, name: string): string | undefined {
  if (headers === undefined) return undefined;
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    // ``Headers.get`` is already case-insensitive per the spec.
    return headers.get(name) ?? undefined;
  }
  const wanted = name.toLowerCase();
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === wanted) {
      return (headers as Record<string, string>)[key];
    }
  }
  return undefined;
}

/**
 * Parse a header string to a JSON-style integer, or ``undefined`` when
 * it is absent / non-numeric. Strict: only an all-digit token (with an
 * optional sign) counts, so ``"n/a"`` or ``"12px"`` is skipped, never
 * coerced.
 */
function headerInt(value: string | undefined): number | undefined {
  if (value === undefined) return undefined;
  const trimmed = value.trim();
  if (!/^[+-]?\d+$/.test(trimmed)) return undefined;
  const n = Number(trimmed);
  return Number.isInteger(n) ? n : undefined;
}

function extractBedrockResponse(headers: HeaderBag): ResponseAttrs {
  const attrs: ResponseAttrs = {};
  const input = headerInt(getHeader(headers, "x-amzn-bedrock-input-token-count"));
  if (input !== undefined) {
    attrs[ATTR_INPUT_TOKENS] = input;
  }
  const output = headerInt(getHeader(headers, "x-amzn-bedrock-output-token-count"));
  if (output !== undefined) {
    attrs[ATTR_OUTPUT_TOKENS] = output;
  }
  return attrs;
}
