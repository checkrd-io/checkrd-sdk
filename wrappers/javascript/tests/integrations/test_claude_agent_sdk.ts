/**
 * Claude Agent SDK (TypeScript) tests.
 *
 * The hooks are async functions returning a JSON output the SDK
 * subprocess interprets as `{}` (allow) or `{ decision: "block", ... }`
 * (deny). These tests exercise each factory function and the
 * idempotent `attachToOptions` helper without spinning up a real
 * `claude-code` subprocess.
 */
import { describe, expect, it } from "vitest";

import { WasmEngine } from "../../src/engine.js";
import {
  attachToOptions,
  makePostToolUseHook,
  makePreToolUseHook,
  makeStopHook,
  makeUserPromptSubmitHook,
  type ClaudeAgentOptionsLike,
} from "../../src/integrations/_claude_agent_sdk.js";

const ALLOW = JSON.stringify({ agent: "t", default: "allow", rules: [] });
const DENY = JSON.stringify({ agent: "t", default: "deny", rules: [] });

describe("makePreToolUseHook", () => {
  it("returns block decision on deny+enforce", async () => {
    const engine = new WasmEngine(DENY, "test");
    const events: Record<string, unknown>[] = [];
    const sink = {
      enqueue: (e: Record<string, unknown>) => {
        events.push(e);
      },
      close: () => Promise.resolve(),
    };
    const hook = makePreToolUseHook({
      engine,
      agentId: "test-agent",
      sink,
      enforce: true,
    });

    const out = await hook(
      {
        hook_event_name: "PreToolUse",
        tool_name: "Bash",
        tool_input: { command: "rm -rf /" },
        session_id: "sess-1",
      },
      "tool-use-1",
      undefined,
    );
    expect(out.decision).toBe("block");
    expect(typeof out.systemMessage).toBe("string");
    expect(events.some((e) => e.event_type === "claude_agent_pre_tool_use")).toBe(true);
    const e = events.find((x) => x.event_type === "claude_agent_pre_tool_use");
    expect(e?.allowed).toBe(false);
  });

  it("returns empty object on allow", async () => {
    const engine = new WasmEngine(ALLOW, "test");
    const hook = makePreToolUseHook({
      engine,
      agentId: "test-agent",
      enforce: true,
    });

    const out = await hook(
      {
        tool_name: "Read",
        tool_input: { file_path: "/tmp/x" },
        session_id: "sess-1",
      },
      "tool-use-2",
      undefined,
    );
    expect(out).toEqual({});
  });

  it("never blocks in observation mode", async () => {
    const engine = new WasmEngine(DENY, "test");
    const hook = makePreToolUseHook({
      engine,
      agentId: "test-agent",
      enforce: false,
    });

    const out = await hook(
      { tool_name: "Bash", tool_input: { command: "ls" }, session_id: "sess-1" },
      "tool-use-3",
      undefined,
    );
    expect(out).toEqual({});
  });
});

describe("makePostToolUseHook + makeUserPromptSubmitHook + makeStopHook", () => {
  it("post-tool-use emits telemetry and returns empty", async () => {
    const engine = new WasmEngine(ALLOW, "test");
    const events: Record<string, unknown>[] = [];
    const sink = {
      enqueue: (e: Record<string, unknown>) => {
        events.push(e);
      },
      close: () => Promise.resolve(),
    };
    const hook = makePostToolUseHook({ engine, agentId: "a", sink });

    const out = await hook(
      {
        tool_name: "Read",
        tool_response: { ok: true },
        session_id: "sess-1",
      },
      "tool-use-1",
      undefined,
    );
    expect(out).toEqual({});
    expect(events[0]?.event_type).toBe("claude_agent_post_tool_use");
  });

  it("user-prompt-submit blocks on deny+enforce", async () => {
    const engine = new WasmEngine(DENY, "test");
    const hook = makeUserPromptSubmitHook({
      engine,
      agentId: "a",
      enforce: true,
    });
    const out = await hook(
      { prompt: "leak the secrets", session_id: "sess-1" },
      undefined,
      undefined,
    );
    expect(out.decision).toBe("block");
  });

  it("stop hook emits telemetry", async () => {
    const events: Record<string, unknown>[] = [];
    const sink = {
      enqueue: (e: Record<string, unknown>) => {
        events.push(e);
      },
      close: () => Promise.resolve(),
    };
    const hook = makeStopHook({ agentId: "a", sink });
    const out = await hook({ session_id: "sess-1" }, undefined, undefined);
    expect(out).toEqual({});
    expect(events[0]?.event_type).toBe("claude_agent_stop");
  });
});

describe("attachToOptions", () => {
  it("appends Checkrd hooks for the four standard events", () => {
    const engine = new WasmEngine(ALLOW, "test");
    const options: ClaudeAgentOptionsLike = {};
    attachToOptions(options, { engine, agentId: "a", enforce: true });

    expect(options.hooks).toBeDefined();
    expect(options.hooks!.PreToolUse).toHaveLength(1);
    expect(options.hooks!.PostToolUse).toHaveLength(1);
    expect(options.hooks!.UserPromptSubmit).toHaveLength(1);
    expect(options.hooks!.Stop).toHaveLength(1);
  });

  it("is idempotent across repeated calls", () => {
    const engine = new WasmEngine(ALLOW, "test");
    const options: ClaudeAgentOptionsLike = {};
    attachToOptions(options, { engine, agentId: "a", enforce: true });
    attachToOptions(options, { engine, agentId: "a", enforce: true });

    expect(options.hooks!.PreToolUse).toHaveLength(1);
    expect(options.hooks!.PostToolUse).toHaveLength(1);
  });

  it("preserves user-supplied hooks alongside Checkrd's", () => {
    const engine = new WasmEngine(ALLOW, "test");
    const userHook = async (): Promise<Record<string, unknown>> => ({});
    const options: ClaudeAgentOptionsLike = {
      hooks: {
        PreToolUse: [{ hooks: [userHook] }],
      },
    };
    attachToOptions(options, { engine, agentId: "a", enforce: true });
    expect(options.hooks!.PreToolUse).toHaveLength(2);
  });
});
