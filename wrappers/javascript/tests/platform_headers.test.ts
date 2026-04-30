/**
 * Platform headers (``X-Checkrd-SDK-*`` family) + ``Checkrd-Version``
 * pinning.
 *
 * Every control-plane request MUST carry this metadata so operators
 * running the Checkrd dashboard can answer questions like:
 *
 *   - "What fraction of our fleet is still on SDK < 0.3.0?"
 *   - "Do Cloudflare Workers callers see a different error rate than Node?"
 *   - "Are there callers sending an unpinned `Checkrd-Version`?"
 *
 * Matches the ``X-Stainless-*`` pattern shipped by the OpenAI and
 * Anthropic SDKs. Parallel Python test lives under
 * ``tests/test_platform_headers.py`` — when the header contract
 * changes in one, it changes in both.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { defaultControlHeaders } from "../src/_retry.js";
import {
  __resetPlatformInfoForTesting,
  platformHeaders,
  platformInfo,
} from "../src/_platform.js";
import { VERSION } from "../src/_version.js";

describe("platformInfo", () => {
  beforeEach(() => {
    __resetPlatformInfoForTesting();
  });

  it("reports language as 'javascript'", () => {
    expect(platformInfo().lang).toBe("javascript");
  });

  it("reports the package VERSION as sdkVersion", () => {
    // A regression here would cause a subtle telemetry drift: clients
    // would look like they're on a different version than npm shows.
    expect(platformInfo().sdkVersion).toBe(VERSION);
  });

  it("detects Node as the runtime under vitest", () => {
    // Vitest runs on Node, and the real process.versions.node is set.
    expect(platformInfo().runtime).toBe("node");
    expect(platformInfo().runtimeVersion).toMatch(/^\d+\.\d+\.\d+/);
  });

  it("reports a non-empty os/arch string", () => {
    expect(platformInfo().os.length).toBeGreaterThan(0);
    expect(platformInfo().arch.length).toBeGreaterThan(0);
  });

  it("memoizes the snapshot (stable reference across calls)", () => {
    // Detection is cheap but not free — must be cached so the telemetry
    // hot path doesn't pay for it on every event.
    expect(platformInfo()).toBe(platformInfo());
  });

  it("__resetPlatformInfoForTesting re-runs detection", () => {
    const first = platformInfo();
    __resetPlatformInfoForTesting();
    const second = platformInfo();
    // Different object, same values.
    expect(first).not.toBe(second);
    expect(second.sdkVersion).toBe(first.sdkVersion);
  });
});

describe("platformHeaders", () => {
  it("emits all six headers", () => {
    const h = platformHeaders();
    expect(h).toHaveProperty("X-Checkrd-SDK-Lang");
    expect(h).toHaveProperty("X-Checkrd-SDK-Version");
    expect(h).toHaveProperty("X-Checkrd-SDK-Runtime");
    expect(h).toHaveProperty("X-Checkrd-SDK-Runtime-Version");
    expect(h).toHaveProperty("X-Checkrd-SDK-OS");
    expect(h).toHaveProperty("X-Checkrd-SDK-Arch");
  });

  it("values are always strings (ingestion cannot receive 'unknown' objects)", () => {
    const h = platformHeaders();
    for (const [, value] of Object.entries(h)) {
      expect(typeof value).toBe("string");
      expect(value.length).toBeGreaterThan(0);
    }
  });

  it("accepts a synthetic info object for testing runtime-specific branches", () => {
    const h = platformHeaders({
      lang: "javascript",
      sdkVersion: "9.9.9-test",
      runtime: "workerd",
      runtimeVersion: "1.20251015.0",
      os: "linux",
      arch: "x64",
    });
    expect(h["X-Checkrd-SDK-Runtime"]).toBe("workerd");
    expect(h["X-Checkrd-SDK-Version"]).toBe("9.9.9-test");
  });
});

describe("defaultControlHeaders", () => {
  it("includes the platform headers", () => {
    const h = defaultControlHeaders("ck_test_xyz");
    expect(h["X-Checkrd-SDK-Lang"]).toBe("javascript");
    expect(h["X-Checkrd-SDK-Version"]).toBe(VERSION);
    expect(h["X-Checkrd-SDK-Runtime"]).toBe("node");
  });

  it("sets X-API-Key to the caller's key", () => {
    expect(defaultControlHeaders("ck_live_abc")["X-API-Key"]).toBe(
      "ck_live_abc",
    );
  });

  it("sets a User-Agent matching checkrd-js/<VERSION>", () => {
    expect(defaultControlHeaders("k")["User-Agent"]).toBe(
      `checkrd-js/${VERSION}`,
    );
  });

  it("generates a fresh Idempotency-Key per call", () => {
    // The key must be fresh per-call but stable within the retry loop
    // that surrounds a single request — callers manage that by
    // capturing the header set before the retry loop, exactly as
    // _key_registrar.ts does.
    const a = defaultControlHeaders("k")["Idempotency-Key"];
    const b = defaultControlHeaders("k")["Idempotency-Key"];
    expect(a).not.toBe(b);
    expect(a).toMatch(/^checkrd-[0-9a-f-]+$/);
  });

  it("omits Checkrd-Version when apiVersion is not set", () => {
    expect(defaultControlHeaders("k")).not.toHaveProperty("Checkrd-Version");
  });

  it("omits Checkrd-Version when apiVersion is an empty string", () => {
    expect(defaultControlHeaders("k", { apiVersion: "" })).not.toHaveProperty(
      "Checkrd-Version",
    );
  });

  it("stamps Checkrd-Version when apiVersion is non-empty", () => {
    expect(
      defaultControlHeaders("k", { apiVersion: "2026-04-24" })[
        "Checkrd-Version"
      ],
    ).toBe("2026-04-24");
  });
});

describe("header application at send sites", () => {
  // These are end-to-end checks that verify the telemetry batcher, key
  // registrar, and control receiver all actually stamp the platform
  // headers on real outbound requests — the helper is only useful if
  // every send site uses it.

  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    __resetPlatformInfoForTesting();
    fetchSpy = vi.fn(async () => new Response("{}", { status: 200 }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("TelemetryBatcher sends X-Checkrd-SDK-* on telemetry POST", async () => {
    const { TelemetryBatcher } = await import("../src/batcher.js");
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-1",
      // Anonymous-mode batcher: `engine` is optional and the batcher
      // sends unsigned. For this test we only care that the
      // platform headers appear, not that the body is signed.
      fetch: fetchSpy as unknown as typeof fetch,
      apiVersion: "2026-04-24",
      flushIntervalMs: 100_000, // force manual flush
      maxAttempts: 1,
    });
    batcher.start();
    batcher.enqueue({
      request_id: "r1",
      agent_id: "agent-1",
      timestamp: "2026-04-24T00:00:00Z",
      method: "GET",
      url_host: "api.openai.com",
      url_path: "/v1/x",
    });
    await batcher.flush();
    await batcher.stop();

    expect(fetchSpy).toHaveBeenCalled();
    const firstCall = fetchSpy.mock.calls[0];
    expect(firstCall).toBeDefined();
    const init = firstCall?.[1] as RequestInit | undefined;
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers).toBeDefined();
    expect(headers?.["X-Checkrd-SDK-Lang"]).toBe("javascript");
    expect(headers?.["X-Checkrd-SDK-Version"]).toBe(VERSION);
    expect(headers?.["Checkrd-Version"]).toBe("2026-04-24");
  });

  it("registerPublicKey sends X-Checkrd-SDK-* on key-register POST", async () => {
    const { registerPublicKey } = await import("../src/_key_registrar.js");
    await registerPublicKey({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-1",
      publicKey: new Uint8Array(32),
      fetch: fetchSpy as unknown as typeof fetch,
      apiVersion: "2026-04-24",
    });
    expect(fetchSpy).toHaveBeenCalled();
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit | undefined;
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers?.["X-Checkrd-SDK-Lang"]).toBe("javascript");
    expect(headers?.["Checkrd-Version"]).toBe("2026-04-24");
    expect(headers?.["X-API-Key"]).toBe("ck_test");
  });

  it("ControlReceiver sends X-Checkrd-SDK-* on the SSE subscribe GET", async () => {
    // Closing the stream immediately is the easiest way to make the
    // receiver's connect() return cleanly — we only care about the
    // request headers, not the streaming behavior (that's what
    // receiver.test.ts covers). A never-ending stream would make this
    // test hang inside parseSSE.
    fetchSpy = vi.fn(async (url: string) => {
      if (url.endsWith("/state")) {
        return new Response(JSON.stringify({ kill_switch_active: false }), {
          status: 200,
        });
      }
      // Subscribe path: return an empty (already-closed) SSE stream so
      // parseSSE yields zero events and returns.
      return new Response("", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    });

    const { ControlReceiver } = await import("../src/receiver.js");
    const receiver = new ControlReceiver({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-1",
      engine: { setKillSwitch: vi.fn(), reloadPolicy: vi.fn() },
      fetch: fetchSpy as unknown as typeof fetch,
      apiVersion: "2026-04-24",
      initialBackoffMs: 5,
    });
    receiver.start();
    // Wait for the subscribe request (as opposed to just the state poll).
    for (let i = 0; i < 100; i++) {
      const seenSubscribe = fetchSpy.mock.calls.some(
        ([url]) => typeof url === "string" && url.endsWith("/control"),
      );
      if (seenSubscribe) break;
      await new Promise((r) => setTimeout(r, 10));
    }
    await receiver.stop();

    const subscribeCall = fetchSpy.mock.calls.find(
      ([url]) => typeof url === "string" && url.endsWith("/control"),
    );
    expect(subscribeCall).toBeDefined();
    const init = subscribeCall?.[1] as RequestInit | undefined;
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers?.["X-Checkrd-SDK-Lang"]).toBe("javascript");
    expect(headers?.["Checkrd-Version"]).toBe("2026-04-24");
  });
});
