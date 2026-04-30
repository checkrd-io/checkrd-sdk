import { afterEach, describe, expect, it, vi } from "vitest";

import { TelemetryBatcher } from "../src/batcher.js";
import { resolve } from "../src/_settings.js";

function makeFetch(
  impl: (url: string, init: RequestInit) => Promise<Response>,
): typeof globalThis.fetch {
  return vi.fn(async (
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    return impl(url, init ?? {});
  }) as unknown as typeof globalThis.fetch;
}

describe("TelemetryBatcher — apiVersion header", () => {
  let batcher: TelemetryBatcher | null = null;
  afterEach(async () => { await batcher?.stop(); batcher = null; });

  it("stamps Checkrd-Version when apiVersion is set", async () => {
    const calls: Record<string, string>[] = [];
    const fetchImpl = makeFetch(async (_url, init) => {
      calls.push(init.headers as Record<string, string>);
      return new Response("", { status: 200 });
    });
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "k",
      agentId: "a",
      fetch: fetchImpl,
      batchSize: 1,
      flushIntervalMs: 10_000,
      apiVersion: "2026-04-01",
    });
    batcher.start();
    batcher.enqueue({ n: 1 });
    await batcher.flush();
    expect(calls[0]!["Checkrd-Version"]).toBe("2026-04-01");
  });

  it("omits Checkrd-Version when apiVersion is absent", async () => {
    const calls: Record<string, string>[] = [];
    const fetchImpl = makeFetch(async (_url, init) => {
      calls.push(init.headers as Record<string, string>);
      return new Response("", { status: 200 });
    });
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "k",
      agentId: "a",
      fetch: fetchImpl,
      batchSize: 1,
      flushIntervalMs: 10_000,
    });
    batcher.start();
    batcher.enqueue({ n: 1 });
    await batcher.flush();
    expect(calls[0]).not.toHaveProperty("Checkrd-Version");
  });
});

describe("TelemetryBatcher — sampling", () => {
  it("drops allowed events at sampling rate 0", () => {
    const fetchImpl = makeFetch(async () => new Response("", { status: 200 }));
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "k",
      agentId: "a",
      fetch: fetchImpl,
      samplingRate: 0,
    });
    batcher.start();
    for (let i = 0; i < 10; i++) batcher.enqueue({ allowed: true, n: i });
    const diag = batcher.diagnostics();
    expect(diag.pending).toBe(0);
    expect(diag.droppedSampled).toBe(10);
    void batcher.stop();
  });

  it("never samples denied events even at rate 0", () => {
    const fetchImpl = makeFetch(async () => new Response("", { status: 200 }));
    const batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "k",
      agentId: "a",
      fetch: fetchImpl,
      samplingRate: 0,
      batchSize: 100,
      flushIntervalMs: 10_000,
    });
    batcher.start();
    batcher.enqueue({ allowed: false, deny_reason: "blocked", n: 1 });
    batcher.enqueue({ deny_reason: "rate_limit", n: 2 });
    const diag = batcher.diagnostics();
    expect(diag.pending).toBe(2);
    expect(diag.droppedSampled).toBe(0);
    void batcher.stop();
  });
});

describe("Settings — environment + apiVersion + samplingRate", () => {
  it("environment=production maps to api.checkrd.io", () => {
    const s = resolve({ environment: "production", apiKey: "k" });
    expect(s.controlPlaneUrl).toBe("https://api.checkrd.io");
    expect(s.environment).toBe("production");
  });

  it("explicit controlPlaneUrl overrides environment", () => {
    const s = resolve({
      environment: "production",
      controlPlaneUrl: "https://my.custom.host",
      apiKey: "k",
    });
    expect(s.controlPlaneUrl).toBe("https://my.custom.host");
  });

  it("samplingRate clamps to [0,1]", () => {
    expect(resolve({ samplingRate: -5 }).samplingRate).toBe(0);
    expect(resolve({ samplingRate: 2 }).samplingRate).toBe(1);
    expect(resolve({ samplingRate: 0.25 }).samplingRate).toBe(0.25);
    expect(resolve({}).samplingRate).toBe(1);
  });

  it("apiVersion flows through to settings", () => {
    expect(resolve({ apiVersion: "2026-04-01" }).apiVersion).toBe("2026-04-01");
    expect(resolve({}).apiVersion).toBe("");
  });
});
