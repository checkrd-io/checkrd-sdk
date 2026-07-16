/**
 * Unit contract for the shared `resolveBuiltin` helper.
 *
 * `resolveBuiltin` is the single synchronous cross-runtime Node-builtin
 * loader used by `engine.ts`, `webhooks.ts`, and `config.ts`. Its
 * behavioral contract is:
 *
 *   - a real Node built-in (`node:crypto`) resolves to a usable module,
 *   - an unknown specifier resolves to `null` (so callers can throw an
 *     accurate, directional error instead of leaking a raw loader throw).
 *
 * The in-process artifact test (`tests/node_esm_artifact.test.ts`) proves
 * the `getBuiltinModule`-first ordering matters against the BUILT bundle
 * under Node ESM; this pins the pure contract in isolation.
 */
import { describe, expect, it } from "vitest";

import { resolveBuiltin } from "../src/_builtin.js";

describe("resolveBuiltin", () => {
  it("resolves a real Node built-in to a usable module", () => {
    const crypto = resolveBuiltin("node:crypto") as {
      createHmac?: (algo: string, key: string) => unknown;
    } | null;
    expect(crypto).not.toBeNull();
    // Not merely defined — the resolved object must be the real module.
    expect(typeof crypto?.createHmac).toBe("function");
  });

  it("returns null for a specifier that is not a Node built-in", () => {
    // Exercises: strategy 1 miss (getBuiltinModule → undefined/throw) →
    // strategy 2 miss (require throws / esbuild __require throws) → null.
    expect(resolveBuiltin("node:__definitely_not_a_real_builtin__")).toBeNull();
  });
});
