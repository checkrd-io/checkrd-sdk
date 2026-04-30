/**
 * Single source of truth for credential-bearing header names and
 * secret-ish object keys. Both the HTTP transport (redacts request
 * headers before they reach user hooks) and the logger (redacts
 * nested log attributes before they reach a sink) consult these sets.
 *
 * Keeping them in one module prevents drift: when a new vendor ships
 * another auth header (e.g. `x-new-provider-key`), a single addition
 * covers every redaction path.
 *
 * All names MUST be lowercase — consumers lowercase inputs before
 * testing membership. HTTP header names are case-insensitive per
 * RFC 9110; JSON object keys are case-sensitive but we redact the
 * well-known casings anyway.
 */

/**
 * HTTP header names (lowercase) whose values must never leak into
 * logs, hook event payloads, or telemetry. Add to this list when a
 * new vendor ships a new authentication header.
 */
export const SENSITIVE_HEADER_NAMES: ReadonlySet<string> = new Set([
  // Standard
  "authorization",
  "proxy-authorization",
  "cookie",
  "set-cookie",
  // Generic API key conventions
  "api-key",
  "x-api-key",
  // Checkrd's own
  "checkrd-api-key",
  "x-checkrd-api-key",
  // Vendor-specific
  "anthropic-api-key",
  "openai-api-key",
  "openai-organization",
  "x-goog-api-key",
]);

/**
 * Object-key names commonly used to hold credentials or other
 * secret material. Matched case-sensitively on the raw key — the
 * variants cover the most common casings (`apiKey`, `api_key`, etc.)
 * without forcing every caller into one style.
 */
export const SENSITIVE_KEY_NAMES: ReadonlySet<string> = new Set([
  "apikey",
  "api_key",
  "apiKey",
  "secret",
  "password",
  "token",
  "bearer",
  "private_key",
  "privateKey",
  "authorization",
]);

/** Replacement string emitted wherever a sensitive value is stripped. */
export const REDACTED = "[REDACTED]";

/**
 * Whether a header name is considered sensitive. Lowercases the input
 * before membership testing so callers can pass the raw casing.
 */
export function isSensitiveHeader(name: string): boolean {
  return SENSITIVE_HEADER_NAMES.has(name.toLowerCase());
}

/**
 * Query-string parameter names (lowercased) that must be scrubbed from
 * URLs before the URL is stored in telemetry, logs, or span attributes.
 * Covers the common "auth in the query string" anti-patterns: API keys
 * passed via `?api_key=...`, signed URLs with `?signature=...`, OAuth
 * tokens in `?access_token=...`, etc.
 *
 * Matching is case-insensitive (URL query keys are conventionally
 * case-insensitive in practice even though RFC 3986 says otherwise).
 */
export const SENSITIVE_QUERY_PARAMS: ReadonlySet<string> = new Set([
  "api_key",
  "apikey",
  "access_token",
  "accesstoken",
  "auth",
  "authorization",
  "bearer",
  "password",
  "passwd",
  "private_key",
  "privatekey",
  "secret",
  "signature",
  "sig",
  "token",
  "x-api-key",
  "x-auth-token",
]);

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !(v instanceof Error) && !Array.isArray(v);
}

/**
 * Recursively redact well-known sensitive fields and header values. Keeps
 * the object shape so downstream sinks still have something useful to
 * show the operator.
 *
 * Depth cap at 4 levels — unbounded recursion would be a DoS vector on
 * adversarial nested structures. The cap matches OpenTelemetry's
 * attribute-depth conventions.
 *
 * Single source of truth — before lived in `_logger.ts`. Moved here so
 * telemetry sinks can reuse it without creating a cycle through the
 * logger module.
 */
export function redactSensitive(input: unknown, depth = 0): unknown {
  if (depth > 4) return input;
  if (Array.isArray(input)) {
    // `[[name, value]]` (the wire-compatible header shape used across the SDK)
    if (input.every((x): x is unknown[] => Array.isArray(x) && x.length === 2)) {
      return input.map((pair): unknown[] => {
        const [name, value] = pair as [unknown, unknown];
        if (typeof name !== "string") return pair;
        const safe = SENSITIVE_HEADER_NAMES.has(name.toLowerCase()) ? REDACTED : value;
        return [name, safe];
      });
    }
    return input.map((v) => redactSensitive(v, depth + 1));
  }
  if (isPlainObject(input)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(input)) {
      if (SENSITIVE_KEY_NAMES.has(k) || SENSITIVE_HEADER_NAMES.has(k.toLowerCase())) {
        out[k] = REDACTED;
      } else {
        out[k] = redactSensitive(v, depth + 1);
      }
    }
    return out;
  }
  return input;
}

/**
 * URL-bearing keys on a telemetry event that must be passed through
 * {@link scrubUrl} before reaching a non-Checkrd sink. Other keys are
 * handled by {@link redactSensitive}'s name-based redaction, which is
 * where object keys like `api_key` or `authorization` get caught.
 */
const URL_KEYS: readonly string[] = ["url", "url_full", "url_path", "target_uri"];

/**
 * Scrub a telemetry event for export to a third-party sink. Applies
 * {@link redactSensitive} over the whole object (name-based redaction
 * of `authorization`, `api_key`, `token`, etc. at any nesting depth),
 * then scrubs URL query strings on known URL-bearing fields.
 *
 * Called at the boundary of every non-Checkrd sink: {@link OtlpSink},
 * {@link ConsoleSink}, {@link JsonFileSink}. The trusted control-plane
 * path ({@link ControlPlaneSink}) skips this — the control plane is
 * the intended recipient of the full signed payload and signing happens
 * over canonical bytes; scrubbing in the middle would invalidate them.
 */
export function scrubTelemetryEvent(
  event: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  const redacted = redactSensitive(event) as Record<string, unknown>;
  for (const key of URL_KEYS) {
    const v = redacted[key];
    if (typeof v === "string") redacted[key] = scrubUrl(v);
  }
  return redacted;
}

/**
 * Scrub secret-looking query parameters from a URL string. Returns the
 * URL unchanged if parsing fails (never throws — the caller is usually
 * a telemetry path that must not surface a new error).
 *
 * Accepts both fully-qualified URLs (``https://api.example.com/x?k=v``)
 * and path-only forms (``/v1/x?k=v``) — the telemetry layer stores the
 * host and path as separate fields, so the URL-bearing telemetry keys
 * include path-only values. We re-parse with a dummy base and strip
 * it off the output when the original input was path-only.
 *
 * Example:
 *   scrubUrl("https://api.example.com/v1/x?api_key=sk-abc&limit=10")
 *   // "https://api.example.com/v1/x?api_key=[REDACTED]&limit=10"
 *   scrubUrl("/v1/x?token=abc")
 *   // "/v1/x?token=[REDACTED]"
 */
export function scrubUrl(input: string): string {
  // Fast path: URLs without a `?` have nothing to scrub. Saves ~90% of
  // telemetry events from paying the URL-parse cost.
  if (!input.includes("?")) return input;

  // `new URL(input)` requires a scheme. When the caller hands us a
  // path-only value ("/v1/x?k=v"), parse against a sentinel base and
  // strip it back off before returning. The sentinel host is chosen so
  // that any URL canonicalization the `URL` constructor performs can't
  // leave a trace in the output — `checkrd.invalid` is in the
  // reserved `.invalid` TLD (RFC 6761 §6.4) and can never resolve.
  const isPathOnly = !/^[a-zA-Z][a-zA-Z\d+.-]*:/.test(input);
  const SENTINEL_BASE = "https://scrub.checkrd.invalid";

  let url: URL;
  try {
    url = isPathOnly ? new URL(input, SENTINEL_BASE) : new URL(input);
  } catch {
    return input;
  }
  let modified = false;
  for (const key of Array.from(url.searchParams.keys())) {
    if (SENSITIVE_QUERY_PARAMS.has(key.toLowerCase())) {
      url.searchParams.set(key, REDACTED);
      modified = true;
    }
  }
  if (!modified) return input;
  // `URLSearchParams.set` percent-encodes the value, so the `REDACTED`
  // marker comes out as `%5BREDACTED%5D`. That's technically correct
  // but ugly in dashboards — operators glance at URLs and expect the
  // marker to jump out. Swap the encoded form back for the literal on
  // the (known, SDK-controlled) marker string. Other real values that
  // happen to contain the same marker substring would be weird
  // upstream data we wouldn't want to corrupt, but `[REDACTED]` isn't
  // a plausible real value for a bearer token / api key either, so
  // this is safe in practice.
  const encodedMarker = encodeURIComponent(REDACTED);
  const out = isPathOnly
    ? `${url.pathname}${url.search}${url.hash}`
    : url.toString();
  return out.split(encodedMarker).join(REDACTED);
}
