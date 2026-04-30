/**
 * Property-based tests for the TS <-> WASM FFI boundary.
 *
 * Direct port of `wrappers/python/tests/test_ffi_properties.py` — same
 * invariants, same coverage scope, different runner (fast-check in place
 * of hypothesis). Shared bar keeps both wrappers calibrated against
 * identical edge cases.
 */
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import { WasmEngine } from "../src/engine.js";
import { CheckrdInitError } from "../src/exceptions.js";

const TS = "2026-03-28T14:30:00Z";
const TS_MS = 1_774_708_200_000;

const ALLOW_ALL = JSON.stringify({ agent: "test", default: "allow", rules: [] });
const DENY_ALL = JSON.stringify({ agent: "test", default: "deny", rules: [] });

const methodArb = fc.constantFrom("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS");

const hostArb = fc.constantFrom(
  "api.stripe.com",
  "api.openai.com",
  "api.anthropic.com",
  "example.com",
);

const urlArb = fc
  .tuple(
    hostArb,
    fc.string({
      minLength: 0,
      maxLength: 50,
      unit: fc.constantFrom(..."abcdefghijklmnopqrstuvwxyz0123456789/-_"),
    }),
  )
  .map(([host, path]) => `https://${host}/${path}`);

const headerNameArb = fc.constantFrom(
  "Authorization",
  "Content-Type",
  "X-Request-Id",
  "User-Agent",
  "Accept",
);

const headerValueArb = fc.string({
  minLength: 0,
  maxLength: 200,
  unit: fc.constantFrom(
    ..."abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-/:;=,",
  ),
});

const headersArb = fc.array(fc.tuple(headerNameArb, headerValueArb) as fc.Arbitrary<[string, string]>, {
  minLength: 0,
  maxLength: 8,
});

const requestIdArb = fc.string({
  minLength: 1,
  maxLength: 50,
  unit: fc.constantFrom(..."abcdefghijklmnopqrstuvwxyz0123456789-_"),
});

const anyUtf8Arb = fc.string({ minLength: 0, maxLength: 500 });

// -----------------------------------------------------------------
// FFI string round-trip through evaluate()
// -----------------------------------------------------------------

describe("UTF-8 round-trip via request_id", () => {
  const engine = new WasmEngine(ALLOW_ALL, "test-agent");
  it("every UTF-8 string survives the Python→WASM→Python round trip (100 examples)", () => {
    fc.assert(
      fc.property(anyUtf8Arb, (payload) => {
        const res = engine.evaluate({
          request_id: payload,
          method: "GET",
          url: "https://example.com/",
          headers: [],
          body: null,
          timestamp: TS,
          timestamp_ms: TS_MS,
        });
        expect(res.request_id).toBe(payload);
      }),
      { numRuns: 100 },
    );
  });
});

// -----------------------------------------------------------------
// evaluate() robustness
// -----------------------------------------------------------------

describe("evaluate() robustness", () => {
  const engine = new WasmEngine(ALLOW_ALL, "test-agent");
  it("every well-formed request yields a valid EvalResult", () => {
    fc.assert(
      fc.property(methodArb, urlArb, headersArb, requestIdArb, (method, url, headers, requestId) => {
        const res = engine.evaluate({
          request_id: requestId,
          method,
          url,
          headers,
          body: null,
          timestamp: TS,
          timestamp_ms: TS_MS,
        });
        expect(res.allowed).toBe(true);
        expect(res.request_id).toBe(requestId);
        expect(typeof res.telemetry_json).toBe("string");
        // telemetry_json must be parseable
        JSON.parse(res.telemetry_json);
      }),
      { numRuns: 100 },
    );
  });
});

// -----------------------------------------------------------------
// Policy invariants
// -----------------------------------------------------------------

describe("Policy semantic invariants", () => {
  it("allow-all allows every request", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test-agent");
    fc.assert(
      fc.property(methodArb, urlArb, (method, url) => {
        const res = engine.evaluate({
          request_id: "req",
          method,
          url,
          headers: [],
          body: null,
          timestamp: TS,
          timestamp_ms: TS_MS,
        });
        expect(res.allowed).toBe(true);
      }),
      { numRuns: 100 },
    );
  });

  it("deny-all denies every request", () => {
    const engine = new WasmEngine(DENY_ALL, "test-agent");
    fc.assert(
      fc.property(methodArb, urlArb, (method, url) => {
        const res = engine.evaluate({
          request_id: "req",
          method,
          url,
          headers: [],
          body: null,
          timestamp: TS,
          timestamp_ms: TS_MS,
        });
        expect(res.allowed).toBe(false);
        expect(res.deny_reason).toBeTruthy();
      }),
      { numRuns: 100 },
    );
  });
});

// -----------------------------------------------------------------
// Keypair derivation round-trip
// -----------------------------------------------------------------

describe("Keypair derive_public round-trip", () => {
  it("derive_public_key matches the public half across repeated generations", () => {
    for (let i = 0; i < 10; i++) {
      const { privateKey, publicKey } = WasmEngine.generateKeypair();
      expect(privateKey.length).toBe(32);
      expect(publicKey.length).toBe(32);
      const derived = WasmEngine.derivePublicKey(privateKey);
      expect(derived).toEqual(publicKey);
    }
  });
});

// -----------------------------------------------------------------
// Malformed input path
// -----------------------------------------------------------------

describe("Malformed policy input", () => {
  const malformedPolicies = [
    "",
    "null",
    "[]",
    "{",
    '{"default": "not_a_mode"}',
    '{"default": "allow", "rules": [null]}',
    '{"default": "allow", "rules": [{"name": null}]}',
    "not-json-at-all",
  ];

  it.each(malformedPolicies)("rejects %p with CheckrdInitError", (malformed) => {
    expect(() => new WasmEngine(malformed, "test-agent")).toThrow(CheckrdInitError);
  });
});
