/**
 * Tests for the OTLP/HTTP-JSON sink.
 *
 * Two layers are exercised: the {@link eventsToOtlpJson} translator (a
 * pure function — easy to test exhaustively) and the {@link OtlpSink}
 * lifecycle (batching, flush triggers, network failure isolation).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { eventsToOtlpJson, OtlpSink } from "../src/_otlp.js";

interface OtlpPayload {
  resourceSpans: Array<{
    resource: { attributes: Array<{ key: string; value: Record<string, unknown> }> };
    schemaUrl?: string;
    scopeSpans: Array<{
      scope: { name: string };
      schemaUrl?: string;
      spans: Array<{
        traceId: string;
        spanId: string;
        name: string;
        kind: number;
        startTimeUnixNano: string;
        endTimeUnixNano: string;
        attributes: Array<{ key: string; value: Record<string, unknown> }>;
        status: { code: number; message?: string };
      }>;
    }>;
  }>;
}

function parsePayload(json: string): OtlpPayload {
  return JSON.parse(json) as OtlpPayload;
}

describe("eventsToOtlpJson", () => {
  it("emits a single resourceSpans envelope with service.name", () => {
    const json = eventsToOtlpJson([], "my-service");
    const payload = parsePayload(json);
    expect(payload.resourceSpans).toHaveLength(1);
    const attrs = payload.resourceSpans[0]!.resource.attributes;
    const serviceName = attrs.find((a) => a.key === "service.name");
    expect(serviceName?.value).toEqual({ stringValue: "my-service" });
  });

  it("pins the GenAI semconv version via schema_url (RFC: switch-over)", () => {
    const payload = parsePayload(eventsToOtlpJson([], "svc"));
    const rs = payload.resourceSpans[0]!;
    // Pinned EXACTLY to 1.41.0 — the version whose attribute names we
    // emit (`gen_ai.provider.name`, not the deprecated `gen_ai.system`).
    expect(rs.schemaUrl).toBe("https://opentelemetry.io/schemas/1.41.0");
    expect(rs.scopeSpans[0]!.schemaUrl).toBe(rs.schemaUrl);
  });

  it("translates HTTP semantic-convention attributes", () => {
    const json = eventsToOtlpJson(
      [
        {
          method: "POST",
          url_host: "api.openai.com",
          url_path: "/v1/chat/completions",
          status_code: 200,
          latency_ms: 123.4,
          timestamp_ms: 1_700_000_000_000,
          request_id: "abc123",
        },
      ],
      "checkrd",
    );
    const span = parsePayload(json).resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    expect(span.kind).toBe(3); // SPAN_KIND_CLIENT

    const findAttr = (k: string) => span.attributes.find((a) => a.key === k);
    expect(findAttr("http.request.method")?.value).toEqual({ stringValue: "POST" });
    expect(findAttr("url.full")?.value).toEqual({
      stringValue: "https://api.openai.com/v1/chat/completions",
    });
    expect(findAttr("http.response.status_code")?.value).toEqual({ intValue: "200" });
    expect(findAttr("checkrd.latency_ms")?.value).toEqual({ doubleValue: 123.4 });
  });

  it("translates GenAI attributes from modern dotted event keys", () => {
    // Transport / body-extractor path: events carry the latest
    // (semconv 1.41.x) dotted keys. They map straight through.
    const json = eventsToOtlpJson(
      [
        {
          method: "POST",
          "gen_ai.provider.name": "openai",
          "gen_ai.operation.name": "chat",
          "gen_ai.request.model": "gpt-4o",
          "gen_ai.response.model": "gpt-4o-2024-08-06",
          "gen_ai.usage.input_tokens": 250,
          "gen_ai.usage.output_tokens": 500,
          "gen_ai.request.stream": true,
        },
      ],
      "checkrd",
    );
    const span = parsePayload(json).resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    const findAttr = (k: string) => span.attributes.find((a) => a.key === k);
    // Emits `gen_ai.provider.name`, NOT the deprecated `gen_ai.system`.
    expect(findAttr("gen_ai.provider.name")?.value).toEqual({ stringValue: "openai" });
    expect(findAttr("gen_ai.system")).toBeUndefined();
    expect(findAttr("gen_ai.operation.name")?.value).toEqual({ stringValue: "chat" });
    expect(findAttr("gen_ai.request.model")?.value).toEqual({ stringValue: "gpt-4o" });
    expect(findAttr("gen_ai.response.model")?.value).toEqual({
      stringValue: "gpt-4o-2024-08-06",
    });
    expect(findAttr("gen_ai.usage.input_tokens")?.value).toEqual({ intValue: "250" });
    expect(findAttr("gen_ai.usage.output_tokens")?.value).toEqual({ intValue: "500" });
    expect(findAttr("gen_ai.request.stream")?.value).toEqual({ boolValue: true });
  });

  it("translates GenAI attributes from flat wire-schema keys (adapter path)", () => {
    // Framework adapters (Vercel AI SDK, LangChain, OpenAI Agents) emit
    // the flat `TelemetryEventInput` keys. They must still surface the
    // modern `gen_ai.provider.name` attribute — never `gen_ai.system`.
    const json = eventsToOtlpJson(
      [
        {
          method: "POST",
          gen_ai_system: "openai",
          gen_ai_model: "gpt-4o",
          gen_ai_input_tokens: 250,
          gen_ai_output_tokens: 500,
        },
      ],
      "checkrd",
    );
    const span = parsePayload(json).resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    const findAttr = (k: string) => span.attributes.find((a) => a.key === k);
    expect(findAttr("gen_ai.provider.name")?.value).toEqual({ stringValue: "openai" });
    expect(findAttr("gen_ai.system")).toBeUndefined();
    expect(findAttr("gen_ai.request.model")?.value).toEqual({ stringValue: "gpt-4o" });
    expect(findAttr("gen_ai.usage.input_tokens")?.value).toEqual({ intValue: "250" });
    expect(findAttr("gen_ai.usage.output_tokens")?.value).toEqual({ intValue: "500" });
  });

  it("translates Checkrd-specific attributes", () => {
    const json = eventsToOtlpJson(
      [
        {
          agent_id: "sales-agent",
          policy_result: "deny",
          deny_reason: "outside business hours",
        },
      ],
      "checkrd",
    );
    const span = parsePayload(json).resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    const findAttr = (k: string) => span.attributes.find((a) => a.key === k);
    expect(findAttr("checkrd.agent_id")?.value).toEqual({ stringValue: "sales-agent" });
    expect(findAttr("checkrd.policy_result")?.value).toEqual({ stringValue: "deny" });
    expect(findAttr("checkrd.deny_reason")?.value).toEqual({
      stringValue: "outside business hours",
    });
  });

  it("maps span_status_code to the OTLP status enum", () => {
    const allOk = parsePayload(
      eventsToOtlpJson([{ span_status_code: "OK" }], "x"),
    );
    expect(allOk.resourceSpans[0]!.scopeSpans[0]!.spans[0]!.status.code).toBe(1);

    const error = parsePayload(
      eventsToOtlpJson(
        [{ span_status_code: "ERROR", span_status_message: "policy denied" }],
        "x",
      ),
    );
    const errStatus = error.resourceSpans[0]!.scopeSpans[0]!.spans[0]!.status;
    expect(errStatus.code).toBe(2);
    expect(errStatus.message).toBe("policy denied");

    const unset = parsePayload(eventsToOtlpJson([{}], "x"));
    expect(unset.resourceSpans[0]!.scopeSpans[0]!.spans[0]!.status.code).toBe(0);
  });

  it("derives a 32-hex traceId from request_id when present", () => {
    const json = eventsToOtlpJson(
      [{ request_id: "11112222-3333-4444-5555-666677778888" }],
      "x",
    );
    const span = parsePayload(json).resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    expect(span.traceId).toMatch(/^[0-9a-f]{32}$/);
    expect(span.traceId).toContain("11112222");
  });

  it("falls back to a random 32-hex traceId when request_id is missing", () => {
    const json = eventsToOtlpJson([{ method: "GET" }], "x");
    const span = parsePayload(json).resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    expect(span.traceId).toMatch(/^[0-9a-f]{32}$/);
  });

  it("converts timestamps to nanoseconds as strings", () => {
    const json = eventsToOtlpJson(
      [{ timestamp_ms: 1_700_000_000_000, latency_ms: 500 }],
      "x",
    );
    const span = parsePayload(json).resourceSpans[0]!.scopeSpans[0]!.spans[0]!;
    expect(span.startTimeUnixNano).toBe("1700000000000000000");
    // 1_700_000_000_000 ms + 500 ms = 1_700_000_000_500 ms
    expect(span.endTimeUnixNano).toBe("1700000000500000000");
  });
});

describe("OtlpSink", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("normalises bare endpoints by appending /v1/traces", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    sink.enqueue({ method: "GET" });
    await sink.flush();
    const call = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("https://otlp.example.com/v1/traces");
    await sink.close();
  });

  it("does not duplicate /v1/traces when the caller already includes it", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com/v1/traces",
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    sink.enqueue({});
    await sink.flush();
    const call = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("https://otlp.example.com/v1/traces");
    await sink.close();
  });

  it("forwards headers passed by the caller (auth tokens, dataset names)", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      headers: { "x-honeycomb-team": "secret", "x-honeycomb-dataset": "checkrd" },
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    sink.enqueue({});
    await sink.flush();
    const call = fetch.mock.calls[0] as unknown as [string, RequestInit];
    const headers = call[1].headers as Record<string, string>;
    expect(headers["x-honeycomb-team"]).toBe("secret");
    expect(headers["x-honeycomb-dataset"]).toBe("checkrd");
    expect(headers["Content-Type"]).toBe("application/json");
    await sink.close();
  });

  it("flushes when the buffer reaches maxBatchSize", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      maxBatchSize: 3,
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    sink.enqueue({ method: "A" });
    sink.enqueue({ method: "B" });
    expect(fetch).not.toHaveBeenCalled();
    sink.enqueue({ method: "C" }); // triggers flush
    // The flush is fired async — let microtasks resolve.
    await vi.waitFor(() => {
      expect(fetch).toHaveBeenCalledTimes(1);
    });
    await sink.close();
  });

  it("does not crash on transient network failures", async () => {
    // Real timers for this test — `doFlush` delegates to `fetchWithRetry`,
    // whose exponential-backoff sleeps are setTimeout-based and hang
    // under fake timers.
    vi.useRealTimers();
    const fetch = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const log = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      fetch: fetch as unknown as typeof globalThis.fetch,
      logger: log,
    });
    sink.enqueue({});
    await sink.flush();
    // failure surfaces only via logger; never throws
    expect(log.warn).toHaveBeenCalled();
    await sink.close();
  }, 15_000);

  it("does not enqueue after close()", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    await sink.close();
    sink.enqueue({ method: "POST" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("close() flushes pending events", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    sink.enqueue({ method: "GET" });
    sink.enqueue({ method: "POST" });
    expect(fetch).not.toHaveBeenCalled();
    await sink.close();
    expect(fetch).toHaveBeenCalledOnce();
  });

  // ---------------------------------------------------------------------------
  // Metrics flush (M-15): a GenAI event drives a second POST to /v1/metrics
  // alongside the spans POST to /v1/traces.
  // ---------------------------------------------------------------------------

  it("POSTs GenAI metrics to /v1/metrics alongside spans on /v1/traces", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    sink.enqueue({
      method: "POST",
      "gen_ai.provider.name": "openai",
      "gen_ai.operation.name": "chat",
      "gen_ai.request.model": "gpt-4o",
      "gen_ai.usage.input_tokens": 1000,
      "gen_ai.usage.output_tokens": 500,
      latency_ms: 1200,
    });
    await sink.flush();

    const calls = fetch.mock.calls as unknown as [string, RequestInit][];
    expect(calls).toHaveLength(2);
    const urls = calls.map((c) => c[0]);
    expect(urls).toContain("https://otlp.example.com/v1/traces");
    expect(urls).toContain("https://otlp.example.com/v1/metrics");

    // The metrics body carries the two GenAI histogram instruments.
    const metricsCall = calls.find((c) => c[0].endsWith("/v1/metrics"));
    const body = JSON.parse(metricsCall![1].body as string) as {
      resourceMetrics: {
        scopeMetrics: { metrics: { name: string; unit: string }[] }[];
      }[];
    };
    const metrics = body.resourceMetrics[0]!.scopeMetrics[0]!.metrics;
    const names = metrics.map((m) => m.name).sort();
    expect(names).toEqual([
      "gen_ai.client.operation.duration",
      "gen_ai.client.token.usage",
    ]);
    await sink.close();
  });

  it("derives the /v1/metrics endpoint when the caller pins /v1/traces", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://api.honeycomb.io/v1/traces",
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    sink.enqueue({ "gen_ai.provider.name": "openai", latency_ms: 300 });
    await sink.flush();
    const urls = (fetch.mock.calls as unknown as [string, RequestInit][]).map((c) => c[0]);
    // The traces URL is respected verbatim; the metrics URL swaps the signal.
    expect(urls).toContain("https://api.honeycomb.io/v1/traces");
    expect(urls).toContain("https://api.honeycomb.io/v1/metrics");
    await sink.close();
  });

  it("re-exports cumulative metric totals across flushes (CUMULATIVE temporality)", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const sink = new OtlpSink({
      endpoint: "https://otlp.example.com",
      fetch: fetch as unknown as typeof globalThis.fetch,
    });
    const genaiEvent = {
      "gen_ai.provider.name": "openai",
      "gen_ai.operation.name": "chat",
      "gen_ai.request.model": "gpt-4o",
      "gen_ai.usage.input_tokens": 10,
    };
    sink.enqueue({ ...genaiEvent });
    await sink.flush();
    sink.enqueue({ ...genaiEvent });
    await sink.flush();

    // Grab the LAST metrics POST — its input series must reflect BOTH events
    // (count 2, sum 20), proving the accumulator is not reset between flushes.
    const calls = fetch.mock.calls as unknown as [string, RequestInit][];
    const metricsBodies = calls
      .filter((c) => c[0].endsWith("/v1/metrics"))
      .map((c) => JSON.parse(c[1].body as string) as {
        resourceMetrics: {
          scopeMetrics: {
            metrics: {
              name: string;
              histogram: {
                dataPoints: {
                  attributes: { key: string; value: { stringValue: string } }[];
                  count: string;
                  sum: number;
                }[];
              };
            }[];
          }[];
        }[];
      });
    expect(metricsBodies.length).toBeGreaterThanOrEqual(2);
    const lastBody = metricsBodies[metricsBodies.length - 1]!;
    const tokenMetric = lastBody.resourceMetrics[0]!.scopeMetrics[0]!.metrics.find(
      (m) => m.name === "gen_ai.client.token.usage",
    )!;
    const inputPoint = tokenMetric.histogram.dataPoints.find((dp) =>
      dp.attributes.some(
        (a) => a.key === "gen_ai.token.type" && a.value.stringValue === "input",
      ),
    )!;
    expect(Number(inputPoint.count)).toBe(2);
    expect(inputPoint.sum).toBe(20);
    await sink.close();
  });
});
