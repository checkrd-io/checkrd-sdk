/**
 * Hono middleware tests. We don't import Hono itself — the middleware
 * is structurally typed against Hono's `Context` shape, so we pass a
 * hand-rolled object that implements the handful of methods we touch
 * (`set`, `get`, `json`). If Hono ever changes its interface in a
 * breaking way, the real Hono tests in `examples/hono-worker/` will
 * catch it; the unit tests here are bulletproof against Hono version
 * churn.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { CheckrdPolicyDenied } from "../../src/exceptions.js";
import { checkrdHono } from "../../src/integrations/_hono.js";
import { resetCheckrdNext } from "../../src/integrations/_next.js";

const ALLOW_ALL = { agent: "t", default: "allow", rules: [] };

interface StubContext {
  vars: Record<string, unknown>;
  set: (key: string, value: unknown) => void;
  get: (key: string) => unknown;
  json: (data: unknown, status?: number) => Response;
  req: { method: string; url: string };
}

function stubContext(): StubContext {
  const vars: Record<string, unknown> = {};
  return {
    vars,
    set: (key, value) => {
      vars[key] = value;
    },
    get: (key) => vars[key],
    json: (data, status = 200) =>
      Response.json(data as Record<string, unknown>, { status }),
    req: { method: "POST", url: "http://localhost/chat" },
  };
}

afterEach(() => {
  // Reset the shared `initCheckrd` cache so policy changes between
  // tests actually take effect (the Hono middleware shares it).
  resetCheckrdNext();
});

describe("checkrdHono", () => {
  it("attaches checkrdFetch to the Hono variable store", async () => {
    const ctx = stubContext();
    const next = vi.fn(async () => {
      expect(typeof ctx.get("checkrdFetch")).toBe("function");
    });
    const mw = checkrdHono({ policy: ALLOW_ALL, agentId: "test" });
    await mw(ctx, next);
    expect(next).toHaveBeenCalledOnce();
    expect(ctx.vars["checkrdFetch"]).toBeDefined();
  });

  it("catches a CheckrdPolicyDenied thrown downstream → 403 JSON", async () => {
    const ctx = stubContext();
    const next = async (): Promise<void> => {
      throw new CheckrdPolicyDenied({
        reason: "blocked",
        requestId: "req_abc",
        url: "https://api.example.com/",
        dashboardUrl: "https://app.checkrd.io/e/abc",
      });
    };
    const mw = checkrdHono({ policy: ALLOW_ALL, agentId: "test" });
    const res = (await mw(ctx, next))!;
    expect(res.status).toBe(403);
    const body = await res.json() as { error: { type: string; request_id: string; dashboard_url: string } };
    expect(body.error.type).toBe("policy_denied");
    expect(body.error.request_id).toBe("req_abc");
    expect(body.error.dashboard_url).toBe("https://app.checkrd.io/e/abc");
  });

  it("lets non-policy errors pass through untouched", async () => {
    const ctx = stubContext();
    const next = async (): Promise<void> => {
      throw new Error("downstream error");
    };
    const mw = checkrdHono({ policy: ALLOW_ALL, agentId: "test" });
    await expect(mw(ctx, next)).rejects.toThrow("downstream error");
  });

  it("returns undefined on success so Hono continues the chain", async () => {
    const ctx = stubContext();
    const next = vi.fn(async () => undefined);
    const mw = checkrdHono({ policy: ALLOW_ALL, agentId: "test" });
    const out = await mw(ctx, next);
    expect(out).toBeUndefined();
  });
});
