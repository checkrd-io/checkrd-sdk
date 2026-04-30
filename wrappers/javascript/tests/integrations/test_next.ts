/**
 * Next.js App Router helper tests.
 *
 * We can't run Next itself in unit tests, so the tests exercise the
 * surface directly: the module-scoped cache, the Route wrapper that
 * turns denies into 403 JSON, and the Action wrapper that propagates
 * errors. Next-specific integration (instrumentation.ts, onRequestError)
 * is covered in `examples/next-app-router/` end-to-end.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { CheckrdPolicyDenied } from "../../src/exceptions.js";
import {
  checkrdAction,
  checkrdRoute,
  initCheckrd,
  resetCheckrdNext,
} from "../../src/integrations/_next.js";

const ALLOW_ALL = { agent: "t", default: "allow", rules: [] };

afterEach(() => {
  resetCheckrdNext();
});

describe("initCheckrd", () => {
  it("returns a context with a callable fetch", async () => {
    const ctx = await initCheckrd({ policy: ALLOW_ALL, agentId: "test" });
    expect(typeof ctx.fetch).toBe("function");
    expect(ctx.isNode).toBe(true);
  });

  it("caches the promise across calls — only inits once", async () => {
    const a = initCheckrd({ policy: ALLOW_ALL, agentId: "test" });
    const b = initCheckrd({ policy: ALLOW_ALL, agentId: "test" });
    expect(a).toBe(b);
    await a;
  });

  it("clears the cache on init failure so the next call retries", async () => {
    // An unreadable file is a reliable init failure path on the sync
    // node init route (policy resolution throws).
    const attempt1 = initCheckrd({
      policy: "/nonexistent/path/to/policy.yaml",
      agentId: "test",
    });
    await expect(attempt1).rejects.toThrow();
    // Next call should return a NEW promise (cache cleared), not the
    // same rejected one.
    const attempt2 = initCheckrd({ policy: ALLOW_ALL, agentId: "test" });
    await expect(attempt2).resolves.toBeDefined();
    expect(attempt1).not.toBe(attempt2);
  });
});

describe("checkrdRoute", () => {
  it("runs the handler with fetch attached", async () => {
    const handler = vi.fn(async ({ fetch }: { fetch: typeof globalThis.fetch; request: Request }) => {
      expect(typeof fetch).toBe("function");
      return Response.json({ ok: true });
    });
    const route = checkrdRoute(handler, { policy: ALLOW_ALL, agentId: "test" });
    const res = await route(new Request("http://localhost/"));
    expect(res.status).toBe(200);
    expect(handler).toHaveBeenCalledOnce();
  });

  it("maps CheckrdPolicyDenied to a 403 JSON response", async () => {
    const route = checkrdRoute(() => {
      throw new CheckrdPolicyDenied({
        reason: "disallowed",
        requestId: "req_xyz",
        url: "https://api.example.com/",
        dashboardUrl: "https://app.checkrd.io/events/req_xyz",
      });
    }, { policy: ALLOW_ALL, agentId: "test" });
    const res = await route(new Request("http://localhost/"));
    expect(res.status).toBe(403);
    const body = await res.json() as { error: { type: string; message: string; request_id: string; dashboard_url: string } };
    expect(body.error.type).toBe("policy_denied");
    expect(body.error.message).toBe("disallowed");
    expect(body.error.request_id).toBe("req_xyz");
    expect(body.error.dashboard_url).toBe("https://app.checkrd.io/events/req_xyz");
  });

  it("lets non-policy errors propagate", async () => {
    const route = checkrdRoute(() => {
      throw new TypeError("something else broke");
    }, { policy: ALLOW_ALL, agentId: "test" });
    await expect(route(new Request("http://localhost/"))).rejects.toBeInstanceOf(TypeError);
  });
});

describe("checkrdAction", () => {
  it("passes fetch + forwards args + returns result", async () => {
    const handler = vi.fn(
      async ({ fetch }: { fetch: typeof globalThis.fetch }, name: string, n: number) => {
        expect(typeof fetch).toBe("function");
        return `${name}:${String(n)}`;
      },
    );
    const action = checkrdAction(handler, { policy: ALLOW_ALL, agentId: "test" });
    const result = await action("user", 42);
    expect(result).toBe("user:42");
  });

  it("propagates errors thrown by the handler (no conversion)", async () => {
    const action = checkrdAction(
      (_ctx: unknown) => {
        throw new CheckrdPolicyDenied({
          reason: "nope",
          requestId: "r",
          url: "u",
        });
      },
      { policy: ALLOW_ALL, agentId: "test" },
    );
    await expect(action()).rejects.toBeInstanceOf(CheckrdPolicyDenied);
  });
});
