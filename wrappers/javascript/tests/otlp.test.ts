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
    scopeSpans: Array<{
      scope: { name: string };
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

  it("translates GenAI semantic-convention attributes when present", () => {
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
    expect(findAttr("gen_ai.system")?.value).toEqual({ stringValue: "openai" });
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
});
