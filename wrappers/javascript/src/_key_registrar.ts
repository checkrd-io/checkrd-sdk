/**
 * Public-key registration with the control plane.
 *
 * Mirrors Python's `_maybe_register_public_key()` in
 * `wrappers/python/src/checkrd/__init__.py`. On SDK init, if we hold
 * an Ed25519 private key *and* a control-plane URL + API key, we POST
 * the public-key bytes to `/v1/agents/{id}/public-key` so the server
 * can verify RFC 9421 signatures on our telemetry batches. Without
 * this round trip, the control plane has no key to check against
 * and the "signed telemetry" guarantee is theatrical.
 *
 * Design properties matching the Python implementation:
 *
 *   - Fire-and-forget. Never blocks `init()` / `initAsync()`.
 *   - Bounded retries with exponential backoff + jitter for transient
 *     server errors (5xx, network failures).
 *   - Permanent-error fast-fail:
 *       - 409 conflict = the server already has a different key for
 *         this agent. Retry won't help; log a clear remediation.
 *       - 401 / 403 = bad API key. Retry won't help; warn the user.
 *   - Success response bodies are ignored; the HTTP status is the
 *     contract.
 */

import { defaultControlHeaders } from "./_retry.js";
import type { Logger } from "./_logger.js";

/** Default maximum registration attempts including the first try. */
const DEFAULT_MAX_RETRIES = 3;
/** Initial backoff between retries, in seconds. */
const INITIAL_DELAY_SECS = 1;
/** Ceiling on the per-attempt backoff, in seconds. */
const MAX_DELAY_SECS = 10;
/** Default per-attempt HTTP timeout for the registration request. */
const DEFAULT_ATTEMPT_TIMEOUT_MS = 5000;

/** Options for {@link registerPublicKey}. */
export interface RegisterPublicKeyOptions {
  /** Control-plane base URL. No trailing slash required; we normalise. */
  controlPlaneUrl: string;
  /** API key for `X-API-Key`. */
  apiKey: string;
  /** Agent ID used in the URL path. URL-encoded before being spliced in. */
  agentId: string;
  /** 32-byte Ed25519 public key. */
  publicKey: Uint8Array;
  /** Optional logger; defaults to silent. */
  logger?: Logger | undefined;
  /**
   * Optional fetch implementation override. Defaults to `globalThis.fetch`.
   * Provided for tests and for runtimes that bind a non-global fetch.
   */
  fetch?: typeof fetch;
  /**
   * Stripe-style date pin sent as ``Checkrd-Version`` if non-empty.
   * Threaded through so control-plane registrations land on the same
   * API version as the telemetry this key will sign.
   */
  apiVersion?: string | undefined;
  /**
   * Maximum attempts (including the first). Defaults to 3. Threaded
   * from the public ``Checkrd({ maxRetries })`` constructor option.
   */
  maxRetries?: number;
  /**
   * Per-attempt HTTP timeout in ms. Defaults to 5000. Threaded from
   * the public ``Checkrd({ timeout })`` constructor option.
   */
  timeoutMs?: number;
}

/**
 * POST `/v1/agents/{id}/public-key` to register this agent's public
 * key with the control plane. Runs in the background; the returned
 * promise resolves when the worker completes (success or terminal
 * failure). Callers typically ignore the promise — {@link init} and
 * {@link initAsync} do — so a registration delay never blocks the
 * hot path.
 */
export function registerPublicKey(
  options: RegisterPublicKeyOptions,
): Promise<void> {
  return registerWithRetry(options);
}

async function registerWithRetry(
  options: RegisterPublicKeyOptions,
): Promise<void> {
  const { controlPlaneUrl, apiKey, agentId, publicKey, logger } = options;
  const fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  const maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
  const timeoutMs = options.timeoutMs ?? DEFAULT_ATTEMPT_TIMEOUT_MS;

  if (publicKey.byteLength !== 32) {
    logger?.warn(
      `checkrd: refusing to register ${publicKey.byteLength.toString()}-byte public key; expected 32`,
    );
    return;
  }

  const url =
    controlPlaneUrl.replace(/\/+$/, "") +
    `/v1/agents/${encodeURIComponent(agentId)}/public-key`;
  const body = JSON.stringify({ public_key: bytesToHex(publicKey) });
  // Stripe-style idempotency: same key across every retry so the
  // control plane can dedupe. Generated once per registration.
  // `defaultControlHeaders` gives us the consolidated header set —
  // Content-Type, X-API-Key, Idempotency-Key, User-Agent, the
  // X-Checkrd-SDK-* platform family, and optional Checkrd-Version.
  const versionOpt =
    options.apiVersion !== undefined ? { apiVersion: options.apiVersion } : {};
  const headers: Record<string, string> = defaultControlHeaders(
    apiKey, versionOpt,
  );

  let delaySecs = INITIAL_DELAY_SECS;
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, timeoutMs);
    let response: Response;
    try {
      response = await fetchImpl(url, {
        method: "POST",
        headers,
        body,
        signal: controller.signal,
      });
    } catch {
      // Network-level failure (DNS, TCP, TLS, abort). Retry if we have attempts left.
      clearTimeout(timer);
      if (attempt < maxRetries - 1) {
        logger?.debug(
          `checkrd: public key registration failed (network), ` +
            `retry ${(attempt + 1).toString()}/${maxRetries.toString()} ` +
            `in ${delaySecs.toFixed(1)}s`,
        );
        await sleep(delaySecs * 1000);
        delaySecs = Math.min(delaySecs * 2, MAX_DELAY_SECS);
        continue;
      }
      break;
    }
    clearTimeout(timer);

    if (response.status < 400) {
      logger?.debug(
        `checkrd: public key registration ok (HTTP ${response.status.toString()})`,
      );
      return;
    }

    if (response.status === 409) {
      // Key mismatch is permanent — retrying won't help.
      logger?.warn(
        `checkrd: public key for agent ${agentId} differs from the key ` +
          "already registered with the control plane. If you rotated keys, " +
          "revoke the old key in the dashboard first.",
      );
      return;
    }

    if (response.status === 401 || response.status === 403) {
      // Auth errors are permanent — retrying won't help.
      logger?.warn(
        `checkrd: public key registration failed ` +
          `(HTTP ${response.status.toString()} — check your API key). ` +
          "Telemetry signature verification may fail on the server side.",
      );
      return;
    }

    // Transient server error — retry if attempts remain.
    if (attempt < maxRetries - 1) {
      logger?.debug(
        `checkrd: public key registration HTTP ${response.status.toString()}, ` +
          `retry ${(attempt + 1).toString()}/${maxRetries.toString()} ` +
          `in ${delaySecs.toFixed(1)}s`,
      );
      await sleep(delaySecs * 1000);
      // Jitter: uniform(delay/2, delay) to prevent thundering herd.
      const jitter = delaySecs * (0.5 + Math.random() * 0.5);
      delaySecs = Math.min(jitter * 2, MAX_DELAY_SECS);
    }
  }

  logger?.warn(
    `checkrd: public key registration failed after ` +
      `${maxRetries.toString()} attempts for agent ${agentId}. ` +
      "The control plane does not have this agent's public key — " +
      "telemetry signature verification will fail server-side. " +
      `Check network connectivity to ${controlPlaneUrl} and verify ` +
      "your API key.",
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Convert a Uint8Array to lowercase hex. */
function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (const byte of bytes) {
    out += byte.toString(16).padStart(2, "0");
  }
  return out;
}
