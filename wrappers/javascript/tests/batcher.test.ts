import { afterEach, describe, expect, it, vi } from "vitest";

import { TelemetryBatcher } from "../src/batcher.js";

function makeFetch(
  impl: (url: string, init: RequestInit) => Promise<Response>,
): typeof globalThis.fetch {
  return vi.fn(async (
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    const url = typeof input === "string"
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url;
    return impl(url, init ?? {});
  }) as unknown as typeof globalThis.fetch;
}

describe("TelemetryBatcher", () => {
  let batcher: TelemetryBatcher | null = null;

  afterEach(async () => {
    await batcher?.stop();
    batcher = null;
  });

  it("enqueues events and flushes at batchSize", async () => {
    const calls: { body: string; headers: HeadersInit | undefined }[] = [];
    const fetchImpl = makeFetch(async (_url, init) => {
      calls.push({ body: init.body as string, headers: init.headers });
      return new Response("{}", { status: 200 });
    });
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      fetch: fetchImpl,
      batchSize: 3,
      flushIntervalMs: 10_000,
    });
    batcher.start();
    batcher.enqueue({ n: 1 });
    batcher.enqueue({ n: 2 });
    batcher.enqueue({ n: 3 });
    await new Promise((r) => setTimeout(r, 10));
    await batcher.flush();
    expect(calls).toHaveLength(1);
    const events = JSON.parse(calls[0]!.body) as { n: number }[];
    expect(events).toHaveLength(3);
  });

  it("stamps every POST with a fresh Idempotency-Key and API key", async () => {
    const capturedHeaders: Record<string, string>[] = [];
    const fetchImpl = makeFetch(async (_url, init) => {
      const hdrs = init.headers as Record<string, string>;
      capturedHeaders.push(hdrs);
      return new Response("", { status: 200 });
    });
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      fetch: fetchImpl,
      batchSize: 1,
      flushIntervalMs: 10_000,
    });
    batcher.start();
    batcher.enqueue({ n: 1 });
    await batcher.flush();
    batcher.enqueue({ n: 2 });
    await batcher.flush();
    expect(capturedHeaders).toHaveLength(2);
    expect(capturedHeaders[0]!["X-API-Key"]).toBe("ck_live_xyz");
    expect(capturedHeaders[0]!["Idempotency-Key"]).not.toBe(
      capturedHeaders[1]!["Idempotency-Key"],
    );
  });

  it("drops events past maxQueueSize and records the counter", () => {
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      fetch: makeFetch(async () => new Response("", { status: 200 })),
      batchSize: 1000, // high so we don't auto-flush
      maxQueueSize: 2,
      flushIntervalMs: 10_000,
    });
    batcher.start();
    batcher.enqueue({ n: 1 });
    batcher.enqueue({ n: 2 });
    batcher.enqueue({ n: 3 });
    batcher.enqueue({ n: 4 });
    const diag = batcher.diagnostics();
    expect(diag.pending).toBe(2);
    expect(diag.droppedBackpressure).toBe(2);
  });

  it("counts failed sends under droppedSendError", async () => {
    const fetchImpl = makeFetch(async () => new Response("boom", { status: 500 }));
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      fetch: fetchImpl,
      batchSize: 1,
      maxAttempts: 1,
      flushIntervalMs: 10_000,
    });
    batcher.start();
    batcher.enqueue({ n: 1 });
    await batcher.flush();
    expect(batcher.diagnostics().droppedSendError).toBe(1);
    expect(batcher.diagnostics().sent).toBe(0);
  });

  it("emits a throttled warn on backpressure drop and suppresses within window", () => {
    const warnCalls: { msg: string; payload: unknown }[] = [];
    const logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn((msg: string, payload?: unknown) => {
        warnCalls.push({ msg, payload });
      }),
      error: vi.fn(),
    };
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      fetch: makeFetch(async () => new Response("", { status: 200 })),
      batchSize: 1000,
      maxQueueSize: 1,
      flushIntervalMs: 10_000,
      logger,
      backpressureWarnIntervalMs: 60_000,
    });
    batcher.start();
    batcher.enqueue({ n: 1 }); // accepted
    batcher.enqueue({ n: 2 }); // dropped — first warn
    batcher.enqueue({ n: 3 }); // dropped — suppressed
    batcher.enqueue({ n: 4 }); // dropped — suppressed
    expect(warnCalls).toHaveLength(1);
    expect(warnCalls[0]!.msg).toMatch(/backpressure/i);
    expect(warnCalls[0]!.payload).toMatchObject({
      droppedBackpressure: 1,
      maxQueueSize: 1,
    });
    expect(batcher.diagnostics().droppedBackpressure).toBe(3);
  });

  it("re-emits the backpressure warn after the throttle window elapses", () => {
    const warnCalls: string[] = [];
    const logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn((msg: string) => {
        warnCalls.push(msg);
      }),
      error: vi.fn(),
    };
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      fetch: makeFetch(async () => new Response("", { status: 200 })),
      batchSize: 1000,
      maxQueueSize: 1,
      flushIntervalMs: 10_000,
      logger,
      backpressureWarnIntervalMs: 1, // 1 ms window
    });
    batcher.start();
    batcher.enqueue({ n: 1 }); // accepted
    batcher.enqueue({ n: 2 }); // first drop → warn
    expect(warnCalls).toHaveLength(1);
    // Wait past the 1 ms window so the next drop re-emits.
    return new Promise<void>((r) => setTimeout(r, 5)).then(() => {
      batcher!.enqueue({ n: 3 }); // second drop → warn again (window elapsed)
      expect(warnCalls).toHaveLength(2);
    });
  });

  it("stop() drains the queue before resolving", async () => {
    const calls: string[] = [];
    const fetchImpl = makeFetch(async (_url, init) => {
      calls.push(init.body as string);
      return new Response("", { status: 200 });
    });
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_live_xyz",
      agentId: "agent-1",
      fetch: fetchImpl,
      batchSize: 1000,
      flushIntervalMs: 10_000,
      shutdownTimeoutMs: 2000,
    });
    batcher.start();
    batcher.enqueue({ n: 1 });
    batcher.enqueue({ n: 2 });
    await batcher.stop();
    expect(calls).toHaveLength(1);
    expect(JSON.parse(calls[0]!)).toHaveLength(2);
    batcher = null;
  });
});
