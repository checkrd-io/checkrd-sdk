/**
 * Unified `Checkrd` client — the recommended API entry point.
 *
 * Mirrors the `new OpenAI({apiKey})` / `new Anthropic({apiKey})` /
 * `new Stripe(key)` pattern. One object holds the resolved
 * configuration; `.wrap()` attaches it to a base fetch; `.withOptions()`
 * produces an immutable sibling with overrides; `.instrumentOpenAI()` /
 * `.instrumentAnthropic()` flip on global monkey-patching.
 *
 * Why the class:
 *   The prior surface (`init()` + `wrap()` + `instrumentOpenAI()` as
 *   separate top-level functions) was flexible but hard to pick up —
 *   92 named exports, no obvious starting point. Consolidation into a
 *   single object lets tutorials open with one line:
 *
 *     const checkrd = new Checkrd({ apiKey: "ck_live_..." });
 *     const myFetch = checkrd.wrap(fetch);
 *
 * Backwards compatibility:
 *   The top-level `wrap` / `wrapAsync` / `init` / `instrumentOpenAI` /
 *   `instrumentAnthropic` functions remain. The class delegates to
 *   them internally, so existing integrations keep working without
 *   changes.
 */

import {
  healthy,
  init,
  instrumentAnthropic,
  instrumentOpenAI,
  shutdown,
  uninstrumentAnthropic,
  uninstrumentOpenAI,
  wrap,
  wrapAsync,
  type HealthReport,
  type InitAsyncOptions,
  type InitOptions,
} from "./index.js";
import { CheckrdInitError } from "./exceptions.js";
import { readEnv } from "./_env.js";
import { resolve } from "./_settings.js";
import type { FetchFn } from "./transports/fetch.js";

/**
 * Strongly-typed sentinel used by {@link Checkrd.withOptions} to
 * distinguish "omitted" (reuse current value) from "set to undefined"
 * (unset the field). Matches the ``NotGiven`` pattern shipped by the
 * OpenAI and Anthropic SDKs — ``api_key: null`` UNSETS, omitting the
 * key entirely KEEPS. JavaScript does not have a true sentinel
 * primitive; we use a branded unique symbol.
 */
const UNSET: unique symbol = Symbol("checkrd.UNSET");

/**
 * Per-key override map accepted by {@link Checkrd.withOptions}: every
 * field of {@link InitOptions} can be overridden, omitted (inherits from
 * the parent), or explicitly cleared with the {@link UNSET} sentinel.
 */
type WithOptionsOverrides = {
  [K in keyof InitOptions]?: InitOptions[K] | typeof UNSET;
};

/**
 * Unified Checkrd client. Wraps a single shared configuration and
 * exposes the full verb set as methods instead of free functions.
 *
 * ```ts
 * import { Checkrd } from "checkrd";
 *
 * const checkrd = new Checkrd({ apiKey: "ck_live_xyz", agentId: "my-agent" });
 * const myFetch = checkrd.wrap(globalThis.fetch);
 * const response = await myFetch("https://api.openai.com/v1/...");
 *
 * // Immutable clone with overrides (OpenAI-SDK pattern).
 * const strict = checkrd.withOptions({ securityMode: "strict" });
 *
 * // Clean up background resources.
 * await checkrd.close();
 * ```
 *
 * All constructor options fall back to the environment when omitted:
 * `CHECKRD_API_KEY`, `CHECKRD_BASE_URL`, `CHECKRD_AGENT_ID`,
 * `CHECKRD_ENFORCE`, `CHECKRD_SECURITY_MODE`, `CHECKRD_API_VERSION`.
 */
export class Checkrd {
  /**
   * Frozen snapshot of the options the client was constructed with.
   * Kept private so callers cannot mutate the options after the fact —
   * a slice of the OpenAI SDK's `ClientOptions` immutability story.
   */
  private readonly options: Readonly<InitOptions>;
  /**
   * Tracks whether `close()` was called so repeated invocations are
   * no-ops. Matches `TelemetryBatcher.stop()` semantics.
   */
  private closed = false;
  /**
   * True once `ensureGlobalContext` has installed a global runtime
   * (via `init()`). Guards against double-installation when the user
   * calls `.instrument*()` repeatedly.
   */
  private globalContextInstalled = false;

  constructor(options: InitOptions = {}) {
    this.options = Object.freeze({ ...options });
  }

  // -------------------------------------------------------------------
  // Introspection
  // -------------------------------------------------------------------

  /**
   * Returns the API key if one was provided at construction. Prefers
   * explicit > env var > undefined. This getter NEVER surfaces the
   * value in `toString()` / `JSON.stringify()` — it's a read-only
   * property for testing and config-visualization code, not a log
   * field. See {@link toJSON}.
   */
  get apiKey(): string | undefined {
    if (this.options.apiKey !== undefined) return this.options.apiKey;
    return readEnv("CHECKRD_API_KEY");
  }

  /**
   * Agent ID after the env-var / platform fallbacks have been
   * applied. The value can change on re-resolution (e.g. if
   * `CHECKRD_AGENT_ID` was set after construction), so we defer to
   * `resolve()` on every read rather than caching.
   */
  get agentId(): string {
    // Re-resolve on every read so env var updates are visible —
    // matches how the existing top-level functions behave, and makes
    // `agentId` consistent with `.healthy()` output.
    const resolved = this.resolveSettings();
    return resolved.agentId;
  }

  /**
   * Control-plane base URL (after env fallback). Empty string when
   * neither the constructor nor `CHECKRD_BASE_URL` provided one.
   */
  get baseUrl(): string {
    return this.resolveSettings().controlPlaneUrl;
  }

  // -------------------------------------------------------------------
  // Core verbs
  // -------------------------------------------------------------------

  /**
   * Return a Checkrd-enforced `fetch` shaped like the standard
   * `fetch`. Pass the base fetch you want to wrap — typically
   * `globalThis.fetch` or a prebound instance; leave `undefined` to
   * default to `globalThis.fetch`.
   *
   * The returned function carries the full Checkrd pipeline (policy
   * eval, telemetry enqueue, control-receiver kill-switch), mirroring
   * the top-level `wrap()` function exactly.
   *
   * @param baseFetch - Underlying fetch to delegate to. Defaults to
   *   ``globalThis.fetch``. Pass an explicit value when you need a
   *   non-global fetch — e.g. one with a custom `dispatcher` (undici)
   *   or a Workers `getMiniflareFetch()`.
   * @returns A fetch-shaped callable that runs the Checkrd pipeline
   *   on every request.
   * @throws {CheckrdInitError} If the client has been closed via
   *   {@link close} — wrapping a closed client would leak resources.
   * @example
   * ```ts
   * const myFetch = checkrd.wrap(globalThis.fetch);
   * const openai = new OpenAI({ fetch: myFetch });
   * ```
   */
  wrap(baseFetch?: FetchFn): FetchFn {
    if (this.closed) {
      // Use the same init-error class the rest of the SDK does so
      // `catch (e: CheckrdInitError)` keeps working.
      throw new CheckrdInitError(
        "Checkrd client has been closed. Construct a new instance " +
          "(or call `withOptions({})`) — wrap() on a closed client " +
          "would leak resources.",
      );
    }
    return wrap(baseFetch, this.options);
  }

  /**
   * Asynchronous variant of {@link wrap}. Uses {@link wrapAsync} under
   * the hood, so the WASM engine is loaded via the runtime-agnostic
   * async factory — essential for Cloudflare Workers, Vercel Edge,
   * Deno, and any environment where synchronous WASM instantiation is
   * unavailable.
   *
   * @param baseFetch - Underlying fetch to delegate to. Defaults to
   *   ``globalThis.fetch``.
   * @param options - Optional overrides applied on top of the
   *   client's construction-time options. Useful for per-request
   *   policy or agent-id changes without constructing a new
   *   {@link Checkrd}.
   * @returns A promise resolving to the wrapped fetch.
   * @example Cloudflare Worker
   * ```ts
   * export default {
   *   async fetch(req: Request, env: Env) {
   *     const checkrd = new Checkrd({ apiKey: env.CHECKRD_API_KEY });
   *     const myFetch = await checkrd.wrapAsync();
   *     // ... use myFetch in OpenAI / Anthropic clients
   *   }
   * };
   * ```
   */
  async wrapAsync(
    baseFetch?: FetchFn,
    options?: InitAsyncOptions,
  ): Promise<FetchFn> {
    const merged: InitAsyncOptions = { ...this.options, ...options };
    return wrapAsync(baseFetch, merged);
  }

  /**
   * Return a new `Checkrd` with the given options overridden.
   *
   * Immutable clone — the source client is unchanged. Options not
   * listed in the overrides retain their current value; to UNSET a
   * field, pass `null` (not `undefined`, which is indistinguishable
   * from "not given" on a JS call site).
   *
   * ```ts
   * const strict = checkrd.withOptions({ securityMode: "strict" });
   * const v2 = checkrd.withOptions({ apiVersion: "2026-05-01" });
   * ```
   */
  withOptions(overrides: Partial<InitOptions>): Checkrd {
    return new Checkrd({ ...this.options, ...overrides });
  }

  /**
   * Globally patch the `openai` SDK to route every new `OpenAI()`
   * instance through Checkrd. Idempotent — calling twice is a no-op.
   *
   * Order matters: call this BEFORE the first `new OpenAI(...)` in
   * your application. The patch wraps the constructor, so existing
   * client instances keep their pre-patch fetch.
   *
   * @throws {CheckrdInitError} If the engine cannot be initialized
   *   from the configured options (missing policy, invalid key,
   *   WASM integrity failure).
   * @example
   * ```ts
   * import OpenAI from "openai";
   * import { Checkrd } from "checkrd";
   *
   * const checkrd = new Checkrd({ apiKey: process.env.CHECKRD_API_KEY! });
   * checkrd.instrumentOpenAI();              // BEFORE any new OpenAI()
   * const client = new OpenAI();             // automatically wrapped
   * ```
   */
  instrumentOpenAI(): void {
    this.ensureGlobalContext();
    instrumentOpenAI();
  }

  /**
   * Globally patch the `@anthropic-ai/sdk` package. See
   * {@link instrumentOpenAI} for semantics.
   */
  instrumentAnthropic(): void {
    this.ensureGlobalContext();
    instrumentAnthropic();
  }

  /**
   * Un-patch every vendor SDK previously instrumented via this
   * client. Safe to call even if nothing was instrumented.
   */
  uninstrumentAll(): void {
    uninstrumentOpenAI();
    uninstrumentAnthropic();
  }

  /**
   * Return the current SDK health snapshot. Identical payload to the
   * top-level {@link healthy} function — exposed on the class for
   * callers who only import `Checkrd`.
   */
  healthy(): HealthReport {
    return healthy();
  }

  /**
   * Tear down background resources — the telemetry batcher, the SSE
   * control receiver, and any global `init()` context installed by
   * `.instrument*()`. Idempotent. Returns a promise so callers can
   * `await checkrd.close()` even though the Python-equivalent shutdown
   * is synchronous under the hood.
   *
   * @returns A promise that resolves once shutdown completes (or
   *   immediately if the client was already closed).
   * @example Express app shutdown
   * ```ts
   * const checkrd = new Checkrd();
   * process.on("SIGTERM", async () => {
   *   await checkrd.close();   // drain telemetry, disconnect SSE
   *   process.exit(0);
   * });
   * ```
   */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    if (this.globalContextInstalled) {
      try {
        await shutdown();
      } catch {
        // close() must never raise — a failing shutdown in test or
        // production is a warning, not a new exception to propagate.
      }
    }
    // No separate return value — consumers don't need per-subsystem
    // results; they need a single "cleanly stopped" signal.
  }

  /**
   * Custom JSON serialization. `JSON.stringify(checkrd)` intentionally
   * omits `apiKey` so a misconfigured logger can't leak credentials
   * into an observability pipeline. Matches Stripe's pattern of
   * `repr(stripe.Stripe(api_key="..."))` omitting the key.
   */
  toJSON(): {
    agentId: string | undefined;
    baseUrl: string | undefined;
    apiVersion: string | undefined;
    hasApiKey: boolean;
    closed: boolean;
  } {
    const settings = this.resolveSettings();
    return {
      agentId: settings.agentId,
      baseUrl: settings.controlPlaneUrl,
      apiVersion: settings.apiVersion,
      hasApiKey: Boolean(this.apiKey),
      closed: this.closed,
    };
  }

  /**
   * Readable summary for REPL / debug contexts. NEVER shows the API
   * key value — only whether one is set.
   */
  toString(): string {
    const json = this.toJSON();
    return `Checkrd(agentId=${json.agentId ?? "(none)"}, ` +
      `baseUrl=${json.baseUrl ?? "(none)"}, ` +
      `hasApiKey=${String(json.hasApiKey)})`;
  }

  // -------------------------------------------------------------------
  // Internals
  // -------------------------------------------------------------------

  private ensureGlobalContext(): void {
    if (this.globalContextInstalled) return;
    // Real-browser guard and other checks live inside init();
    // we pass the raw options.
    init(this.options);
    this.globalContextInstalled = true;
  }

  private resolveSettings(): {
    agentId: string;
    controlPlaneUrl: string;
    apiVersion: string;
  } {
    // Re-run settings resolution on every call — inexpensive (pure
    // function over env vars) and ensures we pick up late-binding
    // environment changes. Returning a shaped subset avoids leaking
    // the full internal settings type through a public getter.
    const settings = resolve({
      agentId: this.options.agentId,
      apiKey: this.options.apiKey,
      controlPlaneUrl: this.options.controlPlaneUrl,
      enforce: this.options.enforce,
      debug: this.options.debug ?? false,
      securityMode: this.options.securityMode,
      apiVersion: this.options.apiVersion,
    });
    return {
      agentId: settings.agentId,
      controlPlaneUrl: settings.controlPlaneUrl,
      apiVersion: settings.apiVersion,
    };
  }
}

// Re-export the UNSET sentinel so callers can use it with `withOptions`
// if they want to unset a field without the "undefined vs missing"
// ambiguity JS has. Most users won't need it — they can just pass the
// new value directly — but the pattern is available for parity with
// OpenAI/Anthropic SDKs.
export { UNSET };

export type { WithOptionsOverrides };
