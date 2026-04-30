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
  const { engine, enforce, agentId, sink, logger, dashboardUrl } = opts;

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
    if (!sink || telemetryJson.length === 0) return;
    try {
      const event = JSON.parse(telemetryJson) as Record<string, unknown>;
      event.agent_id = extra.agentId;
      event.ai_sdk = {
        operation: extra.operation,
        provider: extra.provider,
        model: extra.model,
      };
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
    if (!sink) return;
    const latencyMs = Math.max(0, Date.now() - startMs);
    sink.enqueue({
      event_type: "ai_sdk_completion",
      request_id: requestId,
      agent_id: agentId,
      latency_ms: latencyMs,
      operation: extra.operation,
      provider: extra.provider,
      model: extra.model,
      input_tokens: extra.inputTokens,
      output_tokens: extra.outputTokens,
      finish_reason: extra.finishReason,
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
