/**
 * SECURITY: pricing trust roots are STRUCTURALLY SEPARATE from policy
 * trust roots (TUF-style per-role key separation).
 *
 * `trustedPricingKeysJson()` and `trustedPolicyKeysJson()` are parallel,
 * disjoint accessors over parallel, disjoint arrays. This file pins:
 *
 *   1. The shipped lists share NO keyid (disjointness invariant).
 *   2. The pricing override env var (`CHECKRD_PRICING_TRUST_OVERRIDE_JSON`)
 *      is independent of the policy override env var, under the same
 *      `CHECKRD_ALLOW_TRUST_OVERRIDE=1` double-gate.
 *   3. Overriding ONE list does not move the other.
 *
 * The runtime cross-purpose REJECTION (a pricing-key-signed envelope fed to
 * the policy verifier, and vice versa) is proven in `pricing_signing.test.ts`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  trustedPolicyKeysJson,
  trustedPricingKeysJson,
  type TrustedKey,
} from "../src/_trust.js";

const OVERRIDE_ENVS = [
  "CHECKRD_POLICY_TRUST_OVERRIDE_JSON",
  "CHECKRD_PRICING_TRUST_OVERRIDE_JSON",
  "CHECKRD_ALLOW_TRUST_OVERRIDE",
] as const;

afterEach(() => {
  for (const name of OVERRIDE_ENVS) delete process.env[name];
});

function keyids(json: string): Set<string> {
  const parsed = JSON.parse(json) as TrustedKey[];
  return new Set(parsed.map((k) => k.keyid));
}

describe("pricing vs policy trust-root separation", () => {
  it("the shipped pricing and policy key lists share no keyid", () => {
    const policyIds = keyids(trustedPolicyKeysJson());
    const pricingIds = keyids(trustedPricingKeysJson());
    const overlap = [...pricingIds].filter((id) => policyIds.has(id));
    expect(overlap).toEqual([]);
  });

  it("trustedPricingKeysJson is a separate accessor (not the policy list)", () => {
    // Both are empty pre-1.0, but they must be DISTINCT call paths reading
    // DISTINCT arrays. Override only the PRICING list and confirm the policy
    // accessor is unaffected — that proves they aren't aliased.
    process.env.CHECKRD_ALLOW_TRUST_OVERRIDE = "1";
    process.env.CHECKRD_PRICING_TRUST_OVERRIDE_JSON = JSON.stringify([
      {
        keyid: "pricing-dev",
        public_key_hex: "aa".repeat(32),
        valid_from: 0,
        valid_until: 2_000_000_000,
      },
    ]);
    const pricing = keyids(trustedPricingKeysJson());
    const policy = keyids(trustedPolicyKeysJson());
    expect(pricing.has("pricing-dev")).toBe(true);
    expect(policy.has("pricing-dev")).toBe(false);
  });

  it("overriding the POLICY list does not move the PRICING list", () => {
    process.env.CHECKRD_ALLOW_TRUST_OVERRIDE = "1";
    process.env.CHECKRD_POLICY_TRUST_OVERRIDE_JSON = JSON.stringify([
      {
        keyid: "policy-dev",
        public_key_hex: "bb".repeat(32),
        valid_from: 0,
        valid_until: 2_000_000_000,
      },
    ]);
    expect(keyids(trustedPolicyKeysJson()).has("policy-dev")).toBe(true);
    expect(keyids(trustedPricingKeysJson()).has("policy-dev")).toBe(false);
  });

  it("ignores the pricing override without the double-gate", () => {
    // Override set but CHECKRD_ALLOW_TRUST_OVERRIDE absent → ignored, falls
    // back to the (empty) production pricing list. A single env var is not
    // enough to swap the money-signing trust anchor.
    process.env.CHECKRD_PRICING_TRUST_OVERRIDE_JSON = JSON.stringify([
      {
        keyid: "sneaky",
        public_key_hex: "cc".repeat(32),
        valid_from: 0,
        valid_until: 2_000_000_000,
      },
    ]);
    const logger = { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() };
    const result = keyids(trustedPricingKeysJson(logger));
    expect(result.has("sneaky")).toBe(false);
    expect(logger.warn).toHaveBeenCalled();
  });

  it("warns loudly on an empty pricing override (all updates rejected)", () => {
    process.env.CHECKRD_ALLOW_TRUST_OVERRIDE = "1";
    process.env.CHECKRD_PRICING_TRUST_OVERRIDE_JSON = "[]";
    const logger = { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() };
    const json = trustedPricingKeysJson(logger);
    expect(JSON.parse(json)).toEqual([]);
    expect(
      logger.warn.mock.calls.some((c) =>
        String(c[0]).includes("all signed pricing updates will be rejected"),
      ),
    ).toBe(true);
  });
});
