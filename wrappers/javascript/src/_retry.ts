/**
 * Retry + idempotency helpers for control-plane HTTP calls.
 *
 * The formula is lifted verbatim from the OpenAI and Anthropic TypeScript
 * SDKs (which both lifted it from Stripe before them):
 *
 *   sleepSeconds = min(0.5 * 2^retries, 8.0)
 *   jitter       = 1 - Math.random() * 0.25   // up to 25% down-jitter
 *   sleepMs      = sleepSeconds * jitter * 1000
 *
 * Server hints (`retry-after-ms` and `retry-after`) override the formula
 * when present. This keeps us respectful of the control-plane's
 * backpressure signals instead of bulldozing through a 429 storm.
 */

import {
  makeAPIError,
  APIUserAbortError,
  type APIStatusErrorDetails,
} from "./exceptions.js";
import { platformHeaders } from "./_platform.js";
import { VERSION } from "./_version.js";
import type { Logger } from "./_logger.js";
import type { CircuitBreaker } from "./_circuit_breaker.js";

/** Options for {@link fetchWithRetry}. */
export interface RetryOptions {
  /** Base fetch to use. Defaults to the global `fetch`. */
  fetch?: typeof fetch;
  /** Max attempts including the first try. Default: 3. */
  maxAttempts?: number;
  /**
   * **Per-attempt** request timeout in ms. Default: 30_000.
   *
   * Each retry gets a fresh ``timeoutMs``-second budget — total wall
   * time can be up to ``maxAttempts * (timeoutMs + maxSleepSecs)``
   * once backoff is accounted for. This matches the OpenAI SDK's
   * ``timeout`` semantic (per-attempt, not overall) so users moving
   * between SDKs get consistent behavior.
   *
   * To bound *total* wall time, pass an ``AbortSignal`` with a
   * deadline (``AbortSignal.timeout(60_000)``) — the loop honors the
   * signal and aborts mid-flight rather than starting another retry.
   */
  timeoutMs?: number;
  /** Max retry sleep ceiling in seconds. Default: 8. */
  maxSleepSecs?: number;
  /** User's AbortSignal. Aborts cancel all retries immediately. */
  signal?: AbortSignal | undefined;
  /** Logger for backoff / retry events. */
  logger?: Logger | undefined;
  /** Optional hook fired before each attempt (0-indexed attempt number). */
  onAttempt?: (attempt: number) => void;
  /** Circuit breaker that can short-circuit when the control plane is flapping. */
  circuitBreaker?: CircuitBreaker | undefined;
}

/**
 * Status codes that should be retried. Matches OpenAI's 408/409/429/≥500
 * list plus the non-standard `x-should-retry` opt-in header.
 */
function shouldRetryStatus(status: number, headers: Headers): boolean {
  const hint = headers.get("x-should-retry");
  if (hint === "true") return true;
  if (hint === "false") return false;
  return status === 408 || status === 409 || status === 429 || status >= 500;
}

function parseRetryAfter(headers: Headers, maxSleepSecs: number): number | null {
  const retryAfterMs = headers.get("retry-after-ms");
  if (retryAfterMs !== null) {
    const ms = Number.parseInt(retryAfterMs, 10);
    if (Number.isFinite(ms) && ms > 0) return Math.min(ms, maxSleepSecs * 1000 * 2);
  }
  const retryAfter = headers.get("retry-after");
  if (retryAfter !== null) {
    const asNumber = Number.parseFloat(retryAfter);
    if (Number.isFinite(asNumber) && asNumber > 0) {
      return Math.min(asNumber * 1000, maxSleepSecs * 1000 * 2);
    }
    // HTTP-date form
    const date = Date.parse(retryAfter);
    if (Number.isFinite(date)) {
      const delta = date - Date.now();
      if (delta > 0) return Math.min(delta, maxSleepSecs * 1000 * 2);
    }
  }
  return null;
}

function computeBackoffMs(attempt: number, maxSleepSecs: number): number {
  const seconds = Math.min(0.5 * Math.pow(2, attempt), maxSleepSecs);
  const jitter = 1 - Math.random() * 0.25;
  return Math.round(seconds * jitter * 1000);
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new APIUserAbortError());
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new APIUserAbortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Generate an Idempotency-Key header value. Safe to call any number of
 * times; each invocation yields a fresh UUID.
 */
export function newIdempotencyKey(): string {
  // crypto.randomUUID is available in Node 16+, Bun, Deno, Cloudflare
  // Workers, Vercel Edge — every runtime we target.
  return `checkrd-${globalThis.crypto.randomUUID()}`;
}

/** Options for {@link defaultControlHeaders}. */
export interface DefaultControlHeadersOptions {
  /**
   * Stripe-style date pin sent as ``Checkrd-Version`` when non-empty.
   * Lets the control plane handle old + new clients simultaneously and
   * lets customers pin a known-good API shape across rollouts.
   */
  apiVersion?: string;
}

/**
 * Headers that every Checkrd control-plane request defaults to. Consolidated
 * here so the telemetry batcher, key registrar, SSE receiver, and anything
 * else we add all send identical metadata — operators looking at ingestion
 * logs see a single consistent shape.
 *
 * Always-on headers:
 *   - ``Content-Type: application/json``
 *   - ``X-API-Key`` (caller-supplied)
 *   - ``Idempotency-Key`` (fresh UUID per call — callers MUST capture the
 *     header object once and reuse it across retries so the control plane
 *     can dedupe a retry of an already-accepted request; the batcher and
 *     key registrar both follow this pattern)
 *   - ``User-Agent: checkrd-js/<version>``
 *   - ``X-Checkrd-SDK-*`` platform/runtime/version family
 *
 * Optional:
 *   - ``Checkrd-Version`` when ``opts.apiVersion`` is a non-empty string.
 */
export function defaultControlHeaders(
  apiKey: string,
  opts: DefaultControlHeadersOptions = {},
): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-API-Key": apiKey,
    "Idempotency-Key": newIdempotencyKey(),
    "User-Agent": `checkrd-js/${VERSION}`,
    ...platformHeaders(),
  };
  if (opts.apiVersion && opts.apiVersion.length > 0) {
    headers["Checkrd-Version"] = opts.apiVersion;
  }
  return headers;
}

/**
 * Upper bound on the response body we'll buffer when constructing a
 * {@link APIError}. A hostile or misconfigured control plane
 * could otherwise return a multi-gigabyte error body and exhaust host
 * memory before the exception is even thrown.
 */
const MAX_ERROR_BODY_BYTES = 64 * 1024;

/**
 * Header names whose values must not be attached to
 * {@link APIError.headers}. Mirrors the transport-layer allowlist
 * plus a set of commonly-reflected auth-bearing headers that
 * misconfigured proxies sometimes echo back on error responses.
 */
const ERROR_HEADER_BLOCKLIST: ReadonlySet<string> = new Set([
  "authorization",
  "x-api-key",
  "api-key",
  "proxy-authorization",
  "cookie",
  "set-cookie",
  "anthropic-api-key",
  "openai-api-key",
  "checkrd-api-key",
  "x-checkrd-api-key",
  "x-forwarded-authorization",
  "x-goog-api-key",
]);

async function readBoundedText(response: Response): Promise<string> {
  const reader = response.body?.getReader();
  if (!reader) {
    try {
      const raw = await response.text();
      return raw.slice(0, MAX_ERROR_BODY_BYTES);
    } catch {
      return "";
    }
  }
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (total < MAX_ERROR_BODY_BYTES) {
      const { value, done } = await reader.read();
      if (done) break;
      const allowed = Math.min(value.byteLength, MAX_ERROR_BODY_BYTES - total);
      chunks.push(value.subarray(0, allowed));
      total += allowed;
      if (total >= MAX_ERROR_BODY_BYTES) {
        try {
          await reader.cancel();
        } catch {
          // no-op
        }
        break;
      }
    }
  } catch {
    // fall through with whatever we collected
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // no-op
    }
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8").decode(merged);
}

async function parseErrorBody(response: Response): Promise<{
  body: import("./exceptions.js").APIErrorBody | undefined;
  message: string;
}> {
  const text = await readBoundedText(response);
  if (text.length === 0) {
    return { body: undefined, message: `HTTP ${response.status.toString()} ${response.statusText}` };
  }
  try {
    const parsed = JSON.parse(text) as { error?: import("./exceptions.js").APIErrorBody };
    const body = parsed.error ?? undefined;
    const message =
      body?.message ??
      `HTTP ${response.status.toString()} ${response.statusText}: ${text.slice(0, 200)}`;
    return { body, message };
  } catch {
    return {
      body: undefined,
      message: `HTTP ${response.status.toString()} ${response.statusText}: ${text.slice(0, 200)}`,
    };
  }
}

function collectHeaders(headers: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  headers.forEach((value, key) => {
    const k = key.toLowerCase();
    out[k] = ERROR_HEADER_BLOCKLIST.has(k) ? "[REDACTED]" : value;
  });
  return out;
}

/**
 * Execute an HTTP request with the retry + idempotency contract expected
 * by the Checkrd control plane. Throws a {@link APIError}
 * subclass on a non-retryable or final failure.
 *
 * The request body is expected to be idempotency-safe — callers pass an
 * `Idempotency-Key` header (see {@link defaultControlHeaders}) so the
 * server can dedupe retries.
 */
export async function fetchWithRetry(
  url: string,
  init: RequestInit,
  opts: RetryOptions = {},
): Promise<Response> {
  const {
    fetch: fetchImpl = globalThis.fetch.bind(globalThis),
    maxAttempts = 3,
    timeoutMs = 30_000,
    maxSleepSecs = 8,
    signal,
    logger,
    onAttempt,
    circuitBreaker,
  } = opts;

  // Short-circuit before touching the network if the breaker says so.
  // We treat an open breaker exactly like a control-plane connection
  // failure — callers can retry once the reset window elapses.
  if (circuitBreaker && !circuitBreaker.allow()) {
    throw makeAPIError({
      status: null,
      message: "control-plane circuit breaker open; refusing to attempt call",
    });
  }

  let lastError: unknown = null;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    onAttempt?.(attempt);
    if (signal?.aborted) throw new APIUserAbortError();

    const attemptController = new AbortController();
    const timer = setTimeout(() => { attemptController.abort(); }, timeoutMs);
    const onOuterAbort = (): void => { attemptController.abort(); };
    signal?.addEventListener("abort", onOuterAbort, { once: true });

    // Stamp X-Checkrd-Retry-Count on attempts > 0 so the control
    // plane can correlate "this is retry N of M" in its access logs.
    // Mirrors OpenAI's `X-Stainless-Retry-Count` header.
    //
    // ``redirect: "error"`` is the default — the Checkrd control plane
    // is a known single-origin API that should never redirect. A 3xx
    // from ``api.checkrd.io`` would indicate either a compromised
    // endpoint, a misconfigured corporate proxy that intercepts TLS,
    // or DNS hijacking — all of which warrant failing closed rather
    // than silently following to a potentially attacker-controlled
    // host (the textbook SSRF-via-redirect path). Callers who need to
    // override (e.g., a private gateway that serves redirects to an
    // internal URL) pass ``redirect`` explicitly in ``init``.
    const attemptInit: RequestInit = {
      ...init,
      signal: attemptController.signal,
      redirect: init.redirect ?? "error",
    };
    if (attempt > 0) {
      const merged = new Headers(init.headers);
      merged.set("X-Checkrd-Retry-Count", attempt.toString());
      attemptInit.headers = merged;
    }

    let response: Response;
    try {
      response = await fetchImpl(url, attemptInit);
    } catch (err) {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onOuterAbort);
      if (signal?.aborted) throw new APIUserAbortError();
      lastError = err;
      if (attempt < maxAttempts - 1) {
        const delay = computeBackoffMs(attempt, maxSleepSecs);
        logger?.debug("control plane call failed, retrying", { attempt, delay, err });
        await sleep(delay, signal);
        continue;
      }
      throw makeAPIError({
        status: null,
        message: err instanceof Error ? err.message : String(err),
        cause: err,
      });
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onOuterAbort);
    }

    if (response.ok) {
      circuitBreaker?.recordSuccess();
      return response;
    }

    const retryable = shouldRetryStatus(response.status, response.headers);
    if (!retryable || attempt >= maxAttempts - 1) {
      circuitBreaker?.recordFailure();
      const { body, message } = await parseErrorBody(response);
      const details: APIStatusErrorDetails & { status: number } = {
        status: response.status,
        body,
        headers: collectHeaders(response.headers),
        requestId:
          response.headers.get("checkrd-request-id") ??
          response.headers.get("x-request-id") ??
          undefined,
        message,
      };
      throw makeAPIError(details);
    }

    // Drain and discard the retryable response body before backoff so we
    // don't leak the underlying connection.
    try {
      await response.body?.cancel();
    } catch {
      // best-effort cleanup
    }
    const serverHint = parseRetryAfter(response.headers, maxSleepSecs);
    const delay = serverHint ?? computeBackoffMs(attempt, maxSleepSecs);
    logger?.debug("retryable response, backing off", {
      status: response.status,
      delay,
      attempt,
    });
    await sleep(delay, signal);
  }

  // Should be unreachable — the loop above either returns or throws.
  throw lastError instanceof Error
    ? lastError
    : new Error("fetchWithRetry exhausted without return");
}
