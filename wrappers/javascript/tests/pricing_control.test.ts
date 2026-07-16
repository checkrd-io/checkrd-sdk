/**
 * End-to-end cost-metering "go live" tests for the control-receiver
 * pricing-consume path (Stream M / M-14).
 *
 * These prove the wiring the dispatcher unit tests
 * (`control.test.ts::pricing_updated`) cannot: that a signed
 * `PricingBundle` delivered on an `init` / `pricing_updated` SSE event or
 * a poll response actually INSTALLS into a real `WasmEngine`, so
 * `getActivePricingVersion()` becomes > 0 and the settle path can price
 * calls. The signer here is the runtime's own Ed25519 via
 * `crypto.subtle` — an implementation fully independent of the WASM
 * core's `ed25519-dalek` verifier — pinned via the pricing trust
 * override, exactly as `pricing_signing.test.ts` does for the raw FFI.
 *
 * Structurally mirrors the policy-install tests, but asserts the two
 * critical differences of the pricing path:
 *   1. Fail-open — a rejected/stale/rolled-back bundle NEVER throws or
 *      blocks; the previous table stays and the receiver keeps running.
 *   2. Trust isolation — a bundle signed by a key in the POLICY trust
 *      list but NOT the pricing list is rejected, proving the pricing
 *      verifier consults the pricing roots (`trustedPricingKeysJson`).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_PRICING_MAX_AGE_SECS,
  handleControlEvent,
  type ControlLogger,
  type PolicyUpdateOptions,
  type PricingUpdateOptions,
} from "../src/control.js";
import { ControlReceiver } from "../src/receiver.js";
import { WasmEngine } from "../src/engine.js";
import { PricingSignatureError } from "../src/exceptions.js";
import {
  trustedPolicyKeysJson,
  trustedPricingKeysJson,
} from "../src/_trust.js";

// ---------------------------------------------------------------------------
// DSSE signing helpers (signer side = runtime WebCrypto, independent of the
// WASM verifier under test). Copied from `pricing_signing.test.ts`.
// ---------------------------------------------------------------------------

const PRICING_PAYLOAD_TYPE = "application/vnd.checkrd.pricing-bundle+json";

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
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

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

function buildPricingBundle(opts: { version?: number; signedAt?: number }): Uint8Array {
  const bundle = {
    schema_version: 1,
    version: opts.version ?? 1,
    signed_at: opts.signedAt ?? Math.floor(Date.now() / 1000),
    rounding: "half_up",
    skus: [
      {
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
      },
    ],
  };
  return new TextEncoder().encode(JSON.stringify(bundle));
}

async function buildEnvelope(
  privateKey: CryptoKey,
  keyid: string,
  payload: Uint8Array,
  payloadType: string,
): Promise<unknown> {
  const sig = new Uint8Array(
    await globalThis.crypto.subtle.sign(
      { name: "Ed25519" },
      privateKey,
      pae(payloadType, payload),
    ),
  );
  // Return the parsed envelope object (the wire shape the server sends as
  // `pricing_envelope` is a JSON value, not a string).
  return {
    payloadType,
    payload: bytesToB64(payload),
    signatures: [{ keyid, sig: bytesToB64(sig) }],
  };
}

function trustEntry(publicKeyHex: string, keyid: string): unknown {
  return {
    keyid,
    public_key_hex: publicKeyHex,
    valid_from: 0,
    valid_until: Number.MAX_SAFE_INTEGER,
  };
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

type LogMethod = (message: string, ...args: unknown[]) => void;
function makeLogger(): ControlLogger & {
  warn: ReturnType<typeof vi.fn<LogMethod>>;
  error: ReturnType<typeof vi.fn<LogMethod>>;
} {
  return { warn: vi.fn<LogMethod>(), error: vi.fn<LogMethod>() };
}

/** A PricingUpdateOptions that pins the given key directly (no env). */
function pricingUpdateFor(
  publicKeyHex: string,
  keyid: string,
  extra: Partial<PricingUpdateOptions> = {},
): PricingUpdateOptions {
  return {
    loadTrustedKeys: () => JSON.stringify([trustEntry(publicKeyHex, keyid)]),
    maxAgeSecs: DEFAULT_PRICING_MAX_AGE_SECS,
    ...extra,
  };
}

const OVERRIDE_ENVS = [
  "CHECKRD_PRICING_TRUST_OVERRIDE_JSON",
  "CHECKRD_POLICY_TRUST_OVERRIDE_JSON",
  "CHECKRD_ALLOW_TRUST_OVERRIDE",
] as const;

afterEach(() => {
  for (const name of OVERRIDE_ENVS) delete process.env[name];
});

// ===========================================================================
// HEADLINE 1: the path goes live — a received init installs the bundle and
// getActivePricingVersion() becomes > 0.
// ===========================================================================

describe("cost metering goes live: init installs a signed pricing bundle", () => {
  it("getActivePricingVersion() == the bundle version after a received init", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    const envelope = await buildEnvelope(
      privateKey,
      "test-pricing-cp",
      buildPricingBundle({ version: 5 }),
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();
    expect(engine.getActivePricingVersion()).toBe(0); // dark before

    handleControlEvent(
      engine,
      "init",
      JSON.stringify({ kill_switch_active: false, pricing_envelope: envelope }),
      makeLogger(),
      undefined,
      pricingUpdateFor(publicKeyHex, "test-pricing-cp"),
    );
    // installSignedPricing is fire-and-forget (async trust load); poll for it.
    for (let i = 0; i < 50; i++) {
      if (engine.getActivePricingVersion() > 0) break;
      await new Promise((r) => setTimeout(r, 5));
    }
    // THE MONEY ASSERTION: metering is live.
    expect(engine.getActivePricingVersion()).toBe(5);

    // And the settle path now prices calls.
    const settled = engine.settleUsage("req-live", {
      provider: "anthropic",
      model: "claude-sonnet-4",
      input_tokens: 1000,
      output_tokens: 500,
    });
    expect(settled.pricing_status).toBe("priced");
    expect(settled.cost_usd_micros).toBe(10_500);
  });

  it("pins via CHECKRD_PRICING_TRUST_OVERRIDE_JSON + the double-gate (env path)", async () => {
    // Prove the env-backed accessor (`trustedPricingKeysJson`) — the real
    // default the receiver uses — also drives a live install end-to-end.
    const { privateKey, publicKeyHex } = await generateEd25519();
    process.env.CHECKRD_ALLOW_TRUST_OVERRIDE = "1";
    process.env.CHECKRD_PRICING_TRUST_OVERRIDE_JSON = JSON.stringify([
      trustEntry(publicKeyHex, "env-pricing-cp"),
    ]);
    const envelope = await buildEnvelope(
      privateKey,
      "env-pricing-cp",
      buildPricingBundle({ version: 3 }),
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();
    handleControlEvent(
      engine,
      "pricing_updated",
      JSON.stringify({ version: 3, pricing_envelope: envelope }),
      makeLogger(),
      undefined,
      { loadTrustedKeys: () => trustedPricingKeysJson() },
    );
    for (let i = 0; i < 50; i++) {
      if (engine.getActivePricingVersion() > 0) break;
      await new Promise((r) => setTimeout(r, 5));
    }
    expect(engine.getActivePricingVersion()).toBe(3);
  });
});

// ===========================================================================
// pricing_updated: higher version installs; rollback rejected fail-open.
// ===========================================================================

describe("pricing_updated version handling", () => {
  it("installs a higher version, then rejects a rollback while keeping the old table (fail-open, no throw)", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    const pu = pricingUpdateFor(publicKeyHex, "cp");
    const logger = makeLogger();
    const engine = makeEngine();

    // Install v2 via init.
    handleControlEvent(
      engine,
      "init",
      JSON.stringify({
        pricing_envelope: await buildEnvelope(
          privateKey,
          "cp",
          buildPricingBundle({ version: 2 }),
          PRICING_PAYLOAD_TYPE,
        ),
      }),
      logger,
      undefined,
      pu,
    );
    for (let i = 0; i < 50; i++) {
      if (engine.getActivePricingVersion() === 2) break;
      await new Promise((r) => setTimeout(r, 5));
    }
    expect(engine.getActivePricingVersion()).toBe(2);

    // pricing_updated to v9 → installs.
    handleControlEvent(
      engine,
      "pricing_updated",
      JSON.stringify({
        version: 9,
        pricing_envelope: await buildEnvelope(
          privateKey,
          "cp",
          buildPricingBundle({ version: 9 }),
          PRICING_PAYLOAD_TYPE,
        ),
      }),
      logger,
      undefined,
      pu,
    );
    for (let i = 0; i < 50; i++) {
      if (engine.getActivePricingVersion() === 9) break;
      await new Promise((r) => setTimeout(r, 5));
    }
    expect(engine.getActivePricingVersion()).toBe(9);

    // pricing_updated back to v4 (rollback) → rejected fail-open: the
    // dispatcher must not throw and the version must stay at 9. A properly
    // signed, correctly-typed v4 bundle still gets refused by the WASM
    // core's strict-greater monotonic check (FFI -21), which the pricing
    // path swallows to a warning.
    const v4 = await buildEnvelope(
      privateKey,
      "cp",
      buildPricingBundle({ version: 4 }),
      PRICING_PAYLOAD_TYPE,
    );
    expect(() =>
      handleControlEvent(
        engine,
        "pricing_updated",
        JSON.stringify({ version: 4, pricing_envelope: v4 }),
        logger,
        undefined,
        pu,
      ),
    ).not.toThrow();
    await new Promise((r) => setTimeout(r, 20));
    expect(engine.getActivePricingVersion()).toBe(9); // unchanged
    // Rollback rejection is logged as a WARNING (fail-open), never error.
    expect(logger.warn).toHaveBeenCalled();
    expect(logger.error).not.toHaveBeenCalled();
  });
});

// ===========================================================================
// Hash-cache: identical re-delivery skips the FFI install.
// ===========================================================================

describe("pricing hash-cache idempotency (real engine)", () => {
  it("identical re-delivery with the same active_pricing_hash skips the FFI", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    const HASH = "c".repeat(64);
    const envelope = await buildEnvelope(
      privateKey,
      "cp",
      buildPricingBundle({ version: 6 }),
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();
    // Spy on the FFI install to count calls.
    const spy = vi.spyOn(engine, "reloadPricingSigned");
    let lastHash: string | null = null;
    const pu: PricingUpdateOptions = pricingUpdateFor(publicKeyHex, "cp", {
      getLastHash: () => lastHash,
      onInstalled: (_v, h) => {
        lastHash = h;
      },
    });

    // First delivery installs (hash present → cache seeded).
    handleControlEvent(
      engine,
      "init",
      JSON.stringify({ pricing_envelope: envelope, active_pricing_hash: HASH }),
      makeLogger(),
      undefined,
      pu,
    );
    for (let i = 0; i < 50; i++) {
      if (engine.getActivePricingVersion() === 6) break;
      await new Promise((r) => setTimeout(r, 5));
    }
    expect(engine.getActivePricingVersion()).toBe(6);
    expect(spy).toHaveBeenCalledTimes(1);

    // Second, identical delivery → hash matches → FFI NOT called again.
    handleControlEvent(
      engine,
      "pricing_updated",
      JSON.stringify({ hash: HASH, version: 6, pricing_envelope: envelope }),
      makeLogger(),
      undefined,
      pu,
    );
    await new Promise((r) => setTimeout(r, 20));
    expect(spy).toHaveBeenCalledTimes(1); // still 1 — no re-install
    expect(engine.getActivePricingVersion()).toBe(6);
  });
});

// ===========================================================================
// HEADLINE 2: TRUST ISOLATION — a policy-list key is rejected by pricing.
// ===========================================================================

describe("SECURITY: pricing install uses the PRICING trust roots, not the policy roots", () => {
  it("a bundle signed by a key in the POLICY trust list but NOT the pricing list is rejected (fail-open)", async () => {
    // One key. We pin it ONLY in the POLICY override, leave the PRICING
    // override empty. A correctly-typed, correctly-signed pricing bundle
    // must still be rejected because the pricing verifier consults the
    // pricing trust list, where this key does not appear. This is the
    // proof that `trustedPricingKeysJson()` — not `trustedPolicyKeysJson()`
    // — drives the pricing install.
    const { privateKey, publicKeyHex } = await generateEd25519();
    process.env.CHECKRD_ALLOW_TRUST_OVERRIDE = "1";
    process.env.CHECKRD_POLICY_TRUST_OVERRIDE_JSON = JSON.stringify([
      trustEntry(publicKeyHex, "policy-only-key"),
    ]);
    // PRICING override deliberately NOT set → pricing trust list is empty
    // (pre-1.0 production list), so this key is unknown to the pricing
    // verifier.

    // Sanity: the key IS in the policy list and NOT in the pricing list.
    const policyIds = new Set(
      (JSON.parse(trustedPolicyKeysJson()) as { keyid: string }[]).map(
        (k) => k.keyid,
      ),
    );
    const pricingIds = new Set(
      (JSON.parse(trustedPricingKeysJson()) as { keyid: string }[]).map(
        (k) => k.keyid,
      ),
    );
    expect(policyIds.has("policy-only-key")).toBe(true);
    expect(pricingIds.has("policy-only-key")).toBe(false);

    const envelope = await buildEnvelope(
      privateKey,
      "policy-only-key",
      buildPricingBundle({ version: 1 }),
      PRICING_PAYLOAD_TYPE, // correct pricing payload type
    );
    const engine = makeEngine();
    const logger = makeLogger();
    // The receiver default: pricing trust loader = trustedPricingKeysJson.
    const pu: PricingUpdateOptions = {
      loadTrustedKeys: () => trustedPricingKeysJson(),
    };
    handleControlEvent(
      engine,
      "pricing_updated",
      JSON.stringify({ version: 1, pricing_envelope: envelope }),
      logger,
      undefined,
      pu,
    );
    await new Promise((r) => setTimeout(r, 30));
    // REJECTED: metering stays dark (version 0), fail-open (no throw, warn).
    expect(engine.getActivePricingVersion()).toBe(0);
    expect(logger.warn).toHaveBeenCalled();
    expect(logger.error).not.toHaveBeenCalled();
  });

  it("direct FFI cross-check: the pricing verifier rejects the policy-list key with unknown_or_no_signer", async () => {
    // The above proves it through the dispatcher (fail-open swallows the
    // error). Here we assert the exact FFI rejection code to nail down
    // WHY it was rejected: the signer is not in the pricing trust list.
    const { privateKey } = await generateEd25519();
    const envelope = await buildEnvelope(
      privateKey,
      "signer-A",
      buildPricingBundle({ version: 1 }),
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();
    // Pricing trust list contains a DIFFERENT keyid than the signer.
    const wrongList = JSON.stringify([trustEntry("11".repeat(32), "signer-B")]);
    try {
      engine.reloadPricingSigned({
        envelopeJson: JSON.stringify(envelope),
        trustedKeysJson: wrongList,
        nowUnixSecs: Math.floor(Date.now() / 1000),
        maxAgeSecs: DEFAULT_PRICING_MAX_AGE_SECS,
      });
      throw new Error("expected rejection: signer not in pricing trust list");
    } catch (err) {
      expect(err).toBeInstanceOf(PricingSignatureError);
      expect((err as PricingSignatureError).ffiCode).toBe(-17);
      expect((err as PricingSignatureError).reason).toBe("unknown_or_no_signer");
    }
    expect(engine.getActivePricingVersion()).toBe(0);
  });
});

// ===========================================================================
// FAIL-OPEN: a bad / stale bundle logs a warning, does not throw, keeps going.
// ===========================================================================

describe("pricing fail-open posture", () => {
  it("a stale bundle (signed_at far in the past) is rejected without throwing; metering stays dark", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    const stale = await buildEnvelope(
      privateKey,
      "cp",
      // signed_at ~1 year ago, well beyond the 24h freshness window.
      buildPricingBundle({
        version: 1,
        signedAt: Math.floor(Date.now() / 1000) - 400 * 24 * 60 * 60,
      }),
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();
    const logger = makeLogger();
    expect(() =>
      handleControlEvent(
        engine,
        "pricing_updated",
        JSON.stringify({ version: 1, pricing_envelope: stale }),
        logger,
        undefined,
        pricingUpdateFor(publicKeyHex, "cp"),
      ),
    ).not.toThrow();
    await new Promise((r) => setTimeout(r, 30));
    expect(engine.getActivePricingVersion()).toBe(0);
    expect(logger.warn).toHaveBeenCalled();
  });

  it("a malformed pricing_envelope logs and keeps running (no throw)", async () => {
    const engine = makeEngine();
    const logger = makeLogger();
    expect(() =>
      handleControlEvent(
        engine,
        "pricing_updated",
        JSON.stringify({ version: 1, pricing_envelope: { not: "a real envelope" } }),
        logger,
        undefined,
        pricingUpdateFor("00".repeat(32), "cp"),
      ),
    ).not.toThrow();
    await new Promise((r) => setTimeout(r, 20));
    expect(engine.getActivePricingVersion()).toBe(0);
    expect(logger.warn).toHaveBeenCalled();
  });
});

// ===========================================================================
// Full ControlReceiver: an SSE `init` frame drives a live install.
// ===========================================================================

describe("ControlReceiver installs the pricing bundle from a live SSE init", () => {
  let receiver: ControlReceiver | null = null;
  afterEach(async () => {
    await receiver?.stop();
    receiver = null;
  });

  it("boots metering from the init frame using the default pricing trust loader", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    process.env.CHECKRD_ALLOW_TRUST_OVERRIDE = "1";
    process.env.CHECKRD_PRICING_TRUST_OVERRIDE_JSON = JSON.stringify([
      trustEntry(publicKeyHex, "sse-pricing-cp"),
    ]);
    const envelope = await buildEnvelope(
      privateKey,
      "sse-pricing-cp",
      buildPricingBundle({ version: 8 }),
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();

    // SSE stream: a single `init` frame carrying the pricing envelope,
    // then EOF so the receiver doesn't hang.
    const initData = JSON.stringify({
      kill_switch_active: false,
      pricing_envelope: envelope,
      active_pricing_hash: "d".repeat(64),
    });
    const sseBody = `event: init\ndata: ${initData}\n\n`;
    const fetchImpl = vi.fn(async (url: string) => {
      if (url.endsWith("/state")) {
        return new Response(JSON.stringify({ kill_switch_active: false }), {
          status: 200,
        });
      }
      return new Response(sseBody, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    });

    receiver = new ControlReceiver({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      engine,
      fetch: fetchImpl as unknown as typeof globalThis.fetch,
      initialBackoffMs: 5,
      // policyUpdate disabled (no policy in this test); pricingUpdate left
      // at its default so the receiver builds `trustedPricingKeysJson`.
      policyUpdate: null,
    });
    receiver.start();
    for (let i = 0; i < 100; i++) {
      if (engine.getActivePricingVersion() === 8) break;
      await new Promise((r) => setTimeout(r, 10));
    }
    expect(engine.getActivePricingVersion()).toBe(8);
  });

  it("installs the pricing bundle from the /control/state poll fallback", async () => {
    const { privateKey, publicKeyHex } = await generateEd25519();
    process.env.CHECKRD_ALLOW_TRUST_OVERRIDE = "1";
    process.env.CHECKRD_PRICING_TRUST_OVERRIDE_JSON = JSON.stringify([
      trustEntry(publicKeyHex, "poll-pricing-cp"),
    ]);
    const envelope = await buildEnvelope(
      privateKey,
      "poll-pricing-cp",
      buildPricingBundle({ version: 11 }),
      PRICING_PAYLOAD_TYPE,
    );
    const engine = makeEngine();
    const fetchImpl = vi.fn(async (url: string) => {
      if (url.endsWith("/state")) {
        return new Response(
          JSON.stringify({
            kill_switch_active: false,
            pricing_envelope: envelope,
            active_pricing_hash: "e".repeat(64),
          }),
          { status: 200 },
        );
      }
      // Hang SSE so the poll path is what installs.
      return new Response("", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    });
    receiver = new ControlReceiver({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      engine,
      fetch: fetchImpl as unknown as typeof globalThis.fetch,
      initialBackoffMs: 1000,
      policyUpdate: null,
    });
    receiver.start();
    for (let i = 0; i < 100; i++) {
      if (engine.getActivePricingVersion() === 11) break;
      await new Promise((r) => setTimeout(r, 10));
    }
    expect(engine.getActivePricingVersion()).toBe(11);
  });
});

// A tiny compile-time reference so the unused-import guard doesn't trip on
// PolicyUpdateOptions (imported to mirror the policy test surface).
const _typeRef: PolicyUpdateOptions | null = null;
void _typeRef;
