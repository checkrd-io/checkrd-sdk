/**
 * Mistral SDK integration. Patches `@mistralai/mistralai`'s `Mistral`
 * constructor to inject a Checkrd-wrapped fetch when the caller did not
 * supply one.
 *
 * The Mistral SDK's client (`new Mistral({apiKey, httpClient})`) takes
 * an `httpClient` option; when absent it constructs a default one from
 * global fetch. We wrap that same fetch.
 */
import { lazyRequireOptional } from "./_require.js";

import {
  assertVendorShapeAny,
  createWrappedFetch,
  Instrumentor,
  type InstrumentorOptions,
} from "./_base.js";

/**
 * Candidate exports on the `@mistralai/mistralai` module. The
 * client has shipped as a named `Mistral` export and as the
 * module's `default` export across SDK majors, so we accept
 * either. If both disappear in a future rename,
 * `assertVendorShapeAny` logs a structured warning and the patch
 * becomes a no-op rather than silently losing instrumentation.
 */
const MISTRAL_CANDIDATE_EXPORTS = ["Mistral", "default"] as const;

interface MistralModule { Mistral?: unknown; default?: unknown }

/** Options for {@link MistralInstrumentor}. */
export type MistralInstrumentorOptions = InstrumentorOptions;

/** Instrument `@mistralai/mistralai` so every new client routes through Checkrd. */
export class MistralInstrumentor extends Instrumentor {
  private replacements: { key: "Mistral" | "default"; original: unknown }[] = [];

  constructor(private readonly options: MistralInstrumentorOptions) {
    super();
  }

  protected override applyPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: MistralModule;
    try {
      mod = requireOptional("@mistralai/mistralai") as MistralModule;
    } catch {
      return;
    }
    if (!assertVendorShapeAny("@mistralai/mistralai", mod, MISTRAL_CANDIDATE_EXPORTS, this.options)) {
      // Package present but neither candidate export is a function —
      // `assertVendorShapeAny` already emitted a structured warning.
      // Bail rather than patch a half-recognised module.
      return;
    }
    const wrappedFetch = createWrappedFetch(this.options);

    const patchKey = (key: "Mistral" | "default"): void => {
      const OriginalCtor = mod[key];
      if (typeof OriginalCtor !== "function") return;
      this.replacements.push({ key, original: OriginalCtor });
      const Patched = new Proxy(OriginalCtor as new (opts?: Record<string, unknown>) => unknown, {
        construct(target, args: unknown[], newTarget) {
          const first = args[0] as Record<string, unknown> | undefined;
          const merged: Record<string, unknown> = { ...(first ?? {}) };
          // The SDK key is `httpClient`. We substitute `fetch` onto the
          // client constructor's default internal path when the user did
          // not pass their own.
          if (merged.httpClient === undefined && merged.fetch === undefined) {
            merged.fetch = wrappedFetch;
          }
          return Reflect.construct(target, [merged], newTarget) as object;
        },
      });
      mod[key] = Patched;
    };

    patchKey("Mistral");
    patchKey("default");
  }

  protected override revertPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: MistralModule;
    try {
      mod = requireOptional("@mistralai/mistralai") as MistralModule;
    } catch {
      return;
    }
    for (const { key, original } of this.replacements) {
      mod[key] = original;
    }
    this.replacements = [];
  }
}
