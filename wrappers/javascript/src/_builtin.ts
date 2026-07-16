/**
 * Resolve a Node built-in module synchronously across runtimes, without a
 * static `import "node:..."` (which would force the whole bundle to require
 * Node and break edge runtimes at module load).
 *
 * Shared by every synchronous Node-only code path in the SDK:
 *   - `engine.ts`  — `node:fs` / `node:crypto` / `node:url` to locate,
 *     hash, and read `checkrd_core.wasm` for the sync `WasmEngine`
 *     constructor.
 *   - `webhooks.ts` — `node:crypto` for the sync `verifyWebhook` HMAC.
 *   - `config.ts`   — `node:fs` for file-path policy loading.
 *
 * Resolution strategy, ordered to cover every supported Node runtime
 * without pulling a Node-only specifier into the edge bundle:
 *
 *   1. `globalThis.process.getBuiltinModule(spec)` — Node 20.16+ / 22+
 *      exposes this in BOTH ESM and CJS. The spec string is read at call
 *      time, so tsup/esbuild never sees `node:fs` as a static import and
 *      the edge bundle stays runtime-neutral. **This must come first**:
 *      Node ESM has no ambient `require`, so strategy 2's bare `require`
 *      is rewritten by esbuild into a `__require(...)` shim that THROWS
 *      `Dynamic require of "..." is not supported`. `getBuiltinModule`
 *      is the only synchronous way to reach a Node built-in from ESM.
 *   2. `require(spec)` — Node CJS and Bun. Covers older Node bundles that
 *      ship the SDK as CommonJS and predate `getBuiltinModule`.
 *   3. Return `null`. The caller casts to its own module shim and throws
 *      an accurate, directional error — the builtin genuinely isn't here
 *      (Deno without `--node-compat`, Cloudflare Workers, or the browser).
 *
 * Both `getBuiltinModule` and `require` are invoked lazily inside this
 * function, never at module scope, so importing a module that uses this
 * helper never eagerly touches a Node built-in — the property the
 * edge-runtime smoke test (`tests/edge_runtime.test.ts`) pins.
 */
export function resolveBuiltin(spec: string): unknown {
  // Strategy 1: Node 20.16+ / 22+ `process.getBuiltinModule` — works in ESM.
  try {
    const proc = (globalThis as {
      process?: { getBuiltinModule?: (spec: string) => unknown };
    }).process;
    const mod = proc?.getBuiltinModule?.(spec);
    if (mod) return mod;
  } catch {
    // Fall through.
  }
  // Strategy 2: Node CJS / Bun `require`. In Node ESM this is esbuild's
  // throwing `__require` shim — caught here and treated as "unavailable".
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- sync load on Node CJS / Bun
    return require(spec) as unknown;
  } catch {
    // Fall through.
  }
  return null;
}
