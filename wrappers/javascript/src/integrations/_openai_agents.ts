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

  onTraceStart(_trace: TraceLike): void {
    // Trace start/end events have no clean mapping to the
    // ``TelemetryEventInput`` wire schema and would 422 at the
    // ingest endpoint. The per-span events below carry the same
    // ``trace_id`` so the dashboard reconstructs the trace from
    // spans alone — OpenTelemetry's contract every observability
    // vendor follows.
  }

  onTraceEnd(_trace: TraceLike): void {
    // See onTraceStart.
  }

  onSpanStart(_span: SpanLike): void {
    // We only emit on span END. Start events have no latency or
    // final span_status_code, and the ingest schema doesn't
    // accept partial events — emitting one would generate a
    // duplicate row that confuses dashboards and burns batch
    // capacity. OpenTelemetry's contract is end-of-span only.
  }

  onSpanEnd(span: SpanLike): void {
    if (!this.sink) return;
    const { kind, target, extra } = classifySpan(span);
    this.enqueue(buildSpanEvent({
      span,
      agentId: this.agentId,
      kind,
      target,
      extra,
      latencyMs: spanLatencyMs(span),
    }));
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

  // Denied. Emit a wire-schema-compliant deny event so
  // observation-mode operators see what would have been blocked.
  // No ``event_type`` / ``kind`` / ``target`` — those would 422
  // at the ingest endpoint.
  if (options.sink) {
    try {
      const now = new Date();
      options.sink.enqueue({
        request_id: result.request_id,
        agent_id: options.agentId,
        timestamp: now.toISOString(),
        url_host: AUTHORITY,
        url_path: `/${kind}/${target}`,
        method: "POST",
        status_code: 403,
        policy_result: "denied",
        deny_reason: result.deny_reason,
        span_name: `openai-agents.${kind} ${target}`,
        span_status_code: "ERROR",
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

/**
 * True when ``raw`` is a string of exactly ``expectedLen`` lowercase-
 * hex characters. The ingest endpoint validates trace_id (32 hex)
 * and span_id (16 hex) against the W3C Trace Context format —
 * OpenAI Agents uses opaque ``trace_xxx`` / ``span_xxx`` strings
 * instead, which 422 the batch. Drop non-conforming values from
 * the wire payload.
 */
function hexIdOrUndefined(raw: unknown, expectedLen: number): string | undefined {
  if (typeof raw !== "string" || raw.length !== expectedLen) return undefined;
  for (let i = 0; i < raw.length; i += 1) {
    const c = raw.charCodeAt(i);
    const isHex =
      (c >= 48 && c <= 57) || (c >= 97 && c <= 102); // 0-9, a-f
    if (!isHex) return undefined;
  }
  return raw;
}

/**
 * Wire-schema-compliant TelemetryEventInput for one finished
 * OpenAI Agents span. Mirrors the Python adapter's
 * `_build_span_event` so a single dashboard query covers both
 * runtimes.
 */
function buildSpanEvent(args: {
  span: SpanLike;
  agentId: string;
  kind: string;
  target: string;
  extra: Record<string, unknown>;
  latencyMs: number | null;
}): Record<string, unknown> {
  const now = new Date();
  const rawTraceId = args.span.traceId ?? "";
  const event: Record<string, unknown> = {
    request_id: rawTraceId || `openai-agents-${now.getTime().toString()}`,
    agent_id: args.agentId,
    timestamp: now.toISOString(),
    url_host: AUTHORITY,
    url_path: `/${args.kind}/${args.target || "unknown"}`,
    method: "POST",
    status_code: 200,
    policy_result: "allowed",
    span_name: `openai-agents.${args.kind} ${args.target || "unknown"}`,
    span_status_code: "OK",
  };
  const traceId = hexIdOrUndefined(rawTraceId, 32);
  const spanId = hexIdOrUndefined(args.span.spanId, 16);
  const parentSpanId = hexIdOrUndefined(args.span.parentId, 16);
  if (traceId !== undefined) event.trace_id = traceId;
  if (spanId !== undefined) event.span_id = spanId;
  if (parentSpanId !== undefined) event.parent_span_id = parentSpanId;
  if (args.latencyMs !== null) event.latency_ms = args.latencyMs;

  // GenAI semconv mapping for ``generation`` spans.
  if (args.extra.input_tokens != null) {
    event.gen_ai_input_tokens = args.extra.input_tokens;
  }
  if (args.extra.output_tokens != null) {
    event.gen_ai_output_tokens = args.extra.output_tokens;
  }
  if (args.kind === "generation") {
    event.gen_ai_model = args.target;
  }
  return event;
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
