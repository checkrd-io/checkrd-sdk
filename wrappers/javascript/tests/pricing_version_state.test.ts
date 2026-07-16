/**
 * Tests for the in-memory pricing-version high-water-mark in `_state.ts`
 * (M-12, step 3).
 *
 * POSTURE: in-memory only, mirroring the JS policy-version posture exactly.
 * The authoritative rollback counter lives inside the WASM instance
 * (`last_pricing_version`); this module value is the restore seam fed back
 * via `engine.setInitialPricingVersion()` on a fresh engine. There is no
 * disk persistence (the JS SDK does not persist policy versions either).
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  _resetPricingVersionForTests,
  getPricingVersionHighWater,
  recordPricingVersion,
} from "../src/_state.js";

afterEach(() => {
  _resetPricingVersionForTests();
});

describe("pricing version high-water-mark", () => {
  it("starts at 0", () => {
    expect(getPricingVersionHighWater()).toBe(0);
  });

  it("records a monotonically increasing version", () => {
    recordPricingVersion(3);
    expect(getPricingVersionHighWater()).toBe(3);
    recordPricingVersion(7);
    expect(getPricingVersionHighWater()).toBe(7);
  });

  it("never moves backwards (rollback defense)", () => {
    recordPricingVersion(7);
    recordPricingVersion(3); // lower → ignored
    recordPricingVersion(7); // equal → ignored
    expect(getPricingVersionHighWater()).toBe(7);
  });
});
