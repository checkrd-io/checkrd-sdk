/**
 * Per-status error classes.
 *
 * Mirrors the OpenAI / Anthropic / Stripe TypeScript SDK shape so
 * users who already know those libraries don't have to relearn
 * anything. ``CheckrdError`` is the base; everything is catchable
 * via that single ``catch`` clause when callers don't care about
 * the status code.
 *
 * Reference shape: ``openai-node/src/error.ts``,
 * ``anthropic-sdk-typescript/src/error.ts``.
 *
 * The control plane returns flat RFC 9457 ``application/problem+json``
 * bodies (see ``crates/api/src/errors.rs``). The whole JSON object *is*
 * the problem document — there is no nested ``{ error: { ... } }``
 * envelope. Branch on the machine-readable ``code`` extension member.
 */

/** One per-field validation failure, keyed by an RFC 6901 JSON Pointer. */
export interface ProblemErrorItem {
  /** RFC 6901 JSON Pointer to the offending field, e.g. ``/email``. */
  pointer: string;
  /** Human-readable explanation of why this field was rejected. */
  detail: string;
}

/**
 * Flat RFC 9457 ``application/problem+json`` body returned for any
 * non-2xx response. Every member is optional because a degraded edge
 * (an LB 502, a non-JSON gateway error) may return a partial or empty
 * document — callers must defend against ``undefined``.
 */
export interface ErrorBody {
  /** RFC 9457 type URI, e.g. ``https://checkrd.io/errors/invalid_api_key``. */
  type?: string;
  /** RFC 9457 stable, human-readable problem-type summary. */
  title?: string;
  /** RFC 9457 HTTP status, duplicated in-body. */
  status?: number;
  /** RFC 9457 occurrence-specific explanation. */
  detail?: string;
  /** RFC 9457 URI reference identifying this specific occurrence. */
  instance?: string;
  /** Stable, machine-readable code (extension member). Branch on this. */
  code?: string;
  /** Per-field validation failures (RFC 6901 JSON Pointers). */
  errors?: ProblemErrorItem[];
  /** Server-generated correlation ID (extension member). */
  request_id?: string;
}

/**
 * Base class for every error this SDK raises. Catch this if you
 * want to handle anything network- or API-related; catch the more
 * specific subclasses below for finer-grained handling.
 */
export class CheckrdError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CheckrdError";
    Object.setPrototypeOf(this, CheckrdError.prototype);
  }
}

/** Base for any error returned by the API. */
export class APIError extends CheckrdError {
  readonly request: Request | undefined;
  readonly body: ErrorBody | undefined;

  constructor(message: string, request?: Request, body?: ErrorBody) {
    super(message);
    this.name = "APIError";
    this.request = request;
    this.body = body;
    Object.setPrototypeOf(this, APIError.prototype);
  }

  /** Stable, machine-readable error code (top-level ``code`` member). */
  get code(): string | undefined {
    return this.body?.code;
  }

  /** RFC 9457 problem ``type`` URI, e.g. ``.../errors/<code>``. */
  get type(): string | undefined {
    return this.body?.type;
  }

  /** Per-field validation failures (RFC 6901 JSON Pointers), if any. */
  get errors(): ProblemErrorItem[] | undefined {
    return this.body?.errors;
  }

  /**
   * JSON Pointer to the first offending request field, when the body
   * carries per-field validation failures (typically a 422). The flat
   * replacement for the removed Stripe-style ``param``.
   */
  get param(): string | undefined {
    return this.body?.errors?.[0]?.pointer;
  }
}

/**
 * Network reached the local resolver but never made it to Checkrd
 * (DNS failure, TCP reset, TLS handshake failure, etc.).
 */
export class APIConnectionError extends APIError {
  constructor(message: string, request?: Request) {
    super(message, request);
    this.name = "APIConnectionError";
    Object.setPrototypeOf(this, APIConnectionError.prototype);
  }
}

/**
 * The request was started but did not complete before the
 * configured timeout. Distinct from a 408/504 returned by the
 * server.
 */
export class APITimeoutError extends APIConnectionError {
  constructor(message: string, request?: Request) {
    super(message, request);
    this.name = "APITimeoutError";
    Object.setPrototypeOf(this, APITimeoutError.prototype);
  }
}

/**
 * Base for any non-2xx response. Always exposes ``status``,
 * ``response``, and ``requestId`` (from the
 * ``checkrd-request-id`` header — useful for support tickets).
 *
 * ``retryAfterMs`` carries the parsed ``Retry-After`` hint (seconds
 * or HTTP-date) when the server supplied one — useful on a 429 even
 * though this client surfaces it rather than auto-sleeping on it.
 */
export class APIStatusError extends APIError {
  readonly status: number;
  readonly response: Response;
  readonly requestId: string | undefined;
  readonly retryAfterMs: number | undefined;

  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, undefined, body);
    this.name = "APIStatusError";
    this.status = response.status;
    this.response = response;
    this.requestId =
      body?.request_id ??
      response.headers.get("checkrd-request-id") ??
      response.headers.get("x-request-id") ??
      undefined;
    this.retryAfterMs = retryAfterMs;
    Object.setPrototypeOf(this, APIStatusError.prototype);
  }
}

export class BadRequestError extends APIStatusError {
  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, response, body, retryAfterMs);
    this.name = "BadRequestError";
    Object.setPrototypeOf(this, BadRequestError.prototype);
  }
}

export class AuthenticationError extends APIStatusError {
  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, response, body, retryAfterMs);
    this.name = "AuthenticationError";
    Object.setPrototypeOf(this, AuthenticationError.prototype);
  }
}

export class PermissionDeniedError extends APIStatusError {
  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, response, body, retryAfterMs);
    this.name = "PermissionDeniedError";
    Object.setPrototypeOf(this, PermissionDeniedError.prototype);
  }
}

export class NotFoundError extends APIStatusError {
  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, response, body, retryAfterMs);
    this.name = "NotFoundError";
    Object.setPrototypeOf(this, NotFoundError.prototype);
  }
}

export class ConflictError extends APIStatusError {
  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, response, body, retryAfterMs);
    this.name = "ConflictError";
    Object.setPrototypeOf(this, ConflictError.prototype);
  }
}

export class UnprocessableEntityError extends APIStatusError {
  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, response, body, retryAfterMs);
    this.name = "UnprocessableEntityError";
    Object.setPrototypeOf(this, UnprocessableEntityError.prototype);
  }
}

export class RateLimitError extends APIStatusError {
  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, response, body, retryAfterMs);
    this.name = "RateLimitError";
    Object.setPrototypeOf(this, RateLimitError.prototype);
  }
}

export class InternalServerError extends APIStatusError {
  constructor(
    message: string,
    response: Response,
    body?: ErrorBody,
    retryAfterMs?: number,
  ) {
    super(message, response, body, retryAfterMs);
    this.name = "InternalServerError";
    Object.setPrototypeOf(this, InternalServerError.prototype);
  }
}

/**
 * Parse a ``Retry-After`` header into milliseconds. Accepts the two
 * RFC 9110 forms: a non-negative integer number of seconds, or an
 * HTTP-date. Returns ``undefined`` when the header is absent or
 * malformed. Mirrors ``parseRetryAfter`` in the runtime SDK's
 * ``_retry.ts``.
 */
export function parseRetryAfterMs(headers: Headers): number | undefined {
  const retryAfterMs = headers.get("retry-after-ms");
  if (retryAfterMs !== null) {
    const ms = Number.parseInt(retryAfterMs, 10);
    if (Number.isFinite(ms) && ms > 0) return ms;
  }
  const retryAfter = headers.get("retry-after");
  if (retryAfter !== null) {
    const asSeconds = Number.parseFloat(retryAfter);
    if (Number.isFinite(asSeconds) && asSeconds > 0) {
      return Math.round(asSeconds * 1000);
    }
    // HTTP-date form (e.g. "Wed, 21 Oct 2026 07:28:00 GMT").
    const date = Date.parse(retryAfter);
    if (Number.isFinite(date)) {
      const delta = date - Date.now();
      if (delta > 0) return delta;
    }
  }
  return undefined;
}

/**
 * Pick the right subclass based on ``response.status``. Mirrors
 * the dispatch table the OpenAI SDK uses; each branch returns the
 * specific subclass so callers can ``catch`` just one type.
 *
 * The body is the flat RFC 9457 problem document itself — there is no
 * nested ``error`` envelope to unwrap. Degrades gracefully: a missing
 * or non-JSON body yields a sane ``HTTP <status>`` message and a
 * ``code`` of ``undefined``.
 */
export function makeStatusError(
  response: Response,
  body: ErrorBody | undefined,
): APIStatusError {
  const message =
    body?.detail ?? body?.title ?? `HTTP ${response.status.toString()}`;
  const retryAfterMs = parseRetryAfterMs(response.headers);
  switch (response.status) {
    case 400:
      return new BadRequestError(message, response, body, retryAfterMs);
    case 401:
      return new AuthenticationError(message, response, body, retryAfterMs);
    case 403:
      return new PermissionDeniedError(message, response, body, retryAfterMs);
    case 404:
      return new NotFoundError(message, response, body, retryAfterMs);
    case 409:
      return new ConflictError(message, response, body, retryAfterMs);
    case 422:
      return new UnprocessableEntityError(message, response, body, retryAfterMs);
    case 429:
      return new RateLimitError(message, response, body, retryAfterMs);
    default:
      if (response.status >= 500) {
        return new InternalServerError(message, response, body, retryAfterMs);
      }
      return new APIStatusError(message, response, body, retryAfterMs);
  }
}
