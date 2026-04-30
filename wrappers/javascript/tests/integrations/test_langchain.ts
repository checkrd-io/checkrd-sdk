/**
 * LangChain.js callback handler tests.
 *
 * Uses the real ``WasmEngine`` against a YAML policy string so the
 * eval path is exercised end-to-end. The framework peer
 * (``@langchain/core``) is a dev dependency; we use real ``LLMResult``
 * and ``BaseMessage`` shapes from it where convenient and stub the
 * rest with minimal structural objects.
 */
import { describe, expect, it, vi } from "vitest";
import type { LLMResult } from "@langchain/core/outputs";
import { HumanMessage } from "@langchain/core/messages";

import { WasmEngine } from "../../src/engine.js";
import { CheckrdPolicyDenied } from "../../src/exceptions.js";
import { CheckrdCallbackHandler } from "../../src/integrations/_langchain.js";

const ALLOW = JSON.stringify({ agent: "t", default: "allow", rules: [] });
const DENY = JSON.stringify({ agent: "t", default: "deny", rules: [] });

interface CapturedEvent {
  event_type?: unknown;
  agent_id?: unknown;
  target?: unknown;
  outcome?: unknown;
  input_tokens?: unknown;
  output_tokens?: unknown;
  error?: unknown;
  [key: string]: unknown;
}

function makeListSink() {
  const events: CapturedEvent[] = [];
  return {
    enqueue: vi.fn((e: CapturedEvent) => {
      events.push(e);
    }),
    close: vi.fn(() => Promise.resolve()),
    events,
  };
}

const newRunId = (): string => globalThis.crypto.randomUUID();

describe("CheckrdCallbackHandler — allow path", () => {
  it("emits a langchain_llm telemetry event with token usage on llm_end", async () => {
    const engine = new WasmEngine(ALLOW, "test");
    const sink = makeListSink();
    const handler = new CheckrdCallbackHandler({
      engine,
      enforce: true,
      agentId: "test-agent",
      sink,
    });

    const runId = newRunId();
    await handler.handleLLMStart(
      { lc: 1, type: "constructor", id: ["langchain", "ChatOpenAI"], kwargs: { model: "gpt-4o" } },
      ["hello"],
      runId,
    );

    const result: LLMResult = {
      generations: [[{ text: "hi" }]],
      llmOutput: { tokenUsage: { promptTokens: 5, completionTokens: 2 } },
    };
    await handler.handleLLMEnd(result, runId);

    expect(sink.events).toHaveLength(1);
    const event = sink.events[0]!;
    expect(event.event_type).toBe("langchain_llm");
    expect(event.agent_id).toBe("test-agent");
    expect(event.target).toBe("gpt-4o");
    expect(event.outcome).toBe("ok");
    expect(event.input_tokens).toBe(5);
    expect(event.output_tokens).toBe(2);
  });

  it("uses the tool name as target on tool events", async () => {
    const engine = new WasmEngine(ALLOW, "test");
    const sink = makeListSink();
    const handler = new CheckrdCallbackHandler({
      engine,
      enforce: true,
      agentId: "test-agent",
      sink,
    });

    const runId = newRunId();
    await handler.handleToolStart(
      { lc: 1, type: "constructor", id: ["langchain", "Tool"], name: "search_database", kwargs: {} },
      "select count(*)",
      runId,
    );
    await handler.handleToolEnd("42", runId);

    expect(sink.events).toHaveLength(1);
    expect(sink.events[0]!.event_type).toBe("langchain_tool");
    expect(sink.events[0]!.target).toBe("search_database");
  });

  it("handles chat_model_start with structured messages", async () => {
    const engine = new WasmEngine(ALLOW, "test");
    const sink = makeListSink();
    const handler = new CheckrdCallbackHandler({
      engine,
      enforce: true,
      agentId: "test-agent",
      sink,
    });

    const runId = newRunId();
    await handler.handleChatModelStart(
      { lc: 1, type: "constructor", id: ["langchain", "ChatOpenAI"], kwargs: { model: "gpt-4o" } },
      [[new HumanMessage("hello")]],
      runId,
    );
    await handler.handleLLMEnd(
      { generations: [[{ text: "hi" }]], llmOutput: {} },
      runId,
    );
    expect(sink.events).toHaveLength(1);
    expect(sink.events[0]!.event_type).toBe("langchain_chat_model");
    expect(sink.events[0]!.target).toBe("gpt-4o");
  });
});

describe("CheckrdCallbackHandler — deny path", () => {
  it("throws CheckrdPolicyDenied from on_llm_start when enforce=true", async () => {
    const engine = new WasmEngine(DENY, "test");
    const handler = new CheckrdCallbackHandler({
      engine,
      enforce: true,
      agentId: "test-agent",
    });

    await expect(
      handler.handleLLMStart(
        { lc: 1, type: "constructor", id: ["langchain", "ChatOpenAI"], kwargs: { model: "gpt-4o" } },
        ["hello"],
        newRunId(),
      ),
    ).rejects.toThrow(CheckrdPolicyDenied);
  });

  it("does not throw in observation mode (enforce=false)", async () => {
    const engine = new WasmEngine(DENY, "test");
    const handler = new CheckrdCallbackHandler({
      engine,
      enforce: false,
      agentId: "test-agent",
    });

    await expect(
      handler.handleLLMStart(
        { lc: 1, type: "constructor", id: ["langchain", "ChatOpenAI"], kwargs: { model: "gpt-4o" } },
        ["hello"],
        newRunId(),
      ),
    ).resolves.toBeUndefined();
  });
});

describe("CheckrdCallbackHandler — error events", () => {
  it("emits outcome=error on chain_error", async () => {
    const engine = new WasmEngine(ALLOW, "test");
    const sink = makeListSink();
    const handler = new CheckrdCallbackHandler({
      engine,
      enforce: true,
      agentId: "test-agent",
      sink,
    });
    const runId = newRunId();
    await handler.handleChainStart(
      { lc: 1, type: "constructor", id: ["langchain", "Chain"], name: "my-chain", kwargs: {} },
      { q: "x" },
      runId,
    );
    await handler.handleChainError(new Error("boom"), runId);

    expect(sink.events).toHaveLength(1);
    expect(sink.events[0]!.outcome).toBe("error");
    expect(sink.events[0]!.error).toBe("Error");
  });
});
