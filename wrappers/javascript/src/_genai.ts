/**
 * OpenTelemetry GenAI semantic conventions extraction.
 *
 * Maps an outbound LLM API call's URL to the OTel GenAI attribute set
 * defined at
 * https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/gen-ai-spans.md.
 *
 * Mirrors `wrappers/python/src/checkrd/_genai.py` one-for-one — both
 * SDKs emit identical labels so dashboards, alerts, and runbooks work
 * against either runtime without re-mapping.
 *
 * Why URL-only (not body-derived):
 *   - The transport sees the request URL on every call without
 *     buffering the body. ``gen_ai.provider.name`` and
 *     ``gen_ai.operation.name`` are the two attributes downstream
 *     tooling needs first; both come from the URL.
 *   - Other attributes (``gen_ai.request.model``,
 *     ``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``)
 *     require parsing request / response bodies. Those are added in
 *     a future revision behind an explicit opt-in to keep the PII
 *     surface bounded — Checkrd's "zero data processor" stance is
 *     structural, not best-effort.
 *   - The OTel spec marks ``gen_ai.provider.name`` and
 *     ``gen_ai.operation.name`` as required / recommended for
 *     GenAI spans; everything else is opportunistic.
 */

/**
 * Provider mapping (host → OTel ``gen_ai.provider.name``).
 *
 * Matches both the exact-host case (``api.openai.com``) and the
 * suffix case for hosts that include a region segment
 * (``…amazonaws.com``). Suffix matches are checked AFTER exact
 * matches so a vendor with both patterns routes to the same provider
 * name.
 */
const PROVIDER_EXACT: Readonly<Record<string, string>> = {
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
};

// Suffix-anchored providers. Used for hosts where the leading segment
// is the user-controlled deployment name (e.g. Azure OpenAI).
const PROVIDER_SUFFIX: readonly (readonly [string, string])[] = [
  // Azure OpenAI deployments (e.g. ``my-deployment.openai.azure.com``).
  [".openai.azure.com", "azure.openai"],
];

// Substring-AND-suffix providers. AWS Bedrock and Google Vertex AI
// embed a region segment in the middle of the host
// (``bedrock-runtime.us-east-1.amazonaws.com``,
// ``us-central1-aiplatform.googleapis.com``), so a pure ``endsWith``
// would match S3 / DynamoDB / unrelated GCP APIs. Both the substring
// (the service marker) and the suffix (the cloud) must match.
const PROVIDER_SUBSTRING_SUFFIX: readonly (readonly [string, string, string])[] = [
  ["bedrock-runtime.", ".amazonaws.com", "aws.bedrock"],
  ["bedrock.", ".amazonaws.com", "aws.bedrock"],
  ["aiplatform.", ".googleapis.com", "google.vertex_ai"],
];

/**
 * Operation mapping (path → OTel ``gen_ai.operation.name``).
 *
 * Substring matches against the URL path. Order matters: the more
 * specific patterns come first so ``/v1/chat/completions`` resolves
 * to ``chat`` rather than the broader ``completions`` →
 * ``text_completion``.
 */
const OPERATION_PATTERNS: readonly (readonly [string, string])[] = [
  ["/chat/completions", "chat"],
  ["/messages", "chat"], // Anthropic
  ["/converse", "chat"], // AWS Bedrock Converse API
  ["/responses", "chat"], // OpenAI Responses API (2025+)
  ["/embeddings", "embeddings"],
  ["/embed", "embeddings"],
  ["/completions", "text_completion"],
  // Lower-cased patterns — paths are normalized to lowercase before
  // matching, so the table entries must be lowercase too.
  [":generatecontent", "generate_content"],
  [":streamgeneratecontent", "generate_content"],
  ["/images/generations", "image.generation"],
  ["/audio/speech", "audio.speech"],
  ["/audio/transcriptions", "audio.transcription"],
  ["/tools/", "execute_tool"],
];

/**
 * Return the OTel ``gen_ai.provider.name`` for a given URL host.
 * Returns ``undefined`` when the host doesn't match any known
 * provider — callers should omit ``gen_ai.provider.name`` entirely
 * rather than emit a ``"unknown"`` placeholder. The OTel spec treats
 * absence and the literal string ``"unknown"`` differently for
 * billing / aggregation queries.
 *
 * Case-insensitive: hosts are lower-cased before lookup.
 */
export function detectProvider(urlHost: string): string | undefined {
  if (!urlHost) return undefined;
  const host = urlHost.toLowerCase().trim();
  if (host in PROVIDER_EXACT) return PROVIDER_EXACT[host];
  for (const [suffix, provider] of PROVIDER_SUFFIX) {
    if (host.endsWith(suffix)) return provider;
  }
  for (const [substring, suffix, provider] of PROVIDER_SUBSTRING_SUFFIX) {
    if (host.includes(substring) && host.endsWith(suffix)) return provider;
  }
  return undefined;
}

/**
 * Return the OTel ``gen_ai.operation.name`` for a given URL path.
 * Returns ``undefined`` for paths that don't look like a GenAI
 * endpoint — same omit-vs-unknown rationale as
 * {@link detectProvider}.
 */
export function detectOperation(urlPath: string): string | undefined {
  if (!urlPath) return undefined;
  const path = urlPath.toLowerCase();
  for (const [pattern, op] of OPERATION_PATTERNS) {
    if (path.includes(pattern)) return op;
  }
  return undefined;
}

/**
 * Build the OTel GenAI attribute dict for a given URL. Returns only
 * the attributes that could be confidently derived — no ``"unknown"``
 * placeholders. Caller merges the result into the telemetry event
 * payload::
 *
 *   Object.assign(event, attributesForUrl(host, path));
 *   // event["gen_ai.provider.name"] = "openai"
 *   // event["gen_ai.operation.name"] = "chat"
 *
 * An empty object means "no GenAI attributes inferred" — the call is
 * probably not an LLM request, or the SDK's mapping table needs
 * updating for a new vendor.
 */
export function attributesForUrl(
  urlHost: string,
  urlPath: string,
): Record<string, string> {
  const attrs: Record<string, string> = {};
  const provider = detectProvider(urlHost);
  if (provider !== undefined) attrs["gen_ai.provider.name"] = provider;
  const operation = detectOperation(urlPath);
  if (operation !== undefined) attrs["gen_ai.operation.name"] = operation;
  return attrs;
}
