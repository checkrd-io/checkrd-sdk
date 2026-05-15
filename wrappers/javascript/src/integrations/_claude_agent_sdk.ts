/* eslint-disable @typescript-eslint/require-await -- the Claude Agent
   SDK's hook protocol requires async functions returning a JSON output
   so the agent runtime can ``await`` the decision; many of our hooks
   delegate to a sync ``evaluateForHook()`` and have no inner ``await``.
   The async signature is structural, not nominal. */
/**
 * Anthropic Claude Agent SDK (TypeScript) integration.
 *
 * The Claude Agent SDK exposes lifecycle hooks via
 * `ClaudeAgentOptions.hooks` — async functions invoked at well-defined
 * points in the agent's run loop: `PreToolUse`, `PostToolUse`,
 * `UserPromptSubmit`, `Stop`, etc.
 *
 * This module ships factory functions that return hook callbacks
 * wired to the Checkrd WASM core:
 *
 * - {@link makePreToolUseHook} — evaluate every tool call and block
 *   on deny.
 * - {@link makePostToolUseHook} — emit telemetry per tool result.
 * - {@link makeUserPromptSubmitHook} — gate user prompts before
 *   Claude reasons about them.
 * - {@link makeStopHook} — emit a final telemetry event when the
 *   agent finishes.
 *
 * The {@link attachToOptions} convenience wires the four standard
 * hooks onto an existing `ClaudeAgentOptions`. Idempotent — calling
 * twice does not register duplicates. User-supplied hooks remain.
 *
 * Mirrors `checkrd.integrations.claude_agent_sdk` in the Python SDK
 * one-for-one. Operators write one policy YAML; the same rules fire
 * across Python and JS Claude agents on the
 * `claude-agent.local` synthetic-URL scheme.
 *
 * Deny semantics: on deny the hook returns
 * `{ decision: "block", systemMessage: <reason> }` per the SDK's
 * documented protocol. The `claude-code` CLI subprocess interprets
 * this as "do not run the tool / do not proceed" and reports the
 * `systemMessage` back to the agent's reasoning loop.
 *
 * Why duck-typed: the Claude Agent SDK is rapidly evolving on both
 * the Python and TS sides. Structural typing means a minor SDK bump
 * doesn't force a Checkrd release. The contract verified in tests
 * is narrow: hook input is a JSON object with a `tool_name` /
 * `prompt` / `session_id` field; the hook returns a JSON object.
 */

import type { WasmEngine, EvaluateRequest } from "../engine.js";
import type { TelemetrySink } from "../sinks.js";
import type { Logger } from "../_logger.js";

const AUTHORITY = "claude-agent.local";

/**
 * Symbol marker on hooks installed by Checkrd. Used by
 * {@link attachToOptions} to detect prior installation and avoid
 * registering duplicate hooks across repeated calls.
 */
const CHECKRD_INSTALLED = Symbol.for("checkrd.installed");

/** Output shape the Claude Agent SDK accepts from a hook. */
export type HookJsonOutput = Record<string, unknown> & {
  decision?: "block";
  systemMessage?: string;
  permissionDecision?: "allow" | "deny" | "ask";
};

/** Async hook callback shape used by `ClaudeAgentOptions.hooks`. */
export type HookCallback = (
  input: Record<string, unknown>,
  toolUseId: string | undefined,
  context: unknown,
) => Promise<HookJsonOutput>;

/** Subset of `HookMatcher` we construct. */
export interface HookMatcherLike {
  matcher?: string | undefined;
  hooks: HookCallback[];
  timeout?: number | undefined;
}

/** Subset of `ClaudeAgentOptions` we mutate in {@link attachToOptions}. */
export interface ClaudeAgentOptionsLike {
  hooks?: Record<string, HookMatcherLike[]> | undefined;
  [key: string]: unknown;
}

/** Shared options for every hook factory. */
export interface CheckrdClaudeAgentOptions {
  engine: WasmEngine;
  agentId: string;
  sink?: TelemetrySink | undefined;
  enforce?: boolean | undefined;
  dashboardUrl?: string | undefined;
  logger?: Logger | undefined;
}

// ---------------------------------------------------------------------
// Public factory functions
// ---------------------------------------------------------------------

/**
 * Build a `PreToolUse` hook that policy-evaluates each tool call.
 *
 * The synthetic URL is `https://claude-agent.local/tools/<tool_name>`
 * so policy authors write rules against `claude-agent.local/tools/Bash`,
 * `claude-agent.local/tools/Write`, etc.
 */
export function makePreToolUseHook(
  options: CheckrdClaudeAgentOptions,
): HookCallback {
  const enforce = options.enforce ?? true;
  const dashboardBase = (options.dashboardUrl ?? "").replace(/\/$/, "");

  const hook: HookCallback = async (input, toolUseId) => {
    const toolName = asString(input.tool_name) ?? "unknown";
    const toolInput = input.tool_input ?? {};
    const sessionId = asString(input.session_id) ?? "";
    const requestId = toolUseId ?? sessionId;

    const result = evaluateForHook({
      engine: options.engine,
      requestId,
      kind: "tools",
      target: toolName,
      bodyObj: { tool_input: toolInput, session_id: sessionId },
      extraHeaders: [
        ["x-claude-agent-tool", toolName],
        ["x-claude-agent-tool-use-id", toolUseId ?? ""],
        ["x-claude-agent-session-id", sessionId],
      ],
    });

    enqueueSafe(options, buildHookEvent({
      requestId: result.request_id,
      agentId: options.agentId,
      kind: "tools",
      target: toolName,
      allowed: result.allowed,
      denyReason: result.deny_reason,
    }));

    if (result.allowed) return {};
    if (!enforce) {
      options.logger?.warn(
        `checkrd: claude-agent tool ${toolName} denied (observation mode): ${result.deny_reason ?? ""}`,
      );
      return {};
    }
    let message: string = result.deny_reason ?? "policy denied";
    if (dashboardBase) {
      message = `${message} (dashboard: ${dashboardBase}/events/${result.request_id})`;
    }
    return { decision: "block", systemMessage: message };
  };

  markInstalled(hook);
  return hook;
}

/**
 * Build a `PostToolUse` hook that emits telemetry per tool result.
 * Does not block — purely observational.
 */
export function makePostToolUseHook(
  options: CheckrdClaudeAgentOptions,
): HookCallback {
  const hook: HookCallback = async (input, toolUseId) => {
    const toolName = asString(input.tool_name) ?? "unknown";
    const toolResponse = input.tool_response;
    const sessionId = asString(input.session_id) ?? "";

    void toolResponse;
    enqueueSafe(options, buildHookEvent({
      requestId: toolUseId ?? sessionId,
      agentId: options.agentId,
      kind: "post-tool",
      target: toolName,
      allowed: true,
      denyReason: null,
    }));
    return {};
  };

  markInstalled(hook);
  return hook;
}

/**
 * Build a `UserPromptSubmit` hook that policy-evaluates user prompts
 * before Claude reasons about them. Useful for prompt-injection
 * defenses, sensitive-topic blocks, etc.
 */
export function makeUserPromptSubmitHook(
  options: CheckrdClaudeAgentOptions,
): HookCallback {
  const enforce = options.enforce ?? true;
  const dashboardBase = (options.dashboardUrl ?? "").replace(/\/$/, "");

  const hook: HookCallback = async (input) => {
    const prompt = asString(input.prompt) ?? "";
    const sessionId = asString(input.session_id) ?? "";

    const result = evaluateForHook({
      engine: options.engine,
      requestId: sessionId,
      kind: "prompts",
      target: "user-prompt",
      bodyObj: { prompt, session_id: sessionId },
      extraHeaders: [["x-claude-agent-session-id", sessionId]],
    });

    void prompt;
    enqueueSafe(options, buildHookEvent({
      requestId: result.request_id,
      agentId: options.agentId,
      kind: "prompts",
      target: "user-prompt",
      allowed: result.allowed,
      denyReason: result.deny_reason,
    }));

    if (result.allowed) return {};
    if (!enforce) {
      options.logger?.warn(
        `checkrd: claude-agent user prompt denied (observation mode): ${result.deny_reason ?? ""}`,
      );
      return {};
    }
    let message: string = result.deny_reason ?? "policy denied";
    if (dashboardBase) {
      message = `${message} (dashboard: ${dashboardBase}/events/${result.request_id})`;
    }
    return { decision: "block", systemMessage: message };
  };

  markInstalled(hook);
  return hook;
}

/** Build a `Stop` hook that emits a final telemetry event when the agent finishes. */
export function makeStopHook(
  options: Omit<CheckrdClaudeAgentOptions, "engine"> & { engine?: WasmEngine },
): HookCallback {
  const hook: HookCallback = async (input) => {
    const sessionId = asString(input.session_id) ?? "";
    enqueueSafe(options, buildHookEvent({
      requestId: sessionId || `claude-agent-stop-${Date.now().toString()}`,
      agentId: options.agentId,
      kind: "stop",
      target: "agent",
      allowed: true,
      denyReason: null,
    }));
    return {};
  };

  markInstalled(hook);
  return hook;
}

// ---------------------------------------------------------------------
// Convenience: attach all four hooks to an options object
// ---------------------------------------------------------------------

/** Options for {@link attachToOptions}. */
export interface AttachToOptionsOptions extends CheckrdClaudeAgentOptions {
  /**
   * Optional regex-string scoping the `PreToolUse` and `PostToolUse`
   * matchers (e.g. `"Bash|Write|Edit"`). Default `undefined` matches
   * every tool.
   */
  toolMatcher?: string | undefined;
}

/**
 * Mutate `options` to add Checkrd hooks on the four standard events.
 *
 * Idempotent: calling this twice on the same options object does not
 * add duplicate hooks. User-supplied hooks remain in place — Checkrd
 * hooks are appended to the same matchers.
 *
 *     import { query, ClaudeAgentOptions } from "@anthropic-ai/claude-agent-sdk";
 *     import { attachToOptions } from "checkrd/claude-agent-sdk";
 *
 *     const options: ClaudeAgentOptions = {};
 *     attachToOptions(options, { engine, agentId, sink, enforce: true });
 *     for await (const msg of query({ prompt: "...", options })) { ... }
 */
export function attachToOptions(
  options: ClaudeAgentOptionsLike,
  attachOptions: AttachToOptionsOptions,
): ClaudeAgentOptionsLike {
  const preTool = makePreToolUseHook(attachOptions);
  const postTool = makePostToolUseHook(attachOptions);
  const promptSubmit = makeUserPromptSubmitHook(attachOptions);
  const stop = makeStopHook(attachOptions);

  const hooks: Record<string, HookMatcherLike[]> = { ...(options.hooks ?? {}) };

  function appendIfNew(event: string, matcher: HookMatcherLike): void {
    const existing = hooks[event] ?? [];
    for (const hm of existing) {
      for (const fn of hm.hooks) {
        if ((fn as unknown as Record<symbol, unknown>)[CHECKRD_INSTALLED]) {
          return;
        }
      }
    }
    hooks[event] = [...existing, matcher];
  }

  appendIfNew("PreToolUse", {
    matcher: attachOptions.toolMatcher,
    hooks: [preTool],
    timeout: 30,
  });
  appendIfNew("PostToolUse", {
    matcher: attachOptions.toolMatcher,
    hooks: [postTool],
  });
  appendIfNew("UserPromptSubmit", { hooks: [promptSubmit] });
  appendIfNew("Stop", { hooks: [stop] });

  options.hooks = hooks;
  return options;
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function evaluateForHook(args: {
  engine: WasmEngine;
  requestId: string;
  kind: string;
  target: string;
  bodyObj: unknown;
  extraHeaders: [string, string][];
}) {
  const url = `https://${AUTHORITY}/${args.kind}/${args.target}`;
  const now = new Date();
  const request: EvaluateRequest = {
    request_id: args.requestId,
    method: "POST",
    url,
    headers: [["x-claude-agent-kind", args.kind], ...args.extraHeaders],
    body: safeJson(args.bodyObj),
    timestamp: now.toISOString(),
    timestamp_ms: now.valueOf(),
  };
  return args.engine.evaluate(request);
}

function enqueueSafe(
  options: { sink?: TelemetrySink | undefined; logger?: Logger | undefined },
  event: Record<string, unknown>,
): void {
  if (!options.sink) return;
  try {
    options.sink.enqueue(event);
  } catch (err) {
    options.logger?.warn(
      "checkrd: claude-agent telemetry enqueue failed",
      err,
    );
  }
}

/**
 * Build a wire-schema-compliant TelemetryEventInput for one
 * Claude Agent SDK hook callback. Mirrors the Python adapter so
 * a single dashboard query covers both runtimes. Older shapes
 * emitted ``event_type`` / ``tool_name`` / ``allowed`` and
 * 422'd the batch at the ingest endpoint.
 */
function buildHookEvent(args: {
  requestId: string;
  agentId: string;
  kind: string;
  target: string;
  allowed: boolean;
  denyReason: string | null | undefined;
}): Record<string, unknown> {
  const now = new Date();
  const event: Record<string, unknown> = {
    request_id: args.requestId || `claude-agent-${now.getTime().toString()}`,
    agent_id: args.agentId,
    timestamp: now.toISOString(),
    url_host: AUTHORITY,
    url_path: `/${args.kind}/${args.target}`,
    method: "POST",
    span_name: `claude-agent.${args.kind} ${args.target}`,
  };
  if (args.allowed) {
    event.status_code = 200;
    event.policy_result = "allowed";
    event.span_status_code = "OK";
  } else {
    event.status_code = 403;
    event.policy_result = "denied";
    event.span_status_code = "ERROR";
    if (args.denyReason) event.deny_reason = args.denyReason;
  }
  return event;
}

function markInstalled(fn: HookCallback): void {
  (fn as unknown as Record<symbol, unknown>)[CHECKRD_INSTALLED] = true;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
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

// `preview` removed alongside the legacy ``event_type``-shaped
// telemetry events that referenced it — the wire-schema-compliant
// builds in ``buildHookEvent`` no longer carry free-form
// previews. Re-add when the schema gains a structured ``body``
// or ``preview`` slot.
