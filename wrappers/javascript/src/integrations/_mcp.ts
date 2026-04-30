/**
 * Model Context Protocol (MCP) integration.
 *
 * Two adapters ship:
 *
 *   - {@link wrapMcpClient} — wrap an MCP client so `callTool`,
 *     `readResource`, and `getPrompt` calls are policy-evaluated
 *     before they reach the server. Agent-side governance: the
 *     usual Checkrd deny semantics, applied to MCP tool invocations.
 *
 *   - {@link wrapMcpServer} — wrap the request-handler registration
 *     on an MCP server so every tool/resource/prompt request is
 *     policy-evaluated before the user's handler runs. Server-side
 *     governance: the MCP server's operator can enforce the same
 *     policy layer agents do.
 *
 * The adapters are intentionally **structurally typed** against the
 * shape of `@modelcontextprotocol/sdk` rather than importing the
 * concrete classes. MCP's surface is iterating quickly; keeping the
 * integration duck-typed means an SDK minor bump doesn't force a
 * Checkrd release. Consumers pay a small `as unknown as` cast at the
 * boundary; we verify the runtime shape in tests.
 *
 * Why MCP specifically: as of 2026 the MCP SDK sees ~97M monthly
 * downloads across Python + TS; the Linux Foundation hosts the spec;
 * 30+ CVEs have been disclosed in the server ecosystem in its first
 * year. An audited, policy-enforcing middleware is the missing piece
 * nobody in the ecosystem has shipped yet.
 */

import { CheckrdPolicyDenied } from "../exceptions.js";
import type { EvaluateRequest, WasmEngine } from "../engine.js";
import type { TelemetryEvent, TelemetrySink } from "../sinks.js";
import type { Logger } from "../_logger.js";

/** Shared options for both wrapping helpers. */
export interface McpPolicyOptions {
  /** WASM engine instance — typically the one returned by `init()`. */
  engine: WasmEngine;
  /** True = raise on deny; false = log-only (observe mode). */
  enforce: boolean;
  /** Agent id used as the telemetry correlation field. */
  agentId: string;
  /** Optional sink that receives per-call telemetry events. */
  sink?: TelemetrySink | undefined;
  /** Optional logger for middleware diagnostics. */
  logger?: Logger | undefined;
  /** Dashboard base URL used in deep links on denial. */
  dashboardUrl?: string | undefined;
  /**
   * Friendly server name used as the authority in synthetic MCP URLs.
   * Default: `"mcp"`. Set to e.g. `"github-mcp"` so policy URL
   * matchers can distinguish servers.
   */
  serverName?: string | undefined;
}

/**
 * Minimal structural shape of the MCP SDK Client. Only the methods we
 * intercept are named; any others pass through via Proxy fallback.
 */
export interface McpClientLike {
  callTool: (params: { name: string; arguments?: unknown }, ...rest: unknown[]) => Promise<unknown>;
  readResource?: (params: { uri: string }, ...rest: unknown[]) => Promise<unknown>;
  getPrompt?: (params: { name: string; arguments?: unknown }, ...rest: unknown[]) => Promise<unknown>;
  listTools?: (...rest: unknown[]) => Promise<unknown>;
  listResources?: (...rest: unknown[]) => Promise<unknown>;
  listPrompts?: (...rest: unknown[]) => Promise<unknown>;
  [key: string | symbol]: unknown;
}

/**
 * Minimal structural shape of the MCP SDK Server. The methods we
 * intercept are `setRequestHandler` (for ad-hoc schema-based handlers)
 * and the higher-level convenience registrars when present
 * (`registerTool`, `registerResource`, `registerPrompt`).
 */
export interface McpServerLike {
  setRequestHandler?: (schema: unknown, handler: McpRequestHandler, ...rest: unknown[]) => unknown;
  registerTool?: (
    name: string,
    config: unknown,
    handler: McpRequestHandler,
  ) => unknown;
  registerResource?: (
    name: string,
    uri: string,
    config: unknown,
    handler: McpRequestHandler,
  ) => unknown;
  registerPrompt?: (
    name: string,
    config: unknown,
    handler: McpRequestHandler,
  ) => unknown;
  [key: string | symbol]: unknown;
}

/** MCP request handler function shape. Kept loose to match SDK evolution. */
export type McpRequestHandler = (
  request: { method?: string; params?: { name?: string; uri?: string; arguments?: unknown; [key: string]: unknown } },
  extra?: unknown,
) => Promise<unknown>;

type InterceptKind = "tool" | "resource" | "prompt" | "list";

interface InterceptDescriptor {
  /** Method being called, e.g. `callTool`, `readResource`. */
  method: InterceptKind;
  /** The specific tool/resource name, or `"*"` for bulk list ops. */
  name: string;
  /** JSON-serialisable arguments, for body matchers. */
  arguments: unknown;
  /** Synthetic HTTP-ish URL used for policy matching and telemetry. */
  url: string;
}

/**
 * Wrap an MCP client so every tool / resource / prompt call is
 * policy-evaluated before reaching the server.
 *
 * The returned object is a Proxy over the original client — pass it
 * anywhere you'd pass the original. Calls to unmethods / unknown
 * properties flow straight through untouched.
 */
export function wrapMcpClient<T extends McpClientLike>(
  client: T,
  options: McpPolicyOptions,
): T {
  const serverName = options.serverName ?? "mcp";

  return new Proxy(client, {
    get(target, prop, receiver) {
      const original = Reflect.get(target, prop, receiver) as unknown;
      if (typeof original !== "function") return original;

      if (prop === "callTool") {
        return async function patchedCallTool(
          this: unknown,
          params: { name: string; arguments?: unknown },
          ...rest: unknown[]
        ): Promise<unknown> {
          evaluateOrThrow(options, {
            method: "tool",
            name: params.name,
            arguments: params.arguments ?? null,
            url: `https://${serverName}/tools/${encodeURIComponent(params.name)}`,
          });
          return (original as (...args: unknown[]) => Promise<unknown>).call(
            target, params, ...rest,
          );
        };
      }

      if (prop === "readResource") {
        return async function patchedReadResource(
          this: unknown,
          params: { uri: string },
          ...rest: unknown[]
        ): Promise<unknown> {
          evaluateOrThrow(options, {
            method: "resource",
            name: params.uri,
            arguments: null,
            url: `https://${serverName}/resources?uri=${encodeURIComponent(params.uri)}`,
          });
          return (original as (...args: unknown[]) => Promise<unknown>).call(
            target, params, ...rest,
          );
        };
      }

      if (prop === "getPrompt") {
        return async function patchedGetPrompt(
          this: unknown,
          params: { name: string; arguments?: unknown },
          ...rest: unknown[]
        ): Promise<unknown> {
          evaluateOrThrow(options, {
            method: "prompt",
            name: params.name,
            arguments: params.arguments ?? null,
            url: `https://${serverName}/prompts/${encodeURIComponent(params.name)}`,
          });
          return (original as (...args: unknown[]) => Promise<unknown>).call(
            target, params, ...rest,
          );
        };
      }

      // List/meta methods — still policy-evaluated so operators can
      // restrict which tools an agent is allowed to enumerate, but
      // treated as a `list` intercept kind so default-allow policies
      // don't accidentally reject the handshake.
      if (
        prop === "listTools" ||
        prop === "listResources" ||
        prop === "listPrompts"
      ) {
        return async function patchedList(
          this: unknown,
          ...rest: unknown[]
        ): Promise<unknown> {
          evaluateOrThrow(options, {
            method: "list",
            name: "*",
            arguments: null,
            url: `https://${serverName}/${prop.replace(/^list/, "").toLowerCase()}`,
          });
          return (original as (...args: unknown[]) => Promise<unknown>).call(
            target, ...rest,
          );
        };
      }

      return original;
    },
  });
}

/**
 * Wrap an MCP server so every handler registration is intercepted and
 * every handler invocation runs through Checkrd's policy engine
 * before the user's code.
 */
export function wrapMcpServer<T extends McpServerLike>(
  server: T,
  options: McpPolicyOptions,
): T {
  const serverName = options.serverName ?? "mcp";

  // Patch `setRequestHandler` if present. The handler receives the
  // raw request; we wrap the supplied handler to policy-check before
  // delegating.
  return new Proxy(server, {
    get(target, prop, receiver) {
      const original = Reflect.get(target, prop, receiver) as unknown;
      if (typeof original !== "function") return original;

      if (prop === "setRequestHandler") {
        return function patchedSetRequestHandler(
          this: unknown,
          schema: unknown,
          handler: McpRequestHandler,
          ...rest: unknown[]
        ): unknown {
          const wrapped = wrapHandler(handler, options, serverName, "tool");
          return (original as (...args: unknown[]) => unknown).call(
            target, schema, wrapped, ...rest,
          );
        };
      }

      if (prop === "registerTool" || prop === "registerResource" || prop === "registerPrompt") {
        const kind: InterceptKind =
          prop === "registerTool"
            ? "tool"
            : prop === "registerResource"
              ? "resource"
              : "prompt";
        return function patchedRegistrar(
          this: unknown,
          ...args: unknown[]
        ): unknown {
          // The handler is always the last positional arg in both
          // SDK v0.x and v1.x convenience registrars.
          const handler = args[args.length - 1];
          if (typeof handler === "function") {
            args[args.length - 1] = wrapHandler(
              handler as McpRequestHandler,
              options,
              serverName,
              kind,
            );
          }
          return (original as (...a: unknown[]) => unknown).call(target, ...args);
        };
      }

      return original;
    },
  });
}

function wrapHandler(
  handler: McpRequestHandler,
  options: McpPolicyOptions,
  serverName: string,
  kind: InterceptKind,
): McpRequestHandler {
  return async (request, extra) => {
    const params = request.params ?? {};
    const name = typeof params.name === "string"
      ? params.name
      : typeof params.uri === "string"
        ? params.uri
        : "unknown";
    const args = params.arguments ?? null;
    const url = `https://${serverName}/${kindToPath(kind)}/${encodeURIComponent(name)}`;
    evaluateOrThrow(options, {
      method: kind,
      name,
      arguments: args,
      url,
    });
    return handler(request, extra);
  };
}

function kindToPath(kind: InterceptKind): string {
  switch (kind) {
    case "tool":
      return "tools";
    case "resource":
      return "resources";
    case "prompt":
      return "prompts";
    case "list":
      return "list";
  }
}

function evaluateOrThrow(
  options: McpPolicyOptions,
  descriptor: InterceptDescriptor,
): void {
  const { engine, enforce, agentId, sink, logger, dashboardUrl } = options;
  const requestId = globalThis.crypto.randomUUID();
  const now = new Date();
  const body = descriptor.arguments === null ? null : safeStringify(descriptor.arguments);

  const evalReq: EvaluateRequest = {
    request_id: requestId,
    method: "POST",
    url: descriptor.url,
    headers: [
      ["content-type", "application/json"],
      ["x-mcp-method", descriptor.method],
      ["x-mcp-target", descriptor.name],
    ],
    body,
    timestamp: now.toISOString(),
    timestamp_ms: now.getTime(),
  };

  const result = engine.evaluate(evalReq);
  enqueueEval(sink, logger, result.telemetry_json, agentId);

  if (result.allowed) return;

  logger?.warn("MCP policy deny", {
    requestId: result.request_id,
    url: descriptor.url,
    reason: result.deny_reason,
    kind: descriptor.method,
    target: descriptor.name,
  });

  if (!enforce) return;

  throw new CheckrdPolicyDenied({
    reason: result.deny_reason ?? "policy denied",
    requestId: result.request_id,
    url: descriptor.url,
    ...(dashboardUrl !== undefined ? { dashboardUrl } : {}),
  });
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
