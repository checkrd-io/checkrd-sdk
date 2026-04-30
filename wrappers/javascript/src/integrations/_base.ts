/**
 * Instrumentor base — mirrors `integrations/_base.py`.
 *
 * Vendor SDKs in the modern JS ecosystem accept a `fetch` option in
 * their constructor, so every instrumentor is the same thing: attach
 * the Checkrd-wrapped `fetch` to the SDK's client factory and remember
 * how to revert.
 *
 * # Why constructor-injection (not import-in-the-middle)
 *
 * The OpenTelemetry ecosystem patches at module-load time using
 * ``@opentelemetry/instrumentation`` + ``import-in-the-middle`` /
 * ``require-in-the-middle``. That works in Node and lets the
 * instrumentor patch a vendor module that was already imported, but
 * it carries hard costs we are unwilling to pay:
 *
 *   - **Node-only**. Loader hooks don't exist on Cloudflare Workers,
 *     Vercel Edge, Deno (without ``--unstable-byonm``), or Bun.
 *     Checkrd has to run on every JS runtime its customers use, so
 *     the instrumentation strategy has to follow.
 *   - **Setup ceremony**. Users would need to launch their app with
 *     ``node --import checkrd/preload`` (Sentry pattern), which adds
 *     a documentation-and-support burden every customer has to learn.
 *   - **Bundler defeat**. Loader hooks fight tree-shaking and the way
 *     bundlers like Vite, esbuild, and Rollup statically resolve
 *     imports. Web-app deployments would silently lose
 *     instrumentation.
 *
 * Constructor injection works because every modern AI vendor SDK
 * (OpenAI, Anthropic, Cohere, Groq, Mistral, Together, Google GenAI)
 * accepts a ``fetch`` option in its constructor — Stainless's
 * generator emits it as a stable contract. We Proxy the constructor
 * so user code that does ``new OpenAI({...})`` transparently flows
 * through Checkrd's wrapped fetch, without preload flags or runtime
 * gating. The trade-off is that ``checkrd.instrument*()`` MUST be
 * called before the user constructs their first vendor client; the
 * SDK README and the ``Checkrd`` class docstring both call this out.
 *
 * If a future vendor SDK ever moves to async constructors or stops
 * accepting ``fetch``, that integration switches to direct
 * monkey-patch of the relevant prototype method (``shimmer`` style)
 * — still no loader hooks required.
 */
import { wrapFetch, type FetchFn, type WrapFetchOptions } from "../transports/fetch.js";

/**
 * Common base class. Subclasses supply a way to install / uninstall the
 * wrapped `fetch` into a vendor SDK; the base handles idempotency and
 * an ergonomic instance factory.
 */
export abstract class Instrumentor {
  private installed = false;

  /** Apply the patch, if not already applied. */
  instrument(): void {
    if (this.installed) return;
    this.applyPatch();
    this.installed = true;
  }

  /** Revert the patch if previously applied; otherwise no-op. */
  uninstrument(): void {
    if (!this.installed) return;
    this.revertPatch();
    this.installed = false;
  }

  /** Whether this instrumentor currently has an active patch. */
  get isInstalled(): boolean {
    return this.installed;
  }

  protected abstract applyPatch(): void;
  protected abstract revertPatch(): void;
}

/** Options stored by {@link Instrumentor} subclasses for runtime patching. */
export interface InstrumentorOptions extends WrapFetchOptions {
  /** Optional base fetch to wrap. Defaults to the global `fetch`. */
  baseFetch?: FetchFn;
}

/**
 * Returns a new `fetch` that applies Checkrd enforcement around the
 * configured `baseFetch`. Factored out so every vendor integration can
 * reuse the same wrapping logic.
 */
export function createWrappedFetch(options: InstrumentorOptions): FetchFn {
  const base = options.baseFetch ?? globalThis.fetch.bind(globalThis);
  return wrapFetch(base, options);
}

// ---------------------------------------------------------------------------
// Vendor shape assertion
// ---------------------------------------------------------------------------

/**
 * Verify that an imported vendor module exposes the exports our
 * instrumentor expects to patch. Returns `true` when every requested
 * export is a function on the module; otherwise emits a structured
 * warning via the configured logger and returns `false`.
 *
 * # Why this exists
 *
 * The instrumentors patch by name — e.g. `mod.OpenAI = Patched`. If a
 * vendor renames the export (e.g. OpenAI ships an `OpenAI` → `Client`
 * rename in a major), the previous code path was a silent no-op:
 * `if (typeof OriginalCtor !== "function") return;`. Production then
 * runs without enforcement until ops notices the missing telemetry.
 *
 * Loud-but-not-fatal is the right posture: throwing would crash apps
 * that load Checkrd lazily; staying silent loses observability. We
 * log a structured warning carrying the offending module's keys
 * (truncated to avoid log spam) so SREs can see the structural break
 * the moment it lands. CI's `javascript_vendor_matrix` job exercises
 * pinned floor + recent versions of each vendor SDK so the warning
 * fires on the build, not in customer production.
 *
 * # Logging
 *
 * Uses `options.logger` if available (Sentry-style structured logger
 * passed in by the caller); falls back to `console.warn` otherwise.
 * Calling code never sees an exception from this function — it just
 * gets back `false`, treats that as "do not patch", and bails.
 */
export function assertVendorShape(
  vendor: string,
  mod: unknown,
  expectedExports: readonly string[],
  options: InstrumentorOptions,
): boolean {
  // CJS modules can set `module.exports` to a function (a class), so
  // both `"object"` and `"function"` are valid module shapes. ESM-from-
  // CJS interop preserves function-shaped exports too.
  if (mod === null || (typeof mod !== "object" && typeof mod !== "function")) {
    emitShapeMismatch(vendor, options, {
      reason: "module is not an object or function",
      typeofModule: typeof mod,
      expectedExports: [...expectedExports],
    });
    return false;
  }
  const m = mod as Record<string, unknown>;
  const missing = expectedExports.filter((k) => typeof m[k] !== "function");
  if (missing.length === 0) return true;

  // Dump the first 20 keys so SREs can see what the module DID
  // expose. Truncated to keep log lines small even if the vendor
  // ships hundreds of utilities.
  const presentKeys = Object.keys(m).slice(0, 20);
  emitShapeMismatch(vendor, options, {
    reason: "expected exports missing",
    expectedExports: [...expectedExports],
    missing,
    presentKeys,
  });
  return false;
}

/**
 * Variant of {@link assertVendorShape} for vendors that ship the
 * patch target under more than one possible name (e.g. `Mistral`
 * exported as both a `default` and a named `Mistral` depending on
 * the major). Returns `true` when AT LEAST ONE of the candidate
 * names is a function on the module. The instrumentor still
 * patches whichever names actually exist; the assertion just
 * guards against the case where ALL of them disappear in a vendor
 * rename.
 */
export function assertVendorShapeAny(
  vendor: string,
  mod: unknown,
  candidateExports: readonly string[],
  options: InstrumentorOptions,
): boolean {
  if (mod === null || (typeof mod !== "object" && typeof mod !== "function")) {
    emitShapeMismatch(vendor, options, {
      reason: "module is not an object or function",
      typeofModule: typeof mod,
      candidateExports: [...candidateExports],
    });
    return false;
  }
  const m = mod as Record<string, unknown>;
  const present = candidateExports.filter((k) => typeof m[k] === "function");
  if (present.length > 0) return true;

  const presentKeys = Object.keys(m).slice(0, 20);
  emitShapeMismatch(vendor, options, {
    reason: "no candidate export is a function on the module",
    candidateExports: [...candidateExports],
    presentKeys,
  });
  return false;
}

function emitShapeMismatch(
  vendor: string,
  options: InstrumentorOptions,
  detail: Record<string, unknown>,
): void {
  const message = `checkrd: ${vendor} vendor SDK shape mismatch — instrumenting as no-op`;
  if (options.logger?.warn) {
    options.logger.warn(message, { vendor, ...detail });
  } else {
    // Avoid using console directly when a logger was provided (the
    // logger may be configured to drop console output). When no
    // logger is configured, the loud-fallback IS console.warn — that
    // matches `Sentry.init()`'s default behavior.
    console.warn(message, { vendor, ...detail });
  }
}
