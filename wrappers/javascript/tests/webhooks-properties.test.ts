/**
 * Property-based tests for ``checkrd/webhooks``.
 *
 * Companion to ``webhooks.test.ts`` (the unit suite). Direct port of
 * ``wrappers/python/tests/test_webhooks_properties.py`` — same
 * invariants, same coverage scope, different runner (fast-check in
 * place of hypothesis). Shared bar keeps both wrappers calibrated
 * against identical edge cases.
 *
 * Three invariants matter for the security-critical surface here:
 *
 * 1. **Parser is total.** No matter how garbled the signature header,
 *    ``verifyWebhook`` throws ``WebhookVerificationError`` (with one
 *    of the five documented codes) — never an uncaught ``TypeError``,
 *    ``RangeError``, or ``SyntaxError``. A parser that crashes is a
 *    denial-of-service vector for any framework that catches only
 *    ``WebhookVerificationError``.
 *
 * 2. **Hex decoder is well-formed-or-rejects.** Anything that isn't
 *    exactly 64 hex chars is dropped at parse time, before HMAC runs.
 *    This is the hand-verifiable property behind the constant-time
 *    claim — ``timingSafeEqual`` only ever sees equal-length 32-byte
 *    buffers.
 *
 * 3. **Round-trip / tamper.** For any (secret, body, timestamp) triple
 *    we generate, the signature we compute verifies; flipping any byte
 *    of body, secret, or timestamp invalidates it.
 */
import { createHmac } from "node:crypto";
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  verifyWebhook,
  WebhookVerificationError,
} from "../src/webhooks.js";

// Documented codes — properties assert the parser only ever throws
// these. A refactor that introduced a new code without updating the
// type union would fail here.
const DOCUMENTED_CODES = new Set([
  "missing_header",
  "malformed_header",
  "timestamp_out_of_range",
  "signature_mismatch",
  "empty_secret",
]);

const NOW = 1_730_000_000;

function sign(secret: string, ts: number, body: Uint8Array): string {
  const prefix = new TextEncoder().encode(`${ts.toString()}.`);
  const buf = new Uint8Array(prefix.byteLength + body.byteLength);
  buf.set(prefix, 0);
  buf.set(body, prefix.byteLength);
  return createHmac("sha256", secret).update(buf).digest("hex");
}

function asBytes(s: string): Uint8Array {
  return new TextEncoder().encode(s);
}

/**
 * Run the parser, swallow only the documented exception type, and
 * assert the code is in the documented set. Any other thrown value is
 * re-thrown — vitest will surface it as a parser bug.
 */
function expectDocumentedOrPass(fn: () => void): void {
  try {
    fn();
  } catch (err) {
    if (err instanceof WebhookVerificationError) {
      expect(DOCUMENTED_CODES.has(err.code)).toBe(true);
      return;
    }
    throw err;
  }
}

// fast-check arbitraries — bounded so each property runs in <100ms.

const tsArb = fc.integer({ min: NOW - 10_000, max: NOW + 10_000 });

const secretArb = fc
  .string({
    minLength: 8,
    maxLength: 64,
    unit: fc.constantFrom(
      ..."abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    ),
  });

const bodyArb = fc.uint8Array({ minLength: 0, maxLength: 512 });

// Any UTF-8 text. fast-check's default ``string()`` excludes lone
// surrogates so we get a valid input every time.
const textArb = fc.string({ minLength: 0, maxLength: 400 });

// 64-char hex string — exercises the "well-formed reaches HMAC"
// property below.
const hexArb = fc.string({
  minLength: 64,
  maxLength: 64,
  unit: fc.constantFrom(..."0123456789abcdefABCDEF"),
});

// ---------------------------------------------------------------------
// 1. Parser is total
// ---------------------------------------------------------------------

describe("verifyWebhook — parser is total", () => {
  it("any non-empty string is either accepted or rejected with a documented code", () => {
    fc.assert(
      fc.property(textArb, (header) => {
        if (header.length === 0) return; // empty handled by missing_header test
        expectDocumentedOrPass(() => {
          verifyWebhook({
            rawBody: "x",
            signatureHeader: header,
            secret: "whsec_test_xxxxxxxxxxxxxxxx",
            nowUnixSecs: () => NOW,
          });
        });
      }),
      { numRuns: 200 },
    );
  });

  it("a valid timestamp paired with garbage v1 always classifies as malformed_header", () => {
    fc.assert(
      fc.property(tsArb, fc.string({ maxLength: 100 }), (ts, garbage) => {
        // Skip the case where garbage is a well-formed 64-hex string —
        // that path is exercised by the next property.
        if (
          garbage.length === 64 &&
          /^[0-9a-fA-F]{64}$/.test(garbage)
        ) {
          return;
        }
        try {
          verifyWebhook({
            rawBody: "x",
            signatureHeader: `t=${ts.toString()},v1=${garbage}`,
            secret: "whsec_test",
            nowUnixSecs: () => ts,
          });
          // If verification succeeds the property doesn't apply
          // (vanishingly unlikely; would imply a 64-hex collision).
        } catch (err) {
          expect(err).toBeInstanceOf(WebhookVerificationError);
          expect((err as WebhookVerificationError).code).toBe(
            "malformed_header",
          );
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------
// 2. Hex decoder is well-formed-or-rejects
// ---------------------------------------------------------------------

describe("verifyWebhook — hex decoder", () => {
  it("any signature length other than 64 is malformed_header", () => {
    fc.assert(
      fc.property(
        tsArb,
        fc.integer({ min: 0, max: 128 }).filter((n) => n !== 64),
        (ts, sigLen) => {
          const sig = "a".repeat(sigLen);
          try {
            verifyWebhook({
              rawBody: "x",
              signatureHeader: `t=${ts.toString()},v1=${sig}`,
              secret: "whsec_test",
              nowUnixSecs: () => ts,
            });
            expect.fail("expected malformed_header");
          } catch (err) {
            expect(err).toBeInstanceOf(WebhookVerificationError);
            expect((err as WebhookVerificationError).code).toBe(
              "malformed_header",
            );
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it("non-hex chars in a 64-char signature are malformed_header", () => {
    fc.assert(
      fc.property(
        tsArb,
        fc.constantFrom("G", "Z", "g", "z", "!", " ", "/", "ñ", "💀"),
        fc.integer({ min: 0, max: 63 }),
        (ts, badChar, position) => {
          const chars = Array.from({ length: 64 }, () => "a");
          chars[position] = badChar;
          const sig = chars.join("");
          try {
            verifyWebhook({
              rawBody: "x",
              signatureHeader: `t=${ts.toString()},v1=${sig}`,
              secret: "whsec_test",
              nowUnixSecs: () => ts,
            });
            expect.fail("expected malformed_header");
          } catch (err) {
            expect(err).toBeInstanceOf(WebhookVerificationError);
            expect((err as WebhookVerificationError).code).toBe(
              "malformed_header",
            );
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it("any well-formed 64-hex signature reaches the HMAC compare path", () => {
    fc.assert(
      fc.property(tsArb, hexArb, (ts, hex) => {
        try {
          verifyWebhook({
            rawBody: "x",
            signatureHeader: `t=${ts.toString()},v1=${hex}`,
            secret: "whsec_test",
            nowUnixSecs: () => ts,
          });
          // Vanishingly unlikely to actually match — would require
          // collision with a real HMAC.
        } catch (err) {
          expect(err).toBeInstanceOf(WebhookVerificationError);
          // The only acceptable failure mode is signature_mismatch —
          // anything else means the regex changed.
          expect((err as WebhookVerificationError).code).toBe(
            "signature_mismatch",
          );
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------
// 3. Round-trip / tamper invariants
// ---------------------------------------------------------------------

describe("verifyWebhook — round-trip and tamper", () => {
  it("any signed (secret, ts, body) triple round-trips through verify", () => {
    fc.assert(
      fc.property(secretArb, tsArb, bodyArb, (secret, ts, body) => {
        const sig = sign(secret, ts, body);
        // No exception → success. ``verifyWebhook`` returns void.
        verifyWebhook({
          rawBody: body,
          signatureHeader: `t=${ts.toString()},v1=${sig}`,
          secret,
          nowUnixSecs: () => ts,
        });
      }),
      { numRuns: 100 },
    );
  });

  it("flipping any body byte invalidates the signature", () => {
    fc.assert(
      fc.property(
        secretArb,
        tsArb,
        bodyArb.filter((b) => b.length > 0),
        fc.integer({ min: 0, max: 1023 }),
        (secret, ts, body, flipPosition) => {
          const sig = sign(secret, ts, body);
          const pos = flipPosition % body.length;
          const tampered = new Uint8Array(body);
          // Bracket assignment is safe — ``pos`` is in range by
          // construction. The TypeScript ``noUncheckedIndexedAccess``
          // narrowing wants an explicit non-null assertion.
          tampered[pos] = (tampered[pos]! ^ 0x01) & 0xff;
          try {
            verifyWebhook({
              rawBody: tampered,
              signatureHeader: `t=${ts.toString()},v1=${sig}`,
              secret,
              nowUnixSecs: () => ts,
            });
            expect.fail("tampered body must not verify");
          } catch (err) {
            expect(err).toBeInstanceOf(WebhookVerificationError);
            expect((err as WebhookVerificationError).code).toBe(
              "signature_mismatch",
            );
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it("rewriting the timestamp invalidates the captured signature", () => {
    fc.assert(
      fc.property(secretArb, tsArb, bodyArb, (secret, ts, body) => {
        const sig = sign(secret, ts, body);
        const replayTs = ts + 1;
        try {
          verifyWebhook({
            rawBody: body,
            signatureHeader: `t=${replayTs.toString()},v1=${sig}`,
            secret,
            nowUnixSecs: () => replayTs,
          });
          expect.fail("timestamp replay must not verify");
        } catch (err) {
          expect(err).toBeInstanceOf(WebhookVerificationError);
          expect((err as WebhookVerificationError).code).toBe(
            "signature_mismatch",
          );
        }
      }),
      { numRuns: 100 },
    );
  });

  it("verifying under the wrong secret invalidates", () => {
    fc.assert(
      fc.property(
        secretArb,
        secretArb,
        tsArb,
        bodyArb,
        (good, wrong, ts, body) => {
          if (good === wrong) return;
          const sig = sign(good, ts, body);
          try {
            verifyWebhook({
              rawBody: body,
              signatureHeader: `t=${ts.toString()},v1=${sig}`,
              secret: wrong,
              nowUnixSecs: () => ts,
            });
            expect.fail("wrong secret must not verify");
          } catch (err) {
            expect(err).toBeInstanceOf(WebhookVerificationError);
            expect((err as WebhookVerificationError).code).toBe(
              "signature_mismatch",
            );
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it("rotation list verifies regardless of which slot matches", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 2, max: 5 }),
        fc.integer({ min: 0, max: 4 }),
        tsArb,
        bodyArb,
        (count, matchIndex, ts, body) => {
          if (matchIndex >= count) return;
          const decoys = Array.from(
            { length: count },
            (_unused, i) => `whsec_decoy_${i.toString().padStart(2, "0")}_xxxxxxxx`,
          );
          const real = "whsec_real_xxxxxxxxxxxxxxxx";
          decoys[matchIndex] = real;
          const sig = sign(real, ts, body);
          verifyWebhook({
            rawBody: body,
            signatureHeader: `t=${ts.toString()},v1=${sig}`,
            secret: decoys,
            nowUnixSecs: () => ts,
          });
        },
      ),
      { numRuns: 50 },
    );
  });

  it("raw bytes decoded as latin-1 never crash the parser", () => {
    fc.assert(
      fc.property(
        fc.uint8Array({ minLength: 1, maxLength: 400 }),
        (bytes) => {
          // Latin-1 decode round-trips every byte; this is the
          // widest plausible header string a buggy framework could
          // hand us. Mirrors the Python latin-1 property.
          const header = Array.from(bytes, (b) => String.fromCharCode(b)).join(
            "",
          );
          expectDocumentedOrPass(() => {
            verifyWebhook({
              rawBody: asBytes("x"),
              signatureHeader: header,
              secret: "whsec_test_xxxxxxxxxxxxxxxx",
              nowUnixSecs: () => NOW,
            });
          });
        },
      ),
      { numRuns: 100 },
    );
  });
});
