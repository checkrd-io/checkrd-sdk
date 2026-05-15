/**
 * Cloudflare Workers / Vercel Edge / Deno edge-runtime deployment.
 *
 * Edge runtimes can't load WASM via Node's `fs`/`crypto`, so use the
 * async variants: `initAsync`, `wrapAsync`, `WasmEngine.create()`.
 * They use `fetch` + `WebAssembly.compile` + `crypto.subtle` —
 * primitives every edge runtime exposes.
 *
 * Bundle the WASM alongside your worker and pass it explicitly; this
 * avoids import-resolution quirks in some edge bundlers.
 *
 * The control plane is canonical: ``initAsync()`` fetches your
 * dashboard's published policy bundle and installs it before
 * resolving. No ``policy:`` argument here — the dashboard is the
 * source of truth, and mixing local + remote policies in app code
 * is intentionally refused by the SDK (same posture as OPA, where
 * a service binary never inlines its own policy alongside an active
 * bundle agent).
 *
 * Cloudflare Workers example (wrangler.toml:
 *   [build]
 *   command = "..."
 *   [[rules]]
 *   type = "CompiledWasm"
 *   globs = ["**\/*.wasm"]
 * ):
 */
import wasm from "./checkrd_core.wasm";
import { initAsync, instrumentOpenAI } from "checkrd";
import OpenAI from "openai";

export interface Env {
  CHECKRD_API_KEY: string;
  CHECKRD_AGENT_ID: string; // UUID from your dashboard
  OPENAI_API_KEY: string;
}

export default {
  async fetch(_req: Request, env: Env): Promise<Response> {
    await initAsync({
      apiKey: env.CHECKRD_API_KEY,
      agentId: env.CHECKRD_AGENT_ID,
      wasm, // pre-compiled module bound at build time
      dangerouslyAllowBrowser: true, // edge workers look browser-like
    });
    instrumentOpenAI();

    const client = new OpenAI({ apiKey: env.OPENAI_API_KEY });
    const response = await client.chat.completions.create({
      model: "gpt-4o",
      messages: [{ role: "user", content: "Hello in five words." }],
    });

    return Response.json({ text: response.choices[0]?.message.content });
  },
};

/**
 * Vercel Edge / Next.js variant — same primitives, different wiring.
 *
 *   import wasmUrl from "./checkrd_core.wasm?url";
 *   import { initAsync } from "checkrd";
 *
 *   export const runtime = "edge";
 *
 *   export async function GET() {
 *     await initAsync({
 *       apiKey: process.env.CHECKRD_API_KEY,
 *       agentId: process.env.CHECKRD_AGENT_ID,
 *       wasm: wasmUrl,
 *       dangerouslyAllowBrowser: true,
 *     });
 *     // ... instrument + run agent ...
 *   }
 *
 * For per-request (non-global) enforcement, use `wrapAsync`:
 *
 *   const checkrdFetch = await wrapAsync(undefined, {
 *     apiKey: env.CHECKRD_API_KEY,
 *     agentId: env.CHECKRD_AGENT_ID,
 *     wasm,
 *     dangerouslyAllowBrowser: true,
 *   });
 *   const client = new OpenAI({ fetch: checkrdFetch });
 */
