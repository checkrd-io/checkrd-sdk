/**
 * Mastra agent framework adapter.
 *
 * Mastra is the fastest-growing TypeScript agent framework as of
 * 2026 (~22K GitHub stars, ~1.8M monthly downloads). Mastra
 * agents internally call OpenAI / Anthropic / Vercel-AI-SDK
 * providers — meaning the existing Checkrd vendor instrumentors
 * already cover their hot path transitively.
 *
 * What this module adds is the ergonomic Mastra-shaped entry point
 * so adopters can wire Checkrd in idiomatically:
 *
 *   - {@link checkrdMastraTelemetry} — produces a Mastra
 *     `Telemetry` provider config that fans events into the
 *     Checkrd telemetry sink.
 *   - {@link wrapMastraAgent} — wrap a Mastra agent so its
 *     `.generate` / `.stream` calls are policy-evaluated even
 *     when the agent uses a custom provider that doesn't go
 *     through one of the seven instrumented vendor SDKs.
 *
 * Structurally typed against Mastra's surface; no hard
 * dependency on the `mastra` package.
 */

import type { TelemetryEvent, TelemetrySink } from "../sinks.js";
import type { WasmEngine } from "../engine.js";
import type { Logger } from "../_logger.js";
import { CheckrdPolicyDenied } from "../exceptions.js";

/**
 * Minimal shape of a Mastra agent. Only the methods we wrap are
 * named; everything else passes through via Proxy.
 */
export interface MastraAgentLike {
  name?: string;
  generate?: (input: unknown, options?: unknown) => Promise<unknown>;
  stream?: (input: unknown, options?: unknown) => Promise<unknown>;
  [key: string | symbol]: unknown;
}

/** Options for {@link wrapMastraAgent} and {@link checkrdMastraTelemetry}. */
export interface MastraIntegrationOptions {
  /** Engine instance — typically the one returned from `init()`. */
  engine: WasmEngine;
  /** Raise on deny when true; observe-only when false. */
  enforce: boolean;
  /** Agent ID used as a telemetry correlation field. */
  agentId: string;
  /** Optional sink that receives per-call telemetry events. */
  sink?: TelemetrySink | undefined;
  /** Optional logger for adapter diagnostics. */
  logger?: Logger | undefined;
  /** Dashboard base URL for deny deep-links. */
  dashboardUrl?: string | undefined;
}

/**
 * Wrap a Mastra agent so every `.generate` / `.stream` invocation
 * is policy-evaluated before the underlying model call runs.
 *
 *     import { Agent } from "@mastra/core/agent";
 *     import { openai } from "@ai-sdk/openai";
 *     import { wrapMastraAgent } from "checkrd/mastra";
 *
 *     const raw = new Agent({
 *       name: "support-bot",
 *       model: openai("gpt-4o"),
 *       instructions: "You are a helpful support agent.",
 *     });
 *
 *     export const agent = wrapMastraAgent(raw, {
 *       engine, enforce: true, agentId: "support-bot",
 *     });
 */
export function wrapMastraAgent<T extends MastraAgentLike>(
  agent: T,
  options: MastraIntegrationOptions,
): T {
  const agentName = agent.name ?? options.agentId;

  return new Proxy(agent, {
    get(target, prop, receiver) {
      const original = Reflect.get(target, prop, receiver) as unknown;
      if (typeof original !== "function") return original;

      if (prop === "generate" || prop === "stream") {
        const method = prop;
        return async function patchedMastraInvoke(
          this: unknown,
          input: unknown,
          opts?: unknown,
        ): Promise<unknown> {
          const requestId = globalThis.crypto.randomUUID();
          const now = new Date();
          const body = safeStringify(input);

          const result = options.engine.evaluate({
            request_id: requestId,
            method: "POST",
            url: `https://mastra.local/agents/${encodeURIComponent(agentName)}/${method}`,
            headers: [
              ["content-type", "application/json"],
              ["x-mastra-agent", agentName],
            ],
            body,
            timestamp: now.toISOString(),
            timestamp_ms: now.getTime(),
          });

          enqueueEval(options.sink, options.logger, result.telemetry_json, options.agentId);

          if (!result.allowed) {
            options.logger?.warn("Mastra agent invocation denied", {
              requestId: result.request_id,
              agent: agentName,
              reason: result.deny_reason,
            });
            if (options.enforce) {
              throw new CheckrdPolicyDenied({
                reason: result.deny_reason ?? "policy denied",
                requestId: result.request_id,
                url: `mastra://${agentName}/${method}`,
                ...(options.dashboardUrl !== undefined ? { dashboardUrl: options.dashboardUrl } : {}),
              });
            }
          }

          return (original as (i: unknown, o?: unknown) => Promise<unknown>).call(
            target, input, opts,
          );
        };
      }

      return original;
    },
  });
}

/**
 * Build a Mastra-compatible telemetry provider config. Mastra's
 * telemetry layer expects a function that receives event records
 * and is responsible for fan-out; we return a closure that pumps
 * every event into the configured sink.
 *
 *     import { Mastra } from "@mastra/core";
 *     import { checkrdMastraTelemetry } from "checkrd/mastra";
 *
 *     export const mastra = new Mastra({
 *       telemetry: checkrdMastraTelemetry({
 *         engine, enforce: false, agentId: "mastra-app",
 *         sink,  // your existing TelemetrySink (ControlPlane / OTLP / file)
 *       }),
 *       agents: { ... },
 *     });
 */
export function checkrdMastraTelemetry(
  options: MastraIntegrationOptions,
): { onEvent: (event: TelemetryEvent) => void } {
  const sink = options.sink;
  return {
    onEvent: (event) => {
      if (!sink) return;
      try {
        const enriched: TelemetryEvent = {
          ...event,
          agent_id: options.agentId,
          telemetry_source: "mastra",
        };
        sink.enqueue(enriched);
      } catch (err) {
        options.logger?.debug("checkrdMastraTelemetry: enqueue failed", { err });
      }
    },
  };
}

function safeStringify(value: unknown): string | null {
  try {
    return JSON.stringify(value);
  } catch {
    return null;
  }
}

function enqueueEval(
  sink: TelemetrySink | undefined,
  logger: Logger | undefined,
  telemetryJson: string,
  agentId: string,
): void {
  if (!sink || telemetryJson.length === 0) return;
  try {
    const event = JSON.parse(telemetryJson) as TelemetryEvent;
    event.agent_id = agentId;
    sink.enqueue(event);
  } catch (err) {
    logger?.debug("failed to parse telemetry_json from engine", { err });
  }
}
