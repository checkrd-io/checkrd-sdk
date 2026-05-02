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
 */

/** Stripe-style error envelope returned for any non-2xx response. */
export interface ErrorBody {
  type?: string;
  code?: string;
  message: string;
  param?: string;
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

  /** Stripe-style error code from ``body.error.code``. */
  get code(): string | undefined {
    return this.body?.code;
  }

  /** Pointer to the offending request parameter (400 only). */
  get param(): string | undefined {
    return this.body?.param;
  }

  /** Coarse-grained category. */
  get type(): string | undefined {
    return this.body?.type;
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
 */
export class APIStatusError extends APIError {
  readonly status: number;
  readonly response: Response;
  readonly requestId: string | undefined;

  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, undefined, body);
    this.name = "APIStatusError";
    this.status = response.status;
    this.response = response;
    this.requestId =
      response.headers.get("checkrd-request-id") ??
      response.headers.get("x-request-id") ??
      undefined;
    Object.setPrototypeOf(this, APIStatusError.prototype);
  }
}

export class BadRequestError extends APIStatusError {
  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, response, body);
    this.name = "BadRequestError";
    Object.setPrototypeOf(this, BadRequestError.prototype);
  }
}

export class AuthenticationError extends APIStatusError {
  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, response, body);
    this.name = "AuthenticationError";
    Object.setPrototypeOf(this, AuthenticationError.prototype);
  }
}

export class PermissionDeniedError extends APIStatusError {
  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, response, body);
    this.name = "PermissionDeniedError";
    Object.setPrototypeOf(this, PermissionDeniedError.prototype);
  }
}

export class NotFoundError extends APIStatusError {
  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, response, body);
    this.name = "NotFoundError";
    Object.setPrototypeOf(this, NotFoundError.prototype);
  }
}

export class ConflictError extends APIStatusError {
  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, response, body);
    this.name = "ConflictError";
    Object.setPrototypeOf(this, ConflictError.prototype);
  }
}

export class UnprocessableEntityError extends APIStatusError {
  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, response, body);
    this.name = "UnprocessableEntityError";
    Object.setPrototypeOf(this, UnprocessableEntityError.prototype);
  }
}

export class RateLimitError extends APIStatusError {
  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, response, body);
    this.name = "RateLimitError";
    Object.setPrototypeOf(this, RateLimitError.prototype);
  }
}

export class InternalServerError extends APIStatusError {
  constructor(message: string, response: Response, body?: ErrorBody) {
    super(message, response, body);
    this.name = "InternalServerError";
    Object.setPrototypeOf(this, InternalServerError.prototype);
  }
}

/**
 * Pick the right subclass based on ``response.status``. Mirrors
 * the dispatch table the OpenAI SDK uses; each branch returns the
 * specific subclass so callers can ``catch`` just one type.
 */
export function makeStatusError(
  response: Response,
  body: { error?: ErrorBody } | undefined,
): APIStatusError {
  const errBody = body?.error;
  const message = errBody?.message ?? `HTTP ${response.status.toString()}`;
  switch (response.status) {
    case 400:
      return new BadRequestError(message, response, errBody);
    case 401:
      return new AuthenticationError(message, response, errBody);
    case 403:
      return new PermissionDeniedError(message, response, errBody);
    case 404:
      return new NotFoundError(message, response, errBody);
    case 409:
      return new ConflictError(message, response, errBody);
    case 422:
      return new UnprocessableEntityError(message, response, errBody);
    case 429:
      return new RateLimitError(message, response, errBody);
    default:
      if (response.status >= 500) {
        return new InternalServerError(message, response, errBody);
      }
      return new APIStatusError(message, response, errBody);
  }
}
