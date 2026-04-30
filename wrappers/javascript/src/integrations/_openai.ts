/**
 * OpenAI SDK integration. Patches the module's default export so that
 * every `new OpenAI({...})` / `new AzureOpenAI({...})` constructor
 * receives Checkrd's wrapped `fetch` unless the user passed an
 * explicit one.
 *
 * Zero runtime dep — we only touch `openai` types via `import type`.
 */
import { lazyRequireOptional } from "./_require.js";

import {
  assertVendorShape,
  createWrappedFetch,
  Instrumentor,
  type InstrumentorOptions,
} from "./_base.js";

/**
 * Exports we expect on the `openai` module. If a future major
 * removes/renames any of these, `assertVendorShape` logs a loud
 * warning and the patch becomes a no-op — instead of silently
 * losing instrumentation in production. Pinning these names also
 * documents the surface our instrumentation depends on so reviewers
 * can spot when a vendor major might break us.
 */
const OPENAI_EXPECTED_EXPORTS = ["OpenAI"] as const;

type OpenAIModule = typeof import("openai");

// `createRequire` is the Node-blessed way to do a sync require from an
// ESM module — used here because the OpenAI SDK is a *runtime-optional*
// peer dependency: we only want to touch it if the user installed it.
// `import()` would work too but would force the Instrumentor API async.
/** Options for {@link OpenAIInstrumentor}. */
export type OpenAIInstrumentorOptions = InstrumentorOptions;

/**
 * Instrument the `openai` package so every new client sends requests
 * through Checkrd. Safe to call multiple times (idempotent).
 */
export class OpenAIInstrumentor extends Instrumentor {
  private originalConstructors: {
    name: "OpenAI" | "AzureOpenAI";
    ctor: unknown;
  }[] = [];

  constructor(private readonly options: OpenAIInstrumentorOptions) {
    super();
  }

  protected override applyPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: OpenAIModule;
    try {
      mod = requireOptional("openai") as OpenAIModule;
    } catch {
      // Package not installed — instrumenting is a no-op. Uninstrument
      // remains a no-op too since nothing was patched. This is the
      // expected path on edge runtimes / minimal images that don't
      // bundle openai.
      return;
    }
    if (!assertVendorShape("openai", mod, OPENAI_EXPECTED_EXPORTS, this.options)) {
      // Package present but shape doesn't match — `assertVendorShape`
      // already emitted a structured warning. Bail rather than patch
      // a half-recognised module.
      return;
    }
    const wrappedFetch = createWrappedFetch(this.options);

    const patchCtor = (name: "OpenAI" | "AzureOpenAI"): void => {
      const OriginalCtor = (mod as unknown as Record<string, unknown>)[name];
      // `OpenAI` is checked by assertVendorShape; `AzureOpenAI` is
      // optional and may not exist on all openai majors, so we
      // tolerate its absence silently here.
      if (typeof OriginalCtor !== "function") return;
      this.originalConstructors.push({ name, ctor: OriginalCtor });

      // Proxy the constructor so we inject `fetch` only when the caller
      // did not provide one. This preserves ergonomics for users who
      // have their own fetch mocking (tests) while instrumenting the
      // common case transparently.
      const Patched = new Proxy(OriginalCtor as new (opts?: Record<string, unknown>) => unknown, {
        construct(target, args: unknown[], newTarget) {
          const first = args[0] as Record<string, unknown> | undefined;
          const merged: Record<string, unknown> = { ...(first ?? {}) };
          if (merged.fetch === undefined) merged.fetch = wrappedFetch;
          return Reflect.construct(target, [merged], newTarget) as object;
        },
      });
      (mod as unknown as Record<string, unknown>)[name] = Patched;
    };

    patchCtor("OpenAI");
    patchCtor("AzureOpenAI");
  }

  protected override revertPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: OpenAIModule;
    try {
      mod = requireOptional("openai") as OpenAIModule;
    } catch {
      return;
    }
    for (const { name, ctor } of this.originalConstructors) {
      (mod as unknown as Record<string, unknown>)[name] = ctor;
    }
    this.originalConstructors = [];
  }
}
