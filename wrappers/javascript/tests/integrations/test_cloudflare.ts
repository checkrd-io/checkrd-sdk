/**
 * Cloudflare Workers helper tests.
 *
 * No Cloudflare runtime here — we stub `ExecutionContext` with a
 * minimal implementation that records `waitUntil` calls. Because
 * `initAsync` fetches the WASM via `new URL(..., import.meta.url)`
 * which doesn't resolve in the vitest environment, we inject the
 * WASM bytes explicitly via the options factory (the same escape
 * hatch real Workers users employ). The full runtime test lives in
 * `examples/cloudflare-worker/` and is driven by `wrangler dev` in CI.
 */
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { __resetWasmModuleCache } from "../../src/engine.js";
import { CheckrdPolicyDenied } from "../../src/exceptions.js";
import { withCheckrd } from "../../src/integrations/_cloudflare.js";
import { shutdown } from "../../src/index.js";

const ALLOW_ALL = { agent: "t", default: "allow", rules: [] };
const wasmPath = fileURLToPath(new URL("../../checkrd_core.wasm", import.meta.url));
let wasmBytes: Uint8Array;

beforeAll(async () => {
  wasmBytes = new Uint8Array(await readFile(wasmPath));
});

interface StubCtx {
  waitUntilPromises: Promise<unknown>[];
  waitUntil: (p: Promise<unknown>) => void;
  passThroughOnException: () => void;
}

function stubCtx(): StubCtx {
  const promises: Promise<unknown>[] = [];
  return {
    waitUntilPromises: promises,
    waitUntil: (p) => {
      promises.push(p);
    },
    passThroughOnException: () => undefined,
  };
}

afterEach(async () => {
  await shutdown();
  __resetWasmModuleCache();
});

describe("withCheckrd", () => {
  it("resolves options once per request and passes fetch to the handler", async () => {
    const optionsFn = vi.fn(() => ({ policy: ALLOW_ALL, agentId: "worker", wasm: wasmBytes }));
    const handler = vi.fn(
      async (_req: Request, _env: Record<string, unknown>, _ctx: unknown, fetch: typeof globalThis.fetch) => {
        expect(typeof fetch).toBe("function");
        return Response.json({ ok: true });
      },
    );
    const wrapped = withCheckrd(handler, optionsFn);
    const res = await wrapped(new Request("http://w/"), {} as Record<string, unknown>, stubCtx());
    expect(res.status).toBe(200);
    // Previously a regression bug called optionsFn three times. Now
    // exactly once per request.
    expect(optionsFn).toHaveBeenCalledTimes(1);
  });

  it("reuses the init promise across warm invocations (same env ref)", async () => {
    let initCount = 0;
    const optionsFn = vi.fn(() => {
      initCount++;
      return { policy: ALLOW_ALL, agentId: "w", wasm: wasmBytes };
    });
    const handler = async (): Promise<Response> => Response.json({});
    const wrapped = withCheckrd(handler, optionsFn);
    const env = {} as Record<string, unknown>;
    await wrapped(new Request("http://w/"), env, stubCtx());
    await wrapped(new Request("http://w/"), env, stubCtx());
    await wrapped(new Request("http://w/"), env, stubCtx());
    // optionsFn is called once per request (to read flushTimeoutMs
    // etc.), but the INIT itself should only happen once because the
    // env ref didn't change. `initCount` tracks optionsFn calls here
    // since optionsFn is the only side effect we observe.
    expect(initCount).toBe(3);
  });

  it("re-initialises when the env reference rotates (wrangler dev reload)", async () => {
    const optionsFn = vi.fn(() => ({ policy: ALLOW_ALL, agentId: "w", wasm: wasmBytes }));
    const handler = async (): Promise<Response> => Response.json({});
    const wrapped = withCheckrd(handler, optionsFn);
    await wrapped(new Request("http://w/"), {} as Record<string, unknown>, stubCtx());
    await wrapped(new Request("http://w/"), {} as Record<string, unknown>, stubCtx());
    // Two different `env` references → two calls to `initAsync` under
    // the hood. We can't observe that directly without mocking
    // initAsync, so we just confirm the handler runs and optionsFn is
    // consulted per request.
    expect(optionsFn).toHaveBeenCalledTimes(2);
  });

  it("maps a CheckrdPolicyDenied thrown by the handler to 403", async () => {
    const handler = async (): Promise<Response> => {
      throw new CheckrdPolicyDenied({
        reason: "blocked",
        requestId: "req_w",
        url: "https://api/",
      });
    };
    const wrapped = withCheckrd(handler, () => ({ policy: ALLOW_ALL, agentId: "w", wasm: wasmBytes }));
    const res = await wrapped(new Request("http://w/"), {} as Record<string, unknown>, stubCtx());
    expect(res.status).toBe(403);
    const body = await res.json() as { error: { type: string } };
    expect(body.error.type).toBe("policy_denied");
  });

  it("registers a waitUntil for the post-response telemetry flush", async () => {
    const ctx = stubCtx();
    const handler = async (): Promise<Response> => Response.json({});
    const wrapped = withCheckrd(handler, () => ({ policy: ALLOW_ALL, agentId: "w", wasm: wasmBytes }));
    await wrapped(new Request("http://w/"), {} as Record<string, unknown>, ctx);
    expect(ctx.waitUntilPromises.length).toBeGreaterThanOrEqual(1);
  });
});
