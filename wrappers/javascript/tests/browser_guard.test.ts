/**
 * Browser detection + dangerouslyAllowBrowser warning.
 *
 * The browser guard must:
 *   - Recognize REAL browsers (window + document + navigator) and
 *     throw unless the operator explicitly opts in.
 *   - NOT flag legitimate server runtimes (CF Workers, Vercel Edge,
 *     Deno, Bun, Node) as browsers — the previous `!process.versions.node`
 *     heuristic got this wrong and forced those integrations to
 *     sprinkle `dangerouslyAllowBrowser: true` defensively.
 *   - Fire a loud one-time banner when the operator DOES opt in, so
 *     the "dangerous" in the flag name is not just decorative.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { isRealBrowser } from "../src/index.js";
import {
  __resetBrowserWarningForTesting,
  warnRealBrowserUse,
} from "../src/_logger.js";

describe("isRealBrowser", () => {
  // Tests use the `globals` parameter to simulate runtimes without
  // mutating the real `process`/`globalThis` — `process.versions` is
  // read-only in Node so mutation doesn't work, and leaking synthetic
  // `window`/`document` into other tests would silently break
  // unrelated assertions.

  const browserGlobals = {
    window: {},
    document: {},
    navigator: { userAgent: "Mozilla/5.0 (Test)" },
  };

  it("returns false under Node (the test runtime, no args)", () => {
    // Baseline: vitest runs on Node, and a caller who passes no
    // globals must get Node's real environment → false.
    expect(isRealBrowser()).toBe(false);
  });

  it("returns true when window + document + navigator are all present", () => {
    expect(isRealBrowser(browserGlobals)).toBe(true);
  });

  it("returns false when Deno is present (even with window shim)", () => {
    expect(
      isRealBrowser({ ...browserGlobals, Deno: { version: "1.40" } }),
    ).toBe(false);
  });

  it("returns false when Bun is present", () => {
    expect(
      isRealBrowser({ ...browserGlobals, Bun: { version: "1.0" } }),
    ).toBe(false);
  });

  it("returns false when EdgeRuntime is present (Vercel Edge)", () => {
    expect(
      isRealBrowser({ ...browserGlobals, EdgeRuntime: "vercel" }),
    ).toBe(false);
  });

  it("returns false in Cloudflare Workers (WorkerGlobalScope, no window)", () => {
    // CF Workers do NOT expose `window` — so the guard skips them
    // even though they lack a `process.versions.node` signal.
    expect(
      isRealBrowser({
        WorkerGlobalScope: function WorkerGlobalScope() { },
        // no window, no process.versions.node
      }),
    ).toBe(false);
  });

  it("returns false on Node (process.versions.node present)", () => {
    // Even if some test harness has stubbed window/document into
    // globalThis, the presence of Node versions takes priority.
    expect(
      isRealBrowser({
        ...browserGlobals,
        process: { versions: { node: "20.0.0" } },
      }),
    ).toBe(false);
  });

  it("returns false when window is present but document is missing", () => {
    // JSDOM-lite test environments sometimes stub `window` for
    // compat without a real DOM — require all three signals so we
    // don't false-positive on those.
    expect(
      isRealBrowser({
        window: {},
        navigator: { userAgent: "stub" },
        // no document
      }),
    ).toBe(false);
  });

  it("returns false when navigator.userAgent is missing", () => {
    expect(
      isRealBrowser({
        window: {},
        document: {},
        navigator: {}, // no userAgent
      }),
    ).toBe(false);
  });
});

describe("warnRealBrowserUse", () => {
  let writes: string[];

  beforeEach(() => {
    __resetBrowserWarningForTesting();
    writes = [];
  });

  it("writes a banner on first call", () => {
    warnRealBrowserUse({ writeStderr: (s) => writes.push(s) });
    expect(writes.length).toBe(1);
    expect(writes[0]).toContain("dangerouslyAllowBrowser");
  });

  it("names the specific attack (signing key forgery)", () => {
    // The whole point of this warning vs. a generic "API key in
    // browser" warning — the signing key is qualitatively worse
    // because it enables forged telemetry, not just billing abuse.
    warnRealBrowserUse({ writeStderr: (s) => writes.push(s) });
    const text = writes[0] ?? "";
    expect(text).toContain("signing key");
    expect(text.toLowerCase()).toContain("forge");
  });

  it("distinguishes from plain OpenAI/Anthropic key exposure", () => {
    warnRealBrowserUse({ writeStderr: (s) => writes.push(s) });
    expect(writes[0]).toContain("NOT equivalent");
  });

  it("fires once per process by default", () => {
    warnRealBrowserUse({ writeStderr: (s) => writes.push(s) });
    warnRealBrowserUse({ writeStderr: (s) => writes.push(s) });
    expect(writes.length).toBe(1);
  });

  it("once=false bypasses the guard", () => {
    warnRealBrowserUse({
      writeStderr: (s) => writes.push(s),
      once: false,
    });
    warnRealBrowserUse({
      writeStderr: (s) => writes.push(s),
      once: false,
    });
    expect(writes.length).toBe(2);
  });

  it("includes a docs URL for safer alternatives", () => {
    warnRealBrowserUse({ writeStderr: (s) => writes.push(s) });
    expect(writes[0]).toContain("checkrd.io/docs/browser-use");
  });
});

describe("browser guard behavior", () => {
  // These exercise the guard from the `wrap`/`initPrelude` entry
  // points, but since `isRealBrowser()` returns false in the Node
  // test environment, we verify the negative case: server runtimes
  // do NOT need `dangerouslyAllowBrowser: true`. This catches the
  // regression we were specifically fixing: the previous heuristic
  // threw in CF Workers / Deno / Bun unless the flag was set.
  const originalEnv = { ...process.env };

  beforeEach(() => {
    __resetBrowserWarningForTesting();
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it("init() works without dangerouslyAllowBrowser on Node (regression)", async () => {
    // Node is NOT a real browser and the flag must not be required.
    // A regression here means we over-broaden the guard and break
    // every Node user.
    process.env.CHECKRD_DISABLED = "1";
    const { init } = await import("../src/index.js");
    expect(() => {
      init({ agentId: "test" });
    }).not.toThrow();
  });
});
