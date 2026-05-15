/* eslint-disable @typescript-eslint/require-await -- LangChain's
   ``BaseCallbackHandler`` requires every override to return a Promise so
   the dispatcher can ``await`` it; many of our overrides delegate to a
   sync ``gate()`` / ``emit()`` and have no inner ``await``. The async
   signature is structural, not nominal — the rule's heuristic does not
   apply at this boundary. */
/**
 * LangChain.js callback handler for Checkrd.
 *
 * LangChain's industry-standard extension point is the callback handler
 * protocol — `BaseCallbackHandler` from `@langchain/core/callbacks`.
 * Every Runnable in the LangChain.js ecosystem (chains, LLMs, chat
 * models, tools, retrievers, agents, LangGraph nodes) emits
 * `handle*Start` / `handle*End` / `handle*Error` events through these
 * handlers.
 *
 * This module ships {@link CheckrdCallbackHandler}: a class extending
 * `BaseCallbackHandler` that policy-evaluates each event through the
 * WASM core and emits structured telemetry. Subclassing the framework's
 * own ABC means: zero monkey-patching, zero internal-API risk, and
 * graceful evolution across LangChain.js minor versions.
 *
 * Mirrors `checkrd.integrations.langchain.CheckrdCallbackHandler` in
 * the Python SDK — same options, same synthetic-URL scheme, same
 * deny semantics. Operators write one policy YAML and it works across
 * Python and JS agents.
 *
 * Usage:
 *
 *     import { initAsync } from "checkrd";
 *     import { CheckrdCallbackHandler } from "checkrd/langchain";
 *     import { ChatOpenAI } from "@langchain/openai";
 *
 *     const checkrd = await initAsync({ policy: "policy.yaml", agentId: "research" });
 *
 *     const handler = new CheckrdCallbackHandler({
 *       engine: checkrd.engine,
 *       enforce: true,
 *       agentId: "research",
 *       sink: checkrd.sink,
 *     });
 *
 *     const llm = new ChatOpenAI({ model: "gpt-4o", callbacks: [handler] });
 *     await llm.invoke("Tell me a joke");
 */

import { BaseCallbackHandler } from "@langchain/core/callbacks/base";
import type { Serialized } from "@langchain/core/load/serializable";
import type { BaseMessage } from "@langchain/core/messages";
import type { LLMResult } from "@langchain/core/outputs";
import type { Document } from "@langchain/core/documents";
import type { AgentAction, AgentFinish } from "@langchain/core/agents";

import { CheckrdPolicyDenied } from "../exceptions.js";
import type { WasmEngine, EvaluateRequest, EvalResult } from "../engine.js";
import type { TelemetrySink } from "../sinks.js";
import type { Logger } from "../_logger.js";

/** Synthetic URL authority for LangChain.js events. */
const LANGCHAIN_AUTHORITY = "langchain.local";

/** Options for {@link CheckrdCallbackHandler}. */
export interface CheckrdLangChainOptions {
  /** WASM engine — typically the one returned from initAsync(). */
  engine: WasmEngine;
  /** Raise on deny when true; observe-only when false. */
  enforce: boolean;
  /** Agent ID used as the telemetry correlation field. */
  agentId: string;
  /** Optional sink that receives per-call telemetry events. */
  sink?: TelemetrySink | undefined;
  /** Optional logger for diagnostics. */
  logger?: Logger | undefined;
  /** Dashboard base URL for deep links on denial. */
  dashboardUrl?: string | undefined;
}

interface InFlightEntry {
  startMs: number;
  kind: string;
  target: string;
}

/**
 * LangChain.js callback handler that enforces Checkrd policy.
 *
 * Subclass of `BaseCallbackHandler`. The same instance handles both
 * sync (`.invoke()`) and async (`.invoke()` is async by default in
 * LangChain.js) chains because every callback method on the
 * BaseCallbackHandler is already async — there's no sync/async split
 * in JS analogous to Python's two ABCs.
 *
 * Thread safety: JS is single-threaded; the in-flight map needs no
 * lock. Reuse a single instance across the process.
 *
 * Idempotency: `runId` collisions across concurrent runs are
 * framework-prevented (LangChain mints a fresh UUID per run).
 */
export class CheckrdCallbackHandler extends BaseCallbackHandler {
  /** Required by `BaseCallbackHandler`. Identifies this handler in serialization. */
  readonly name = "CheckrdCallbackHandler";

  /** Tells LangChain to propagate exceptions raised from this handler. */
  override readonly raiseError = true;

  /** Run handler in-line (caller's stack) so deny errors and latency are accurate. */
  override readonly awaitHandlers = true;

  private readonly engine: WasmEngine;
  private readonly enforce: boolean;
  private readonly agentId: string;
  private readonly sink: TelemetrySink | undefined;
  private readonly logger: Logger | undefined;
  private readonly dashboardUrl: string;
  private readonly inFlight = new Map<string, InFlightEntry>();

  constructor(options: CheckrdLangChainOptions) {
    super();
    this.engine = options.engine;
    this.enforce = options.enforce;
    this.agentId = options.agentId;
    this.sink = options.sink;
    this.logger = options.logger;
    this.dashboardUrl = options.dashboardUrl ?? "";
  }

  // ------------------------------------------------------------------
  // Internal: gate + emit
  // ------------------------------------------------------------------

  private gate(args: {
    runId: string;
    parentRunId: string | undefined;
    kind: string;
    target: string;
    body: unknown;
  }): EvalResult {
    const url = `https://${LANGCHAIN_AUTHORITY}/${args.kind}/${args.target}`;
    const now = new Date();
    // Normalize LangChain.js run IDs to W3C Trace Context format.
    // The wire schema requires ``trace_id`` to be 32 lowercase hex
    // chars and ``span_id`` to be 16. LangChain.js IDs are UUIDs
    // (36 chars with dashes); stripping dashes gives a valid
    // 32-hex trace_id and the first 16 chars are a valid span_id.
    const stripUuid = (u: string): string => u.replace(/-/g, "").toLowerCase();
    const traceUuid = args.parentRunId ?? args.runId;
    const traceIdHex = stripUuid(traceUuid);
    const spanIdHex = stripUuid(args.runId).slice(0, 16);
    const parentSpanIdHex = args.parentRunId
      ? stripUuid(args.parentRunId).slice(0, 16)
      : undefined;

    // ``EvaluateRequest`` declares ``parent_span_id`` as ``string`` under
    // ``exactOptionalPropertyTypes``, so the field must either carry a
    // string or be absent. Build the request object conditionally rather
    // than passing ``undefined`` explicitly.
    const request: EvaluateRequest = {
      request_id: args.runId,
      method: "POST",
      url,
      headers: [
        ["x-langchain-kind", args.kind],
        ["x-langchain-target", args.target],
        ["x-langchain-run-id", args.runId],
        ["x-langchain-parent-run-id", args.parentRunId ?? ""],
      ],
      body: safeJson(args.body),
      timestamp: now.toISOString(),
      timestamp_ms: now.valueOf(),
      trace_id: traceIdHex,
      span_id: spanIdHex,
      ...(parentSpanIdHex !== undefined
        ? { parent_span_id: parentSpanIdHex }
        : {}),
    };

    const result = this.engine.evaluate(request);
    this.inFlight.set(args.runId, {
      startMs: performance.now(),
      kind: args.kind,
      target: args.target,
    });

    if (!result.allowed) {
      if (this.enforce) {
        const dashboardUrl = this.buildDashboardUrl(result.request_id);
        throw new CheckrdPolicyDenied({
          reason: result.deny_reason ?? "policy denied",
          requestId: result.request_id,
          url,
          ...(dashboardUrl !== undefined ? { dashboardUrl } : {}),
        });
      }
      this.logger?.warn(
        `checkrd: langchain ${args.kind} ${args.target} denied (observation mode): ${result.deny_reason ?? ""}`,
      );
    }
    return result;
  }

  private emit(args: {
    runId: string;
    outcome: "ok" | "error";
    extra: Record<string, unknown>;
  }): void {
    if (!this.sink) return;
    const entry = this.inFlight.get(args.runId);
    this.inFlight.delete(args.runId);
    if (!entry) return;
    const latencyMs = Math.max(0, Math.round(performance.now() - entry.startMs));
    const now = new Date();
    // ``TelemetryEventInput``-shaped event. Older shapes used
    // ``event_type`` / ``kind`` / ``target`` / ``outcome`` and got
    // 422'd at the ingest endpoint — silently dropping the batch
    // and burning the batcher's retry budget on the next send.
    const event: Record<string, unknown> = {
      request_id: args.runId,
      agent_id: this.agentId,
      timestamp: now.toISOString(),
      url_host: LANGCHAIN_AUTHORITY,
      url_path: `/${entry.kind}/${entry.target}`,
      method: "POST",
      latency_ms: latencyMs,
      policy_result: "allowed",
      span_name: `langchain.${entry.kind} ${entry.target}`,
    };
    if (args.outcome === "error") {
      event.status_code = 500;
      event.span_status_code = "ERROR";
    } else {
      event.status_code = 200;
      event.span_status_code = "OK";
    }

    // GenAI semconv mapping: LangChain.js callbacks expose
    // ``input_tokens`` / ``output_tokens`` for LLM events; map them
    // onto the canonical ``gen_ai_*`` field names so the same
    // dashboard query rolls up tokens across vendor SDKs +
    // LangChain steps.
    const e = args.extra;
    if (e.input_tokens != null) event.gen_ai_input_tokens = e.input_tokens;
    if (e.output_tokens != null) event.gen_ai_output_tokens = e.output_tokens;
    if (entry.kind === "llm" || entry.kind === "chat_model") {
      event.gen_ai_model = entry.target;
    }

    try {
      this.sink.enqueue(event);
    } catch (err) {
      this.logger?.warn(
        `checkrd: telemetry enqueue failed for langchain ${entry.kind}`,
        err,
      );
    }
  }

  private buildDashboardUrl(requestId: string): string | undefined {
    if (!this.dashboardUrl) return undefined;
    return `${this.dashboardUrl.replace(/\/$/, "")}/events/${requestId}`;
  }

  // ------------------------------------------------------------------
  // LLM events (covers chat models via handleChatModelStart)
  // ------------------------------------------------------------------

  override async handleLLMStart(
    llm: Serialized,
    prompts: string[],
    runId: string,
    parentRunId?: string,
    extraParams?: Record<string, unknown>,
    tags?: string[],
    metadata?: Record<string, unknown>,
  ): Promise<void> {
    const target = resolveModelName(llm) ?? "unknown";
    this.gate({
      runId,
      parentRunId,
      kind: "llm",
      target,
      body: { prompts, tags, metadata, extraParams },
    });
  }

  override async handleChatModelStart(
    llm: Serialized,
    messages: BaseMessage[][],
    runId: string,
    parentRunId?: string,
    extraParams?: Record<string, unknown>,
    tags?: string[],
    metadata?: Record<string, unknown>,
  ): Promise<void> {
    const target = resolveModelName(llm) ?? "unknown";
    this.gate({
      runId,
      parentRunId,
      kind: "chat_model",
      target,
      body: {
        messages: messages.map((batch) => batch.map(messageToObject)),
        tags,
        metadata,
        extraParams,
      },
    });
  }

  override async handleLLMEnd(
    output: LLMResult,
    runId: string,
  ): Promise<void> {
    const usage = extractTokenUsage(output);
    this.emit({
      runId,
      outcome: "ok",
      extra: {
        input_tokens: usage.inputTokens,
        output_tokens: usage.outputTokens,
        total_tokens: usage.totalTokens,
        finish_reason: extractFinishReason(output),
      },
    });
  }

  override async handleLLMError(
    err: unknown,
    runId: string,
  ): Promise<void> {
    this.emit({
      runId,
      outcome: "error",
      extra: errorFields(err),
    });
  }

  // ------------------------------------------------------------------
  // Tool events
  // ------------------------------------------------------------------

  override async handleToolStart(
    tool: Serialized,
    input: string,
    runId: string,
    parentRunId?: string,
    tags?: string[],
    metadata?: Record<string, unknown>,
  ): Promise<void> {
    const target = (tool as { name?: string }).name ?? "unknown";
    this.gate({
      runId,
      parentRunId,
      kind: "tool",
      target,
      body: { input, tags, metadata },
    });
  }

  override async handleToolEnd(output: string, runId: string): Promise<void> {
    this.emit({
      runId,
      outcome: "ok",
      extra: { output_preview: preview(output) },
    });
  }

  override async handleToolError(err: unknown, runId: string): Promise<void> {
    this.emit({ runId, outcome: "error", extra: errorFields(err) });
  }

  // ------------------------------------------------------------------
  // Retriever events
  // ------------------------------------------------------------------

  override async handleRetrieverStart(
    retriever: Serialized,
    query: string,
    runId: string,
    parentRunId?: string,
    tags?: string[],
    metadata?: Record<string, unknown>,
  ): Promise<void> {
    const target = (retriever as { name?: string }).name ?? "retriever";
    this.gate({
      runId,
      parentRunId,
      kind: "retriever",
      target,
      body: { query, tags, metadata },
    });
  }

  override async handleRetrieverEnd(
    documents: Document[],
    runId: string,
  ): Promise<void> {
    this.emit({
      runId,
      outcome: "ok",
      extra: { document_count: documents.length },
    });
  }

  override async handleRetrieverError(
    err: unknown,
    runId: string,
  ): Promise<void> {
    this.emit({ runId, outcome: "error", extra: errorFields(err) });
  }

  // ------------------------------------------------------------------
  // Chain events
  // ------------------------------------------------------------------

  override async handleChainStart(
    chain: Serialized,
    inputs: Record<string, unknown>,
    runId: string,
    parentRunId?: string,
    tags?: string[],
    metadata?: Record<string, unknown>,
    runType?: string,
    runName?: string,
  ): Promise<void> {
    const target = runName ?? (chain as { name?: string }).name ?? "chain";
    this.gate({
      runId,
      parentRunId,
      kind: "chain",
      target,
      body: { inputs, tags, metadata, runType },
    });
  }

  override async handleChainEnd(
    outputs: Record<string, unknown>,
    runId: string,
  ): Promise<void> {
    this.emit({
      runId,
      outcome: "ok",
      extra: {
        output_keys: typeof outputs === "object" ? Object.keys(outputs).sort() : [],
      },
    });
  }

  override async handleChainError(err: unknown, runId: string): Promise<void> {
    this.emit({ runId, outcome: "error", extra: errorFields(err) });
  }

  // ------------------------------------------------------------------
  // Agent events
  //
  // Agent actions/finishes piggyback on the parent chain's run_id; we
  // emit telemetry without gating (the underlying tool call is gated
  // separately via handleToolStart).
  // ------------------------------------------------------------------

  override async handleAgentAction(
    action: AgentAction,
    runId: string,
  ): Promise<void> {
    if (!this.sink) return;
    const tool = action.tool || "unknown";
    try {
      this.sink.enqueue(makeAgentEvent({
        runId,
        agentId: this.agentId,
        kind: "agent_action",
        target: tool,
      }));
    } catch (err) {
      this.logger?.warn("checkrd: telemetry enqueue failed for agent_action", err);
    }
  }

  override async handleAgentEnd(
    finish: AgentFinish,
    runId: string,
  ): Promise<void> {
    if (!this.sink) return;
    // ``finish`` is unused for the wire payload — it only carries
    // ``return_values`` shape, which was previously logged as a
    // sorted-key list under a ``return_values_keys`` field that
    // the ingest schema rejects. We still log it at debug for
    // local correlation when DEBUG=1.
    void finish;
    try {
      this.sink.enqueue(makeAgentEvent({
        runId,
        agentId: this.agentId,
        kind: "agent_finish",
        target: "finish",
      }));
    } catch (err) {
      this.logger?.warn("checkrd: telemetry enqueue failed for agent_finish", err);
    }
  }
}

/**
 * Build a wire-schema-compliant TelemetryEventInput for the agent-
 * action / agent-finish callbacks. Those don't go through ``gate``
 * (they piggyback on the parent chain's evaluation), so we
 * synthesize a minimal event here. No ``event_type`` / ``tool`` /
 * ``return_values_keys`` — those would trigger HTTP 422 at the
 * ingest endpoint.
 */
function makeAgentEvent(args: {
  runId: string;
  agentId: string;
  kind: string;
  target: string;
}): Record<string, unknown> {
  return {
    request_id: args.runId,
    agent_id: args.agentId,
    timestamp: new Date().toISOString(),
    url_host: LANGCHAIN_AUTHORITY,
    url_path: `/${args.kind}/${args.target}`,
    method: "POST",
    status_code: 200,
    policy_result: "allowed",
    span_name: `langchain.${args.kind} ${args.target}`,
    span_status_code: "OK",
  };
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function resolveModelName(serialized: Serialized | undefined): string | null {
  if (!serialized) return null;
  const kwargs = (serialized as { kwargs?: Record<string, unknown> }).kwargs;
  if (kwargs && typeof kwargs === "object") {
    for (const key of ["model", "modelName", "deploymentName", "name"]) {
      const v = kwargs[key];
      if (typeof v === "string" && v) return v;
    }
  }
  const id = (serialized as { id?: unknown[]; name?: string }).id;
  if (Array.isArray(id) && id.length) {
    const last = id[id.length - 1];
    if (typeof last === "string") return last;
  }
  const name = (serialized as { name?: string }).name;
  if (typeof name === "string" && name) return name;
  return null;
}

function messageToObject(message: BaseMessage): Record<string, unknown> {
  // BaseMessage exposes `_getType()` and `content`; when present, prefer
  // structured serialization via `.toDict()` / `.lc_serializable` if the
  // user has it. Fall back to a tight pair so body matchers still work.
  const tdict = (message as { toDict?: () => unknown }).toDict;
  if (typeof tdict === "function") {
    try {
      const out = tdict.call(message);
      if (out && typeof out === "object") return out as Record<string, unknown>;
    } catch {
      // fall through
    }
  }
  return {
    type: typeof (message as { _getType?: () => string })._getType === "function"
      ? (message as { _getType: () => string })._getType()
      : message.constructor.name,
    content: (message as { content?: unknown }).content,
  };
}

function extractTokenUsage(result: LLMResult): {
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
} {
  const out = { inputTokens: null, outputTokens: null, totalTokens: null } as {
    inputTokens: number | null;
    outputTokens: number | null;
    totalTokens: number | null;
  };
  const llmOutput = (result as { llmOutput?: Record<string, unknown> }).llmOutput;
  if (llmOutput === undefined) return out;
  const tu =
    llmOutput.tokenUsage ?? llmOutput.usage ?? llmOutput.usageMetadata;
  if (typeof tu !== "object" || tu === null) return out;
  const u = tu as Record<string, unknown>;
  out.inputTokens = coerceInt(u.promptTokens ?? u.inputTokens ?? u.input_tokens);
  out.outputTokens = coerceInt(
    u.completionTokens ?? u.outputTokens ?? u.output_tokens,
  );
  out.totalTokens = coerceInt(u.totalTokens ?? u.total_tokens);
  return out;
}

function extractFinishReason(result: LLMResult): string | null {
  const gens = (result as { generations?: unknown[][] }).generations;
  if (!Array.isArray(gens)) return null;
  for (const batch of gens) {
    if (!Array.isArray(batch)) continue;
    for (const gen of batch) {
      const info = (gen as { generationInfo?: Record<string, unknown> })
        .generationInfo;
      if (info !== undefined) {
        const reason = info.finishReason ?? info.finish_reason ?? info.stopReason;
        if (typeof reason === "string") return reason;
      }
    }
  }
  return null;
}

function coerceInt(v: unknown): number | null {
  if (v == null) return null;
  const n = typeof v === "string" ? parseInt(v, 10) : Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
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

function preview(value: unknown, maxLen = 256): string {
  const s = typeof value === "string" ? value : safeJson(value);
  return s.length <= maxLen ? s : `${s.slice(0, maxLen)}...`;
}

function errorFields(err: unknown): Record<string, unknown> {
  if (err instanceof Error) {
    return { error: err.name, error_message: err.message };
  }
  return { error: typeof err, error_message: String(err) };
}
