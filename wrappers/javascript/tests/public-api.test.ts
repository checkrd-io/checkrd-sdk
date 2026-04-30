/**
 * End-to-end tests of the public API (wrap / init / instrument). These
 * exercise `src/index.ts` — the entry point that users actually touch —
 * and the vendor instrumentor patching machinery.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CheckrdInitError,
  CheckrdPolicyDenied,
  healthy,
  init,
  instrumentAnthropic,
  instrumentOpenAI,
  shutdown,
  uninstrumentAnthropic,
  uninstrumentOpenAI,
  version,
  wrap,
} from "../src/index.js";

const ALLOW_ALL = { agent: "test", default: "allow", rules: [] };
const DENY_ALL = { agent: "test", default: "deny", rules: [] };

afterEach(async () => {
  await shutdown();
  uninstrumentOpenAI();
  uninstrumentAnthropic();
});

describe("version", () => {
  it("exports a non-empty semver string", () => {
    expect(version).toMatch(/^\d+\.\d+\.\d+$/);
  });
});

describe("wrap()", () => {
  it("returns a fetch that enforces allow-all policy", async () => {
    const base = vi.fn(async () => new Response("{}", { status: 200 }));
    const f = wrap(base as unknown as typeof fetch, { policy: ALLOW_ALL, agentId: "test" });
    const res = await f("https://example.com/");
    expect(res.status).toBe(200);
    expect(base).toHaveBeenCalledOnce();
  });

  it("returns a fetch that blocks under deny-all", async () => {
    const base = vi.fn(async () => new Response("{}", { status: 200 }));
    const f = wrap(base as unknown as typeof fetch, {
      policy: DENY_ALL,
      agentId: "test",
      enforce: true,
    });
    await expect(f("https://example.com/")).rejects.toBeInstanceOf(CheckrdPolicyDenied);
  });

  it("no-ops when CHECKRD_DISABLED=1", async () => {
    process.env["CHECKRD_DISABLED"] = "1";
    try {
      const base = vi.fn(async () => new Response("ok", { status: 200 }));
      const f = wrap(base as unknown as typeof fetch, { policy: DENY_ALL, agentId: "t" });
      // Wrapped fetch is the base fetch unchanged → no policy enforcement.
      const res = await f("https://example.com/");
      expect(res.status).toBe(200);
      expect(base).toHaveBeenCalledOnce();
    } finally {
      delete process.env["CHECKRD_DISABLED"];
    }
  });
});

describe("init() + healthy()", () => {
  it("reports disabled before init", () => {
    expect(healthy().status).toBe("disabled");
  });

  it("reports healthy after successful init", () => {
    init({ policy: ALLOW_ALL, agentId: "my-agent" });
    const h = healthy();
    expect(h.status).toBe("healthy");
    expect(h.engine_loaded).toBe(true);
    expect(h.agent_id).toBe("my-agent");
  });

  it("refuses to silently pass through in strict mode on engine failure", () => {
    expect(() =>
      { init({ policy: "not-a-valid-policy-{", agentId: "bad" }); },
    ).toThrow(CheckrdInitError);
  });

  it("raises if instrumentOpenAI() is called before init()", () => {
    expect(() => { instrumentOpenAI(); }).toThrow(CheckrdInitError);
  });

  it("raises if instrumentAnthropic() is called before init()", () => {
    expect(() => { instrumentAnthropic(); }).toThrow(CheckrdInitError);
  });
});

describe("instrumentOpenAI / instrumentAnthropic", () => {
  it("are idempotent (safe to call repeatedly)", () => {
    init({ policy: ALLOW_ALL, agentId: "t" });
    expect(() => {
      instrumentOpenAI();
      instrumentOpenAI();
      instrumentAnthropic();
      instrumentAnthropic();
    }).not.toThrow();
  });

  it("injects a wrapped fetch when constructing an OpenAI client", async () => {
    init({ policy: ALLOW_ALL, agentId: "t" });
    instrumentOpenAI();
    const mod = await import("openai");
    // The proxied constructor should now accept a fetch option — if we
    // don't pass one, it pulls in Checkrd's wrapped fetch automatically.
    // We just verify construction doesn't throw and produces an object.
    const OpenAI = mod.default;
    const client = new OpenAI({ apiKey: "sk-test" });
    expect(client).toBeTruthy();
    // The constructor returns an instance — nothing specific to assert
    // without mocking openai's internals; this smoke-tests that our
    // Proxy pattern didn't break instantiation.
  });
});

describe("dangerouslyAllowBrowser guard", () => {
  // The previous guard used `!process.versions.node` as the "this is a
  // browser" heuristic, which was overly broad — it flagged every
  // non-Node server runtime (CF Workers, Vercel Edge, Deno) as a
  // browser, forcing operators to sprinkle `dangerouslyAllowBrowser:
  // true` defensively.
  //
  // The new guard uses `isRealBrowser()` which requires window +
  // document + navigator and excludes Deno/Bun/EdgeRuntime signals.
  // That makes it un-spoofable via `process.versions` alone — we can
  // no longer simulate a browser by hiding the Node version. Stronger
  // coverage lives in `tests/browser_guard.test.ts` which exercises
  // `isRealBrowser()` directly via the `globals` injection parameter.

  it("init() does NOT throw on Node (regression: no flag required)", () => {
    // The critical backwards-compat case: Node users must not be
    // required to set the flag. A regression here would force every
    // existing caller to add `dangerouslyAllowBrowser: true`.
    expect(() => {
      init({ policy: ALLOW_ALL, agentId: "t" });
    }).not.toThrow();
  });

  it("init() accepts dangerouslyAllowBrowser=true without issue", () => {
    // Passing the flag on Node is a no-op (no real browser detected,
    // no warning fires). Verified via `tests/browser_guard.test.ts`
    // where we pass synthetic globals that do trigger the warning.
    expect(() => {
      init({
        policy: ALLOW_ALL,
        agentId: "t",
        dangerouslyAllowBrowser: true,
      });
    }).not.toThrow();
  });
});
