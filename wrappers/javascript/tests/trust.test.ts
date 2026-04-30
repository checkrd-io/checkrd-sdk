/**
 * Tests for `_trust.ts` — production trust-list diagnostics.
 *
 * Mirrors `wrappers/python/tests/test_trust.py`'s coverage of
 * `production_trust_status` + `warn_if_misconfigured`. The override
 * mechanism in `trustedPolicyKeysJson` already has its own coverage in
 * the existing receiver/control tests; this file targets the new helpers
 * the JS SDK gained alongside the Python CI guard.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  _resetWarningStateForTests,
  productionTrustStatus,
  warnIfMisconfigured,
} from "../src/_trust.js";

// Shape used by both helpers as their `env` injection point. Matches
// `readEnv`'s contract: returns `undefined` for unset, trimmed string
// otherwise.
type EnvFn = (name: string) => string | undefined;

function makeEnv(values: Record<string, string>): EnvFn {
  return (name: string): string | undefined => values[name];
}

describe("productionTrustStatus", () => {
  it("returns 'override' when the double-gate is active", () => {
    const env = makeEnv({
      CHECKRD_POLICY_TRUST_OVERRIDE_JSON: '[{"keyid":"x"}]',
      CHECKRD_ALLOW_TRUST_OVERRIDE: "1",
    });
    const { level } = productionTrustStatus({ env });
    expect(level).toBe("override");
  });

  it.each(["1", "true", "yes"])(
    "honors '%s' as a valid gate value",
    (gate) => {
      const env = makeEnv({
        CHECKRD_POLICY_TRUST_OVERRIDE_JSON: "[]",
        CHECKRD_ALLOW_TRUST_OVERRIDE: gate,
      });
      expect(productionTrustStatus({ env }).level).toBe("override");
    },
  );

  it("ignores override when gate is not a recognized truthy value", () => {
    // Empty production list + invalid gate → falls through to empty_dev
    // (no production-shaped URL provided) rather than reporting override.
    const env = makeEnv({
      CHECKRD_POLICY_TRUST_OVERRIDE_JSON: "[]",
      CHECKRD_ALLOW_TRUST_OVERRIDE: "0",
    });
    const { level } = productionTrustStatus({ env });
    expect(level).not.toBe("override");
  });

  it("reports 'empty_dev' for localhost URLs with empty production list", () => {
    const { level } = productionTrustStatus({
      baseUrl: "http://localhost:8080",
      env: makeEnv({}),
      keys: [],
    });
    expect(level).toBe("empty_dev");
  });

  it("reports 'empty_dev' when baseUrl is undefined", () => {
    const { level } = productionTrustStatus({
      env: makeEnv({}),
      keys: [],
    });
    expect(level).toBe("empty_dev");
  });

  it.each([
    "https://api.checkrd.io",
    "https://api.staging.checkrd.io",
    "https://api.checkrd.io:8443",
    "wss://api.checkrd.io/v1/agents/x/control",
  ])("reports 'empty_production' for production URL %s", (url) => {
    const { level, message } = productionTrustStatus({
      baseUrl: url,
      env: makeEnv({}),
      keys: [],
    });
    expect(level).toBe("empty_production");
    expect(message).toContain("scripts/generate-policy-signing-key.py");
  });

  it("reports 'ok' when the SDK's pinned production list is non-empty", () => {
    // Default ``keys`` parameter — exercises the actual shipped trust list
    // and proves the SDK is in the safe state for release.
    const { level, message } = productionTrustStatus({
      baseUrl: "https://api.checkrd.io",
      env: makeEnv({}),
    });
    expect(level).toBe("ok");
    expect(message).toContain("key(s)");
  });
});

describe("warnIfMisconfigured", () => {
  beforeEach(() => {
    _resetWarningStateForTests();
  });

  it("fires logger.error exactly once for empty_production", () => {
    const logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    warnIfMisconfigured({ baseUrl: "https://api.checkrd.io", logger, keys: [] });
    warnIfMisconfigured({ baseUrl: "https://api.checkrd.io", logger, keys: [] });
    warnIfMisconfigured({ baseUrl: "https://api.checkrd.io", logger, keys: [] });
    expect(logger.error).toHaveBeenCalledTimes(1);
    expect(logger.error.mock.calls[0]?.[0]).toContain(
      "production trust list is empty",
    );
  });

  it("does not fire for empty_dev or undefined baseUrl", () => {
    const logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    warnIfMisconfigured({ baseUrl: "http://localhost:8080", logger, keys: [] });
    warnIfMisconfigured({ baseUrl: undefined, logger, keys: [] });
    expect(logger.error).not.toHaveBeenCalled();
  });

  it("re-arms after _resetWarningStateForTests", () => {
    const logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    warnIfMisconfigured({ baseUrl: "https://api.checkrd.io", logger, keys: [] });
    expect(logger.error).toHaveBeenCalledTimes(1);
    _resetWarningStateForTests();
    warnIfMisconfigured({ baseUrl: "https://api.checkrd.io", logger, keys: [] });
    expect(logger.error).toHaveBeenCalledTimes(2);
  });

  it("survives a missing logger without throwing", () => {
    expect(() => {
      warnIfMisconfigured({ baseUrl: "https://api.checkrd.io" });
    }).not.toThrow();
  });
});

afterEach(() => {
  _resetWarningStateForTests();
});
