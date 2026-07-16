/**
 * Hono middleware.
 *
 * Hono is the lightweight web framework of choice for Cloudflare
 * Workers, Bun, and Deno agent backends. Its middleware shape is the
 * standard `async (ctx, next) => void` pattern used by Koa, Elysia,
 * and the WinterCG `fetch(request) => Response` model in general.
 *
 * This module exposes {@link checkrdHono}, which:
 *
 *   - lazy-initialises the Checkrd runtime on first request (async
 *     path, works on every edge runtime);
 *   - attaches the Checkrd-enforced `fetch` to `c.var.checkrdFetch`
 *     for downstream handlers;
 *   - catches `CheckrdPolicyDenied` thrown inside the handler chain
 *     and maps it to a 403 `application/problem+json` response (RFC
 *     9457) carrying `detail`, `request_id`, and `dashboard_url` — the
 *     same flat shape the control plane returns, so client code that
 *     handles one handles both.
 *
 * The integration is structurally typed against Hono's `Context` —
 * no hard dependency on the `hono` package, so the middleware works
 * with any middleware chain that passes `(ctx, next)`.
 */

import type { FetchFn } from "../transports/fetch.js";
import { isCheckrdPolicyDenied } from "../exceptions.js";
import { initCheckrd, type InitCheckrdOptions } from "./_next.js";
import { policyDeniedProblem, PROBLEM_JSON_CONTENT_TYPE } from "./_problem.js";

/**
 * Minimal subset of Hono's `Context`. We only read `c.set` (to stash
 * the checkrdFetch) and `c.json` (to produce the deny response). The
 * generic keeps Hono's own type-level `Variables` inference intact
 * in consumer code.
 */
export interface HonoContextLike {
  set: (key: string, value: unknown) => void;
  get: (key: string) => unknown;
  json: (
    data: unknown,
    status?: number,
    // Hono's third `c.json` argument is a header record. We use it to
    // set `Content-Type: application/problem+json` on the deny response.
    headers?: Record<string, string>,
  ) => Response | Promise<Response>;
  req: { method: string; url: string };
}

/** Hono's middleware signature. */
export type HonoMiddleware = (
  c: HonoContextLike,
  next: () => Promise<void>,
) => Promise<Response | undefined>;

/**
 * Declare the context variable set by this middleware. Hono users
 * bind this via `new Hono<{ Variables: CheckrdHonoVariables }>()` to
 * get end-to-end type safety on `c.var.checkrdFetch`.
 */
export interface CheckrdHonoVariables {
  checkrdFetch: FetchFn;
}

/**
 * Produce Hono middleware that makes a Checkrd-enforced `fetch`
 * available to every downstream handler.
 *
 * Usage:
 *
 *     import { Hono } from "hono";
 *     import { checkrdHono, type CheckrdHonoVariables } from "checkrd/hono";
 *     import OpenAI from "openai";
 *
 *     const app = new Hono<{ Variables: CheckrdHonoVariables }>();
 *     app.use("*", checkrdHono({ policy: "./policy.yaml" }));
 *
 *     app.post("/chat", async (c) => {
 *       const client = new OpenAI({ fetch: c.var.checkrdFetch });
 *       const out = await client.chat.completions.create({ ... });
 *       return c.json(out);
 *     });
 */
export function checkrdHono(
  options: InitCheckrdOptions = {},
): HonoMiddleware {
  return async function checkrdMiddleware(c: HonoContextLike, next: () => Promise<void>): Promise<Response | undefined> {
    const ctx = await initCheckrd(options);
    c.set("checkrdFetch", ctx.fetch);
    try {
      await next();
      return undefined;
    } catch (err) {
      if (isCheckrdPolicyDenied(err)) {
        // RFC 9457 problem+json. The third `c.json` arg sets the
        // problem media type (Hono otherwise emits `application/json`).
        return c.json(policyDeniedProblem(err), 403, {
          "Content-Type": PROBLEM_JSON_CONTENT_TYPE,
        });
      }
      throw err;
    }
  };
}

