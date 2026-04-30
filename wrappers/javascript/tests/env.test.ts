import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { readEnv } from "../src/_env.js";

const TEST_VAR = "CHECKRD_TEST_ENV_VAR_XYZ";

describe("readEnv() — process.env path (Node, Bun, Vercel Edge)", () => {
  let saved: string | undefined;
  beforeEach(() => {
    saved = process.env[TEST_VAR];
    delete process.env[TEST_VAR];
  });
  afterEach(() => {
    if (saved === undefined) delete process.env[TEST_VAR];
    else process.env[TEST_VAR] = saved;
  });

  it("returns the value when the var is set", () => {
    process.env[TEST_VAR] = "ck_live_xyz";
    expect(readEnv(TEST_VAR)).toBe("ck_live_xyz");
  });

  it("returns undefined when the var is not set", () => {
    expect(readEnv(TEST_VAR)).toBeUndefined();
  });

  it("returns undefined for empty string (Anthropic pattern)", () => {
    process.env[TEST_VAR] = "";
    expect(readEnv(TEST_VAR)).toBeUndefined();
  });

  it("trims leading/trailing whitespace (OpenAI/Anthropic pattern)", () => {
    process.env[TEST_VAR] = "  ck_live_xyz  ";
    expect(readEnv(TEST_VAR)).toBe("ck_live_xyz");
  });

  it("returns undefined when the value is whitespace-only", () => {
    process.env[TEST_VAR] = "   \t\n  ";
    expect(readEnv(TEST_VAR)).toBeUndefined();
  });

  it("returns a fresh value on each call (no caching)", () => {
    process.env[TEST_VAR] = "first";
    expect(readEnv(TEST_VAR)).toBe("first");
    process.env[TEST_VAR] = "second";
    expect(readEnv(TEST_VAR)).toBe("second");
  });
});

describe("readEnv() — Cloudflare Workers / browser fallback", () => {
  let savedProcess: typeof globalThis.process | undefined;
  beforeEach(() => {
    savedProcess = globalThis.process;
    // Simulate a runtime with no `process` global (Workers, browser).
    delete (globalThis as { process?: unknown }).process;
  });
  afterEach(() => {
    (globalThis as { process?: unknown }).process = savedProcess;
  });

  it("returns undefined without throwing when `process` is missing", () => {
    expect(() => readEnv(TEST_VAR)).not.toThrow();
    expect(readEnv(TEST_VAR)).toBeUndefined();
  });
});

describe("readEnv() — Deno fallback", () => {
  let savedProcess: typeof globalThis.process | undefined;
  beforeEach(() => {
    // Drop process so the Deno branch is reached.
    savedProcess = globalThis.process;
    delete (globalThis as { process?: unknown }).process;
  });
  afterEach(() => {
    (globalThis as { process?: unknown }).process = savedProcess;
    delete (globalThis as { Deno?: unknown }).Deno;
  });

  it("reads from Deno.env.get() when process is absent", () => {
    (globalThis as { Deno?: unknown }).Deno = {
      env: { get: (name: string) => (name === TEST_VAR ? "deno-value" : undefined) },
    };
    expect(readEnv(TEST_VAR)).toBe("deno-value");
  });

  it("trims whitespace from Deno values", () => {
    (globalThis as { Deno?: unknown }).Deno = {
      env: { get: () => "  deno-value  " },
    };
    expect(readEnv(TEST_VAR)).toBe("deno-value");
  });

  it("returns undefined when Deno.env.get() throws (permission denied)", () => {
    (globalThis as { Deno?: unknown }).Deno = {
      env: {
        get: () => {
          throw new Error("permission denied: --allow-env not granted");
        },
      },
    };
    expect(() => readEnv(TEST_VAR)).not.toThrow();
    expect(readEnv(TEST_VAR)).toBeUndefined();
  });

  it("returns undefined when Deno.env.get is missing entirely", () => {
    (globalThis as { Deno?: unknown }).Deno = {};
    expect(readEnv(TEST_VAR)).toBeUndefined();
  });
});
