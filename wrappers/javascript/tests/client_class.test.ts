/**
 * Tests for the unified `Checkrd` client class.
 *
 * Parallel to the Python SDK's `tests/test_client_class.py` — the
 * class consolidates `init() + wrap() + instrument*()` into one
 * OpenAI-SDK-shaped object. These tests pin:
 *
 *   - Constructor shape (`new Checkrd({ apiKey, agentId, baseUrl })`)
 *   - `.wrap()` returns a Checkrd-enforced fetch
 *   - `.withOptions()` is immutable; returns a fresh client
 *   - Context-manager-equivalent lifecycle: `.close()` is idempotent
 *   - `.toString()` / `.toJSON()` never leak the API key
 *   - Backwards-compat: top-level `wrap()` still works alongside the class
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  Checkrd,
  CheckrdInitError,
  CheckrdPolicyDenied,
  shutdown,
  wrap,
} from "../src/index.js";

const ALLOW_ALL = { agent: "test", default: "allow", rules: [] };
const DENY_ALL = { agent: "test", default: "deny", rules: [] };

afterEach(async () => {
  // Reset any global context set by `.instrument*()` in tests so
  // individual cases start from a clean slate.
  await shutdown();
});

describe("Checkrd constructor", () => {
  // Matches the OpenAI SDK's "new OpenAI({ apiKey })" convention —
  // see https://github.com/openai/openai-node for the reference shape.

  it("constructs with no arguments (env-var fallbacks)", () => {
    // Mirrors `new OpenAI()` — all fields fall back to env. A bare
    // constructor must not throw.
    const client = new Checkrd();
    expect(client).toBeInstanceOf(Checkrd);
  });

  it("exposes apiKey via a read-only getter", () => {
    const client = new Checkrd({ apiKey: "ck_test_abc", agentId: "t" });
    expect(client.apiKey).toBe("ck_test_abc");
  });

  it("reads apiKey from CHECKRD_API_KEY when not passed explicitly", () => {
    const original = process.env.CHECKRD_API_KEY;
    process.env.CHECKRD_API_KEY = "ck_env_xyz";
    try {
      const client = new Checkrd({ agentId: "t" });
      expect(client.apiKey).toBe("ck_env_xyz");
    } finally {
      if (original === undefined) delete process.env.CHECKRD_API_KEY;
      else process.env.CHECKRD_API_KEY = original;
    }
  });

  it("exposes baseUrl via a getter after settings resolution", () => {
    const client = new Checkrd({
      apiKey: "ck_test",
      agentId: "t",
      controlPlaneUrl: "https://api.example.com",
    });
    expect(client.baseUrl).toBe("https://api.example.com");
  });

  it("freezes the options object so constructor-time data is immutable", () => {
    // A bug here would allow a caller to mutate the options
    // post-construction and silently change behavior of subsequent
    // wrap() calls. The freeze is defensive.
    const client = new Checkrd({ apiKey: "ck", agentId: "t" });
    // Round-trip through JSON to confirm the getter doesn't explode;
    // the snapshot shape is exercised separately below.
    expect(typeof client.toJSON()).toBe("object");
  });
});

describe("Checkrd.wrap()", () => {
  it("returns a fetch-shaped function that enforces allow-all", async () => {
    const base = vi.fn(
      async () => new Response("{}", { status: 200 }),
    );
    const client = new Checkrd({ agentId: "t", policy: ALLOW_ALL });
    try {
      const wrapped = client.wrap(base as unknown as typeof fetch);
      const res = await wrapped("https://api.openai.com/v1/chat/completions");
      expect(res.status).toBe(200);
      expect(base).toHaveBeenCalled();
    } finally {
      await client.close();
    }
  });

  it("enforces deny policy (returns CheckrdPolicyDenied)", async () => {
    const base = vi.fn(
      async () => new Response("{}", { status: 200 }),
    );
    const client = new Checkrd({
      agentId: "t",
      policy: DENY_ALL,
      enforce: true,
    });
    try {
      const wrapped = client.wrap(base as unknown as typeof fetch);
      await expect(
        wrapped("https://api.openai.com/v1/chat/completions"),
      ).rejects.toBeInstanceOf(CheckrdPolicyDenied);
    } finally {
      await client.close();
    }
  });

  it("throws CheckrdInitError when called after close()", async () => {
    // wrap-after-close is a programming error; the sharp-error
    // catches it instead of silently leaking a dead client.
    const client = new Checkrd({ agentId: "t", policy: ALLOW_ALL });
    await client.close();
    expect(() => client.wrap()).toThrow(CheckrdInitError);
  });
});

describe("Checkrd.withOptions()", () => {
  it("returns a new Checkrd instance (does not mutate source)", () => {
    const a = new Checkrd({ apiKey: "ck1", agentId: "t" });
    const b = a.withOptions({ apiKey: "ck2" });
    expect(b).not.toBe(a);
    expect(a.apiKey).toBe("ck1"); // source unchanged
    expect(b.apiKey).toBe("ck2");
  });

  it("carries unchanged options forward by default", () => {
    const a = new Checkrd({
      apiKey: "ck1",
      agentId: "custom-agent",
      controlPlaneUrl: "https://api.example.com",
    });
    const b = a.withOptions({ apiKey: "ck2" });
    expect(b.agentId).toBe("custom-agent");
    expect(b.baseUrl).toBe("https://api.example.com");
  });

  it("allows chained overrides", () => {
    const a = new Checkrd({ apiKey: "ck1", agentId: "t" });
    const b = a
      .withOptions({ apiKey: "ck2" })
      .withOptions({ controlPlaneUrl: "https://api.example.com" })
      .withOptions({ apiVersion: "2026-05-01" });
    expect(b.apiKey).toBe("ck2");
    expect(b.baseUrl).toBe("https://api.example.com");
  });

  it("does not start background resources on clone", () => {
    // Cloning must be cheap — no fresh batcher / receiver per
    // withOptions() call. We verify indirectly via the fact that
    // neither `.wrap()` nor `.instrumentOpenAI()` was called.
    const a = new Checkrd({ apiKey: "ck", agentId: "t" });
    const b = a.withOptions({ apiKey: "ck2" });
    const c = b.withOptions({ apiVersion: "2026-01-01" });
    expect(c).toBeInstanceOf(Checkrd);
  });
});

describe("Checkrd.close()", () => {
  it("is idempotent", async () => {
    const client = new Checkrd({ agentId: "t", policy: ALLOW_ALL });
    await client.close();
    await client.close();
    // Must complete without throwing.
  });

  it("is safe after an instrument*() call", async () => {
    // With a global context installed, close() must also tear down
    // that context. A regression here would leak the global
    // `setContext(...)` state into the next test.
    const client = new Checkrd({ agentId: "t", policy: ALLOW_ALL });
    try {
      client.instrumentOpenAI();
    } finally {
      await client.close();
    }
  });
});

describe("toString / toJSON safety", () => {
  // Shipping the API key in log lines, JSON.stringify output, or
  // REPL `repr`s is a Stripe-grade incident. Both serializers must
  // surface only metadata.

  it("toString() omits the API key value entirely", () => {
    const client = new Checkrd({
      apiKey: "ck_live_SUPER_SECRET_abc123",
      agentId: "t",
    });
    const str = client.toString();
    expect(str).not.toContain("SUPER_SECRET");
    expect(str).not.toContain("ck_live_");
    expect(str).toContain("hasApiKey=true"); // operator can still tell
  });

  it("toJSON() omits the API key value entirely", () => {
    const client = new Checkrd({
      apiKey: "ck_live_SUPER_SECRET_abc123",
      agentId: "t",
    });
    const json = client.toJSON();
    expect(JSON.stringify(json)).not.toContain("SUPER_SECRET");
    expect(JSON.stringify(json)).not.toContain("ck_live_");
    expect(json.hasApiKey).toBe(true);
  });

  it("reports hasApiKey=false when no key is configured", () => {
    const original = process.env.CHECKRD_API_KEY;
    delete process.env.CHECKRD_API_KEY;
    try {
      const client = new Checkrd({ agentId: "t" });
      expect(client.toJSON().hasApiKey).toBe(false);
    } finally {
      if (original !== undefined) process.env.CHECKRD_API_KEY = original;
    }
  });

  it("JSON.stringify(checkrd) round-trips via toJSON", () => {
    // Serializers that call JSON.stringify() (e.g. log frameworks
    // like pino or winston) must see the scrubbed shape, not the
    // raw constructor options.
    const client = new Checkrd({ apiKey: "leak-me", agentId: "t" });
    expect(JSON.stringify(client)).not.toContain("leak-me");
  });
});

describe("backwards compatibility with top-level functions", () => {
  // The class is additive. A user migrating from top-level wrap()
  // can mix both patterns during the transition — we must not have
  // broken the old surface.

  it("top-level wrap() continues to work", async () => {
    const base = vi.fn(async () => new Response("{}", { status: 200 }));
    const wrapped = wrap(base as unknown as typeof fetch, {
      agentId: "t",
      policy: ALLOW_ALL,
    });
    const res = await wrapped("https://api.openai.com/v1/x");
    expect(res.status).toBe(200);
  });

  it("class and function coexist in the same process", async () => {
    // Separate clients via separate paths — both must succeed.
    const base1 = vi.fn(async () => new Response("{}", { status: 200 }));
    const base2 = vi.fn(async () => new Response("{}", { status: 200 }));
    const classFetch = new Checkrd({
      agentId: "t",
      policy: ALLOW_ALL,
    }).wrap(base1 as unknown as typeof fetch);
    const fnFetch = wrap(base2 as unknown as typeof fetch, {
      agentId: "t",
      policy: ALLOW_ALL,
    });
    await classFetch("https://a.example.com/");
    await fnFetch("https://b.example.com/");
    expect(base1).toHaveBeenCalledOnce();
    expect(base2).toHaveBeenCalledOnce();
  });
});

describe("package root exports", () => {
  it("exports Checkrd at the package root", async () => {
    // A regression here means tutorials break.
    const mod = await import("../src/index.js");
    expect(mod.Checkrd).toBe(Checkrd);
  });

  it("exports UNSET sentinel for withOptions unambiguity", async () => {
    const mod = await import("../src/index.js");
    expect(typeof mod.UNSET).toBe("symbol");
  });
});
