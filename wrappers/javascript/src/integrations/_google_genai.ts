/**
 * Google GenAI SDK integration. Patches `@google/genai`'s `GoogleGenAI`
 * constructor so every new client routes through Checkrd.
 *
 * The Google SDK accepts a `httpOptions.fetch` override. When the user
 * does not provide one we inject the Checkrd-wrapped fetch.
 */
import { lazyRequireOptional } from "./_require.js";

import {
  assertVendorShape,
  createWrappedFetch,
  Instrumentor,
  type InstrumentorOptions,
} from "./_base.js";

/**
 * Exports we expect on the `@google/genai` module. The SDK
 * exposes its client as a named `GoogleGenAI` export — if a
 * future major renames or removes it, `assertVendorShape` logs
 * a structured warning and the patch becomes a no-op rather
 * than silently losing instrumentation in production. Pinned
 * here so reviewers can see the surface our instrumentation
 * depends on.
 */
const GOOGLE_GENAI_EXPECTED_EXPORTS = ["GoogleGenAI"] as const;

interface GoogleGenAIModule { GoogleGenAI?: unknown }

/** Options for {@link GoogleGenAIInstrumentor}. */
export type GoogleGenAIInstrumentorOptions = InstrumentorOptions;

/** Instrument `@google/genai` so every new client routes through Checkrd. */
export class GoogleGenAIInstrumentor extends Instrumentor {
  private originalCtor: unknown = null;

  constructor(private readonly options: GoogleGenAIInstrumentorOptions) {
    super();
  }

  protected override applyPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: GoogleGenAIModule;
    try {
      mod = requireOptional("@google/genai") as GoogleGenAIModule;
    } catch {
      return;
    }
    if (!assertVendorShape("@google/genai", mod, GOOGLE_GENAI_EXPECTED_EXPORTS, this.options)) {
      // Package present but shape doesn't match — `assertVendorShape`
      // already emitted a structured warning. Bail rather than patch
      // a half-recognised module.
      return;
    }
    const OriginalCtor = mod.GoogleGenAI;
    if (typeof OriginalCtor !== "function") return;
    this.originalCtor = OriginalCtor;
    const wrappedFetch = createWrappedFetch(this.options);

    const Patched = new Proxy(OriginalCtor as new (opts?: Record<string, unknown>) => unknown, {
      construct(target, args: unknown[], newTarget) {
        const first = args[0] as Record<string, unknown> | undefined;
        const merged: Record<string, unknown> = { ...(first ?? {}) };
        // The SDK nests fetch under httpOptions.fetch. Merge non-destructively
        // so other httpOptions (baseUrl, etc.) the caller supplied survive.
        const httpOptions = (merged.httpOptions as Record<string, unknown> | undefined) ?? {};
        if (httpOptions.fetch === undefined) {
          merged.httpOptions = { ...httpOptions, fetch: wrappedFetch };
        }
        return Reflect.construct(target, [merged], newTarget) as object;
      },
    });
    mod.GoogleGenAI = Patched;
  }

  protected override revertPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    if (this.originalCtor === null) return;
    let mod: GoogleGenAIModule;
    try {
      mod = requireOptional("@google/genai") as GoogleGenAIModule;
    } catch {
      return;
    }
    mod.GoogleGenAI = this.originalCtor;
    this.originalCtor = null;
  }
}
