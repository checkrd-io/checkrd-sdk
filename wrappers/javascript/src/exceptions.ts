/**
 * Checkrd exception hierarchy. Mirrors `wrappers/python/src/checkrd/exceptions.py`
 * one-for-one so the two SDKs feel identical to anyone moving between them.
 *
 * Modeled on the OpenAI / Anthropic / Stripe TypeScript SDKs:
 *
 *   CheckrdError                            // root — every Checkrd error
 *   ├── CheckrdInitError                    // SDK init (WASM, policy, key)
 *   ├── CheckrdPolicyDenied                 // WASM engine denied a request
 *   ├── PolicySignatureError                // DSSE bundle rejected
 *   └── APIError                            // control-plane HTTP error
 *       ├── APIStatusError                  // 4xx/5xx with a response body
 *       │   ├── BadRequestError             // 400
 *       │   ├── AuthenticationError         // 401
 *       │   ├── PermissionDeniedError       // 403
 *       │   ├── NotFoundError               // 404
 *       │   ├── ConflictError               // 409
 *       │   ├── UnprocessableEntityError    // 422
 *       │   ├── RateLimitError              // 429
 *       │   └── InternalServerError         // ≥ 500
 *       ├── APIConnectionError              // network failure, no response
 *       │   └── APITimeoutError             // timeout (subclass of conn err)
 *       ├── APIResponseValidationError      // 2xx body doesn't match schema
 *       └── APIUserAbortError               // caller cancelled the request
 *
 * Catch ``CheckrdError`` to handle anything from the SDK. Catch
 * ``APIStatusError`` to handle any HTTP error with a response body. Catch
 * the most specific subclass for status-code-specific logic (e.g.
 * ``RateLimitError`` to read ``retry-after``).
 *
 * Rationale for two middle classes (``APIStatusError`` vs
 * ``APIConnectionError``): connection errors do not have a response, so
 * the response-bearing fields would be ``undefined`` if every API error
 * shared one shape. Splitting the tree lets TypeScript prove
 * ``response.headers["retry-after"]`` is safe inside a catch on
 * ``APIStatusError`` without runtime undefined checks.
 */

function deriveCode(message: string): string {
  const lower = message.toLowerCase();
  if (lower.includes("integrity")) return "wasm_integrity_failed";
  if (lower.includes("wasm") && lower.includes("not found")) return "wasm_not_found";
  if (lower.includes("kill switch")) return "kill_switch_active";
  if (lower.includes("rate limit")) return "rate_limit_exceeded";
  if (lower.includes("policy")) return "invalid_policy";
  if (lower.includes("key") && lower.includes("invalid")) return "invalid_key";
  return "checkrd_error";
}

/**
 * FFI error code → stable string label. Mirrors
 * `_FFI_ERROR_REASONS` in the Python wrapper so logs and metrics carry
 * identical labels across SDKs.
 */
export const FFI_ERROR_REASONS: Record<number, string> = {
  [-1]: "envelope_json_parse_error",
  [-2]: "invalid_utf8",
  [-3]: "trusted_keys_json_parse_error",
  [-4]: "payload_type_mismatch",
  [-5]: "signature_invalid",
  [-6]: "unknown_or_no_signer",
  [-7]: "key_not_in_validity_window",
  [-8]: "verified_payload_invalid",
  [-9]: "engine_not_initialized",
  [-10]: "schema_version_mismatch",
  [-11]: "bundle_version_not_monotonic",
  [-12]: "bundle_too_old",
  [-13]: "bundle_in_future",
  [-14]: "policy_version_already_set",
};

/** Base URL for per-code documentation. Mirrors Stripe's ``doc_url``. */
export const DOCS_BASE_URL = "https://checkrd.io/errors";

// ===========================================================================
// Base
// ===========================================================================

/**
 * Base class for every error thrown by the Checkrd SDK. Catch this to
 * handle any failure uniformly — both SDK-local errors (init, policy
 * denials, signature verification) and control-plane API errors.
 */
export class CheckrdError extends Error {
  /** Stable machine-readable error code, suitable for metrics. */
  readonly code: string;

  constructor(message: string, code?: string) {
    super(message);
    this.name = "CheckrdError";
    this.code = code ?? deriveCode(message);
    Object.setPrototypeOf(this, CheckrdError.prototype);
  }

  /**
   * Deep link to the per-code remediation guide
   * (``https://checkrd.io/errors/{code}``). Stable URL pattern, safe to
   * include in logs and 4xx envelopes.
   */
  get docsUrl(): string {
    return `${DOCS_BASE_URL}/${this.code}`;
  }
}

// ===========================================================================
// SDK-local errors
// ===========================================================================

/** Raised when the SDK cannot initialize (WASM load, policy parse, key validation). */
export class CheckrdInitError extends CheckrdError {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message);
    this.name = "CheckrdInitError";
    if (options?.cause !== undefined) {
      // ES2022 cause chain — surfaces in Node's stack-trace render and
      // preserves the original SyntaxError / TypeError that triggered
      // the wrap. Same pattern used by APIError in this module.
      (this as { cause?: unknown }).cause = options.cause;
    }
    Object.setPrototypeOf(this, CheckrdInitError.prototype);
  }
}

/** Details attached to a policy-denied event. */
export interface CheckrdPolicyDeniedDetails {
  /** Reason string from the WASM engine, suitable for user messaging. */
  reason: string;
  /** Correlation ID for cross-referencing telemetry and dashboard. */
  requestId: string;
  /** URL that was denied. */
  url: string;
  /** Name of the rule that matched, if the deny came from an explicit rule. */
  ruleName?: string;
  /** Deep-link into the dashboard for this event, if configured. */
  dashboardUrl?: string;
  /** Actionable suggestion, e.g., policy edit hint. */
  suggestion?: string;
}

/**
 * Structural type guard for {@link CheckrdPolicyDenied} that survives
 * cross-realm checks (Next.js server vs client bundles, Workers
 * isolates vs Node, multiple copies of `checkrd` in the dep tree).
 *
 * `instanceof CheckrdPolicyDenied` would fail when two different
 * bundles each define their own class — a real hazard when the SDK is
 * linked through multiple module graphs (Next.js middleware vs
 * app-router server components).
 */
export function isCheckrdPolicyDenied(
  err: unknown,
): err is CheckrdPolicyDenied {
  if (err === null || typeof err !== "object") return false;
  return (err as { name?: unknown }).name === "CheckrdPolicyDenied";
}

/** Raised when a request is denied by the policy engine. */
export class CheckrdPolicyDenied extends CheckrdError {
  readonly reason: string;
  readonly requestId: string;
  readonly url: string;
  readonly ruleName: string | undefined;
  readonly dashboardUrl: string | undefined;
  readonly suggestion: string | undefined;

  constructor(details: CheckrdPolicyDeniedDetails) {
    super(details.reason);
    this.name = "CheckrdPolicyDenied";
    this.reason = details.reason;
    this.requestId = details.requestId;
    this.url = details.url;
    this.ruleName = details.ruleName;
    this.dashboardUrl = details.dashboardUrl;
    this.suggestion = details.suggestion;
    Object.setPrototypeOf(this, CheckrdPolicyDenied.prototype);
  }
}

/** Raised when a signed policy bundle fails verification. */
export class PolicySignatureError extends CheckrdError {
  /** The raw FFI error code from the WASM core (negative integer). */
  readonly ffiCode: number;
  /** Stable string label for ``ffiCode`` (e.g. ``signature_invalid``). */
  readonly reason: string;

  constructor(ffiCode: number, detail?: string) {
    const reason = FFI_ERROR_REASONS[ffiCode] ?? `unknown_ffi_code_${ffiCode.toString()}`;
    super(detail ? `${reason}: ${detail}` : reason, reason);
    this.name = "PolicySignatureError";
    this.ffiCode = ffiCode;
    this.reason = reason;
    Object.setPrototypeOf(this, PolicySignatureError.prototype);
  }
}

// ===========================================================================
// Control-plane API errors
// ===========================================================================

/**
 * Parsed control-plane error-response body. The Checkrd API uses a
 * Stripe-style envelope: ``{ error: { type, code, message, param } }``.
 */
export interface APIErrorBody {
  type?: string;
  code?: string;
  message?: string;
  param?: string | null;
}

/** Constructor details for {@link APIError} subclasses with a response. */
export interface APIStatusErrorDetails {
  /** HTTP status code returned by the control plane. */
  status: number;
  /** Parsed error body, if the response had one. */
  body: APIErrorBody | undefined;
  /** All response headers (lower-cased keys), for forensic logging. */
  headers: Record<string, string>;
  /** Server-generated request ID, from ``Checkrd-Request-Id`` / ``X-Request-Id``. */
  requestId: string | undefined;
  /** Pre-formatted human-readable message. */
  message: string;
}

/** Constructor details for connection-level errors (no response). */
export interface APIConnectionErrorDetails {
  /** Pre-formatted human-readable message. */
  message?: string;
  /** Optional underlying cause for `Error.cause`. */
  cause?: unknown;
}

/**
 * Base class for every control-plane HTTP error. Catch this to handle
 * any control-plane failure uniformly. Subclasses split into two main
 * groups: {@link APIStatusError} (server returned 4xx/5xx) and
 * {@link APIConnectionError} (no response — DNS, TCP, TLS, abort).
 */
export class APIError extends CheckrdError {
  constructor(message: string, code?: string, options?: { cause?: unknown }) {
    super(message, code);
    this.name = "APIError";
    if (options?.cause !== undefined) {
      // ES2022 cause chain — surfaces in Node's stack-trace render.
      (this as { cause?: unknown }).cause = options.cause;
    }
    Object.setPrototypeOf(this, APIError.prototype);
  }
}

/**
 * Control-plane responded with a 4xx or 5xx. Most callers should catch
 * a more specific subclass (e.g. {@link RateLimitError}); this is the
 * supertype for "any HTTP-level failure" branches.
 */
export class APIStatusError extends APIError {
  /** HTTP status code (e.g. 429). */
  readonly status: number;
  /** Parsed error body, if the response had one. */
  readonly body: APIErrorBody | undefined;
  /** All response headers (lower-cased keys). */
  readonly headers: Record<string, string>;
  /** Server-generated request ID — quote this in support tickets. */
  readonly requestId: string | undefined;

  constructor(details: APIStatusErrorDetails) {
    const code =
      details.body?.code ?? `http_${details.status.toString()}`;
    super(details.message, code);
    this.name = "APIStatusError";
    this.status = details.status;
    this.body = details.body;
    this.headers = details.headers;
    this.requestId = details.requestId;
    Object.setPrototypeOf(this, APIStatusError.prototype);
  }
}

/**
 * Network-level failure (DNS, TCP, TLS) before the request reached
 * the server. No response body is available.
 */
export class APIConnectionError extends APIError {
  constructor(details: APIConnectionErrorDetails = {}) {
    super(
      details.message ?? "Connection error.",
      "api_connection_error",
      details.cause !== undefined ? { cause: details.cause } : undefined,
    );
    this.name = "APIConnectionError";
    Object.setPrototypeOf(this, APIConnectionError.prototype);
  }
}

/**
 * Timeout waiting for a response. Subclass of {@link APIConnectionError}
 * because there is no response body — distinguishing timeouts from
 * generic connection errors lets callers apply different retry
 * policies.
 */
export class APITimeoutError extends APIConnectionError {
  constructor(cause?: unknown) {
    super(
      cause !== undefined
        ? { message: "Request timed out.", cause }
        : { message: "Request timed out." },
    );
    this.name = "APITimeoutError";
    // Override base class's code with the more specific timeout label.
    (this as { code: string }).code = "api_timeout";
    Object.setPrototypeOf(this, APITimeoutError.prototype);
  }
}

/**
 * Server returned 2xx but the body did not match the expected schema.
 * Symptom of a control-plane / SDK version skew.
 */
export class APIResponseValidationError extends APIError {
  constructor(message = "Data returned by API invalid for expected schema.") {
    super(message, "response_validation_error");
    this.name = "APIResponseValidationError";
    Object.setPrototypeOf(this, APIResponseValidationError.prototype);
  }
}

/**
 * Request was cancelled by the caller's ``AbortSignal``. Distinct from
 * {@link APITimeoutError} because the cause is user-initiated — do not
 * retry. Mirrors OpenAI's ``APIUserAbortError``.
 */
export class APIUserAbortError extends APIError {
  constructor(message = "Request was aborted.") {
    super(message, "user_abort");
    this.name = "APIUserAbortError";
    Object.setPrototypeOf(this, APIUserAbortError.prototype);
  }
}

// ---------------------------------------------------------------------------
// Status-code subclasses
// ---------------------------------------------------------------------------

/** 400 Bad Request — the SDK sent a malformed request. */
export class BadRequestError extends APIStatusError {
  constructor(details: APIStatusErrorDetails) {
    super(details);
    this.name = "BadRequestError";
    Object.setPrototypeOf(this, BadRequestError.prototype);
  }
}

/** 401 Unauthorized — invalid or missing API key. */
export class AuthenticationError extends APIStatusError {
  constructor(details: APIStatusErrorDetails) {
    super(details);
    this.name = "AuthenticationError";
    Object.setPrototypeOf(this, AuthenticationError.prototype);
  }
}

/** 403 Forbidden — API key lacks permission for the resource. */
export class PermissionDeniedError extends APIStatusError {
  constructor(details: APIStatusErrorDetails) {
    super(details);
    this.name = "PermissionDeniedError";
    Object.setPrototypeOf(this, PermissionDeniedError.prototype);
  }
}

/** 404 Not Found — typically an unknown agent / org / resource id. */
export class NotFoundError extends APIStatusError {
  constructor(details: APIStatusErrorDetails) {
    super(details);
    this.name = "NotFoundError";
    Object.setPrototypeOf(this, NotFoundError.prototype);
  }
}

/** 409 Conflict — Idempotency-Key reuse, duplicate resource. */
export class ConflictError extends APIStatusError {
  constructor(details: APIStatusErrorDetails) {
    super(details);
    this.name = "ConflictError";
    Object.setPrototypeOf(this, ConflictError.prototype);
  }
}

/** 422 Unprocessable Entity — schema validation failed. */
export class UnprocessableEntityError extends APIStatusError {
  constructor(details: APIStatusErrorDetails) {
    super(details);
    this.name = "UnprocessableEntityError";
    Object.setPrototypeOf(this, UnprocessableEntityError.prototype);
  }
}

/**
 * 429 Too Many Requests. Inspect ``headers["retry-after"]`` /
 * ``headers["retry-after-ms"]`` to decide backoff. The SDK retry loop
 * already honors these — callers see this only when retries are
 * exhausted.
 */
export class RateLimitError extends APIStatusError {
  constructor(details: APIStatusErrorDetails) {
    super(details);
    this.name = "RateLimitError";
    Object.setPrototypeOf(this, RateLimitError.prototype);
  }
}

/** ≥500 — control-plane internal error. Generally transient. */
export class InternalServerError extends APIStatusError {
  constructor(details: APIStatusErrorDetails) {
    super(details);
    this.name = "InternalServerError";
    Object.setPrototypeOf(this, InternalServerError.prototype);
  }
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

/**
 * Map an HTTP response to the most specific {@link APIError} subclass.
 * Mirrors OpenAI's ``APIError.generate()`` so the catch-by-type pattern
 * works the same way consumers already expect.
 *
 * Pass ``status: null`` to produce a generic {@link APIConnectionError}
 * — the input is overloaded to keep the dispatch contract simple at
 * the single call-site that maps fetch failures.
 */
export function makeAPIError(
  details:
    | (APIStatusErrorDetails & { status: number })
    | { status: null; message: string; cause?: unknown },
): APIError {
  if (details.status === null) {
    return new APIConnectionError({
      message: details.message,
      cause: details.cause,
    });
  }
  const status = details.status;
  if (status === 400) return new BadRequestError(details);
  if (status === 401) return new AuthenticationError(details);
  if (status === 403) return new PermissionDeniedError(details);
  if (status === 404) return new NotFoundError(details);
  if (status === 409) return new ConflictError(details);
  if (status === 422) return new UnprocessableEntityError(details);
  if (status === 429) return new RateLimitError(details);
  if (status >= 500) return new InternalServerError(details);
  return new APIStatusError(details);
}
