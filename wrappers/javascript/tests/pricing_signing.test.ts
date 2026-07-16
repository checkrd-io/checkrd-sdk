/**
 * End-to-end tests for signed PRICING bundle distribution + cost metering
 * settle (M-12), and the SECURITY-CRITICAL cross-purpose trust separation.
 *
 * The signer side here is the runtime's own Ed25519 via `crypto.subtle` —
 * a completely independent implementation from the WASM core's
 * `ed25519-dalek` verifier. Passing this proves the pricing wire format
 * (DSSE PAE over `application/vnd.checkrd.pricing-bundle+json`) is
 * interoperable with any RFC 8032 / DSSE-conformant library, exactly like
 * the policy interop test (`wrappers/python/tests/test_policy_signing.py`).
 *
 * # Standards anchored
 *
 *   - RFC 8032 (Ed25519) — `crypto.subtle` and `ed25519-dalek`
 *   - DSSE protocol.md — PAE construction
 *   - `crates/shared/src/dsse.rs::PRICING_BUNDLE_PAYLOAD_TYPE` — domain sep
 *   - `crates/shared/src/pricing_bundle.rs::PricingBundle` — wire shape
 *   - `crates/shared/src/cost.rs` — integer micro-USD arithmetic
 */
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import { WasmEngine } from "../src/engine.js";
import type { SettleResult, UsageInput } from "../src/engine.js";
import { PricingSignatureError } from "../src/exceptions.js";

// ---------------------------------------------------------------------------
// DSSE signing helpers — pure, no dependence on any of our own DSSE code on
// the SIGNER side (the WASM core is the verifier under test).
// ---------------------------------------------------------------------------

const PRICING_PAYLOAD_TYPE = "application/vnd.checkrd.pricing-bundle+json";
const POLICY_PAYLOAD_TYPE = "application/vnd.checkrd.policy-bundle+json";
const TEST_MAX_AGE_SECS = 86_400; // 24h, matches the policy interop test

/** Generate an extractable Ed25519 keypair via the runtime WebCrypto. */
async function generateEd25519(): Promise<{
  privateKey: CryptoKey;
  publicKeyHex: string;
}> {
  const pair = (await globalThis.crypto.subtle.generateKey(
    { name: "Ed25519" },
    true,
    ["sign", "verify"],
  )) as CryptoKeyPair;
  const raw = await globalThis.crypto.subtle.exportKey("raw", pair.publicKey);
  return {
    privateKey: pair.privateKey,
    publicKeyHex: bytesToHex(new Uint8Array(raw)),
  };
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function bytesToB64(bytes: Uint8Array): string {
  // Node + edge both expose btoa over a binary string.
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

/** Reconstruct the DSSE PAE byte string per the spec text. */
function pae(payloadType: string, payload: Uint8Array): Uint8Array {
  const enc = new TextEncoder();
  const prefix = enc.encode(
    `DSSEv1 ${payloadType.length.toString()} ${payloadType} ${payload.length.toString()} `,
  );
  const out = new Uint8Array(prefix.length + payload.length);
  out.set(prefix, 0);
  out.set(payload, prefix.length);
  return out;
}

interface PriceSku {
  sku_id: string;
  provider: string;
  model_match: string;
  unit: "per_1m_tokens";
  input_usd_micros_per_unit: number;
  output_usd_micros_per_unit: number;
  cache_read_usd_micros_per_unit?: number;
  cache_write_usd_micros_per_unit?: number;
  default_max_output_tokens: number;
  effective_from: number;
  source: "list" | "org";
}

/** Build a versioned PricingBundle and serialize to JSON bytes. */
function buildPricingBundle(opts: {
  version?: number;
  signedAt?: number;
  skus?: PriceSku[];
}): Uint8Array {
  const bundle = {
    schema_version: 1,
    version: opts.version ?? 1,
    signed_at: opts.signedAt ?? Math.floor(Date.now() / 1000),
    rounding: "half_up",
    skus: opts.skus ?? [defaultSku()],
  };
  return new TextEncoder().encode(JSON.stringify(bundle));
}

function defaultSku(): PriceSku {
  // $3.00 / 1M input, $15.00 / 1M output — Claude-Sonnet-shaped list price.
  return {
    sku_id: "anthropic-claude-sonnet",
    provider: "anthropic",
    model_match: "claude-sonnet-*",
    unit: "per_1m_tokens",
    input_usd_micros_per_unit: 3_000_000,
    output_usd_micros_per_unit: 15_000_000,
    cache_read_usd_micros_per_unit: 300_000,
    cache_write_usd_micros_per_unit: 3_750_000,
    default_max_output_tokens: 8192,
    effective_from: 1_700_000_000,
    source: "list",
  };
}

/** Sign a payload under `payloadType`, returning a DSSE envelope JSON string. */
async function buildEnvelope(
  privateKey: CryptoKey,
  keyid: string,
  payload: Uint8Array,
  payloadType: string,
): Promise<string> {
  const sig = new Uint8Array(
    await globalThis.crypto.subtle.sign(
      { name: "Ed25519" },
      privateKey,
      pae(payloadType, payload),
    ),
  );
  return JSON.stringify({
    payloadType,
    payload: bytesToB64(payload),
    signatures: [{ keyid, sig: bytesToB64(sig) }],
  });
}

function trustListFor(publicKeyHex: string, keyid: string): string {
  return JSON.stringify([
    {
      keyid,
      public_key_hex: publicKeyHex,
      valid_from: 0,
      // MAX_SAFE_INTEGER seconds — ~285M years out, always in-window, and
      // representable exactly in JS (a larger literal would lose precision).
      valid_until: Number.MAX_SAFE_INTEGER,
    },
  ]);
}

function makeEngine(): WasmEngine {
  return new WasmEngine(
    JSON.stringify({
      agent: "test-agent",
      mode: "enforce",
      default: "deny",
      rules: [],
    }),
    "test-agent",
  );
}

// ===========================================================================
// FFI round-trip: sign → reload_pricing_signed → settle_usage
// ===========================================================================

describe("pricing FFI round-trip (crypto.subtle signer ↔ WASM verifier)", () => {
  it("installs a signed pricing bundle and settles a call to the expected cost", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    const payload = buildPricingBundle({ version: 1 });
    const envelope = await buildEnvelope(
      privateKey,
      "test-pricing-cp",
      payload,
      PRICING_PAYLOAD_TYPE,
    );

    const engine = makeEngine();
    expect(engine.getActivePricingVersion()).toBe(0);

    engine.reloadPricingSigned({
      envelopeJson: envelope,
      trustedKeysJson: trustListFor(publicKeyHex, "test-pricing-cp"),
      nowUnixSecs: Math.floor(Date.now() / 1000),
      maxAgeSecs: TEST_MAX_AGE_SECS,
    });
    // Assert 0 (no throw) → install succeeded; version is now the bundle's.
    expect(engine.getActivePricingVersion()).toBe(1);

    // 1000 input @ $3/1M = 3000 micros; 500 output @ $15/1M = 7500 micros.
    // Total = 10_500 micros ($0.0105), exact (no rounding).
    const result: SettleResult = engine.settleUsage("req-1", {
      provider: "anthropic",
      model: "claude-sonnet-4",
      input_tokens: 1000,
      output_tokens: 500,
    });
    expect(result.pricing_status).toBe("priced");
    expect(result.cost_usd_micros).toBe(10_500);
    expect(result.currency).toBe("USD");
    expect(result.pricing_bundle_version).toBe(1);
    expect(result.overflow).toBe(false);
    expect(result.sku_id).toBe("anthropic-claude-sonnet");
  });

  it("returns pricing_status=disabled when no bundle is installed", () => {
    const engine = makeEngine();
    const result = engine.settleUsage("req-x", {
      provider: "anthropic",
      model: "claude-sonnet-4",
      input_tokens: 1000,
      output_tokens: 500,
    });
    expect(result.pricing_status).toBe("disabled");
    expect(result.cost_usd_micros).toBe(0);
    expect(result.pricing_bundle_version).toBe(0);
    expect(result.sku_id).toBeNull();
  });

  it("returns pricing_status=unpriced_model when no SKU matches", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    // A bundle whose only SKU matches a different provider's model glob.
    const sku = defaultSku();
    sku.provider = "openai";
    sku.model_match = "gpt-4o";
    const payload = buildPricingBundle({ version: 1, skus: [sku] });
    const envelope = await buildEnvelope(
      privateKey,
      "cp",
      payload,
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();
    engine.reloadPricingSigned({
      envelopeJson: envelope,
      trustedKeysJson: trustListFor(publicKeyHex, "cp"),
      nowUnixSecs: Math.floor(Date.now() / 1000),
      maxAgeSecs: TEST_MAX_AGE_SECS,
    });
    const result = engine.settleUsage("r", {
      provider: "anthropic",
      model: "claude-sonnet-4",
      input_tokens: 10,
      output_tokens: 10,
    });
    expect(result.pricing_status).toBe("unpriced_model");
    expect(result.pricing_bundle_version).toBe(1);
  });

  it("rejects a rolled-back bundle version with -21 (not monotonic)", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    const trusted = trustListFor(publicKeyHex, "cp");
    const engine = makeEngine();

    // Install v5.
    const v5 = await buildEnvelope(
      privateKey,
      "cp",
      buildPricingBundle({ version: 5 }),
      PRICING_PAYLOAD_TYPE,
    );
    engine.reloadPricingSigned({
      envelopeJson: v5,
      trustedKeysJson: trusted,
      nowUnixSecs: Math.floor(Date.now() / 1000),
      maxAgeSecs: TEST_MAX_AGE_SECS,
    });
    expect(engine.getActivePricingVersion()).toBe(5);

    // v3 rollback → rejected, version unchanged.
    const v3 = await buildEnvelope(
      privateKey,
      "cp",
      buildPricingBundle({ version: 3 }),
      PRICING_PAYLOAD_TYPE,
    );
    try {
      engine.reloadPricingSigned({
        envelopeJson: v3,
        trustedKeysJson: trusted,
        nowUnixSecs: Math.floor(Date.now() / 1000),
        maxAgeSecs: TEST_MAX_AGE_SECS,
      });
      throw new Error("expected PricingSignatureError for rollback");
    } catch (err) {
      expect(err).toBeInstanceOf(PricingSignatureError);
      expect((err as PricingSignatureError).ffiCode).toBe(-21);
      expect((err as PricingSignatureError).reason).toBe(
        "bundle_version_not_monotonic",
      );
    }
    expect(engine.getActivePricingVersion()).toBe(5);
  });

  it("rejects a tampered envelope with -16 (signature invalid)", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    const payload = buildPricingBundle({ version: 1 });
    const envelopeJson = await buildEnvelope(
      privateKey,
      "cp",
      payload,
      PRICING_PAYLOAD_TYPE,
    );
    // Tamper: swap the payload for a DIFFERENT bundle after signing.
    const tampered = JSON.parse(envelopeJson) as {
      payload: string;
      signatures: unknown;
      payloadType: string;
    };
    const evil = buildPricingBundle({
      version: 1,
      skus: [{ ...defaultSku(), input_usd_micros_per_unit: 1 }],
    });
    tampered.payload = bytesToB64(evil);

    const engine = makeEngine();
    try {
      engine.reloadPricingSigned({
        envelopeJson: JSON.stringify(tampered),
        trustedKeysJson: trustListFor(publicKeyHex, "cp"),
        nowUnixSecs: Math.floor(Date.now() / 1000),
        maxAgeSecs: TEST_MAX_AGE_SECS,
      });
      throw new Error("expected PricingSignatureError for tampered envelope");
    } catch (err) {
      expect(err).toBeInstanceOf(PricingSignatureError);
      expect((err as PricingSignatureError).ffiCode).toBe(-16);
      expect((err as PricingSignatureError).reason).toBe("signature_invalid");
    }
  });
});

// ===========================================================================
// SECURITY HEADLINE: cross-purpose trust rejection (TUF per-role keys)
// ===========================================================================
//
// A pricing-key-signed envelope must NOT verify as a policy bundle, and a
// policy-key-signed envelope must NOT verify as a price table. This is the
// defense the structurally-separate trust lists + DSSE payload-type binding
// jointly provide.

describe("SECURITY: cross-purpose signature rejection", () => {
  it("a PRICING-typed envelope is rejected by reload_policy_signed", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    // Sign a (policy) bundle payload but under the PRICING payload type, and
    // feed it to the POLICY verifier. The policy verifier requires the
    // policy payload type, so the PAE prefix bytes differ and verification
    // fails with the policy-side payload-type-mismatch code (-4).
    const policyBundle = new TextEncoder().encode(
      JSON.stringify({
        schema_version: 1,
        version: 1,
        signed_at: Math.floor(Date.now() / 1000),
        policy: { agent: "x", mode: "enforce", default: "allow", rules: [] },
      }),
    );
    const envelope = await buildEnvelope(
      privateKey,
      "cp",
      policyBundle,
      PRICING_PAYLOAD_TYPE, // WRONG type for the policy verifier
    );
    const engine = makeEngine();
    expect(() => {
      engine.reloadPolicySigned({
        envelopeJson: envelope,
        trustedKeysJson: trustListFor(publicKeyHex, "cp"),
        nowUnixSecs: Math.floor(Date.now() / 1000),
        maxAgeSecs: TEST_MAX_AGE_SECS,
      });
    }).toThrow(/payload_type_mismatch/);
    // Policy never installed.
    expect(engine.getActivePolicyVersion()).toBe(0);
  });

  it("a POLICY-typed envelope is rejected by reload_pricing_signed", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    // Sign a pricing bundle payload but under the POLICY payload type, and
    // feed it to the PRICING verifier → pricing-side payload-type-mismatch (-15).
    const payload = buildPricingBundle({ version: 1 });
    const envelope = await buildEnvelope(
      privateKey,
      "cp",
      payload,
      POLICY_PAYLOAD_TYPE, // WRONG type for the pricing verifier
    );
    const engine = makeEngine();
    try {
      engine.reloadPricingSigned({
        envelopeJson: envelope,
        trustedKeysJson: trustListFor(publicKeyHex, "cp"),
        nowUnixSecs: Math.floor(Date.now() / 1000),
        maxAgeSecs: TEST_MAX_AGE_SECS,
      });
      throw new Error("expected rejection of policy-typed envelope by pricing verifier");
    } catch (err) {
      expect(err).toBeInstanceOf(PricingSignatureError);
      expect((err as PricingSignatureError).ffiCode).toBe(-15);
      expect((err as PricingSignatureError).reason).toBe("payload_type_mismatch");
    }
    expect(engine.getActivePricingVersion()).toBe(0);
  });

  it("a validly PRICING-signed bundle does NOT install as a policy even with the right trust key", async () => {
    // The strongest statement: same key, valid pricing signature, correct
    // pricing trust list. Hand the pricing envelope to the policy verifier —
    // it must still refuse (domain separation), proving a captured pricing
    // signature can never be replayed as a policy.
    const { privateKey, publicKeyHex } = await generateEd25519();
    const payload = buildPricingBundle({ version: 9 });
    const pricingEnvelope = await buildEnvelope(
      privateKey,
      "shared-key",
      payload,
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();
    // First prove it DOES install as a price table (sanity).
    engine.reloadPricingSigned({
      envelopeJson: pricingEnvelope,
      trustedKeysJson: trustListFor(publicKeyHex, "shared-key"),
      nowUnixSecs: Math.floor(Date.now() / 1000),
      maxAgeSecs: TEST_MAX_AGE_SECS,
    });
    expect(engine.getActivePricingVersion()).toBe(9);
    // Now the same envelope must be refused by the policy verifier.
    expect(() => {
      engine.reloadPolicySigned({
        envelopeJson: pricingEnvelope,
        trustedKeysJson: trustListFor(publicKeyHex, "shared-key"),
        nowUnixSecs: Math.floor(Date.now() / 1000),
        maxAgeSecs: TEST_MAX_AGE_SECS,
      });
    }).toThrow(/payload_type_mismatch/);
    expect(engine.getActivePolicyVersion()).toBe(0);
  });
});

// ===========================================================================
// Property: settle_usage is total — never throws on arbitrary usage
// ===========================================================================

describe("settle_usage totality (fast-check)", () => {
  const usageArb: fc.Arbitrary<UsageInput> = fc.record(
    {
      provider: fc.string({ maxLength: 40 }),
      model: fc.string({ maxLength: 60 }),
      input_tokens: fc.integer({ min: -1_000, max: 100_000_000 }),
      output_tokens: fc.integer({ min: -1_000, max: 100_000_000 }),
      cache_read_tokens: fc.integer({ min: -1_000, max: 100_000_000 }),
      cache_creation_tokens: fc.integer({ min: -1_000, max: 100_000_000 }),
      reasoning_tokens: fc.integer({ min: -1_000, max: 100_000_000 }),
    },
    { requiredKeys: [] },
  );

  it("never throws on arbitrary usage objects, bundle present or absent", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    const engine = makeEngine();
    engine.reloadPricingSigned({
      envelopeJson: await buildEnvelope(
        privateKey,
        "cp",
        buildPricingBundle({ version: 1 }),
        PRICING_PAYLOAD_TYPE,
      ),
      trustedKeysJson: trustListFor(publicKeyHex, "cp"),
      nowUnixSecs: Math.floor(Date.now() / 1000),
      maxAgeSecs: TEST_MAX_AGE_SECS,
    });

    fc.assert(
      fc.property(usageArb, fc.string({ maxLength: 50 }), (usage, requestId) => {
        const result = engine.settleUsage(requestId, usage);
        // Always a well-formed SettleResult: currency USD, a known status,
        // finite cost, monotone version field.
        expect(result.currency).toBe("USD");
        expect(["priced", "unpriced_model", "disabled"]).toContain(
          result.pricing_status,
        );
        expect(Number.isFinite(result.cost_usd_micros)).toBe(true);
        expect(typeof result.overflow).toBe("boolean");
      }),
      { numRuns: 200 },
    );
  });

  it("never throws with no bundle installed either", () => {
    const engine = makeEngine();
    fc.assert(
      fc.property(usageArb, (usage) => {
        const result = engine.settleUsage("r", usage);
        expect(result.pricing_status).toBe("disabled");
      }),
      { numRuns: 100 },
    );
  });
});
