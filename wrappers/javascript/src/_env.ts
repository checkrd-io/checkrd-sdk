/**
 * Read an environment variable across runtimes.
 *
 * Mirrors the Stainless-generated helpers shipped by the OpenAI and
 * Anthropic SDKs (`src/internal/utils/env.ts` in both): probe `process.env`
 * for Node / Bun / Vercel Edge, then `Deno.env.get()` for Deno, and
 * return `undefined` everywhere else (Cloudflare Workers, browser).
 *
 * Behavior:
 *   - **Trims** leading/trailing whitespace — matches OpenAI/Anthropic.
 *   - **Empty / whitespace-only → `undefined`** (Anthropic's behavior, not
 *     OpenAI's `?? undefined`). In production an empty env var almost
 *     always means "the CI/CD substitution found nothing to inject"; the
 *     safer default is to treat it as unset so callers can chain `??`.
 *   - **Cloudflare Workers**: there is no global `env` — bindings are
 *     passed as the `env` argument to the `fetch` handler. This helper
 *     correctly returns `undefined`; the only working pattern in Workers
 *     is to pass values explicitly to the `Checkrd` constructor.
 *   - **Bun**: not branched separately — Bun polyfills `process.env`.
 *   - **No caching** — fresh read on every call so a runtime hot-reload
 *     of an env var (`process.env.X = "..."` in tests) is observable.
 */
export function readEnv(name: string): string | undefined {
  // Node, Bun, Vercel Edge — anything that exposes `process.env`.
  const proc = (
    globalThis as { process?: { env?: Record<string, string | undefined> } }
  ).process;
  if (proc !== undefined) {
    return normalize(proc.env?.[name]);
  }
  // Deno — Deno.env.get() returns string | undefined and may throw if
  // `--allow-env` is not granted. The optional chain on `.get` makes the
  // probe safe even if a future Deno API stops exposing it.
  const deno = (
    globalThis as { Deno?: { env?: { get?: (n: string) => string | undefined } } }
  ).Deno;
  if (deno !== undefined) {
    try {
      return normalize(deno.env?.get?.(name));
    } catch {
      // Permission-denied (no --allow-env) — treat as "not set".
      return undefined;
    }
  }
  // Cloudflare Workers, browser, anything else: no global env.
  return undefined;
}

/** Trim whitespace, then coerce empty / whitespace-only to `undefined`. */
function normalize(raw: string | undefined): string | undefined {
  if (raw === undefined) return undefined;
  const trimmed = raw.trim();
  return trimmed.length === 0 ? undefined : trimmed;
}
