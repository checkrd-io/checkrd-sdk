/**
 * Next.js App Router helpers.
 *
 * Next.js is the largest deployment target for TypeScript agents —
 * Vercel's own AI SDK is the default for net-new agent code. This
 * module is the one-import ergonomic layer for Next.js builds:
 *
 *   - {@link initCheckrd} — a module-scoped factory that works in
 *     both Node and Edge runtimes, picks the right init path
 *     automatically, and caches the context so repeated invocations
 *     (Server Components, Route Handlers, middleware) share one
 *     engine.
 *
 *   - {@link checkrdRoute} — wrap a Route Handler so every outbound
 *     vendor SDK call made from inside is enforced. Use this on
 *     `app/api/[…]/route.ts` and your API routes pick up Checkrd
 *     without any per-request wiring.
 *
 *   - {@link checkrdAction} — same idea, for Server Actions.
 *
 * Nothing here depends on `next` itself — the helpers are shaped to
 * Next's conventions but work under any framework that exposes a
 * request/response shape over standard `fetch`.
 */

import type { FetchFn } from "../transports/fetch.js";
import { wrapAsync, wrap, type InitAsyncOptions, type InitOptions } from "../index.js";
import { isCheckrdPolicyDenied } from "../exceptions.js";

/** Cached per-process Checkrd state so modules sharing an import get one engine. */
let cached: Promise<CheckrdNextContext> | null = null;

/** Shape returned by {@link initCheckrd}. */
export interface CheckrdNextContext {
  /**
   * Checkrd-enforced `fetch`. Pass to any vendor SDK that accepts a
   * `fetch` option: `new OpenAI({ fetch })`, `createAnthropic({ fetch })`,
   * Vercel AI SDK providers, etc.
   */
  fetch: FetchFn;
  /** True when running in a Node-compatible runtime, false on Edge. */
  readonly isNode: boolean;
}

/** Options for {@link initCheckrd}. Accepts everything `init()` / `initAsync()` accept. */
export type InitCheckrdOptions = InitAsyncOptions;

/**
 * Initialise Checkrd with a single call that works in any Next.js
 * runtime. On Node-compatible runtimes the synchronous {@link wrap}
 * is used (zero top-level-await). On Edge the async {@link wrapAsync}
 * path handles `initAsync` / `WasmEngine.create`. The resulting
 * context is cached so subsequent imports share one engine.
 *
 * Typical usage (Node or Edge):
 *
 *     // app/lib/checkrd.ts
 *     import { initCheckrd } from "checkrd/next";
 *     export const checkrd = initCheckrd({ policy: "./policy.yaml" });
 *
 *     // app/api/agent/route.ts
 *     import { checkrd } from "@/lib/checkrd";
 *     import OpenAI from "openai";
 *
 *     export async function POST(req: Request) {
 *       const { fetch } = await checkrd;
 *       const client = new OpenAI({ fetch });
 *       // ... run agent, return Response ...
 *     }
 */
export function initCheckrd(
  options: InitCheckrdOptions = {},
): Promise<CheckrdNextContext> {
  if (cached) return cached;
  // Capture the promise so we can reset `cached` on failure. Without
  // this reset, one transient init failure poisons every subsequent
  // call for the life of the isolate — the Next.js app would become
  // permanently wedged instead of retrying on the next request.
  // Pattern modelled on Sentry's `getCurrentHub()` idempotency guard.
  const attempt = (async () => {
    if (isNodeRuntime()) {
      // Sync path — no top-level await, nothing edge-specific.
      // Allow the caller to pass `dangerouslyAllowBrowser` on Node
      // too; `wrap` will pick it up and bypass its browser guard.
      const nodeOptions: InitOptions = stripWasmKey(options);
      const fetchFn = wrap(undefined, nodeOptions);
      return { fetch: fetchFn, isNode: true };
    }
    // Edge path — uses fetch + WebAssembly + crypto.subtle only.
    // No longer needs `dangerouslyAllowBrowser: true` as a defensive
    // default: the browser guard was tightened to real-browser
    // detection (requires window + document + navigator), so the
    // Next.js edge runtime is correctly recognized as server-side.
    const fetchFn = await wrapAsync(undefined, options);
    return { fetch: fetchFn, isNode: false };
  })();
  // On failure, clear the cache reference so the next caller retries
  // from scratch. Callers that awaited this promise still see the
  // original rejection — we only rebuild state for *future* calls.
  attempt.catch(() => {
    if (cached === attempt) cached = null;
  });
  cached = attempt;
  return attempt;
}

/** For tests — forget the cached context. */
export function resetCheckrdNext(): void {
  cached = null;
}

/**
 * Route-handler / Server-Action wrapper that resolves the
 * Checkrd-enforced `fetch` up front and passes it to the handler.
 * Catches `CheckrdPolicyDenied` and maps it to a 403 response so your
 * API shape stays stable even when policy blocks a call.
 *
 *     // app/api/agent/route.ts
 *     import { checkrdRoute } from "checkrd/next";
 *     import OpenAI from "openai";
 *
 *     export const runtime = "edge";
 *     export const POST = checkrdRoute(
 *       async ({ request, fetch }) => {
 *         const client = new OpenAI({ fetch });
 *         // ...
 *         return Response.json({ ok: true });
 *       },
 *       { policy: "./policy.yaml" },
 *     );
 */
export function checkrdRoute(
  handler: (ctx: { request: Request; fetch: FetchFn }) => Promise<Response> | Response,
  options: InitCheckrdOptions = {},
): (request: Request) => Promise<Response> {
  return async (request: Request): Promise<Response> => {
    const ctx = await initCheckrd(options);
    try {
      return await handler({ request, fetch: ctx.fetch });
    } catch (err) {
      if (isCheckrdPolicyDenied(err)) {
        return Response.json(
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
      }
      throw err;
    }
  };
}

/**
 * Server Actions wrapper. Unlike {@link checkrdRoute} this doesn't
 * assume a Request / Response shape — it just resolves the context
 * and passes `fetch` to the user's logic.
 *
 *     // app/actions.ts
 *     "use server";
 *     import { checkrdAction } from "checkrd/next";
 *
 *     export const generateCompletion = checkrdAction(
 *       async ({ fetch }, prompt: string) => {
 *         // ... call OpenAI with fetch ...
 *       },
 *       { policy: "./policy.yaml" },
 *     );
 */
export function checkrdAction<TArgs extends unknown[], TResult>(
  handler: (ctx: { fetch: FetchFn }, ...args: TArgs) => Promise<TResult> | TResult,
  options: InitCheckrdOptions = {},
): (...args: TArgs) => Promise<TResult> {
  return async (...args: TArgs): Promise<TResult> => {
    const ctx = await initCheckrd(options);
    return handler({ fetch: ctx.fetch }, ...args);
  };
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

function isNodeRuntime(): boolean {
  const proc = (globalThis as unknown as {
    process?: { versions?: { node?: string } };
  }).process;
  return typeof proc?.versions?.node === "string";
}

function stripWasmKey(options: InitCheckrdOptions): InitOptions {
  // `wasm` is only meaningful for the async path; strip it when we go
  // through the sync `wrap()`. The other fields all pass through.
  const { wasm, ...rest } = options;
  void wasm;
  return rest;
}

