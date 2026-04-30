/**
 * Cloudflare Workers helpers.
 *
 * Workers is the largest non-Node deployment surface for the SDK.
 * Two patterns dominate Workers code:
 *
 *   1. Plain `export default { fetch(req, env, ctx) { ... } }`.
 *   2. Workers + Durable Objects for stateful agent backends.
 *
 * This module ships small wrappers for both. The interesting bits
 * are: (a) the WASM module binding — Cloudflare lets you import a
 * `.wasm` file as a `WebAssembly.Module` directly, which we pass to
 * `WasmEngine.create({ wasm })` to skip the runtime fetch; and (b)
 * `ctx.waitUntil` integration so Checkrd's telemetry batcher gets a
 * grace period to flush after the response is returned (Workers
 * otherwise tear the isolate down immediately).
 */

import type { FetchFn } from "../transports/fetch.js";
import { isCheckrdPolicyDenied } from "../exceptions.js";
import { initAsync, shutdown, type InitAsyncOptions } from "../index.js";

/** Minimum shape of Cloudflare's `ExecutionContext`. */
export interface CloudflareExecutionContext {
  waitUntil: (promise: Promise<unknown>) => void;
  passThroughOnException: () => void;
}

/** Minimal handler shape — narrow on purpose to allow Workers v2 / v3 evolution. */
export type WorkerFetchHandler<TEnv = Record<string, unknown>> = (
  request: Request,
  env: TEnv,
  ctx: CloudflareExecutionContext,
) => Promise<Response> | Response;

/** Options for {@link withCheckrd}. */
export interface WithCheckrdOptions extends InitAsyncOptions {
  /**
   * Optional max-age (ms) for the post-response telemetry-flush
   * window. Defaults to 5 seconds — enough for the batcher to drain
   * a steady-state queue. Set higher only for batch-heavy workloads.
   */
  flushTimeoutMs?: number;
}

/**
 * Wrap a Workers `fetch` handler so:
 *
 *   - Checkrd is initialised on the first request (lazy) using the
 *     async path — required for Workers, which does not expose
 *     `node:fs` / `node:crypto`.
 *   - `request.checkrdFetch` is available inside the handler via the
 *     supplied `(request, env, ctx, fetch)` signature.
 *   - `ctx.waitUntil` is used to keep the telemetry batcher alive
 *     long enough to flush after the Response is returned.
 *
 *     // worker.ts
 *     import wasm from "./checkrd_core.wasm";
 *     import { withCheckrd } from "checkrd/cloudflare";
 *     import OpenAI from "openai";
 *
 *     export default {
 *       fetch: withCheckrd(
 *         async (req, env, _ctx, fetch) => {
 *           const client = new OpenAI({ fetch, apiKey: env.OPENAI_API_KEY });
 *           const out = await client.chat.completions.create({ ... });
 *           return Response.json(out);
 *         },
 *         (env) => ({
 *           apiKey: env.CHECKRD_API_KEY,
 *           policy: env.CHECKRD_POLICY,
 *           wasm,
 *         }),
 *       ),
 *     };
 *
 * `optionsFn` receives the env so secrets can flow through directly
 * without bringing process.env into the picture.
 */
export function withCheckrd<TEnv extends Record<string, unknown>>(
  handler: (
    request: Request,
    env: TEnv,
    ctx: CloudflareExecutionContext,
    fetch: FetchFn,
  ) => Promise<Response> | Response,
  optionsFn: (env: TEnv) => WithCheckrdOptions,
): WorkerFetchHandler<TEnv> {
  // Cache the init promise across requests within the same isolate.
  // Workers reuses isolates for warm invocations; we don't want each
  // request paying for WASM compile.
  let initPromise: Promise<FetchFn> | null = null;
  let lastEnv: TEnv | null = null;

  return async (request, env, ctx) => {
    // Resolve options ONCE per request. Previously we called
    // `optionsFn(env)` three times per request (opts spread, browser
    // default, flush timeout read), which duplicated work and — worse
    // — could observe three different return values if the caller's
    // factory was stateful. Cache the resolved options on the request
    // scope so every downstream read sees the same object.
    const rawOptions = optionsFn(env);
    // The browser guard used to falsely flag Cloudflare Workers as
    // "browser-like" (because they lack `process.versions.node`),
    // forcing this integration to default `dangerouslyAllowBrowser` to
    // true. The guard now uses real-browser detection (window +
    // document + navigator) and correctly recognizes Workers as
    // server-side, so this defensive default is no longer necessary —
    // and removing it means the operator's explicit choice is the
    // only thing that controls the flag.
    const opts: WithCheckrdOptions = { ...rawOptions };
    const flushTimeoutMs = rawOptions.flushTimeoutMs ?? 5_000;

    // Detect env rotation (rare in production but happens during
    // `wrangler dev` reloads). Re-init when the env reference changes.
    if (initPromise === null || env !== lastEnv) {
      lastEnv = env;
      const attempt = (async () => {
        await initAsync(opts);
        // The initAsync path stashes a wrapped fetch in the global
        // context; we surface it via wrap() to keep the per-request
        // shape consistent with Hono / Next.
        const { wrapAsync } = await import("../index.js");
        return wrapAsync(undefined, opts);
      })();
      // If init throws, clear the cache so the next request retries
      // rather than replaying the same rejected promise forever.
      attempt.catch(() => {
        if (initPromise === attempt) {
          initPromise = null;
          lastEnv = null;
        }
      });
      initPromise = attempt;
    }

    const checkrdFetch = await initPromise;

    let response: Response;
    try {
      response = await handler(request, env, ctx, checkrdFetch);
    } catch (err) {
      if (isCheckrdPolicyDenied(err)) {
        response = Response.json(
          {
            error: {
              type: "policy_denied",
              message: err.reason,
              request_id: err.requestId,
              dashboard_url: err.dashboardUrl ?? null,
            },
          },
          { status: 403 },
        );
      } else {
        throw err;
      }
    }

    // Give the telemetry batcher a flush window after the response
    // is sent. Workers will otherwise tear the isolate down as soon
    // as the response is delivered, dropping in-flight telemetry.
    ctx.waitUntil(
      withTimeout(shutdownGracefully(), flushTimeoutMs).catch((err: unknown) => {
        // Never let a flush failure crash the worker.
        void err;
      }),
    );

    return response;
  };
}

async function shutdownGracefully(): Promise<void> {
  // `shutdown()` drains pending telemetry and closes the SSE control
  // stream. Idempotent — safe to call from waitUntil on every request.
  await shutdown();
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return Promise.race([
    p,
    new Promise<never>((_resolve, reject) => {
      setTimeout(() => {
        reject(new Error("flush timeout"));
      }, ms);
    }),
  ]);
}

