/**
 * Anthropic SDK integration. Same pattern as {@link OpenAIInstrumentor}:
 * proxy the module's constructor so that every `new Anthropic({...})`
 * gets a Checkrd-wrapped fetch unless the caller supplied one.
 */
import { lazyRequireOptional } from "./_require.js";

import {
  assertVendorShape,
  createWrappedFetch,
  Instrumentor,
  type InstrumentorOptions,
} from "./_base.js";

/**
 * Exports we expect on the `@anthropic-ai/sdk` module. The SDK's
 * client is its `default` export in both ESM and CJS builds — if a
 * future major renames or removes that export, `assertVendorShape`
 * logs a structured warning and the patch becomes a no-op rather
 * than silently losing instrumentation in production. Pinned here
 * so reviewers can see the surface our instrumentation depends on.
 */
const ANTHROPIC_EXPECTED_EXPORTS = ["default"] as const;

type AnthropicModule = typeof import("@anthropic-ai/sdk");

// See _openai.ts for the rationale on createRequire over dynamic import.
/** Options for {@link AnthropicInstrumentor}. */
export type AnthropicInstrumentorOptions = InstrumentorOptions;

/**
 * Instrument the `@anthropic-ai/sdk` package so every new client sends
 * requests through Checkrd. Safe to call multiple times (idempotent).
 */
export class AnthropicInstrumentor extends Instrumentor {
  private originalDefault: unknown = null;

  constructor(private readonly options: AnthropicInstrumentorOptions) {
    super();
  }

  protected override applyPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    let mod: AnthropicModule;
    try {
      mod = requireOptional("@anthropic-ai/sdk") as AnthropicModule;
    } catch {
      return;
    }
    if (!assertVendorShape("@anthropic-ai/sdk", mod, ANTHROPIC_EXPECTED_EXPORTS, this.options)) {
      // Package present but shape doesn't match — `assertVendorShape`
      // already emitted a structured warning. Bail rather than patch
      // a half-recognised module.
      return;
    }
    const wrappedFetch = createWrappedFetch(this.options);

    // The Anthropic SDK exposes its client as the module's `default`
    // export (both in ESM and the CommonJS build).
    const modRec = mod as unknown as { default?: unknown };
    const OriginalCtor = modRec.default;
    if (typeof OriginalCtor !== "function") return;
    this.originalDefault = OriginalCtor;

    const Patched = new Proxy(OriginalCtor as new (opts?: Record<string, unknown>) => unknown, {
      construct(target, args: unknown[], newTarget) {
        const first = args[0] as Record<string, unknown> | undefined;
        const merged: Record<string, unknown> = { ...(first ?? {}) };
        if (merged.fetch === undefined) merged.fetch = wrappedFetch;
        return Reflect.construct(target, [merged], newTarget) as object;
      },
    });
    modRec.default = Patched;
  }

  protected override revertPatch(): void {
    const requireOptional = lazyRequireOptional(import.meta.url);
    if (this.originalDefault === null) return;
    let mod: AnthropicModule;
    try {
      mod = requireOptional("@anthropic-ai/sdk") as AnthropicModule;
    } catch {
      return;
    }
    (mod as unknown as { default?: unknown }).default = this.originalDefault;
    this.originalDefault = null;
  }
}
