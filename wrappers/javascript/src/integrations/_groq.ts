/**
 * Groq SDK integration. Patches `groq-sdk`'s default export (the Groq
 * client constructor) to inject a Checkrd-wrapped fetch when the caller
 * did not supply one. Structurally identical to {@link OpenAIInstrumentor}.
 */
import { lazyRequireOptional } from "./_require.js";

import {
  assertVendorShapeAny,
  createWrappedFetch,
  Instrumentor,
  type InstrumentorOptions,
} from "./_base.js";

/**
 * Candidate exports on the `groq-sdk` module. The Groq SDK has
 * shipped its client as both a `default` export and a named
 * `Groq` export across versions, so we accept either. If a future
 * major drops both, `assertVendorShapeAny` logs a structured
 * warning and the patch becomes a no-op. Pinned here so reviewers
 * can see the surface our instrumentation depends on.
 */
const GROQ_CANDIDATE_EXPORTS = ["Groq", "default"] as const;

interface GroqModule { default?: unknown; Groq?: unknown }

/** Options for {@link GroqInstrumentor}. */
export type GroqInstrumentorOptions = InstrumentorOptions;

/** Instrument the `groq-sdk` package so every new client routes through Checkrd. */
export class GroqInstrumentor extends Instrumentor {
  private replacements: { key: "default" | "Groq"; original: unknown }[] = [];

  constructor(private readonly options: GroqInstrumentorOptions) {
    super();
  }

  protected override applyPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: GroqModule;
    try {
      mod = requireOptional("groq-sdk") as GroqModule;
    } catch {
      return;
    }
    if (!assertVendorShapeAny("groq-sdk", mod, GROQ_CANDIDATE_EXPORTS, this.options)) {
      // Package present but neither candidate export is a function —
      // `assertVendorShapeAny` already emitted a structured warning.
      // Bail rather than patch a half-recognised module.
      return;
    }
    const wrappedFetch = createWrappedFetch(this.options);

    const patchKey = (key: "default" | "Groq"): void => {
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
    patchKey("Groq");
  }

  protected override revertPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: GroqModule;
    try {
      mod = requireOptional("groq-sdk") as GroqModule;
    } catch {
      return;
    }
    for (const { key, original } of this.replacements) {
      mod[key] = original;
    }
    this.replacements = [];
  }
}
