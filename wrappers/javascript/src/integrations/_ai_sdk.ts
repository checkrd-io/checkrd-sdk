/**
 * Vercel AI SDK middleware adapter.
 *
 * Exposes `checkrdMiddleware()` — a `LanguageModelV2Middleware`-shaped
 * object you pass to `wrapLanguageModel({ model, middleware })`. Each
 * call through the wrapped model goes through Checkrd's policy
 * evaluation, and telemetry (including streamed token counts) is
 * enqueued to the configured sink.
 *
 * This is the hook competitors like Langfuse, Braintrust, PostHog, and
 * Helicone all expose. It plugs Checkrd into any AI SDK application
 * (generateText / streamText / generateObject / streamObject / tool
 * calling) without the customer changing their business logic.
 *
 * We avoid a hard dependency on the `ai` package: the middleware uses
 * structural typing, so the same module works against AI SDK v4, v5,
 * and v7-beta. If upstream evolves the middleware shape, callers still
 * get a working adapter — the contract verified in tests is narrow.
 */

import { CheckrdPolicyDenied } from "../exceptions.js";
import type { WasmEngine, EvaluateRequest } from "../engine.js";
import type { TelemetrySink } from "../sinks.js";
import type { Logger } from "../_logger.js";
import { getSink } from "../index.js";

/** Options for {@link checkrdMiddleware}. */
export interface CheckrdMiddlewareOptions {
  /** The WASM engine — typically the one returned from init(). */
  engine: WasmEngine;
  /** Raise on deny when true; observe-only when false. */
  enforce: boolean;
  /** Agent ID, used as a correlation field in telemetry. */
  agentId: string;
  /** Optional sink that receives per-call telemetry events. */
  sink?: TelemetrySink | undefined;
  /** Optional logger for middleware diagnostics. */
  logger?: Logger | undefined;
  /** Dashboard base URL for deep links on denial. */
  dashboardUrl?: string | undefined;
}

/**
 * Minimal subset of the AI SDK's LanguageModelV2 call options we rely
 * on. Kept loose so AI SDK v4 / v5 / v7 all satisfy it.
 */
interface LanguageModelCallOptions {
  [key: string]: unknown;
  prompt?: unknown;
  messages?: unknown;
  mode?: unknown;
  maxTokens?: number;
  temperature?: number;
  topP?: number;
  providerOptions?: unknown;
}

/** Minimum structural model shape we read from. */
interface LanguageModelV2 {
  modelId?: string;
  provider?: string;
  specificationVersion?: string;
}

/** Loose typing for `doGenerate` return values — we only read usage. */
interface GenerateResult {
  [key: string]: unknown;
  usage?: { inputTokens?: number; outputTokens?: number; promptTokens?: number; completionTokens?: number };
  finishReason?: string;
}

/** Loose typing for `doStream` return values — we wrap the `stream` field. */
interface StreamResult {
  [key: string]: unknown;
  stream?: ReadableStream<unknown>;
}

/** Typed as produced in AI SDK v5+ stream chunks. */
interface StreamChunk {
  type?: string;
  [key: string]: unknown;
  usage?: { inputTokens?: number; outputTokens?: number };
  finishReason?: string;
}

/** The subset of LanguageModelV2Middleware we implement. */
export interface LanguageModelMiddleware {
  transformParams?: (args: {
    type: "generate" | "stream";
    params: LanguageModelCallOptions;
    model?: LanguageModelV2;
  }) => Promise<LanguageModelCallOptions> | LanguageModelCallOptions;
  wrapGenerate?: (args: {
    doGenerate: () => Promise<GenerateResult>;
    params: LanguageModelCallOptions;
    model: LanguageModelV2;
  }) => Promise<GenerateResult>;
  wrapStream?: (args: {
    doStream: () => Promise<StreamResult>;
    params: LanguageModelCallOptions;
    model: LanguageModelV2;
  }) => Promise<StreamResult>;
}

/**
 * Construct a Vercel AI SDK middleware that runs Checkrd policy around
 * every call and emits telemetry (with streamed token counts) to the
 * configured sink.
 *
 * Use with `wrapLanguageModel` from the `ai` package:
 *
 *     import { wrapLanguageModel } from "ai";
 *     import { openai } from "@ai-sdk/openai";
 *     import { checkrdMiddleware } from "checkrd/ai-sdk";
 *
 *     const model = wrapLanguageModel({
 *       model: openai("gpt-4o"),
 *       middleware: checkrdMiddleware({ engine, enforce: true, agentId: "my-agent" }),
 *     });
 */
export function checkrdMiddleware(opts: CheckrdMiddlewareOptions): LanguageModelMiddleware {
  const { engine, enforce, agentId, logger, dashboardUrl } = opts;
  // Sink resolution: explicit > global (from `init()` /
  // `initAsync()`). Without this fallback, calling
  // ``checkrdMiddleware({ engine, agentId })`` against a global
  // `init()` setup silently dropped every event — the engine
  // produced telemetry, but `enqueueEvent` short-circuited on
  // `!sink` and nothing reached `/v1/telemetry`. Mirrors the way
  // `wrap()` defaults to the global sink. Resolved lazily inside
  // the helpers below so a sink installed AFTER `checkrdMiddleware`
  // (e.g. test fixtures that build the middleware before calling
  // `init`) still works.
  function resolveSink(): TelemetrySink | undefined {
    return opts.sink ?? getSink();
  }

  function gate(
    params: LanguageModelCallOptions,
    model: LanguageModelV2 | undefined,
    operation: "generate" | "stream",
  ): { requestId: string; startMs: number } {
    const requestId = globalThis.crypto.randomUUID();
    const now = new Date();
    const modelId = model?.modelId ?? "unknown";
    const provider = model?.provider ?? "unknown";
    const url = `ai-sdk://${provider}/${modelId}`;
    // We encode the call parameters into a synthetic JSON body so the
    // policy engine can match on request shape (prompt/messages length,
    // temperature, etc.) the same way it matches real HTTP calls.
    const body = JSON.stringify({
      operation,
      provider,
      model: modelId,
      messages: params.messages ?? null,
      prompt: params.prompt ?? null,
      mode: params.mode ?? null,
      temperature: params.temperature ?? null,
      topP: params.topP ?? null,
      maxTokens: params.maxTokens ?? null,
    });
    const evalReq: EvaluateRequest = {
      request_id: requestId,
      method: "POST",
      url,
      headers: [["content-type", "application/json"]],
      body,
      timestamp: now.toISOString(),
      timestamp_ms: now.getTime(),
    };
    const result = engine.evaluate(evalReq);
    enqueueEvent(result.telemetry_json, { agentId, operation, provider, model: modelId });
    if (!result.allowed) {
      logger?.warn("ai-sdk call denied by policy", {
        requestId: result.request_id,
        provider,
        model: modelId,
        reason: result.deny_reason,
      });
      if (enforce) {
        throw new CheckrdPolicyDenied({
          reason: result.deny_reason ?? "policy denied",
          requestId: result.request_id,
          url,
          dashboardUrl: dashboardUrl ?? "",
        });
      }
    }
    return { requestId: result.request_id, startMs: Date.now() };
  }

  function enqueueEvent(
    telemetryJson: string,
    extra: { agentId: string; operation: string; provider: string; model: string },
  ): void {
    const sink = resolveSink();
    if (!sink || telemetryJson.length === 0) return;
    try {
      const event = JSON.parse(telemetryJson) as Record<string, unknown>;
      // Required wire fields. The engine fills these from the
      // synthetic ``ai-sdk://provider/model`` URL we passed into
      // evaluate(); we just need to make sure agent_id is set and
      // the GenAI semconv fields surface the provider + model. No
      // ``ai_sdk`` blob — the ingestion endpoint rejects unknown
      // keys with HTTP 422.
      event.agent_id = extra.agentId;
      event.gen_ai_system ??= extra.provider;
      event.gen_ai_model ??= extra.model;
      sink.enqueue(event);
    } catch (err) {
      logger?.debug("failed to parse telemetry_json from engine", { err });
    }
  }

  function emitCompletionEvent(
    requestId: string,
    startMs: number,
    extra: {
      operation: "generate" | "stream";
      provider: string;
      model: string;
      inputTokens: number | null;
      outputTokens: number | null;
      finishReason: string | null;
    },
  ): void {
    const sink = resolveSink();
    if (!sink) return;
    const latencyMs = Math.max(0, Date.now() - startMs);
    // Schema-compliant ``TelemetryEventInput`` event. Earlier
    // versions emitted fields like ``event_type``, ``operation``,
    // ``input_tokens`` that the ingestion endpoint rejects with
    // HTTP 422 — those drops were invisible because the batcher
    // logged once-per-minute. Every key here is in the
    // ``TelemetryEventInput`` schema; provider / model / token
    // counts use the GenAI semconv field names so dashboard
    // queries can roll up across vendor SDKs + AI SDK calls.
    const now = new Date();
    sink.enqueue({
      request_id: requestId,
      agent_id: agentId,
      timestamp: now.toISOString(),
      url_host: `${extra.provider}.ai-sdk`,
      url_path: `/${extra.operation}/${extra.model}`,
      method: "POST",
      status_code: 200,
      latency_ms: latencyMs,
      policy_result: "allowed",
      span_name: `ai-sdk.${extra.operation} ${extra.provider}/${extra.model}`,
      span_status_code: "OK",
      gen_ai_system: extra.provider,
      gen_ai_model: extra.model,
      gen_ai_input_tokens: extra.inputTokens,
      gen_ai_output_tokens: extra.outputTokens,
    });
  }

  return {
    async wrapGenerate({ doGenerate, params, model }) {
      const { requestId, startMs } = gate(params, model, "generate");
      const result = await doGenerate();
      emitCompletionEvent(requestId, startMs, {
        operation: "generate",
        provider: model.provider ?? "unknown",
        model: model.modelId ?? "unknown",
        inputTokens: result.usage?.inputTokens ?? result.usage?.promptTokens ?? null,
        outputTokens: result.usage?.outputTokens ?? result.usage?.completionTokens ?? null,
        finishReason: result.finishReason ?? null,
      });
      return result;
    },
    async wrapStream({ doStream, params, model }) {
      const { requestId, startMs } = gate(params, model, "stream");
      const result = await doStream();
      const upstream = result.stream;
      if (!upstream) return result;
      let inputTokens: number | null = null;
      let outputTokens: number | null = null;
      let finishReason: string | null = null;

      const emit = (): void => {
        emitCompletionEvent(requestId, startMs, {
          operation: "stream",
          provider: model.provider ?? "unknown",
          model: model.modelId ?? "unknown",
          inputTokens,
          outputTokens,
          finishReason,
        });
      };

      const transform = new TransformStream<StreamChunk, StreamChunk>({
        transform(chunk, controller) {
          if (chunk.type === "finish" || chunk.finishReason !== undefined) {
            if (chunk.usage !== undefined) {
              inputTokens = chunk.usage.inputTokens ?? inputTokens;
              outputTokens = chunk.usage.outputTokens ?? outputTokens;
            }
            if (chunk.finishReason !== undefined) {
              finishReason = chunk.finishReason;
            }
          }
          controller.enqueue(chunk);
        },
        flush() {
          emit();
        },
      });

      return {
        ...result,
        stream: upstream.pipeThrough(transform),
      };
    },
  };
}
