/**
 * MCP middleware tests — both the client wrapper (intercepts calls
 * before they reach the server) and the server wrapper (intercepts
 * handler registrations before user code runs).
 *
 * The MCP SDK itself is not installed in devDependencies (it's a
 * runtime-optional peer dep when customers actually use MCP), so
 * these tests use hand-rolled structural stand-ins that match the
 * shapes the real SDK emits.
 */
import { describe, expect, it, vi } from "vitest";

import { WasmEngine } from "../../src/engine.js";
import { CheckrdPolicyDenied } from "../../src/exceptions.js";
import {
  wrapMcpClient,
  wrapMcpServer,
  type McpClientLike,
  type McpRequestHandler,
  type McpServerLike,
} from "../../src/integrations/_mcp.js";

const ALLOW_ALL = JSON.stringify({ agent: "t", default: "allow", rules: [] });
const DENY_ALL = JSON.stringify({ agent: "t", default: "deny", rules: [] });
const DENY_SEARCH = JSON.stringify({
  agent: "t",
  default: "allow",
  rules: [
    {
      name: "block-search-tool",
      deny: {
        method: ["POST"],
        url: "mcp/tools/search",
      },
    },
  ],
});

function stubClient(): McpClientLike & {
  callTool: ReturnType<typeof vi.fn>;
  readResource: ReturnType<typeof vi.fn>;
  getPrompt: ReturnType<typeof vi.fn>;
  listTools: ReturnType<typeof vi.fn>;
} {
  return {
    callTool: vi.fn(async (_params: { name: string; arguments?: unknown }) => ({
      content: [{ type: "text", text: "ok" }],
    })),
    readResource: vi.fn(async (_params: { uri: string }) => ({
      contents: [{ uri: _params.uri, text: "hi" }],
    })),
    getPrompt: vi.fn(async (_params: { name: string }) => ({ messages: [] })),
    listTools: vi.fn(async () => ({ tools: [] })),
  };
}

describe("wrapMcpClient — allow path", () => {
  it("forwards callTool when policy allows", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const client = stubClient();
    const wrapped = wrapMcpClient(client, {
      engine,
      enforce: true,
      agentId: "test",
    });
    const result = await wrapped.callTool({ name: "search", arguments: { q: "x" } });
    expect(result).toEqual({ content: [{ type: "text", text: "ok" }] });
    expect(client.callTool).toHaveBeenCalledOnce();
  });

  it("forwards readResource when policy allows", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const client = stubClient();
    const wrapped = wrapMcpClient(client, {
      engine,
      enforce: true,
      agentId: "test",
    });
    await wrapped.readResource!({ uri: "file://x" });
    expect(client.readResource).toHaveBeenCalledOnce();
  });

  it("forwards listTools when policy allows", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const client = stubClient();
    const wrapped = wrapMcpClient(client, {
      engine,
      enforce: true,
      agentId: "test",
    });
    await wrapped.listTools!();
    expect(client.listTools).toHaveBeenCalledOnce();
  });
});

describe("wrapMcpClient — deny path", () => {
  it("throws CheckrdPolicyDenied on default-deny policy under enforce=true", async () => {
    const engine = new WasmEngine(DENY_ALL, "test");
    const client = stubClient();
    const wrapped = wrapMcpClient(client, {
      engine,
      enforce: true,
      agentId: "test",
    });
    await expect(
      wrapped.callTool({ name: "search" }),
    ).rejects.toBeInstanceOf(CheckrdPolicyDenied);
    expect(client.callTool).not.toHaveBeenCalled();
  });

  it("blocks a specific tool via URL matcher", async () => {
    const engine = new WasmEngine(DENY_SEARCH, "test");
    const client = stubClient();
    const wrapped = wrapMcpClient(client, {
      engine,
      enforce: true,
      agentId: "test",
    });
    // search is denied
    await expect(
      wrapped.callTool({ name: "search" }),
    ).rejects.toBeInstanceOf(CheckrdPolicyDenied);
    expect(client.callTool).not.toHaveBeenCalled();
    // other tools still work
    await wrapped.callTool({ name: "fetch" });
    expect(client.callTool).toHaveBeenCalledOnce();
  });

  it("under enforce=false, forwards the call but fires telemetry", async () => {
    const engine = new WasmEngine(DENY_ALL, "test");
    const client = stubClient();
    const wrapped = wrapMcpClient(client, {
      engine,
      enforce: false,
      agentId: "test",
    });
    const result = await wrapped.callTool({ name: "search" });
    expect(result).toEqual({ content: [{ type: "text", text: "ok" }] });
    expect(client.callTool).toHaveBeenCalledOnce();
  });
});

describe("wrapMcpClient — proxy transparency", () => {
  it("forwards unknown properties unchanged", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const client = {
      ...stubClient(),
      customField: "preserved",
      customMethod: () => "result",
    };
    const wrapped = wrapMcpClient(client as unknown as McpClientLike, {
      engine,
      enforce: true,
      agentId: "test",
    });
    expect((wrapped as unknown as { customField: string }).customField).toBe("preserved");
    expect((wrapped as unknown as { customMethod: () => string }).customMethod()).toBe("result");
  });
});

describe("wrapMcpServer — handler wrapping", () => {
  it("registerTool handler runs after policy check under allow-all", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    let registered: McpRequestHandler | null = null;
    const server: McpServerLike = {
      registerTool: vi.fn(
        (_name: string, _config: unknown, handler: McpRequestHandler) => {
          registered = handler;
          return undefined;
        },
      ),
    };
    const userHandler = vi.fn(async () => ({ result: "ok" }));

    const wrapped = wrapMcpServer(server, {
      engine,
      enforce: true,
      agentId: "test",
    });
    wrapped.registerTool!("search", { schema: {} }, userHandler);

    expect(server.registerTool).toHaveBeenCalledOnce();
    // Now invoke the registered handler (which is the policy-wrapped version).
    const out = await registered!({ params: { name: "search", arguments: {} } });
    expect(out).toEqual({ result: "ok" });
    expect(userHandler).toHaveBeenCalledOnce();
  });

  it("registerTool handler blocks user handler under default-deny", async () => {
    const engine = new WasmEngine(DENY_ALL, "test");
    let registered: McpRequestHandler | null = null;
    const server: McpServerLike = {
      registerTool: vi.fn(
        (_name: string, _config: unknown, handler: McpRequestHandler) => {
          registered = handler;
          return undefined;
        },
      ),
    };
    const userHandler = vi.fn(async () => ({ result: "ok" }));

    const wrapped = wrapMcpServer(server, {
      engine,
      enforce: true,
      agentId: "test",
    });
    wrapped.registerTool!("search", { schema: {} }, userHandler);

    await expect(
      registered!({ params: { name: "search" } }),
    ).rejects.toBeInstanceOf(CheckrdPolicyDenied);
    expect(userHandler).not.toHaveBeenCalled();
  });

  it("setRequestHandler wraps the handler the same way", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    let registered: McpRequestHandler | null = null;
    const server: McpServerLike = {
      setRequestHandler: vi.fn((_schema: unknown, handler: McpRequestHandler) => {
        registered = handler;
        return undefined;
      }),
    };
    const userHandler = vi.fn(async () => ({ ok: true }));

    const wrapped = wrapMcpServer(server, {
      engine,
      enforce: true,
      agentId: "test",
    });
    wrapped.setRequestHandler!({ fake: "schema" }, userHandler);

    await registered!({ params: { name: "anything" } });
    expect(userHandler).toHaveBeenCalledOnce();
  });
});
