/**
 * Together AI SDK integration. Patches `together-ai`'s `Together`
 * default export so every new client routes requests through Checkrd.
 *
 * Together's SDK is a thin OpenAI-compatible client and accepts a
 * `fetch` option in its constructor, identical to OpenAI's.
 */
import { lazyRequireOptional } from "./_require.js";

import {
  assertVendorShapeAny,
  createWrappedFetch,
  Instrumentor,
  type InstrumentorOptions,
} from "./_base.js";

/**
 * Candidate exports on the `together-ai` module. The client has
 * shipped as a named `Together` export and as the module's
 * `default` export across versions, so we accept either. If both
 * disappear in a future rename, `assertVendorShapeAny` logs a
 * structured warning and the patch becomes a no-op rather than
 * silently losing instrumentation in production.
 */
const TOGETHER_CANDIDATE_EXPORTS = ["Together", "default"] as const;

interface TogetherModule { default?: unknown; Together?: unknown }

/** Options for {@link TogetherInstrumentor}. */
export type TogetherInstrumentorOptions = InstrumentorOptions;

/** Instrument `together-ai` so every new client routes through Checkrd. */
export class TogetherInstrumentor extends Instrumentor {
  private replacements: { key: "default" | "Together"; original: unknown }[] = [];

  constructor(private readonly options: TogetherInstrumentorOptions) {
    super();
  }

  protected override applyPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: TogetherModule;
    try {
      mod = requireOptional("together-ai") as TogetherModule;
    } catch {
      return;
    }
    if (!assertVendorShapeAny("together-ai", mod, TOGETHER_CANDIDATE_EXPORTS, this.options)) {
      // Package present but neither candidate export is a function —
      // `assertVendorShapeAny` already emitted a structured warning.
      // Bail rather than patch a half-recognised module.
      return;
    }
    const wrappedFetch = createWrappedFetch(this.options);

    const patchKey = (key: "default" | "Together"): void => {
      const OriginalCtor = mod[key];
      if (typeof OriginalCtor !== "function") return;
      this.replacements.push({ key, original: OriginalCtor });
      const Patched = new Proxy(OriginalCtor as new (opts?: Record<string, unknown>) => unknown, {
        construct(target, args: unknown[], newTarget) {
          const first = args[0] as Record<string, unknown> | undefined;
          const merged: Record<string, unknown> = { ...(first ?? {}) };
          if (merged.fetch === undefined) merged.fetch = wrappedFetch;
          return Reflect.construct(target, [merged], newTarget) as object;
        },
      });
      mod[key] = Patched;
    };

    patchKey("default");
    patchKey("Together");
  }

  protected override revertPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: TogetherModule;
    try {
      mod = requireOptional("together-ai") as TogetherModule;
    } catch {
      return;
    }
    for (const { key, original } of this.replacements) {
      mod[key] = original;
    }
    this.replacements = [];
  }
}
