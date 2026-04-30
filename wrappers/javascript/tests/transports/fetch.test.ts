import { describe, expect, it, vi } from "vitest";

import { WasmEngine } from "../../src/engine.js";
import { CheckrdPolicyDenied } from "../../src/exceptions.js";
import { wrapFetch } from "../../src/transports/fetch.js";

const ALLOW_ALL = JSON.stringify({ agent: "test", default: "allow", rules: [] });
const DENY_ALL = JSON.stringify({ agent: "test", default: "deny", rules: [] });

function fakeFetch(): typeof fetch {
  return vi
    .fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }) as unknown as typeof fetch;
}

describe("wrapFetch — happy path", () => {
  it("forwards the request when policy allows", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const base = fakeFetch();
    const f = wrapFetch(base, { engine, enforce: true, agentId: "test" });
    const res = await f("https://example.com/");
    expect(res.status).toBe(200);
    expect(base).toHaveBeenCalledOnce();
  });

  it("calls onAllow with a CheckrdEvent", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const onAllow = vi.fn();
    const f = wrapFetch(fakeFetch(), {
      engine,
      enforce: true,
      agentId: "test",
      onAllow,
    });
    await f("https://example.com/", { headers: { Authorization: "Bearer secret" } });
    expect(onAllow).toHaveBeenCalledOnce();
    const event = onAllow.mock.calls[0]![0] as { headers: Array<[string, string]>; url: string };
    expect(event.url).toContain("example.com");
    // Authorization must be redacted
    const auth = event.headers.find(([k]) => k.toLowerCase() === "authorization");
    expect(auth?.[1]).toBe("[REDACTED]");
  });
});

describe("wrapFetch — deny path", () => {
  it("throws CheckrdPolicyDenied under enforce=true", async () => {
    const engine = new WasmEngine(DENY_ALL, "test");
    const f = wrapFetch(fakeFetch(), { engine, enforce: true, agentId: "test" });
    await expect(f("https://example.com/")).rejects.toBeInstanceOf(CheckrdPolicyDenied);
  });

  it("forwards the request under enforce=false (observe-only) and fires onDeny", async () => {
    const engine = new WasmEngine(DENY_ALL, "test");
    const base = fakeFetch();
    const onDeny = vi.fn();
    const f = wrapFetch(base, { engine, enforce: false, agentId: "test", onDeny });
    const res = await f("https://example.com/");
    expect(res.status).toBe(200); // forwarded despite deny
    expect(base).toHaveBeenCalledOnce();
    expect(onDeny).toHaveBeenCalledOnce();
  });
});

describe("wrapFetch — before_request hook", () => {
  it("short-circuits when the hook returns false", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const base = fakeFetch();
    const f = wrapFetch(base, {
      engine,
      enforce: true,
      agentId: "test",
      beforeRequest: () => false,
    });
    await expect(f("https://example.com/")).rejects.toBeInstanceOf(CheckrdPolicyDenied);
    expect(base).not.toHaveBeenCalled();
  });

  it("proceeds when the hook returns undefined", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const base = fakeFetch();
    const f = wrapFetch(base, {
      engine,
      enforce: true,
      agentId: "test",
      beforeRequest: () => undefined,
    });
    const res = await f("https://example.com/");
    expect(res.status).toBe(200);
    expect(base).toHaveBeenCalledOnce();
  });
});

describe("wrapFetch — body handling", () => {
  it("passes small bodies to the engine", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const onAllow = vi.fn();
    const f = wrapFetch(fakeFetch(), { engine, enforce: true, agentId: "test", onAllow });
    await f("https://example.com/", {
      method: "POST",
      body: JSON.stringify({ hello: "world" }),
      headers: { "Content-Type": "application/json" },
    });
    const event = onAllow.mock.calls[0]![0] as { body?: string };
    expect(event.body).toContain("world");
  });

  it("denies bodies over 1 MB in strict mode (matcher-bypass defense)", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const onAllow = vi.fn();
    const f = wrapFetch(fakeFetch(), {
      engine,
      enforce: true,
      agentId: "test",
      onAllow,
      securityMode: "strict",
    });
    const oversized = "x".repeat(1_100_000);
    await expect(
      f("https://example.com/", { method: "POST", body: oversized }),
    ).rejects.toMatchObject({
      name: "CheckrdPolicyDenied",
      reason: "body exceeds 1MB inspection limit",
    });
    expect(onAllow).not.toHaveBeenCalled();
  });

  it("passes oversized bodies through (body=null) in permissive mode", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const onAllow = vi.fn();
    const f = wrapFetch(fakeFetch(), {
      engine,
      enforce: true,
      agentId: "test",
      onAllow,
      securityMode: "permissive",
    });
    const oversized = "x".repeat(1_100_000);
    await f("https://example.com/", { method: "POST", body: oversized });
    const event = onAllow.mock.calls[0]![0] as { body?: string };
    expect(event.body).toBeUndefined();
  });

  it("counts multi-byte UTF-8 correctly (no 4x bypass via emoji padding)", async () => {
    const engine = new WasmEngine(ALLOW_ALL, "test");
    const onAllow = vi.fn();
    const f = wrapFetch(fakeFetch(), {
      engine,
      enforce: true,
      agentId: "test",
      onAllow,
      securityMode: "strict",
    });
    // 4-byte emoji × 300,000 = 1.2 MB of UTF-8 bytes. String length is
    // 600,000 UTF-16 code units — less than MAX_BODY_BYTES as a count,
    // but clearly over the limit as bytes. This test locks in the byte-
    // level measurement fix.
    const emojiPayload = "\u{1F600}".repeat(300_000);
    await expect(
      f("https://example.com/", { method: "POST", body: emojiPayload }),
    ).rejects.toMatchObject({ reason: "body exceeds 1MB inspection limit" });
  });
});
