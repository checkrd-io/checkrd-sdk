/**
 * Webhook signature verification helpers. Mirrors the Stripe / OpenAI /
 * Anthropic pattern: an HMAC over `{timestamp}.{raw_body}` with a shared
 * secret, delivered as a header the server computes and the client
 * verifies before trusting the payload.
 *
 * The helpers here are transport-agnostic. Feed them the raw body as
 * received by your HTTP framework (Express `req.body` with
 * `express.raw()`, Next.js `req.text()`, Hono `c.req.text()`, Cloudflare
 * `await request.text()`, etc.) — **never** a re-serialized JSON object,
 * because any whitespace change invalidates the HMAC.
 */

import { CheckrdError } from "./exceptions.js";

/** Default clock-skew tolerance, in seconds. Stripe uses 300s. */
const DEFAULT_TOLERANCE_SECS = 300;

/** Internal envelope parsed out of the signature header. */
interface SignatureEnvelope {
  timestamp: number;
  signatures: string[];
}

/**
 * Thrown when a webhook signature fails to verify. `.code` distinguishes
 * failure kinds so callers can alert-vs-reject programmatically.
 */
export class WebhookVerificationError extends CheckrdError {
  constructor(
    message: string,
    code:
      | "missing_header"
      | "malformed_header"
      | "timestamp_out_of_range"
      | "signature_mismatch"
      | "empty_secret",
  ) {
    super(message, code);
    this.name = "WebhookVerificationError";
    Object.setPrototypeOf(this, WebhookVerificationError.prototype);
  }
}

/** Options for {@link verifyWebhook}. */
export interface VerifyWebhookOptions {
  /** Raw request body bytes (or the UTF-8 string form). */
  rawBody: string | Uint8Array;
  /** The `Checkrd-Signature` header (or compatible envelope). */
  signatureHeader: string | null | undefined;
  /** Shared HMAC-SHA256 secret. Pass an array during rotation. */
  secret: string | string[];
  /** Clock-skew tolerance in seconds. Default: 300. */
  toleranceSecs?: number;
  /** Override for time source — test-only. */
  nowUnixSecs?: () => number;
}

/**
 * Verify a webhook signature. Returns silently on success; throws
 * {@link WebhookVerificationError} on any failure reason (missing
 * header, malformed envelope, stale timestamp, signature mismatch).
 *
 *     import { verifyWebhook } from "checkrd/webhooks";
 *
 *     app.post("/checkrd-webhook", express.raw({ type: "*\/*" }), (req, res) => {
 *       try {
 *         verifyWebhook({
 *           rawBody: req.body,
 *           signatureHeader: req.header("checkrd-signature"),
 *           secret: process.env.CHECKRD_WEBHOOK_SECRET!,
 *         });
 *       } catch (err) {
 *         return res.status(400).send("invalid signature");
 *       }
 *       // body is authenticated — parse and handle
 *     });
 */
export function verifyWebhook(opts: VerifyWebhookOptions): void {
  const secrets = Array.isArray(opts.secret) ? opts.secret : [opts.secret];
  if (secrets.length === 0 || secrets.some((s) => s.length === 0)) {
    throw new WebhookVerificationError(
      "webhook secret is empty",
      "empty_secret",
    );
  }
  if (!opts.signatureHeader || opts.signatureHeader.length === 0) {
    throw new WebhookVerificationError(
      "signature header missing",
      "missing_header",
    );
  }

  const envelope = parseSignatureHeader(opts.signatureHeader);

  const now = (opts.nowUnixSecs ?? (() => Math.floor(Date.now() / 1000)))();
  const tolerance = opts.toleranceSecs ?? DEFAULT_TOLERANCE_SECS;
  if (Math.abs(now - envelope.timestamp) > tolerance) {
    throw new WebhookVerificationError(
      "timestamp outside tolerance window",
      "timestamp_out_of_range",
    );
  }

  const bodyBytes =
    typeof opts.rawBody === "string"
      ? new TextEncoder().encode(opts.rawBody)
      : opts.rawBody;
  const signedPayload = buildSignedPayload(envelope.timestamp, bodyBytes);

  // Delegate the comparison to the platform primitive that's
  // guaranteed constant-time on equal-length 32-byte buffers
  // (Node's ``crypto.timingSafeEqual``). No hand-rolled equality
  // on secret-bearing values.
  for (const provided of envelope.signatures) {
    for (const secret of secrets) {
      if (verifyHmacSha256Sync(secret, signedPayload, provided)) return;
    }
  }
  throw new WebhookVerificationError(
    "no candidate signature matched",
    "signature_mismatch",
  );
}

/**
 * Parse a `t=<unix_seconds>,v1=<hex_sig>[,v1=<hex_sig>...]` envelope.
 * The `v1` scheme tag lets us add forward-compatible algorithms (v2 =
 * Ed25519) without breaking existing verifiers.
 */
function parseSignatureHeader(header: string): SignatureEnvelope {
  const parts = header.split(",").map((s) => s.trim()).filter(Boolean);
  let timestamp: number | null = null;
  const signatures: string[] = [];
  for (const part of parts) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    const key = part.slice(0, eq);
    const value = part.slice(eq + 1);
    if (key === "t") {
      const parsed = Number.parseInt(value, 10);
      if (Number.isFinite(parsed)) timestamp = parsed;
    } else if (key === "v1" && /^[0-9a-fA-F]{64}$/.test(value)) {
      signatures.push(value.toLowerCase());
    }
  }
  if (timestamp === null || signatures.length === 0) {
    throw new WebhookVerificationError(
      "signature header is malformed",
      "malformed_header",
    );
  }
  return { timestamp, signatures };
}

function buildSignedPayload(timestamp: number, body: Uint8Array): Uint8Array {
  const prefix = new TextEncoder().encode(`${timestamp.toString()}.`);
  const out = new Uint8Array(prefix.byteLength + body.byteLength);
  out.set(prefix, 0);
  out.set(body, prefix.byteLength);
  return out;
}

/**
 * HMAC-SHA256 always produces 32 bytes (64 hex chars). Decode a hex
 * string into a 32-byte ``Uint8Array`` for constant-time comparison;
 * return ``null`` for any malformed or wrong-length input. The wrong-
 * length rejection is independent of the secret, so it leaks no
 * timing information about a real signature: an attacker who sends a
 * 30-byte signature only learns "30 bytes is wrong" — they already
 * know that.
 */
function decodeHmacSha256SignatureOrNull(hex: string): Uint8Array | null {
  if (hex.length !== 64) return null;
  const out = new Uint8Array(32);
  for (let i = 0; i < 32; i++) {
    const byte = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    if (!Number.isFinite(byte)) return null;
    out[i] = byte;
  }
  return out;
}

/**
 * Minimal structural type for the subset of `node:crypto` we need.
 * Declared explicitly (vs. `import type { Hmac } from 'node:crypto'`)
 * so this module stays parseable on runtimes that do not expose
 * `node:crypto` at all (Cloudflare Workers, Vercel Edge).
 */
interface NodeCryptoShim {
  createHmac(algo: string, secret: string): {
    update(data: Uint8Array): { digest(): Uint8Array };
  };
  timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean;
}

/**
 * Synchronous HMAC verification on Node / Bun.
 *
 * Computes the expected HMAC-SHA256 of ``payload`` with ``secret``,
 * then compares against the provided signature using
 * ``crypto.timingSafeEqual`` on raw 32-byte buffers. Both inputs are
 * always 32 bytes by construction (HMAC-SHA256 output size +
 * {@link decodeHmacSha256SignatureOrNull} pre-validates), so the
 * comparison runs in constant time per Node's
 * {@link https://nodejs.org/api/crypto.html#cryptotimingsafeequala-b spec}.
 *
 * No hand-rolled equality on secret-bearing strings — that's the path
 * that historically leaks length and short-circuits on the first byte
 * mismatch. We delegate to the platform primitive guaranteed to be
 * constant-time.
 */
function verifyHmacSha256Sync(
  secret: string,
  payload: Uint8Array,
  providedHex: string,
): boolean {
  const provided = decodeHmacSha256SignatureOrNull(providedHex);
  if (provided === null) return false;
  const crypto = loadNodeCryptoOrThrow();
  const expected = crypto
    .createHmac("sha256", secret)
    .update(payload)
    .digest();
  // Both buffers are exactly 32 bytes. `timingSafeEqual` on equal-
  // length buffers is constant-time and never throws.
  return crypto.timingSafeEqual(provided, expected);
}

/**
 * Asynchronous HMAC verification on Web-Crypto runtimes (Cloudflare
 * Workers, Vercel Edge, Deno, browsers).
 *
 * Uses ``crypto.subtle.verify("HMAC", ...)`` directly. Per the
 * {@link https://www.w3.org/TR/WebCryptoAPI/#hmac-operations W3C
 * Web Crypto spec, §29.2}, ``verify`` returns ``true`` iff the
 * provided signature matches the HMAC of ``payload`` and the
 * implementation MUST NOT leak timing on the comparison itself.
 * Replaces the previous hand-rolled hex compare which had a small
 * length-leak on malformed input — there is no longer any
 * application-side comparison.
 */
async function verifyHmacSha256Async(
  secret: string,
  payload: Uint8Array,
  providedHex: string,
): Promise<boolean> {
  const provided = decodeHmacSha256SignatureOrNull(providedHex);
  if (provided === null) return false;
  const secretBytes = new TextEncoder().encode(secret);
  const key = await globalThis.crypto.subtle.importKey(
    "raw",
    secretBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  // Copy into fresh ArrayBuffer-backed Uint8Arrays — strict
  // ``BufferSource`` narrowing rejects SharedArrayBuffer-backed views
  // on some runtimes.
  const sigBuf = new Uint8Array(provided.byteLength);
  sigBuf.set(provided);
  const payloadBuf = new Uint8Array(payload.byteLength);
  payloadBuf.set(payload);
  return globalThis.crypto.subtle.verify("HMAC", key, sigBuf, payloadBuf);
}

let _nodeCrypto: NodeCryptoShim | null = null;
/**
 * Lazy-load `node:crypto` for the sync HMAC path. The synchronous
 * webhook API is the intended surface on Node; callers running on
 * Cloudflare Workers / Vercel Edge should instead call
 * {@link verifyWebhookAsync}.
 */
function loadNodeCryptoOrThrow(): NodeCryptoShim {
  if (_nodeCrypto) return _nodeCrypto;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- sync load on Node
    _nodeCrypto = require("node:crypto") as NodeCryptoShim;
    return _nodeCrypto;
  } catch {
    throw new Error(
      "verifyWebhook requires node:crypto. On Cloudflare Workers and " +
        "Vercel Edge, use verifyWebhookAsync which uses Web Crypto.",
    );
  }
}

/**
 * Async webhook verifier backed by Web Crypto. Use on Cloudflare
 * Workers, Vercel Edge, Deno, and browsers where `node:crypto` is
 * unavailable. Semantics are identical to {@link verifyWebhook}.
 */
export async function verifyWebhookAsync(opts: VerifyWebhookOptions): Promise<void> {
  const secrets = Array.isArray(opts.secret) ? opts.secret : [opts.secret];
  if (secrets.length === 0 || secrets.some((s) => s.length === 0)) {
    throw new WebhookVerificationError(
      "webhook secret is empty",
      "empty_secret",
    );
  }
  if (!opts.signatureHeader || opts.signatureHeader.length === 0) {
    throw new WebhookVerificationError(
      "signature header missing",
      "missing_header",
    );
  }

  const envelope = parseSignatureHeader(opts.signatureHeader);
  const now = (opts.nowUnixSecs ?? (() => Math.floor(Date.now() / 1000)))();
  const tolerance = opts.toleranceSecs ?? DEFAULT_TOLERANCE_SECS;
  if (Math.abs(now - envelope.timestamp) > tolerance) {
    throw new WebhookVerificationError(
      "timestamp outside tolerance window",
      "timestamp_out_of_range",
    );
  }

  const bodyBytes =
    typeof opts.rawBody === "string"
      ? new TextEncoder().encode(opts.rawBody)
      : opts.rawBody;
  const signedPayload = buildSignedPayload(envelope.timestamp, bodyBytes);

  // Delegate to ``crypto.subtle.verify`` which is constant-time per
  // the W3C Web Crypto spec — no hand-rolled hex comparison.
  for (const provided of envelope.signatures) {
    for (const secret of secrets) {
      if (await verifyHmacSha256Async(secret, signedPayload, provided)) return;
    }
  }
  throw new WebhookVerificationError(
    "no candidate signature matched",
    "signature_mismatch",
  );
}
