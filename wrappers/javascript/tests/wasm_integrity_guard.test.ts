/**
 * Production-environment guard for the WASM integrity bypass.
 *
 * ``CHECKRD_SKIP_WASM_INTEGRITY=1`` is a legitimate escape hatch for
 * source-checkout contributors whose ``_wasm_integrity.ts`` hasn't been
 * regenerated. It is NOT a legitimate configuration for production.
 * These tests pin the safety net: any well-known ``ENV=production``
 * signal + the skip flag must surface an actionable error unless the
 * operator types the break-glass acknowledgment phrase.
 *
 * Parallel to ``tests/test_engine.py::TestWasmIntegrityProductionGuard``
 * in the Python SDK — when a fix lands in one, it lands in both.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { __shouldSkipIntegrity } from "../src/engine.js";
import { CheckrdInitError } from "../src/exceptions.js";

/** Env vars the guard consults, cleared between tests for isolation. */
const GUARD_ENV_VARS = [
  "CHECKRD_SKIP_WASM_INTEGRITY",
  "CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK",
  "CHECKRD_ENV",
  "CHECKRD_ENVIRONMENT",
  "ENVIRONMENT",
  "ENV",
  "APP_ENV",
  "NODE_ENV",
  "RAILS_ENV",
  "DJANGO_ENV",
  "FLASK_ENV",
  "PYTHON_ENV",
  "DEPLOYMENT_ENVIRONMENT",
] as const;

const PRODUCTION_ENV_VARS = GUARD_ENV_VARS.filter(
  (n) => n !== "CHECKRD_SKIP_WASM_INTEGRITY" && n !== "CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK",
);

describe("WASM integrity production guard", () => {
  let saved: Record<string, string | undefined> = {};

  beforeEach(() => {
    saved = {};
    for (const name of GUARD_ENV_VARS) {
      saved[name] = process.env[name];
      delete process.env[name];
    }
  });

  afterEach(() => {
    for (const name of GUARD_ENV_VARS) {
      if (saved[name] === undefined) delete process.env[name];
      else process.env[name] = saved[name];
    }
  });

  it("returns false when the skip flag is not set", () => {
    // Even in production: if the operator didn't ask to skip, the
    // guard doesn't fire. The guard is about safe use of the skip
    // flag, not about the absence of the flag itself.
    process.env["NODE_ENV"] = "production";
    expect(__shouldSkipIntegrity()).toBe(false);
  });

  it("returns true when the skip flag is set and no production signal", () => {
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    expect(__shouldSkipIntegrity()).toBe(true);
  });

  it("refuses the bypass when NODE_ENV=production", () => {
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    process.env["NODE_ENV"] = "production";
    expect(() => __shouldSkipIntegrity()).toThrowError(
      /production-looking environment/,
    );
  });

  it("refuses the bypass with the specific error type", () => {
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    process.env["NODE_ENV"] = "production";
    expect(() => __shouldSkipIntegrity()).toThrowError(CheckrdInitError);
  });

  it.each(PRODUCTION_ENV_VARS)(
    "refuses the bypass when %s=production",
    (envName) => {
      process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
      process.env[envName] = "production";
      expect(() => __shouldSkipIntegrity()).toThrowError(
        /production-looking environment/,
      );
    },
  );

  it.each(["production", "prod", "canary", "live"])(
    "recognizes %s as a production value",
    (value) => {
      process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
      process.env["NODE_ENV"] = value;
      expect(() => __shouldSkipIntegrity()).toThrowError(
        /production-looking environment/,
      );
    },
  );

  it("normalizes case when matching production values", () => {
    // `ENV=PRODUCTION` from a CI provider's uppercase-everything default
    // must still trip the guard.
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    process.env["NODE_ENV"] = "PRODUCTION";
    expect(() => __shouldSkipIntegrity()).toThrowError(
      /production-looking environment/,
    );
  });

  it("names the offending env var in the error message", () => {
    // Actionable error — the operator needs to know which signal
    // tripped the guard, not a generic "something in your env".
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    process.env["RAILS_ENV"] = "production";
    expect(() => __shouldSkipIntegrity()).toThrowError(
      /RAILS_ENV="production"/,
    );
  });

  it("includes a docs link for the error code", () => {
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    process.env["NODE_ENV"] = "production";
    expect(() => __shouldSkipIntegrity()).toThrowError(
      /checkrd\.io\/errors\/wasm_integrity_skip_in_prod/,
    );
  });

  it("permits bypass when the exact acknowledgment phrase is set", () => {
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    process.env["NODE_ENV"] = "production";
    process.env["CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK"] =
      "i-understand-the-risk";
    expect(__shouldSkipIntegrity()).toBe(true);
  });

  it.each([
    "1",
    "true",
    "yes",
    "i-understand",
    "I-UNDERSTAND-THE-RISK",
    "  i-understand-the-risk  ",
    "i-understand-the-risk\n",
  ])(
    "rejects near-miss acknowledgment phrase %j",
    (phrase) => {
      // Exact-match is deliberate. Any tolerance for whitespace or
      // uppercasing is a pattern an attacker/scripter would exploit
      // to make an automated deploy bypass the guard. The phrase
      // is a muscle-memory barrier, not a configuration knob.
      process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
      process.env["NODE_ENV"] = "production";
      process.env["CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK"] = phrase;
      expect(() => __shouldSkipIntegrity()).toThrowError(
        /production-looking environment/,
      );
    },
  );

  it("non-production values permit bypass without acknowledgment", () => {
    // `NODE_ENV=development` is the canonical dev signal and must not
    // trip the guard — otherwise every local dev workflow would need
    // to set the acknowledgment phrase.
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    process.env["NODE_ENV"] = "development";
    expect(__shouldSkipIntegrity()).toBe(true);
  });

  it("empty NODE_ENV permits bypass without acknowledgment", () => {
    // `NODE_ENV=""` is effectively unset — some shells export empty
    // values unintentionally. Don't treat it as production.
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
    process.env["NODE_ENV"] = "";
    expect(__shouldSkipIntegrity()).toBe(true);
  });

  it("only '1' counts as truthy for the skip flag (no near-misses)", () => {
    // Same tight parsing as the Python SDK: loose variants ('true',
    // 'TRUE ', ' 1\n') must not accidentally disable a defense the
    // operator didn't mean to turn off.
    process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "true";
    process.env["NODE_ENV"] = "production";
    // Does not throw because the skip flag itself isn't recognized.
    expect(__shouldSkipIntegrity()).toBe(false);
  });
});
