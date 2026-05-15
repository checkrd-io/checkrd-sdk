/**
 * Cohere SDK integration. Patches the `cohere-ai` module's
 * `CohereClient` / `CohereClientV2` constructors so every new client
 * gets a Checkrd-wrapped fetch unless the user supplied one.
 *
 * Same rationale as {@link OpenAIInstrumentor} — Cohere's v7 SDK accepts
 * a `fetch` option in its config bag, so no deeper monkey-patching is
 * required.
 */
import { lazyRequireOptional, patchModuleExport } from "./_require.js";

import {
  assertVendorShapeAny,
  createWrappedFetch,
  Instrumentor,
  type InstrumentorOptions,
} from "./_base.js";

/**
 * Candidate exports on the `cohere-ai` module. The SDK ships
 * `CohereClient` (v1 surface) and `CohereClientV2` (v2 surface);
 * we patch whichever the user happens to have. If a future
 * major drops both names, `assertVendorShapeAny` logs a
 * structured warning and the patch becomes a no-op rather than
 * silently losing instrumentation in production.
 */
const COHERE_CANDIDATE_EXPORTS = ["CohereClient", "CohereClientV2"] as const;

type CohereModule = Record<string, unknown>;

/** Options for {@link CohereInstrumentor}. */
export type CohereInstrumentorOptions = InstrumentorOptions;

/** Instrument the `cohere-ai` package so every new client routes through Checkrd. */
export class CohereInstrumentor extends Instrumentor {
  private originalConstructors: { name: string; ctor: unknown }[] = [];

  constructor(private readonly options: CohereInstrumentorOptions) {
    super();
  }

  protected override getOptions(): CohereInstrumentorOptions {
    return this.options;
  }

  protected override applyPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: CohereModule;
    try {
      mod = requireOptional("cohere-ai") as CohereModule;
    } catch {
      return;
    }
    if (!assertVendorShapeAny("cohere-ai", mod, COHERE_CANDIDATE_EXPORTS, this.options)) {
      // Package present but neither candidate export is a function —
      // `assertVendorShapeAny` already emitted a structured warning.
      // Bail rather than patch a half-recognised module.
      return;
    }
    const wrappedFetch = createWrappedFetch(this.options);

    const patchCtor = (name: string): void => {
      const OriginalCtor = mod[name];
      if (typeof OriginalCtor !== "function") return;
      const Patched = new Proxy(OriginalCtor as new (opts?: Record<string, unknown>) => unknown, {
        construct(target, args: unknown[], newTarget) {
          const first = args[0] as Record<string, unknown> | undefined;
          const merged: Record<string, unknown> = { ...(first ?? {}) };
          if (merged.fetcher === undefined && merged.fetch === undefined) {
            // Cohere's v7 SDK accepts `fetcher` (not `fetch`).
            merged.fetcher = wrappedFetch;
          }
          return Reflect.construct(target, [merged], newTarget) as object;
        },
      });
      // patchModuleExport handles both plain CJS and TS-compiled CJS
      // with sealed (getter-backed) exports — the latter silently
      // ignores `mod[name] = Patched`, which was bug #7 in the
      // 2026-05-14 smoke run.
      if (!patchModuleExport(mod, name, Patched)) return;
      this.originalConstructors.push({ name, ctor: OriginalCtor });
    };

    patchCtor("CohereClient");
    patchCtor("CohereClientV2");
  }

  protected override revertPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: CohereModule;
    try {
      mod = requireOptional("cohere-ai") as CohereModule;
    } catch {
      return;
    }
    for (const { name, ctor } of this.originalConstructors) {
      patchModuleExport(mod, name, ctor);
    }
    this.originalConstructors = [];
  }
}
