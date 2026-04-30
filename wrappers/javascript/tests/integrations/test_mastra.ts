/**
 * Mastra agent adapter tests. Mastra is structurally typed against
 * our `MastraAgentLike` shape, so the tests pass a hand-rolled stub
 * implementing the subset we wrap (`generate`, `stream`, an unknown
 * field, an unknown method). A full Mastra integration test belongs
 * in `examples/mastra-agent/`.
 */
import { describe, expect, it, vi } from "vitest";

import { WasmEngine } from "../../src/engine.js";
import { CheckrdPolicyDenied } from "../../src/exceptions.js";
import {
  checkrdMastraTelemetry,
  wrapMastraAgent,
  type MastraAgentLike,
} from "../../src/integrations/_mastra.js";

const ALLOW_ALL = JSON.stringify({ agent: "t", default: "allow", rules: [] });
const DENY_ALL = JSON.stringify({ agent: "t", default: "deny", rules: [] });

function stubAgent(): MastraAgentLike & {
  generate: ReturnType<typeof vi.fn>;
  stream: ReturnType<typeof vi.fn>;
} {
  return {
    name: "support-bot",
    generate: vi.fn(async (_input: unknown) => ({ text: "hi" })),
    stream: vi.fn(async (_input: unknown) => ({ stream: "mock" })),
  };
}

describe("wrapMastraAgent — allow path", () => {
  it("forwards generate() when policy allows", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const raw = stubAgent();
    const agent = wrapMastraAgent(raw, { engine, enforce: true, agentId: "test" });
    const result = await agent.generate!({ messages: [{ role: "user", content: "hi" }] });
    expect(result).toEqual({ text: "hi" });
    expect(raw.generate).toHaveBeenCalledOnce();
  });

  it("forwards stream() when policy allows", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const raw = stubAgent();
    const agent = wrapMastraAgent(raw, { engine, enforce: true, agentId: "test" });
    await agent.stream!({ messages: [] });
    expect(raw.stream).toHaveBeenCalledOnce();
  });
});

describe("wrapMastraAgent — deny path", () => {
  it("blocks generate() under default-deny in enforce mode", async () => {
    const engine = new WasmEngine(DENY_ALL, "test");
    const raw = stubAgent();
    const agent = wrapMastraAgent(raw, { engine, enforce: true, agentId: "test" });
    await expect(agent.generate!({ messages: [] })).rejects.toBeInstanceOf(
      CheckrdPolicyDenied,
    );
    expect(raw.generate).not.toHaveBeenCalled();
  });

  it("under enforce=false, forwards the call but records the deny", async () => {
    const engine = new WasmEngine(DENY_ALL, "test");
    const raw = stubAgent();
    const agent = wrapMastraAgent(raw, {
      engine,
      enforce: false,
      agentId: "test",
    });
    const result = await agent.generate!({ messages: [] });
    expect(result).toEqual({ text: "hi" });
    expect(raw.generate).toHaveBeenCalledOnce();
  });
});

describe("wrapMastraAgent — proxy transparency", () => {
  it("preserves unknown properties and methods", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const raw: MastraAgentLike & { custom: string; compute: () => number } = {
      name: "x",
      generate: async () => ({}),
      stream: async () => ({}),
      custom: "untouched",
      compute: () => 42,
    };
    const wrapped = wrapMastraAgent(raw, { engine, enforce: true, agentId: "x" });
    expect((wrapped as unknown as { custom: string }).custom).toBe("untouched");
    expect((wrapped as unknown as { compute: () => number }).compute()).toBe(42);
  });
});

describe("checkrdMastraTelemetry", () => {
  it("enriches events with agent_id + telemetry_source before fanning to sink", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const sink = {
      enqueue: vi.fn(),
      close: async (): Promise<void> => undefined,
    };
    const telemetry = checkrdMastraTelemetry({
      engine,
      enforce: false,
      agentId: "mastra-app",
      sink,
    });
    telemetry.onEvent({ foo: "bar" });
    expect(sink.enqueue).toHaveBeenCalledOnce();
    const enriched = sink.enqueue.mock.calls[0]![0] as Record<string, unknown>;
    expect(enriched["agent_id"]).toBe("mastra-app");
    expect(enriched["telemetry_source"]).toBe("mastra");
    expect(enriched["foo"]).toBe("bar");
  });

  it("silently skips when no sink is configured", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const telemetry = checkrdMastraTelemetry({
      engine,
      enforce: false,
      agentId: "x",
    });
    expect(() => {
      telemetry.onEvent({});
    }).not.toThrow();
  });
});
