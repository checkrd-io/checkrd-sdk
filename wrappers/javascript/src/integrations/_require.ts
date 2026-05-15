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
 * Replace a single named export on a module's namespace object,
 * tolerating the three shapes vendor packages ship in the wild:
 *
 * 1. Plain CJS object — `mod.X = patched` works directly.
 * 2. TypeScript-compiled CJS with sealed exports — properties are
 *    installed via `Object.defineProperty` with `writable: undefined`
 *    + a getter, so `mod.X = patched` silently no-ops. Worked
 *    against older OpenAI versions but broke around the v5
 *    rewrite to a fully-typed CJS surface.
 * 3. ESM namespace — `Object.defineProperty` with `configurable:
 *    true` still works in Node's interop layer for CJS sources
 *    even when the namespace object reports `[[Writable]]: false`.
 *
 * Returns true on success, false when the property cannot be
 * replaced. Callers treat false the same as "package not
 * installed" and skip instrumentation rather than partially
 * patching.
 */
export function patchModuleExport(
  mod: Record<string, unknown>,
  name: string,
  patched: unknown,
): boolean {
  // Try the simple assignment first — that path runs when the
  // module is hand-written CJS without sealed descriptors.
  try {
    mod[name] = patched;
    if (mod[name] === patched) return true;
  } catch {
    // Fall through to defineProperty.
  }
  // defineProperty path. `configurable: true` is required so that
  // `revertPatch()` can restore the original later.
  try {
    Object.defineProperty(mod, name, {
      configurable: true,
      enumerable: true,
      writable: true,
      value: patched,
    });
    return mod[name] === patched;
  } catch {
    return false;
  }
}

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
