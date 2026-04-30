/* eslint-disable @typescript-eslint/require-await -- the Agents SDK's
   ``InputGuardrail`` / ``OutputGuardrail`` contracts require async
   functions returning a ``GuardrailFunctionOutput`` so the runtime can
   ``await`` the decision; our guardrail bodies delegate to a sync
   ``evaluateGuardrail()`` and have no inner ``await``. The async
   signature is structural, not nominal. */
/**
 * OpenAI Agents SDK (TypeScript) integration.
 *
 * The TS Agents SDK splits its extension surface across two layers,
 * matching the Python SDK's design. Checkrd targets both:
 *
 * 1. **Tracing processors** — observation-only. Receive trace and
 *    span lifecycle events; cannot abort a run.
 * 2. **Guardrails** — enforcement. Run before / after the model
 *    invocation and tripwire to abort on deny.
 *
 * Exports:
 *
 * - {@link CheckrdTracingProcessor} — implements the SDK's
 *   `TracingProcessor` shape; emits a Checkrd telemetry event per
 *   span. Register via `addTraceProcessor(...)` at startup.
 * - {@link checkrdInputGuardrail} — factory returning an
 *   `InputGuardrail`-shaped object the user passes to
 *   `new Agent({ inputGuardrails: [...] })`.
 * - {@link checkrdOutputGuardrail} — same, for output content.
 *
 * Mirrors `checkrd.integrations.openai_agents` in the Python SDK
 * one-for-one. Operators write one policy YAML; the same rules fire
 * across Python and JS agents on the same `openai-agents.local`
 * synthetic-URL scheme.
 *
 * Why duck-typed against `@openai/agents`: the SDK ships under MIT
 * but is moving fast. Structural typing means an SDK minor bump
 * doesn't force a Checkrd release. The contract verified in tests is
 * narrow: trace and span objects expose `traceId` / `spanId` /
 * `parentId` / `spanData` / `startedAt` / `endedAt`.
 */

import type { WasmEngine, EvaluateRequest, EvalResult } from "../engine.js";
import type { TelemetrySink } from "../sinks.js";
import type { Logger } from "../_logger.js";

const AUTHORITY = "openai-agents.local";

/** Shared options for every adapter in this module. */
export interface CheckrdOpenAIAgentsOptions {
  engine: WasmEngine;
  enforce: boolean;
  agentId: string;
  sink?: TelemetrySink | undefined;
  logger?: Logger | undefined;
  dashboardUrl?: string | undefined;
}

// ---------------------------------------------------------------------
// Tracing processor (observability)
// ---------------------------------------------------------------------

/** Minimum structural shape of a Trace from `@openai/agents`. */
export interface TraceLike {
  traceId?: string;
  name?: string;
  [key: string]: unknown;
}

/** Minimum structural shape of a Span. */
export interface SpanLike {
  traceId?: string;
  spanId?: string;
  parentId?: string;
  startedAt?: string;
  endedAt?: string;
  spanData?: SpanDataLike;
  [key: string]: unknown;
}

interface SpanDataLike {
  type?: string;
  model?: string;
  name?: string;
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    promptTokens?: number;
    completionTokens?: number;
  };
  [key: string]: unknown;
}

/** Minimum shape we expose for our tracing processor. */
export interface CheckrdTracingProcessorLike {
  onTraceStart(trace: TraceLike): void;
  onTraceEnd(trace: TraceLike): void;
  onSpanStart(span: SpanLike): void;
  onSpanEnd(span: SpanLike): void;
  shutdown(timeoutMs?: number): void | Promise<void>;
  forceFlush(): void | Promise<void>;
}

/**
 * Tracing processor that emits one Checkrd telemetry event per Agents
 * SDK span. Pure observation — never blocks. Pair with
 * {@link checkrdInputGuardrail} for enforcement.
 *
 * Register via `addTraceProcessor(...)` at startup. The OpenAI traces
 * dashboard keeps working alongside this — `addTraceProcessor` is
 * additive.
 */
export class CheckrdTracingProcessor implements CheckrdTracingProcessorLike {
  private readonly agentId: string;
  private readonly sink: TelemetrySink | undefined;
  private readonly logger: Logger | undefined;

  constructor(options: CheckrdOpenAIAgentsOptions) {
    this.agentId = options.agentId;
    this.sink = options.sink;
    this.logger = options.logger;
  }

  onTraceStart(trace: TraceLike): void {
    if (!this.sink) return;
    this.enqueue({
      event_type: "openai_agents_trace_start",
      request_id: trace.traceId ?? "",
      agent_id: this.agentId,
      trace_name: trace.name,
    });
  }

  onTraceEnd(trace: TraceLike): void {
    if (!this.sink) return;
    this.enqueue({
      event_type: "openai_agents_trace_end",
      request_id: trace.traceId ?? "",
      agent_id: this.agentId,
      trace_name: trace.name,
    });
  }

  onSpanStart(span: SpanLike): void {
    if (!this.sink) return;
    const { kind, target, extra } = classifySpan(span);
    this.enqueue({
      event_type: `openai_agents_${kind}_start`,
      request_id: span.traceId ?? "",
      span_id: span.spanId,
      parent_span_id: span.parentId,
      agent_id: this.agentId,
      kind,
      target,
      ...extra,
    });
  }

  onSpanEnd(span: SpanLike): void {
    if (!this.sink) return;
    const { kind, target, extra } = classifySpan(span);
    this.enqueue({
      event_type: `openai_agents_${kind}_end`,
      request_id: span.traceId ?? "",
      span_id: span.spanId,
      parent_span_id: span.parentId,
      agent_id: this.agentId,
      kind,
      target,
      latency_ms: spanLatencyMs(span),
      ...extra,
    });
  }

  shutdown(): void {
    // Sink owns its own shutdown via the global Checkrd context.
    // No-op here so this processor's lifecycle does not race with
    // other Checkrd consumers (transports, instrumentors) sharing
    // the same sink.
  }

  forceFlush(): void {
    // Best-effort — many sinks expose `flush()` via duck typing.
    const flush = (this.sink as { flush?: () => unknown } | undefined)?.flush;
    if (typeof flush === "function") {
      try {
        flush.call(this.sink);
      } catch (err) {
        this.logger?.warn("checkrd: openai-agents forceFlush failed", err);
      }
    }
  }

  private enqueue(event: Record<string, unknown>): void {
    try {
      this.sink?.enqueue(event);
    } catch (err) {
      this.logger?.warn("checkrd: openai-agents telemetry enqueue failed", err);
    }
  }
}

// ---------------------------------------------------------------------
// Guardrails (enforcement)
// ---------------------------------------------------------------------

/**
 * Output shape returned by guardrail functions in `@openai/agents`.
 * Setting `tripwireTriggered: true` aborts the agent run.
 */
export interface GuardrailFunctionOutputLike {
  outputInfo: Record<string, unknown>;
  tripwireTriggered: boolean;
}

/** Subset of an `InputGuardrail` we need to construct. */
export interface InputGuardrailLike {
  name: string;
  guardrailFunction: (
    context: unknown,
    agent: unknown,
    input: unknown,
  ) => Promise<GuardrailFunctionOutputLike>;
}

/** Subset of an `OutputGuardrail` we need to construct. */
export interface OutputGuardrailLike {
  name: string;
  guardrailFunction: (
    context: unknown,
    agent: unknown,
    output: unknown,
  ) => Promise<GuardrailFunctionOutputLike>;
}

interface GuardrailEvalArgs {
  options: CheckrdOpenAIAgentsOptions;
  kind: "input" | "output";
  target: string;
  bodyObj: unknown;
}

function evaluateGuardrail(args: GuardrailEvalArgs): GuardrailFunctionOutputLike {
  const { options, kind, target, bodyObj } = args;
  const url = `https://${AUTHORITY}/${kind}/${target}`;
  const now = new Date();
  const request: EvaluateRequest = {
    request_id: globalThis.crypto.randomUUID(),
    method: "POST",
    url,
    headers: [
      ["x-openai-agents-kind", kind],
      ["x-openai-agents-target", target],
    ],
    body: safeJson(bodyObj),
    timestamp: now.toISOString(),
    timestamp_ms: now.valueOf(),
  };
  const result: EvalResult = options.engine.evaluate(request);

  if (result.allowed) {
    return {
      outputInfo: {
        checkrd_request_id: result.request_id,
        kind,
        target,
      },
      tripwireTriggered: false,
    };
  }

  // Denied. Emit telemetry first so observation-mode operators see
  // what would have been blocked.
  if (options.sink) {
    try {
      options.sink.enqueue({
        event_type: `openai_agents_${kind}_denied`,
        request_id: result.request_id,
        agent_id: options.agentId,
        kind,
        target,
        deny_reason: result.deny_reason,
      });
    } catch (err) {
      options.logger?.warn(
        "checkrd: openai-agents deny telemetry enqueue failed",
        err,
      );
    }
  }

  if (!options.enforce) {
    options.logger?.warn(
      `checkrd: openai-agents ${kind} ${target} denied (observation mode): ${result.deny_reason ?? ""}`,
    );
    return {
      outputInfo: {
        checkrd_request_id: result.request_id,
        checkrd_observation_only: true,
        deny_reason: result.deny_reason,
      },
      tripwireTriggered: false,
    };
  }

  return {
    outputInfo: {
      checkrd_request_id: result.request_id,
      deny_reason: result.deny_reason,
      dashboard_url: buildDashboardUrl(options.dashboardUrl, result.request_id),
    },
    tripwireTriggered: true,
  };
}

/**
 * Build an input guardrail for `new Agent({ inputGuardrails: [...] })`.
 *
 *     const agent = new Agent({
 *       name: "research",
 *       inputGuardrails: [checkrdInputGuardrail({ engine, enforce: true, agentId, sink })],
 *     });
 */
export function checkrdInputGuardrail(
  options: CheckrdOpenAIAgentsOptions,
): InputGuardrailLike {
  return {
    name: "checkrd_input_guardrail",
    guardrailFunction: async (
      _context: unknown,
      agent: unknown,
      input: unknown,
    ): Promise<GuardrailFunctionOutputLike> => {
      const target =
        (agent as { name?: string } | undefined)?.name ?? "agent";
      return evaluateGuardrail({
        options,
        kind: "input",
        target,
        bodyObj: { input },
      });
    },
  };
}

/**
 * Build an output guardrail for `new Agent({ outputGuardrails: [...] })`.
 */
export function checkrdOutputGuardrail(
  options: CheckrdOpenAIAgentsOptions,
): OutputGuardrailLike {
  return {
    name: "checkrd_output_guardrail",
    guardrailFunction: async (
      _context: unknown,
      agent: unknown,
      output: unknown,
    ): Promise<GuardrailFunctionOutputLike> => {
      const target =
        (agent as { name?: string } | undefined)?.name ?? "agent";
      return evaluateGuardrail({
        options,
        kind: "output",
        target,
        bodyObj: { output },
      });
    },
  };
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function classifySpan(span: SpanLike): {
  kind: string;
  target: string;
  extra: Record<string, unknown>;
} {
  const sd = span.spanData;
  const typeName = sd?.type ?? "Span";
  const extra: Record<string, unknown> = {};

  if (sd && typeof sd.model === "string" && sd.model) {
    if (sd.usage) {
      extra.input_tokens = sd.usage.inputTokens ?? sd.usage.promptTokens;
      extra.output_tokens = sd.usage.outputTokens ?? sd.usage.completionTokens;
    }
    return { kind: "generation", target: sd.model, extra };
  }

  if (sd && typeof sd.name === "string" && sd.name) {
    if (typeName.includes("function") || typeName.includes("tool")) {
      return { kind: "function", target: sd.name, extra };
    }
    if (typeName.includes("handoff")) {
      return { kind: "handoff", target: sd.name, extra };
    }
    if (typeName.includes("agent")) {
      return { kind: "agent", target: sd.name, extra };
    }
    return { kind: typeName.toLowerCase() || "span", target: sd.name, extra };
  }

  if (typeName.includes("guardrail")) {
    const tripwire = (sd as { tripwireTriggered?: unknown } | undefined)
      ?.tripwireTriggered;
    return {
      kind: "guardrail",
      target: typeof tripwire === "boolean" ? String(tripwire) : "",
      extra,
    };
  }

  return { kind: typeName.toLowerCase() || "span", target: "", extra };
}

function spanLatencyMs(span: SpanLike): number | null {
  if (!span.startedAt || !span.endedAt) return null;
  const start = Date.parse(span.startedAt);
  const end = Date.parse(span.endedAt);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return end - start;
}

function buildDashboardUrl(
  base: string | undefined,
  requestId: string,
): string | undefined {
  if (!base) return undefined;
  return `${base.replace(/\/$/, "")}/events/${requestId}`;
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, (_k: string, v: unknown): unknown => {
      if (typeof v === "function") return undefined;
      return v;
    });
  } catch {
    return JSON.stringify({ _repr: String(value) });
  }
}
