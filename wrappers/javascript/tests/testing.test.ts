import { describe, expect, it, vi } from "vitest";

import { MockEngine, mockWrap } from "../src/testing.js";
import { CheckrdPolicyDenied } from "../src/exceptions.js";

describe("MockEngine", () => {
  it("defaults to allow", () => {
    const engine = new MockEngine();
    const result = engine.evaluate({
      request_id: "r",
      method: "POST",
      url: "https://api.openai.com/v1/chat",
      headers: [],
      body: null,
      timestamp: "",
      timestamp_ms: 0,
    });
    expect(result.allowed).toBe(true);
  });

  it("default=deny denies all", () => {
    const engine = new MockEngine({ default: "deny" });
    const result = engine.evaluate({
      request_id: "r",
      method: "GET",
      url: "https://a",
      headers: [],
      body: null,
      timestamp: "",
      timestamp_ms: 0,
    });
    expect(result.allowed).toBe(false);
  });

  it("policyFn controls the decision", () => {
    const engine = new MockEngine({
      policyFn: (method) => method === "GET",
    });
    const ok = engine.evaluate({
      request_id: "r",
      method: "GET",
      url: "u",
      headers: [],
      body: null,
      timestamp: "",
      timestamp_ms: 0,
    });
    const blocked = engine.evaluate({
      request_id: "r",
      method: "POST",
      url: "u",
      headers: [],
      body: null,
      timestamp: "",
      timestamp_ms: 0,
    });
    expect(ok.allowed).toBe(true);
    expect(blocked.allowed).toBe(false);
  });

  it("kill switch overrides policy", () => {
    const engine = new MockEngine({ default: "allow" });
    engine.setKillSwitch(true);
    const result = engine.evaluate({
      request_id: "r",
      method: "GET",
      url: "u",
      headers: [],
      body: null,
      timestamp: "",
      timestamp_ms: 0,
    });
    expect(result.allowed).toBe(false);
    expect(result.deny_reason).toBe("kill switch active");
  });

  it("records every evaluated request for inspection", () => {
    const engine = new MockEngine();
    engine.evaluate({
      request_id: "r",
      method: "POST",
      url: "https://api.example.com/",
      headers: [["x-trace", "abc"]],
      body: null,
      timestamp: "",
      timestamp_ms: 0,
    });
    expect(engine.events).toHaveLength(1);
    expect(engine.events[0]!.url).toBe("https://api.example.com/");
  });
});

describe("mockWrap", () => {
  it("builds a fetch that enforces allow-all by default", async () => {
    const base = vi.fn(async () => new Response("{}", { status: 200 }));
    const f = mockWrap(base as unknown as typeof fetch, {
      enforce: true,
      agentId: "test",
    });
    const res = await f("https://example.com/");
    expect(res.status).toBe(200);
    expect(base).toHaveBeenCalledOnce();
  });

  it("enforces deny when default=deny", async () => {
    const base = vi.fn(async () => new Response("{}", { status: 200 }));
    const f = mockWrap(base as unknown as typeof fetch, {
      enforce: true,
      agentId: "test",
      default: "deny",
    });
    await expect(f("https://example.com/")).rejects.toBeInstanceOf(CheckrdPolicyDenied);
    expect(base).not.toHaveBeenCalled();
  });
});
