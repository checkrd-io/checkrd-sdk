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
 *     and maps it to a 403 JSON response with `request_id` and
 *     `dashboard_url` fields — matches the OpenAI/Stripe error shape
 *     so client code stays simple.
 *
 * The integration is structurally typed against Hono's `Context` —
 * no hard dependency on the `hono` package, so the middleware works
 * with any middleware chain that passes `(ctx, next)`.
 */

import type { FetchFn } from "../transports/fetch.js";
import { isCheckrdPolicyDenied } from "../exceptions.js";
import { initCheckrd, type InitCheckrdOptions } from "./_next.js";

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
        return c.json(
          {
            error: {
              type: "policy_denied",
              message: err.reason,
              request_id: err.requestId,
              dashboard_url: err.dashboardUrl ?? null,
            },
          },
          403,
        );
      }
      throw err;
    }
  };
}

