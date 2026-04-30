import { describe, expect, it, vi } from "vitest";

import { checkrdMiddleware } from "../src/integrations/_ai_sdk.js";
import { CheckrdPolicyDenied } from "../src/exceptions.js";
import { WasmEngine } from "../src/engine.js";
import type { TelemetrySink } from "../src/sinks.js";

const ALLOW_ALL = JSON.stringify({ agent: "t", default: "allow", rules: [] });
const DENY_ALL = JSON.stringify({ agent: "t", default: "deny", rules: [] });

function makeSink(): TelemetrySink & { calls: Record<string, unknown>[] } {
  const calls: Record<string, unknown>[] = [];
  return {
    calls,
    enqueue: (e) => { calls.push(e); },
    close: async () => undefined,
  };
}

describe("checkrdMiddleware — wrapGenerate", () => {
  it("calls the underlying model when policy allows", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "agent-1");
    const sink = makeSink();
    const mw = checkrdMiddleware({
      engine,
      enforce: true,
      agentId: "agent-1",
      sink,
    });
    const doGenerate = vi.fn(async () => ({ text: "hi", usage: { inputTokens: 3, outputTokens: 5 }, finishReason: "stop" }));
    const result = await mw.wrapGenerate!({
      doGenerate,
      params: { prompt: "hello" },
      model: { modelId: "gpt-4o", provider: "openai" },
    });
    expect(result.text).toBe("hi");
    expect(doGenerate).toHaveBeenCalled();
    const completion = sink.calls.find((c) => c["event_type"] === "ai_sdk_completion");
    expect(completion).toBeTruthy();
    expect(completion!["input_tokens"]).toBe(3);
    expect(completion!["output_tokens"]).toBe(5);
  });

  it("throws CheckrdPolicyDenied when policy denies and enforce=true", async () => {
    const engine = new WasmEngine(DENY_ALL, "agent-1");
    const mw = checkrdMiddleware({ engine, enforce: true, agentId: "agent-1" });
    const doGenerate = vi.fn(async () => ({ text: "should not run" }));
    await expect(
      mw.wrapGenerate!({
        doGenerate,
        params: { prompt: "hello" },
        model: { modelId: "gpt-4o", provider: "openai" },
      }),
    ).rejects.toBeInstanceOf(CheckrdPolicyDenied);
    expect(doGenerate).not.toHaveBeenCalled();
  });

  it("still invokes the model under deny when enforce=false (observe mode)", async () => {
    const engine = new WasmEngine(DENY_ALL, "agent-1");
    const mw = checkrdMiddleware({ engine, enforce: false, agentId: "agent-1" });
    const doGenerate = vi.fn(async () => ({ text: "ok" }));
    const result = await mw.wrapGenerate!({
      doGenerate,
      params: { prompt: "x" },
      model: { modelId: "gpt-4o", provider: "openai" },
    });
    expect(result.text).toBe("ok");
  });
});

describe("checkrdMiddleware — wrapStream", () => {
  it("transforms the stream and emits a completion event with usage", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "agent-1");
    const sink = makeSink();
    const mw = checkrdMiddleware({
      engine,
      enforce: true,
      agentId: "agent-1",
      sink,
    });
    const upstream = new ReadableStream<Record<string, unknown>>({
      start(controller) {
        controller.enqueue({ type: "text-delta", delta: "hi" });
        controller.enqueue({
          type: "finish",
          finishReason: "stop",
          usage: { inputTokens: 4, outputTokens: 9 },
        });
        controller.close();
      },
    });
    const doStream = vi.fn(async () => ({ stream: upstream }));
    const result = await mw.wrapStream!({
      doStream,
      params: { prompt: "hi" },
      model: { modelId: "gpt-4o", provider: "openai" },
    });
    const reader = result.stream!.getReader();
    for (;;) {
      const { done } = await reader.read();
      if (done) break;
    }
    reader.releaseLock();
    // Let the flush() finalizer fire.
    await new Promise((r) => setTimeout(r, 0));
    const completion = sink.calls.find((c) => c["event_type"] === "ai_sdk_completion");
    expect(completion).toBeTruthy();
    expect(completion!["input_tokens"]).toBe(4);
    expect(completion!["output_tokens"]).toBe(9);
    expect(completion!["finish_reason"]).toBe("stop");
  });
});
