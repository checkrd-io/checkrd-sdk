import { describe, expect, it, vi } from "vitest";

import {
  captureStreamTokens,
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
