/**
 * OpenAI Agents SDK (TypeScript) tests.
 *
 * Both adapters are duck-typed against the SDK's `TracingProcessor` /
 * `InputGuardrail` / `OutputGuardrail` shapes — these tests use
 * minimal structural stand-ins so they don't depend on the real
 * runtime emitting any specific events. The contract verified is:
 *
 * - {@link CheckrdTracingProcessor} emits one telemetry event per
 *   span with the right `kind` / `target`.
 * - Guardrails return `tripwireTriggered: true` on deny+enforce, and
 *   `tripwireTriggered: false` everywhere else.
 */
import { describe, expect, it, vi } from "vitest";

import { WasmEngine } from "../../src/engine.js";
import {
  CheckrdTracingProcessor,
  checkrdInputGuardrail,
  checkrdOutputGuardrail,
  type SpanLike,
  type TraceLike,
} from "../../src/integrations/_openai_agents.js";

const ALLOW = JSON.stringify({ agent: "t", mode: "enforce", default: "allow", rules: [] });
const DENY = JSON.stringify({ agent: "t", mode: "enforce", default: "deny", rules: [] });

function makeListSink() {
  const events: Record<string, unknown>[] = [];
  return {
    enqueue: vi.fn((e: Record<string, unknown>) => {
      events.push(e);
    }),
    close: vi.fn(() => Promise.resolve()),
    events,
  };
}

describe("CheckrdTracingProcessor", () => {
  it("emits telemetry events for trace + span lifecycle", () => {
    const engine = new WasmEngine(ALLOW, "test");
    const sink = makeListSink();
    const proc = new CheckrdTracingProcessor({
      engine,
      enforce: true,
      agentId: "test-agent",
      sink,
    });

    const trace: TraceLike = { traceId: "trace-1", name: "agent_run" };
    proc.onTraceStart(trace);

    const span: SpanLike = {
      traceId: "trace-1",
      spanId: "span-1",
      startedAt: "2026-04-25T00:00:00.000Z",
      endedAt: "2026-04-25T00:00:01.000Z",
      spanData: {
        type: "GenerationSpanData",
        model: "gpt-4o",
        usage: { inputTokens: 10, outputTokens: 20 },
      },
    };
    proc.onSpanStart(span);
    proc.onSpanEnd(span);
    proc.onTraceEnd(trace);

    // OpenTelemetry-style: only END events emit. Trace start/end
    // and span_start are no-ops because they would 422 the ingest
    // endpoint (no clean wire schema for partial events).
    expect(sink.events).toHaveLength(1);
    const endEvent = sink.events[0]!;
    expect(endEvent.url_host).toBe("openai-agents.local");
    expect(endEvent.url_path).toBe("/generation/gpt-4o");
    expect(endEvent.gen_ai_model).toBe("gpt-4o");
    expect(endEvent.gen_ai_input_tokens).toBe(10);
    expect(endEvent.gen_ai_output_tokens).toBe(20);
    expect(endEvent.latency_ms).toBe(1000);
    expect(endEvent.span_status_code).toBe("OK");
    // No legacy fields — those would trigger 422 server-side.
    expect(endEvent.event_type).toBeUndefined();
    expect(endEvent.kind).toBeUndefined();
    expect(endEvent.target).toBeUndefined();
  });

  it("is a no-op when no sink is configured", () => {
    const engine = new WasmEngine(ALLOW, "test");
    const proc = new CheckrdTracingProcessor({
      engine,
      enforce: true,
      agentId: "test-agent",
    });
    // Just verify these don't throw.
    proc.onTraceStart({ traceId: "t" });
    proc.onSpanStart({ traceId: "t" });
    proc.onSpanEnd({ traceId: "t" });
    proc.onTraceEnd({ traceId: "t" });
    proc.shutdown();
    proc.forceFlush();
  });
});

describe("checkrdInputGuardrail", () => {
  it("returns tripwireTriggered=true on deny + enforce=true", async () => {
    const engine = new WasmEngine(DENY, "test");
    const sink = makeListSink();
    const guard = checkrdInputGuardrail({
      engine,
      enforce: true,
      agentId: "test-agent",
      sink,
    });

    const result = await guard.guardrailFunction(
      null,
      { name: "researcher" },
      "do something risky",
    );
    expect(result.tripwireTriggered).toBe(true);
    expect(result.outputInfo.deny_reason).toBeDefined();
    // Schema-compliant deny event lands on the sink. The
    // synthetic URL path lets policy YAML target the specific
    // guardrail (``url: openai-agents.local/input/researcher``).
    const deny = sink.events.find(
      (e) =>
        e.policy_result === "denied" && e.url_path === "/input/researcher",
    );
    expect(deny).toBeDefined();
    expect(deny?.url_host).toBe("openai-agents.local");
    expect(deny?.status_code).toBe(403);
    expect(deny?.span_status_code).toBe("ERROR");
    expect(deny?.event_type).toBeUndefined();
  });

  it("returns tripwireTriggered=false on allow", async () => {
    const engine = new WasmEngine(ALLOW, "test");
    const guard = checkrdInputGuardrail({
      engine,
      enforce: true,
      agentId: "test-agent",
    });

    const result = await guard.guardrailFunction(
      null,
      { name: "researcher" },
      "summarize",
    );
    expect(result.tripwireTriggered).toBe(false);
  });

  it("never tripwires in observation mode", async () => {
    const engine = new WasmEngine(DENY, "test");
    const guard = checkrdInputGuardrail({
      engine,
      enforce: false,
      agentId: "test-agent",
    });

    const result = await guard.guardrailFunction(null, { name: "x" }, "y");
    expect(result.tripwireTriggered).toBe(false);
    expect(result.outputInfo.checkrd_observation_only).toBe(true);
  });
});

describe("checkrdOutputGuardrail", () => {
  it("evaluates against output content", async () => {
    const engine = new WasmEngine(ALLOW, "test");
    const guard = checkrdOutputGuardrail({
      engine,
      enforce: true,
      agentId: "test-agent",
    });

    const result = await guard.guardrailFunction(
      null,
      { name: "x" },
      "the agent's final answer",
    );
    expect(result.tripwireTriggered).toBe(false);
  });
});
