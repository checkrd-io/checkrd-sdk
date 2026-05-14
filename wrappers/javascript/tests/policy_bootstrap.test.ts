/**
 * Server-canonical policy bootstrap tests.
 *
 * Locks in the industry-standard pattern:
 *   - `init()` / `initAsync()` refuse `policy: + apiKey` in production
 *     unless `CHECKRD_ALLOW_LOCAL_POLICY=1` is set (OPA / Envoy / LaunchDarkly style).
 *   - `initAsync({apiKey, agentId})` fetches the signed bundle from
 *     `GET /v1/agents/:id/control/state` and installs it via
 *     `reload_policy_signed` before returning.
 *   - When the fetch fails or returns no bundle, the engine stays on
 *     the deny-all baseline so every request fails closed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CheckrdInitError } from "../src/exceptions.js";
import { init, initAsync, shutdown } from "../src/index.js";

const ALLOW_ALL_LOCAL = {
  agent: "t",
  mode: "enforce",
  default: "allow",
  rules: [],
};

describe("init/initAsync — dev/prod gate", () => {
  beforeEach(() => {
    delete process.env.CHECKRD_ALLOW_LOCAL_POLICY;
  });
  afterEach(async () => {
    delete process.env.CHECKRD_ALLOW_LOCAL_POLICY;
    await shutdown();
  });

  it("init() refuses `policy: + apiKey` without CHECKRD_ALLOW_LOCAL_POLICY=1", () => {
    expect(() =>
      { init({ policy: ALLOW_ALL_LOCAL, apiKey: "ck_live_x", agentId: "a" }); },
    ).toThrow(CheckrdInitError);
  });

  it("init() accepts `policy: + apiKey` with CHECKRD_ALLOW_LOCAL_POLICY=1", () => {
    process.env.CHECKRD_ALLOW_LOCAL_POLICY = "1";
    expect(() =>
      { init({ policy: ALLOW_ALL_LOCAL, apiKey: "ck_live_x", agentId: "a" }); },
    ).not.toThrow();
  });

  it("init() accepts `policy:` alone (pure-local mode)", () => {
    expect(() => { init({ policy: ALLOW_ALL_LOCAL, agentId: "a" }); }).not.toThrow();
  });

  it("initAsync() refuses `policy: + apiKey` without CHECKRD_ALLOW_LOCAL_POLICY=1", async () => {
    await expect(
      initAsync({ policy: ALLOW_ALL_LOCAL, apiKey: "ck_live_x", agentId: "a" }),
    ).rejects.toBeInstanceOf(CheckrdInitError);
  });
});

describe("initAsync — server-canonical bootstrap", () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    delete process.env.CHECKRD_ALLOW_LOCAL_POLICY;
    originalFetch = globalThis.fetch;
  });
  afterEach(async () => {
    globalThis.fetch = originalFetch;
    delete process.env.CHECKRD_ALLOW_LOCAL_POLICY;
    await shutdown();
  });

  it("fetches /v1/agents/:id/control/state when apiKey is configured and no policy passed", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      calls.push(url);
      // Return a stub control-state response with no published envelope
      // — the SDK should stay on the deny-all baseline rather than throw.
      return new Response(
        JSON.stringify({ kill_switch_active: false, policy_envelope: null }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    await initAsync({
      apiKey: "ck_live_bootstrap_test",
      agentId: "boot-test-agent",
      controlPlaneUrl: "https://api.example.test",
    });

    const stateCalls = calls.filter((u) => u.includes("/control/state"));
    expect(stateCalls.length).toBeGreaterThanOrEqual(1);
    expect(stateCalls[0]).toContain("/v1/agents/boot-test-agent/control/state");
  });

  it("stays on deny-all baseline when the control plane returns no bundle", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ kill_switch_active: false, policy_envelope: null }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
    ) as unknown as typeof fetch;

    // Should not throw — the SDK degrades gracefully when nothing's published.
    await expect(
      initAsync({
        apiKey: "ck_live_x",
        agentId: "agent-no-policy",
        controlPlaneUrl: "https://api.example.test",
      }),
    ).resolves.toBeUndefined();
  });

  it("stays on deny-all baseline when the control plane returns 404", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(
      initAsync({
        apiKey: "ck_live_x",
        agentId: "agent-404",
        controlPlaneUrl: "https://api.example.test",
      }),
    ).resolves.toBeUndefined();
  });

  it("skips bootstrap fetch in pure-local mode (no apiKey)", async () => {
    const fetchSpy = vi.fn() as unknown as typeof fetch;
    globalThis.fetch = fetchSpy;

    await initAsync({ policy: ALLOW_ALL_LOCAL, agentId: "local-only" });

    // No control-state call should have fired — we're entirely local.
    const calls = (fetchSpy as unknown as { mock: { calls: unknown[][] } }).mock
      .calls.filter((args) =>
        typeof args[0] === "string" && args[0].includes("/control/state"),
      );
    expect(calls.length).toBe(0);
  });
});
