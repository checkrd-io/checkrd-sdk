/**
 * Shared helper for the vendor instrumentors.
 *
 * Each instrumentor needs a synchronous `require()` that can load an
 * optional peer dependency only if the user installed it. The obvious
 * path — `import { createRequire } from "node:module"` at the top of
 * the file — eagerly loads `node:module`, which fails on Cloudflare
 * Workers / Vercel Edge / Deno / browser. Instrumenting a vendor SDK
 * on an edge runtime is already meaningless (the vendor SDKs are
 * Node-only themselves), but we want the *module* to load cleanly so
 * the SDK's non-instrumenting APIs remain usable there.
 *
 * This helper caches a single `createRequire(import.meta.url)` the
 * first time `lazyRequireOptional` is called, then defers to that
 * cached function. Subsequent calls are effectively free.
 */

/** The subset of `createRequire(...)` we rely on. */
type RequireLike = (name: string) => unknown;

let _cached: RequireLike | null = null;

/**
 * Return a `require(name)`-shaped function, loading `node:module`
 * lazily. Throws an environment-specific error on runtimes that lack
 * `node:module` — callers should treat a thrown error from the
 * returned function the same way they treat a missing peer package:
 * swallow silently and let instrument() no-op.
 */
export function lazyRequireOptional(importMetaUrl: string): RequireLike {
  if (_cached) return _cached;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- sync load on Node
    const mod = require("node:module") as {
      createRequire(filename: string): RequireLike;
    };
    _cached = mod.createRequire(importMetaUrl);
    return _cached;
  } catch {
    // Return a function that throws on use, not on lookup. Mirrors the
    // existing "package not installed → silent skip" behavior each
    // instrumentor already handles.
    return (): never => {
      throw new Error(
        "node:module is not available in this runtime; vendor " +
          "instrumentation is a no-op on edge runtimes",
      );
    };
  }
}
