/**
 * Tests for `OtelSpanSink`. Parallel to the Python SDK's
 * `tests/test_otel_span_sink.py` — same span-attribute contract,
 * same assertions, same failure modes.
 *
 * Uses `@opentelemetry/sdk-trace-base`'s `InMemorySpanExporter` so we
 * can inspect the exact spans the sink emits. The attribute names
 * asserted here are a **public contract**: dashboards and saved
 * queries in customer observability stacks hang off them. A change
 * without a migration note is a regression.
 */
import { describe, expect, it, beforeEach } from "vitest";

import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";

import { OtelSpanSink } from "../src/_otel_span_sink.js";
import type { TelemetryEvent } from "../src/batcher.js";

function makeEvent(overrides: Record<string, unknown> = {}): TelemetryEvent {
  return {
    request_id: "req-001",
    agent_id: "test-agent",
    method: "POST",
    url_host: "api.openai.com",
    url_path: "/v1/chat/completions",
    status_code: 200,
    latency_ms: 142.5,
    policy_result: "allowed",
    span_name: "POST api.openai.com",
    span_status_code: "OK",
    ...overrides,
  };
}

describe("OtelSpanSink", () => {
  // A fresh tracer per test so spans don't leak between cases.
  // We cast the returned tracer to the sink's structural shape so
  // the internal type stays opaque to the public API.
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let tracer: ReturnType<BasicTracerProvider["getTracer"]>;

  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    // @opentelemetry/sdk-trace-base >= 2.x moved span-processor config
    // into the TracerProvider constructor. The `addSpanProcessor`
    // method was removed in the major bump — we pass the processor
    // upfront to match the current API.
    provider = new BasicTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    });
    tracer = provider.getTracer("test");
  });

  function makeSink(): OtelSpanSink {
    // Cast the OTel tracer to the sink's structural shape. The sink
    // deliberately keeps the OTel types opaque so it can be imported
    // without pulling @opentelemetry/api into the hard dep surface.

    return new OtelSpanSink({ tracer: tracer as any });
  }

  describe("basics", () => {
    it("creates exactly one span per enqueue", () => {
      const sink = makeSink();
      sink.enqueue(makeEvent());
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });

    it("uses the event's span_name when provided", () => {
      const sink = makeSink();
      sink.enqueue(makeEvent({ span_name: "POST custom.example.com" }));
      expect(exporter.getFinishedSpans()[0]?.name).toBe(
        "POST custom.example.com",
      );
    });

    it("falls back to `{METHOD} {host}` when no span_name", () => {
      // Matches OTel HTTP semconv default. Dashboards that group by
      // span name get sensible buckets without the SDK customizing.
      const sink = makeSink();
      const event = makeEvent();
      delete event.span_name;
      sink.enqueue(event);
      expect(exporter.getFinishedSpans()[0]?.name).toBe("POST api.openai.com");
    });

    it("sets span kind to CLIENT", () => {
      const sink = makeSink();
      sink.enqueue(makeEvent());
      // 3 = CLIENT. We match against the numeric constant rather
      // than importing the enum from @opentelemetry/api so the
      // test stays aligned with the sink's hard-coded value.
      expect(exporter.getFinishedSpans()[0]?.kind).toBe(3);
    });
  });

  describe("semconv attributes (public contract)", () => {
    it("stamps HTTP attributes", () => {
      const sink = makeSink();
      sink.enqueue(makeEvent());
      const attrs = exporter.getFinishedSpans()[0]?.attributes ?? {};
      expect(attrs["http.request.method"]).toBe("POST");
      expect(attrs["url.full"]).toBe(
        "https://api.openai.com/v1/chat/completions",
      );
      expect(attrs["http.response.status_code"]).toBe(200);
      expect(attrs["checkrd.latency_ms"]).toBe(142.5);
    });

    it("stamps GenAI attributes", () => {
      const sink = makeSink();
      sink.enqueue(
        makeEvent({
          // Telemetry events now use the OTel-spec field names
          // directly (URL-derived enrichment in the transport
          // produces provider/operation; body-derived extraction
          // produces model/usage when CHECKRD_EXTRACT_GENAI_BODY
          // is on).
          "gen_ai.provider.name": "openai",
          "gen_ai.operation.name": "chat",
          "gen_ai.request.model": "gpt-4o",
          "gen_ai.response.model": "gpt-4o-2024-07-18",
          "gen_ai.usage.input_tokens": 120,
          "gen_ai.usage.output_tokens": 80,
          "gen_ai.request.stream": true,
        }),
      );
      const attrs = exporter.getFinishedSpans()[0]?.attributes ?? {};
      expect(attrs["gen_ai.provider.name"]).toBe("openai");
      expect(attrs["gen_ai.operation.name"]).toBe("chat");
      expect(attrs["gen_ai.request.model"]).toBe("gpt-4o");
      expect(attrs["gen_ai.response.model"]).toBe("gpt-4o-2024-07-18");
      expect(attrs["gen_ai.usage.input_tokens"]).toBe(120);
      expect(attrs["gen_ai.usage.output_tokens"]).toBe(80);
      expect(attrs["gen_ai.request.stream"]).toBe(true);
    });

    it("stamps Checkrd namespace attributes", () => {
      const sink = makeSink();
      sink.enqueue(
        makeEvent({
          agent_id: "prod-agent-42",
          policy_result: "denied",
          deny_reason: "rate-limit-exceeded",
          matched_rule: "block-new-models",
          matched_rule_kind: "deny",
        }),
      );
      const attrs = exporter.getFinishedSpans()[0]?.attributes ?? {};
      expect(attrs["checkrd.agent_id"]).toBe("prod-agent-42");
      expect(attrs["checkrd.policy_result"]).toBe("denied");
      expect(attrs["checkrd.deny_reason"]).toBe("rate-limit-exceeded");
      expect(attrs["checkrd.matched_rule"]).toBe("block-new-models");
      expect(attrs["checkrd.matched_rule_kind"]).toBe("deny");
    });

    it("does NOT stamp missing fields as empty strings", () => {
      // Presence of `gen_ai.provider.name = ""` would corrupt
      // queries like `WHERE gen_ai.provider.name = 'openai'`.
      // Absence is meaningful.
      const sink = makeSink();
      sink.enqueue(makeEvent());
      const attrs = exporter.getFinishedSpans()[0]?.attributes ?? {};
      expect(attrs).not.toHaveProperty("gen_ai.provider.name");
      expect(attrs).not.toHaveProperty("gen_ai.request.model");
      expect(attrs).not.toHaveProperty("checkrd.deny_reason");
      expect(attrs).not.toHaveProperty("checkrd.matched_rule");
    });
  });

  describe("span status", () => {
    it("sets OK on allowed events", () => {
      const sink = makeSink();
      sink.enqueue(makeEvent({ span_status_code: "OK" }));
      // Status code 1 = OK in OTel.
      expect(exporter.getFinishedSpans()[0]?.status.code).toBe(1);
    });

    it("sets ERROR with the message on denied events", () => {
      const sink = makeSink();
      sink.enqueue(
        makeEvent({
          span_status_code: "ERROR",
          span_status_message: "rate-limit exceeded",
        }),
      );
      const status = exporter.getFinishedSpans()[0]?.status;
      expect(status?.code).toBe(2); // 2 = ERROR
      expect(status?.message).toBe("rate-limit exceeded");
    });

    it("leaves status UNSET when not provided", () => {
      const sink = makeSink();
      const event = makeEvent();
      delete event.span_status_code;
      sink.enqueue(event);
      // 0 = UNSET
      expect(exporter.getFinishedSpans()[0]?.status.code).toBe(0);
    });
  });

  describe("robustness", () => {
    it("close() is idempotent", async () => {
      const sink = makeSink();
      await sink.close();
      await sink.close(); // must not throw
    });

    it("enqueue() after close() is a no-op", async () => {
      const sink = makeSink();
      await sink.close();
      sink.enqueue(makeEvent());
      expect(exporter.getFinishedSpans()).toHaveLength(0);
    });

    it("malformed event does not raise out of enqueue()", () => {
      // Telemetry is best-effort — a bug in caller event shaping
      // cannot crash the request hot path. This matches the Python
      // sink's behavior and the OtlpSink fallback.
      const sink = makeSink();
      // `method` as a number trips attribute type validation in
      // the OTel SDK. Sink must swallow and continue.
      expect(() => {
        sink.enqueue({ method: 42, url_host: null } as unknown as TelemetryEvent);
      }).not.toThrow();
    });
  });

  describe("injection", () => {
    it("accepts an explicit tracer", () => {
      // Common case: multiple TracerProviders in the host app
      // (per-tenant, internal vs customer, etc.). The sink must
      // accept a specific tracer without touching the global.
      const customExporter = new InMemorySpanExporter();
      const customProvider = new BasicTracerProvider({
        spanProcessors: [new SimpleSpanProcessor(customExporter)],
      });
      const customTracer = customProvider.getTracer("custom");

      const sink = new OtelSpanSink({

        tracer: customTracer as any,
      });
      sink.enqueue(makeEvent());

      expect(customExporter.getFinishedSpans()).toHaveLength(1);
      // Events land on the custom provider, not the test fixture's one.
      expect(exporter.getFinishedSpans()).toHaveLength(0);
    });
  });

  describe("package advanced exports", () => {
    it("exports OtelSpanSink from checkrd/advanced", async () => {
      // Sinks live on the advanced subpath — power-user surface.
      // The main `checkrd` entry exposes only the curated set
      // (client class, init, errors, webhook helpers).
      const mod = await import("../src/advanced.js");
      expect(mod.OtelSpanSink).toBe(OtelSpanSink);
    });
  });
});
