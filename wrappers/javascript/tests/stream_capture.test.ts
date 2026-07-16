import { describe, expect, it, vi } from "vitest";

import {
  captureStreamTokens,
  captureUsageFromFrames,
  teeResponseForTokens,
  vendorForUrl,
  type StreamVendor,
} from "../src/_stream_capture.js";
import type { TelemetrySink } from "../src/sinks.js";

function streamFromString(text: string): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

function makeSink(): TelemetrySink & { calls: Record<string, unknown>[] } {
  const calls: Record<string, unknown>[] = [];
  return {
    calls,
    enqueue: (event) => { calls.push(event); },
    close: async () => undefined,
  };
}

describe("vendorForUrl", () => {
  it("classifies OpenAI URLs", () => {
    expect(vendorForUrl("https://api.openai.com/v1/chat/completions")).toBe("openai");
  });
  it("classifies Azure OpenAI URLs", () => {
    expect(vendorForUrl("https://foo.openai.azure.com/deployments/gpt-4o/chat")).toBe("openai");
  });
  it("classifies Anthropic URLs", () => {
    expect(vendorForUrl("https://api.anthropic.com/v1/messages")).toBe("anthropic");
  });
  it("returns 'unknown' for other hosts", () => {
    expect(vendorForUrl("https://example.com/foo")).toBe("unknown");
  });
});

describe("captureStreamTokens — OpenAI", () => {
  it("extracts usage from the usage-bearing chunk", async () => {
    const body =
      `data: {"id":"1","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n` +
      `data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n` +
      `data: {"usage":{"prompt_tokens":12,"completion_tokens":7}}\n\n` +
      `data: [DONE]\n\n`;
    const sink = makeSink();
    await captureStreamTokens(streamFromString(body), {
      vendor: "openai",
      requestId: "req-1",
      url: "https://api.openai.com/v1/chat/completions",
      method: "POST",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    });
    expect(sink.calls).toHaveLength(1);
    expect(sink.calls[0]!["input_tokens"]).toBe(12);
    expect(sink.calls[0]!["output_tokens"]).toBe(7);
    expect(sink.calls[0]!["finish_reason"]).toBe("stop");
  });
});

describe("captureStreamTokens — Anthropic", () => {
  it("extracts input_tokens from message_start and output_tokens from final message_delta", async () => {
    const body =
      `event: message_start\ndata: {"message":{"usage":{"input_tokens":9}}}\n\n` +
      `event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"hi"}}\n\n` +
      `event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n` +
      `event: message_stop\ndata: {}\n\n`;
    const sink = makeSink();
    await captureStreamTokens(streamFromString(body), {
      vendor: "anthropic",
      requestId: "req-2",
      url: "https://api.anthropic.com/v1/messages",
      method: "POST",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    });
    expect(sink.calls[0]!["input_tokens"]).toBe(9);
    expect(sink.calls[0]!["output_tokens"]).toBe(4);
    expect(sink.calls[0]!["finish_reason"]).toBe("end_turn");
  });
});

describe("teeResponseForTokens", () => {
  it("passes non-SSE responses through unchanged", () => {
    const res = new Response("plain", {
      status: 200,
      headers: { "content-type": "text/plain" },
    });
    const sink = makeSink();
    const out = teeResponseForTokens(res, {
      vendor: "openai" as StreamVendor,
      requestId: "req-3",
      url: "https://api.openai.com/v1/whatever",
      method: "GET",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    });
    expect(out).toBe(res);
  });

  it("returns a new Response that preserves headers + status for SSE", async () => {
    const encoder = new TextEncoder();
    const sse = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode("data: {\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}\n\ndata: [DONE]\n\n"));
          controller.close();
        },
      }),
      {
        status: 200,
        headers: { "content-type": "text/event-stream", "x-request-id": "abc" },
      },
    );
    const sink = makeSink();
    const out = teeResponseForTokens(sse, {
      vendor: "openai",
      requestId: "req-4",
      url: "https://api.openai.com/v1/chat/completions",
      method: "POST",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    });
    expect(out.headers.get("x-request-id")).toBe("abc");
    // Drain the consumer side to trigger the tee, then wait for the
    // background capture to enqueue the event.
    await out.text();
    // Give the background `captureStreamTokens` promise a tick to settle.
    for (let i = 0; i < 20; i++) {
      if (sink.calls.length > 0) break;
      await new Promise((r) => setTimeout(r, 10));
    }
    expect(sink.calls[0]!["input_tokens"]).toBe(3);
    expect(sink.calls[0]!["output_tokens"]).toBe(2);
  });

  it("never throws when the logger is undefined and the stream is malformed", () => {
    const encoder = new TextEncoder();
    const res = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode("not a real SSE"));
          controller.close();
        },
      }),
      {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      },
    );
    const sink = makeSink();
    expect(() => teeResponseForTokens(res, {
      vendor: "openai",
      requestId: "req-5",
      url: "https://api.openai.com/v1/chat",
      method: "POST",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    })).not.toThrow();
    // suppress unused-var lint noise
    void vi;
  });
});

// ---------------------------------------------------------------------------
// M-11: cache/reasoning detail counters + untallied-on-abandonment, on
// the LIVE `captureStreamTokens` path (the pure seam is covered by
// `stream_capture_fixtures.test.ts` / `stream_capture_properties.test.ts`).
// ---------------------------------------------------------------------------

describe("captureStreamTokens — M-11 OTel usage attrs", () => {
  it("OpenAI: emits cache_read + reasoning detail counters (native inclusive)", async () => {
    const body =
      `data: {"choices":[{"delta":{"content":"hi"}}]}\n\n` +
      `data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":500,"prompt_tokens_details":{"cached_tokens":800},"completion_tokens_details":{"reasoning_tokens":200}}}\n\n` +
      `data: [DONE]\n\n`;
    const sink = makeSink();
    await captureStreamTokens(streamFromString(body), {
      vendor: "openai",
      requestId: "req-m11-oa",
      url: "https://api.openai.com/v1/chat/completions",
      method: "POST",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    });
    const ev = sink.calls[0]!;
    expect(ev["input_tokens"]).toBe(1000);
    expect(ev["output_tokens"]).toBe(500);
    expect(ev["gen_ai.usage.input_tokens"]).toBe(1000);
    expect(ev["gen_ai.usage.output_tokens"]).toBe(500);
    expect(ev["gen_ai.usage.cache_read.input_tokens"]).toBe(800);
    expect(ev["gen_ai.usage.reasoning.output_tokens"]).toBe(200);
    expect(ev["pricing_status"]).toBeUndefined();
  });

  it("Anthropic: normalizes input to inclusive (300+1000+200=1500) and emits cache attrs", async () => {
    const body =
      `event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":300,"output_tokens":1,"cache_read_input_tokens":1000,"cache_creation_input_tokens":200}}}\n\n` +
      `event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n` +
      `event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":350}}\n\n` +
      `event: message_stop\ndata: {"type":"message_stop"}\n\n`;
    const sink = makeSink();
    await captureStreamTokens(streamFromString(body), {
      vendor: "anthropic",
      requestId: "req-m11-an",
      url: "https://api.anthropic.com/v1/messages",
      method: "POST",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    });
    const ev = sink.calls[0]!;
    // Flat field mirrors the inclusive (billed) total, not the raw 300.
    expect(ev["input_tokens"]).toBe(1500);
    expect(ev["output_tokens"]).toBe(350);
    expect(ev["gen_ai.usage.input_tokens"]).toBe(1500);
    expect(ev["gen_ai.usage.output_tokens"]).toBe(350);
    expect(ev["gen_ai.usage.cache_read.input_tokens"]).toBe(1000);
    expect(ev["gen_ai.usage.cache_creation.input_tokens"]).toBe(200);
  });
});

describe("captureStreamTokens — M-11 untallied on abandonment", () => {
  it("OpenAI: stream ends before include_usage frame => untallied, null usage, no attrs", async () => {
    // DELIBERATE behavior change: the old tap emitted whatever partial
    // usage it had. The engine never estimates (TDD §4.2), so an
    // abandoned stream now yields null usage + pricing_status=untallied.
    const body =
      `data: {"choices":[{"delta":{"content":"par"}}]}\n\n` +
      `data: {"choices":[{"delta":{"content":"tial"}}]}\n\n`;
    const sink = makeSink();
    await captureStreamTokens(streamFromString(body), {
      vendor: "openai",
      requestId: "req-m11-abandon-oa",
      url: "https://api.openai.com/v1/chat/completions",
      method: "POST",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    });
    const ev = sink.calls[0]!;
    expect(ev["pricing_status"]).toBe("untallied");
    expect(ev["input_tokens"]).toBeNull();
    expect(ev["output_tokens"]).toBeNull();
    expect(ev["gen_ai.usage.input_tokens"]).toBeUndefined();
  });

  it("Anthropic: message_start but no terminal message_delta => untallied (input not leaked)", async () => {
    const body =
      `event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":300,"output_tokens":1}}}\n\n` +
      `event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"par"}}\n\n`;
    const sink = makeSink();
    await captureStreamTokens(streamFromString(body), {
      vendor: "anthropic",
      requestId: "req-m11-abandon-an",
      url: "https://api.anthropic.com/v1/messages",
      method: "POST",
      agentId: "agent-1",
      sink,
      startMs: Date.now(),
    });
    const ev = sink.calls[0]!;
    expect(ev["pricing_status"]).toBe("untallied");
    expect(ev["input_tokens"]).toBeNull();
    expect(ev["gen_ai.usage.input_tokens"]).toBeUndefined();
  });

  it("captureUsageFromFrames mirrors the live path for the abandoned OpenAI shape", () => {
    const r = captureUsageFromFrames(
      "openai",
      [`data: {"choices":[{"delta":{"content":"x"}}]}\n\n`],
      true,
    );
    expect(r.usageAttrs).toEqual({});
    expect(r.pricingStatus).toBe("untallied");
  });
});
