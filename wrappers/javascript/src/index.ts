/**
 * Checkrd — policy enforcement, kill switch, and telemetry for AI agents.
 *
 * Two modes of use:
 *
 * 1. Explicit per-client wrapping. Pass a base `fetch` (or leave
 *    `undefined` to use the global) plus your options, receive a
 *    Checkrd-enforced fetch:
 *
 *        import { wrapFetch, WasmEngine, loadConfig } from "checkrd";
 *        const engine = new WasmEngine(loadConfig("policy.yaml"), "my-agent");
 *        const myFetch = wrapFetch(fetch, { engine, enforce: true, agentId: "my-agent" });
 *        new OpenAI({ fetch: myFetch, apiKey: "..." });
 *
 * 2. Global instrumentation. `init()` stores the engine once and wires
 *    up the telemetry batcher, SSE receiver, and graceful-shutdown
 *    handlers; then `instrument*()` calls patch vendor SDK constructors
 *    so any new client transparently routes through Checkrd:
 *
 *        import { init, instrumentOpenAI } from "checkrd";
 *        init({ policy: "policy.yaml", agentId: "my-agent", apiKey: "ck_live_..." });
 *        instrumentOpenAI();
 *        // Every `new OpenAI({...})` now runs through Checkrd.
 */
import { loadConfig, type PolicyInput } from "./config.js";
import {
  WasmEngine,
  type WasmEngineCreateOptions,
  type WasmSource,
} from "./engine.js";
import { CheckrdInitError } from "./exceptions.js";
import { AnthropicInstrumentor } from "./integrations/_anthropic.js";
import { CohereInstrumentor } from "./integrations/_cohere.js";
import { GoogleGenAIInstrumentor } from "./integrations/_google_genai.js";
import { GroqInstrumentor } from "./integrations/_groq.js";
import { MistralInstrumentor } from "./integrations/_mistral.js";
import {
  OpenAIInstrumentor,
  type OpenAIInstrumentorOptions,
} from "./integrations/_openai.js";
import { TogetherInstrumentor } from "./integrations/_together.js";
import {
  getContext,
  hasContext,
  isDegraded,
  maybeContext,
  setContext,
  setDegraded,
} from "./_state.js";
import {
  resolve,
  type EnforceMode,
  type SecurityMode,
} from "./_settings.js";
import { VERSION } from "./_version.js";
import { wrapFetch, type FetchFn } from "./transports/fetch.js";
import { TelemetryBatcher } from "./batcher.js";
import { CircuitBreaker } from "./_circuit_breaker.js";
import { ControlPlaneSink, type TelemetrySink } from "./sinks.js";
import { ControlReceiver } from "./receiver.js";
import { attachBrowserUnloadFlush } from "./_browser_flush.js";
import {
  resolveLogger,
  warnDebugPiiRisk,
  warnRealBrowserUse,
  type Logger,
  type LogLevel,
} from "./_logger.js";
import { registerPublicKey } from "./_key_registrar.js";
import { registerDisposable, shutdownAll } from "./_shutdown.js";
import type {
  BeforeRequestHook,
  OnAllowHook,
  OnDenyHook,
} from "./hooks.js";

// Re-export types and classes that are part of the public surface.
// Errors mirror `wrappers/python/src/checkrd/exceptions.py` one-for-one.
export {
  // Base
  CheckrdError,
  // SDK-local
  CheckrdInitError,
  CheckrdPolicyDenied,
  PolicySignatureError,
  // Control-plane API errors
  APIError,
  APIStatusError,
  APIConnectionError,
  APITimeoutError,
  APIResponseValidationError,
  APIUserAbortError,
  // Status-code subclasses
  BadRequestError,
  AuthenticationError,
  PermissionDeniedError,
  NotFoundError,
  ConflictError,
  UnprocessableEntityError,
  RateLimitError,
  InternalServerError,
  // Helpers
  makeAPIError,
  isCheckrdPolicyDenied,
  FFI_ERROR_REASONS,
  DOCS_BASE_URL,
} from "./exceptions.js";
export type {
  APIErrorBody,
  APIStatusErrorDetails,
  APIConnectionErrorDetails,
  CheckrdPolicyDeniedDetails,
} from "./exceptions.js";
// `wrapFetch` stays on the main entry — it's the per-client wrapping
// path users reach for first when they have a vendor SDK that accepts
// a `fetch` option (OpenAI, Anthropic). Lower-level engine + telemetry
// primitives moved to `checkrd/advanced`.
export { wrapFetch } from "./transports/fetch.js";
export type { FetchFn, WrapFetchOptions } from "./transports/fetch.js";

// Hook callback shapes — referenced by `InitOptions` below.
export type {
  BeforeRequestHook,
  CheckrdEvent,
  CheckrdResponse,
  OnAllowHook,
  OnDenyHook,
} from "./hooks.js";

// Cross-realm Symbol used as the property key for the SDK's
// correlation request-id on every wrapped fetch response. Callers
// read it to tie a specific call to a telemetry event without
// re-instrumenting the request path. Mirrors OpenAI Node's
// ``_request_id``; Symbol form avoids vendor-SDK collisions.
export { CHECKRD_REQUEST_ID } from "./hooks.js";

// Configuration enums — narrow string literals consumers parameterize on.
export type { EnforceMode, SecurityMode } from "./_settings.js";

// Logger interface — accepted by `InitOptions.logger`. Users who want
// to construct one (`createConsoleLogger`, `noopLogger`) reach to
// `checkrd/advanced`.
export type { Logger, LogLevel } from "./_logger.js";

// Webhooks — sync (Node) + async (Edge / Workers / browsers).
export {
  verifyWebhook,
  verifyWebhookAsync,
  WebhookVerificationError,
} from "./webhooks.js";
export type { VerifyWebhookOptions } from "./webhooks.js";

/** Current SDK version. */
export const version = VERSION;

// Unified client class — the Week-2 consolidation. The top-level
// `wrap`, `wrapAsync`, `init`, and `instrument*` functions remain for
// backwards compatibility; new integrations are encouraged to use
// `new Checkrd({ apiKey, agentId })` for the single-object surface
// that matches OpenAI / Anthropic / Stripe conventions.
export { Checkrd, UNSET } from "./client.js";
export type { WithOptionsOverrides } from "./client.js";

// ---------------------------------------------------------------------------
// init() / shutdown() / instrument*()
// ---------------------------------------------------------------------------

/** Options for {@link init}. */
export interface InitOptions {
  /** Policy source (path, YAML/JSON string, or object). Required in strict mode. */
  policy?: PolicyInput;
  /** Agent ID override. Falls back to env / platform detection. */
  agentId?: string | undefined;
  /** API key for the control plane. Falls back to `CHECKRD_API_KEY`. */
  apiKey?: string | undefined;
  /** Control-plane base URL. Falls back to `CHECKRD_BASE_URL`. */
  controlPlaneUrl?: string | undefined;
  /** Explicit enforce flag, or "auto" (default) to infer. */
  enforce?: EnforceMode;
  /** Enable debug logging. Shortcut for `logLevel: "debug"`. */
  debug?: boolean;
  /** Structured log level (overrides `debug`). */
  logLevel?: LogLevel | undefined;
  /** User-supplied logger (pino / winston / bunyan-compatible). */
  logger?: Logger | undefined;
  /** Fail-closed ("strict", default) vs. pass-through on engine failure ("permissive"). */
  securityMode?: SecurityMode | undefined;
  /** Runs before policy evaluation. Return `false` to short-circuit. */
  beforeRequest?: BeforeRequestHook | undefined;
  /** Runs after an allowed evaluation. */
  onAllow?: OnAllowHook | undefined;
  /** Runs after a denied evaluation. */
  onDeny?: OnDenyHook | undefined;
  /** Advanced: supply a 32-byte Ed25519 private key. Otherwise anonymous. */
  privateKeyBytes?: Uint8Array;
  /** Override the default ControlPlaneSink with your own (file, OTel, composite, ...). */
  sink?: TelemetrySink | undefined;
  /** Disable the SSE control receiver (telemetry still flows). */
  disableControlReceiver?: boolean;
  /**
   * Stripe-style date pin sent as ``Checkrd-Version`` on every
   * control-plane request when non-empty. Pinning a known-good API
   * shape protects long-running deployments from silent
   * server-side breaking changes. Falls back to
   * ``CHECKRD_API_VERSION``.
   */
  apiVersion?: string | undefined;
  /**
   * Opt in to running in a browser-like environment. Off by default,
   * because the control-plane API key and the Ed25519 signing key live
   * in host process memory — a browser bundle would ship both to end
   * users. Setting this to `true` suppresses the safety check; you are
   * responsible for the consequences. Mirrors the OpenAI /
   * Anthropic SDK pattern.
   */
  dangerouslyAllowBrowser?: boolean;
  /**
   * Maximum attempts (including the first try) for short-lived control-
   * plane POSTs (telemetry batcher, public-key registration). Default
   * `3`. Mirrors the `maxRetries` knob on the OpenAI / Anthropic SDKs.
   * The SSE control receiver has its own long-lived reconnect logic and
   * is not governed by this value.
   */
  maxRetries?: number;
  /**
   * Per-attempt request timeout, in milliseconds, for short-lived
   * control-plane calls. Default `30_000`. Long-lived streams (the SSE
   * control receiver) are NOT bounded by this — they have their own
   * idle / read timeout.
   */
  timeout?: number;
  /**
   * TCP / TLS handshake timeout in milliseconds. Default `5_000`.
   * Currently only honored when the SDK constructs its own internal
   * HTTP client — caller-supplied `fetch` implementations enforce
   * their own connect semantics.
   */
  connectTimeout?: number;
}

/**
 * Options for {@link initAsync} and {@link wrapAsync}. Extends
 * {@link InitOptions} with the optional `wasm` override used by some
 * edge-runtime bundlers.
 */
export interface InitAsyncOptions extends InitOptions {
  /**
   * Explicit source for the WASM binary. When omitted, the SDK loads
   * `new URL('../checkrd_core.wasm', import.meta.url)` via `fetch`.
   * Pass the URL or pre-bound module when your bundler does not make
   * that resolution work at runtime (Cloudflare Workers with a
   * `wasm_modules` binding, Vercel Edge with `?module` imports,
   * Wrangler without the default asset pipeline).
   */
  wasm?: WasmSource;
}

/**
 * Detect whether the SDK is running in a REAL browser — a user-agent
 * environment where code is delivered to end users and keys can't be
 * kept secret. Distinct from other non-Node runtimes (Cloudflare
 * Workers, Vercel Edge, Deno, Bun) which are server-side and do not
 * ship code to end users.
 *
 * The previous heuristic (``!process.versions.node``) falsely flagged
 * every non-Node server runtime, forcing legitimate Cloudflare /
 * Next.js integrations to pass ``dangerouslyAllowBrowser: true`` —
 * which trained operators to sprinkle that flag without understanding
 * it. The replacement checks for signals that ONLY real browsers
 * have: a DOM (``document``), a window object with navigator, and
 * crucially no Node/Bun/Deno/Workerd marker on ``globalThis``.
 *
 * Signals checked (all must match for a "true browser" verdict):
 *   - ``typeof window !== "undefined"``
 *   - ``typeof document !== "undefined"``
 *   - ``typeof navigator !== "undefined"`` with ``userAgent`` string
 *   - no ``Deno`` / ``Bun`` / ``WorkerGlobalScope`` / Node ``process.versions.node``
 */
/**
 * Shape of globals checked by {@link isRealBrowser}. Extracted so
 * tests can pass a synthetic object instead of mutating the real
 * ``process``/``globalThis`` (the former's ``versions`` field is
 * read-only in Node, which makes monkey-patching fragile).
 */
export interface BrowserDetectionGlobals {
  window?: unknown;
  document?: unknown;
  navigator?: { userAgent?: unknown };
  Deno?: unknown;
  Bun?: unknown;
  WorkerGlobalScope?: unknown;
  EdgeRuntime?: unknown;
  process?: { versions?: { node?: unknown; bun?: unknown } };
}

/**
 * Heuristic that returns ``true`` only when the SDK is running in an
 * actual browser tab. Returns ``false`` for Node, Bun, Deno, Cloudflare
 * Workers, Vercel Edge, and other server-side runtimes that may
 * otherwise polyfill browser globals.
 */
export function isRealBrowser(globals?: BrowserDetectionGlobals): boolean {
  const g: BrowserDetectionGlobals = globals ?? globalThis;

  // Server-side runtimes take priority — if any is present, it is
  // NOT a real browser even if some of them polyfill `window` for
  // compat.
  if (g.Deno !== undefined) return false;
  if (g.Bun !== undefined) return false;
  if (g.process?.versions?.node !== undefined) return false;
  if (g.process?.versions?.bun !== undefined) return false;
  if (g.EdgeRuntime !== undefined) return false;
  // Cloudflare Workers expose `WorkerGlobalScope` but no `window`.
  if (
    g.WorkerGlobalScope !== undefined &&
    typeof g.window === "undefined"
  ) return false;

  // Require all three browser signals — any one alone is too weak
  // (some shims provide `window`, some test envs stub `document`).
  return (
    typeof g.window !== "undefined" &&
    typeof g.document !== "undefined" &&
    typeof g.navigator?.userAgent === "string"
  );
}

/** Result of {@link healthy}. */
/**
 * Specific cause when {@link HealthReport.status} is ``"degraded"``.
 * Mirrors the Python SDK's ``DegradationReason`` so cross-language
 * dashboards pivot on the same tokens.
 *
 * Each value maps to a documented remediation:
 *   - ``wasm_failed`` — WASM engine refused to load. Permissive
 *     mode is letting traffic through unevaluated.
 *   - ``control_plane_unreachable`` — telemetry POSTs and SSE
 *     receiver both failing.
 *   - ``control_plane_circuit_open`` — shared {@link CircuitBreaker}
 *     tripped; sleep for the jittered reset window or check the
 *     control plane.
 *   - ``signing_unavailable`` — engine has no Ed25519 key
 *     (anonymous mode). Telemetry batches drop with signing_error.
 *   - ``telemetry_dropping`` — backpressure or send errors past a
 *     threshold. Inspect ``HealthReport.telemetry`` for the
 *     specific drop counter.
 *
 * ``null`` whenever ``status !== "degraded"``.
 */
export type DegradationReason =
  | "wasm_failed"
  | "control_plane_unreachable"
  | "control_plane_circuit_open"
  | "signing_unavailable"
  | "telemetry_dropping";

/**
 * Health snapshot emitted by {@link Checkrd.healthy} and the standalone
 * {@link healthy} helper. Mirrors the Python SDK's ``HealthCheck``
 * TypedDict — same field names, same value semantics — so dashboards
 * can render either SDK's payload with the same query.
 */
export interface HealthReport {
  /** Overall status. */
  status: "healthy" | "degraded" | "disabled";
  /**
   * Stable token identifying which subsystem caused a ``degraded``
   * status. ``null`` when status is not degraded; one of
   * {@link DegradationReason}'s values when it is. Dashboards
   * pivot on this; K8s probes ignore it.
   */
  degradation_reason: DegradationReason | null;
  /** Whether the WASM engine is loaded. */
  engine_loaded: boolean;
  /** Agent ID in effect. */
  agent_id: string | null;
  /** Whether enforcement is active. */
  enforce: boolean | null;
  /** Unix-ms timestamp of the last evaluate() call, or null. */
  last_eval_at: number | null;
  /** Whether a control plane is configured (apiKey + baseUrl both set). */
  control_plane_connected: boolean;
  /** Batcher counters, when a batcher is running. */
  telemetry: {
    sent: number;
    dropped_backpressure: number;
    dropped_send_error: number;
    pending: number;
  } | null;
  /** Receiver counters, when a receiver is running. */
  receiver: {
    running: boolean;
    connected: boolean;
    reconnects: number;
    events_received: number;
  } | null;
}

const OBSERVATION_MODE_POLICY = {
  default: "allow",
  rules: [],
};

function resolvePolicyJson(
  policy: PolicyInput,
): { json: string; explicit: boolean } {
  if (policy !== undefined && policy !== null) {
    return { json: loadConfig(policy), explicit: true };
  }
  try {
    return { json: loadConfig(null), explicit: true };
  } catch {
    return {
      json: JSON.stringify(OBSERVATION_MODE_POLICY),
      explicit: false,
    };
  }
}

/**
 * Shared pre-engine work for `init` and `initAsync`: browser guard,
 * settings resolution, policy JSON resolution, logger selection, and
 * the engine options dict that the caller hands to the constructor
 * or factory.
 */
interface InitPrelude {
  settings: ReturnType<typeof resolve>;
  policyJson: string;
  policyWasExplicit: boolean;
  engineOpts: { privateKeyBytes?: Uint8Array };
  logger: Logger;
}

function initPrelude(options: InitOptions, caller: string): InitPrelude | null {
  if (isRealBrowser() && !options.dangerouslyAllowBrowser) {
    throw new CheckrdInitError(
      `checkrd ${caller} detected a real browser environment (window + ` +
        "document + navigator present, no server-runtime signals). The " +
        "control-plane API key AND the Ed25519 agent signing key live in " +
        "host memory; shipping them to a browser exposes them to every " +
        "end user.\n" +
        "\n" +
        "A leaked agent signing key does NOT just leak data — it lets " +
        "anyone who inspects the bundle FORGE telemetry batches from " +
        "your agent, which can poison downstream policy decisions and " +
        "your audit trail.\n" +
        "\n" +
        "If you really mean to do this (e.g. demo / research / internal " +
        "tool on an authenticated page where this is acceptable), pass " +
        "`dangerouslyAllowBrowser: true` AND expect to rotate the key " +
        "whenever the bundle is viewed by someone who shouldn't have it.\n" +
        "\n" +
        "See https://checkrd.io/errors/browser_use_detected",
    );
  }
  if (isRealBrowser() && options.dangerouslyAllowBrowser) {
    // Loud one-time banner even when the operator opted in. The flag
    // name starts with "dangerously" for a reason; operators who turn
    // it on should not be able to claim they weren't warned.
    warnRealBrowserUse();
  }
  const logger = resolveLogger({
    logger: options.logger,
    logLevel: options.logLevel,
    debug: options.debug,
  });
  const settings = resolve({
    agentId: options.agentId,
    apiKey: options.apiKey,
    controlPlaneUrl: options.controlPlaneUrl,
    enforce: options.enforce,
    debug: options.debug ?? false,
    securityMode: options.securityMode,
    apiVersion: options.apiVersion,
  });
  // Operator-facing PII banner fires BEFORE the disabled short-circuit.
  // An operator running with CHECKRD_DEBUG=1 AND CHECKRD_DISABLED=1 is
  // likely in a rollback scenario — they still want to know that
  // re-enabling Checkrd would route prompt payloads through debug
  // logs. See `_logger.ts::warnDebugPiiRisk` for the full rationale.
  if (settings.debug) {
    warnDebugPiiRisk();
  }
  if (settings.disabled) return null;

  const resolution = resolvePolicyJson(options.policy);
  const engineOpts: { privateKeyBytes?: Uint8Array } = {};
  if (options.privateKeyBytes !== undefined) {
    engineOpts.privateKeyBytes = options.privateKeyBytes;
  }
  return {
    settings,
    policyJson: resolution.json,
    policyWasExplicit: resolution.explicit,
    engineOpts,
    logger,
  };
}

/**
 * Shared post-engine work for `init` and `initAsync`: wire up the
 * telemetry batcher, the control receiver, the graceful-shutdown
 * hooks, and the global context.
 */
function completeInit(
  engine: WasmEngine,
  prelude: InitPrelude,
  options: InitOptions,
): void {
  const { settings, policyWasExplicit, logger } = prelude;
  // The engine is the authority on enforce-vs-dry-run (mirrors OPA-PEP,
  // Envoy ext_authz, Stripe Radar, AWS Config, Cloudflare WAF — every
  // comparable system has the policy carry the mode and the enforcement
  // point trust the verdict). Our policy schema's `mode` field is honored
  // inside the WASM core: `mode: dry_run` makes evaluate_request return
  // allowed=true even when a deny rule matches, so the transport's
  // "block on deny" never fires under dry_run regardless of this flag.
  // When `enforce` is `auto`, default to `true` so a dashboard-published
  // `mode: enforce` policy actually blocks; explicit `false` from the
  // operator still wins. See the Python `_resolve_effective_enforce` for
  // the full rationale.
  const effectiveEnforce = settings.enforceOverride ?? true;
  void policyWasExplicit; // kept on the prelude for future use

  // Register the agent's public key with the control plane so the
  // server can verify RFC 9421 signatures on our telemetry. Fire-
  // and-forget; retries are bounded, all failures log. Only fires
  // when we both hold a private key and know where to send it.
  if (
    settings.hasControlPlane &&
    prelude.engineOpts.privateKeyBytes?.byteLength === 32
  ) {
    try {
      const publicKey = WasmEngine.derivePublicKey(
        prelude.engineOpts.privateKeyBytes,
      );
      void registerPublicKey({
        controlPlaneUrl: settings.controlPlaneUrl,
        apiKey: settings.apiKey,
        agentId: settings.agentId,
        publicKey,
        logger,
        apiVersion: settings.apiVersion,
        ...(options.maxRetries !== undefined && { maxRetries: options.maxRetries }),
        ...(options.timeout !== undefined && { timeoutMs: options.timeout }),
      });
    } catch (err) {
      logger.debug("checkrd: could not derive public key for registration", {
        err,
      });
    }
  }

  let batcher: TelemetryBatcher | undefined;
  let sink: TelemetrySink | undefined = options.sink;
  // One CircuitBreaker per init call, shared between the batcher and
  // the SSE receiver below. When the batcher trips it on a 5xx /
  // network failure, the receiver's reconnect loop short-circuits
  // instead of burning a 90-second SSE read timeout. Single source
  // of truth for control-plane health, mirroring the AWS SDK pattern.
  const sharedBreaker = new CircuitBreaker();
  if (!sink && settings.hasControlPlane) {
    batcher = new TelemetryBatcher({
      controlPlaneUrl: settings.controlPlaneUrl,
      apiKey: settings.apiKey,
      agentId: settings.agentId,
      engine,
      logger,
      apiVersion: settings.apiVersion,
      samplingRate: settings.samplingRate,
      circuitBreaker: sharedBreaker,
      ...(options.maxRetries !== undefined && { maxAttempts: options.maxRetries }),
      ...(options.timeout !== undefined && { timeoutMs: options.timeout }),
    });
    batcher.start();
    sink = new ControlPlaneSink(batcher);
    const batcherRef = batcher;
    // Browser-only: wire `pagehide` + `beforeunload` to fire a
    // synchronous best-effort flush via `fetch(..., { keepalive: true })`
    // so the operator's last events survive a navigation. The helper
    // is a no-op on runtimes without `window`, so the call is safe
    // unconditionally — no extra `isRealBrowser()` branch needed
    // here. Detach on shutdown so re-init in the same process
    // (tests) does not double-attach.
    const detachBrowserFlush = attachBrowserUnloadFlush(batcherRef, {
      logger,
    });
    registerDisposable({
      close: () => {
        detachBrowserFlush();
        return batcherRef.stop();
      },
    });
  }

  let receiver: ControlReceiver | undefined;
  if (settings.hasControlPlane && !options.disableControlReceiver) {
    receiver = new ControlReceiver({
      controlPlaneUrl: settings.controlPlaneUrl,
      apiKey: settings.apiKey,
      agentId: settings.agentId,
      engine,
      // Share the breaker the batcher owns. See the comment above
      // ``new CircuitBreaker()`` for the rationale.
      circuitBreaker: sharedBreaker,
      logger,
      apiVersion: settings.apiVersion,
    });
    receiver.start();
    const receiverRef = receiver;
    registerDisposable({ close: () => receiverRef.stop() });
  }

  setContext({
    engine,
    enforce: effectiveEnforce,
    settings,
    onAllow: options.onAllow,
    onDeny: options.onDeny,
    beforeRequest: options.beforeRequest,
    degraded: false,
    lastEvalAt: null,
    sink,
    batcher,
    receiver,
    logger,
  });
}

/**
 * Initialize the global Checkrd runtime. Call once at startup, then use
 * `instrument*()` to patch vendor SDKs. Node / Bun only — on edge
 * runtimes (Cloudflare Workers, Vercel Edge, Deno, browser) use
 * {@link initAsync} instead.
 */
export function init(options: InitOptions = {}): void {
  setDegraded(false);
  const prelude = initPrelude(options, "init()");
  if (!prelude) return;
  let engine: WasmEngine;
  try {
    engine = new WasmEngine(
      prelude.policyJson,
      prelude.settings.agentId,
      prelude.engineOpts,
    );
  } catch (err) {
    if (options.policy !== undefined && options.policy !== null) throw err;
    if (prelude.settings.securityMode === "strict") throw err;
    setDegraded(true);
    return;
  }
  completeInit(engine, prelude, options);
}

/**
 * Asynchronous variant of {@link init}. Works on every runtime that
 * supports `fetch` + `WebAssembly` + `crypto.subtle` (Node 20+, Bun,
 * Deno, Cloudflare Workers, Vercel Edge, modern browsers).
 *
 *     import { initAsync } from "checkrd";
 *     await initAsync({ policy: "policy.yaml", apiKey: env.CHECKRD_API_KEY });
 *
 * If your bundler does not resolve `new URL('../checkrd_core.wasm',
 * import.meta.url)` to a fetchable asset URL at runtime (some
 * Cloudflare Workers / Vercel Edge configurations), pass the WASM
 * source explicitly via `options.wasm`:
 *
 *     import wasm from "./checkrd_core.wasm";
 *     await initAsync({ policy, apiKey, wasm });
 */
export async function initAsync(options: InitAsyncOptions = {}): Promise<void> {
  setDegraded(false);
  const prelude = initPrelude(options, "initAsync()");
  if (!prelude) return;
  let engine: WasmEngine;
  try {
    const createOpts: WasmEngineCreateOptions = { ...prelude.engineOpts };
    if (options.wasm !== undefined) createOpts.wasm = options.wasm;
    engine = await WasmEngine.create(
      prelude.policyJson,
      prelude.settings.agentId,
      createOpts,
    );
  } catch (err) {
    if (options.policy !== undefined && options.policy !== null) throw err;
    if (prelude.settings.securityMode === "strict") throw err;
    setDegraded(true);
    return;
  }
  completeInit(engine, prelude, options);
}

/**
 * Tear down the global runtime and close the telemetry/control-plane
 * resources. Awaitable — call `await shutdown()` from a SIGTERM handler
 * for deterministic flush. Safe to call multiple times.
 */
export async function shutdown(): Promise<void> {
  setContext(null);
  setDegraded(false);
  await shutdownAll();
}

/**
 * Return the live WASM engine installed by {@link init} or
 * {@link initAsync}. Framework adapters (LangChain.js, OpenAI Agents,
 * AI SDK, MCP, Mastra, Hono) take an `engine` option — this is the
 * canonical way to fetch it after a global init. Mirrors the Python
 * `_GlobalContext` access pattern.
 *
 * @throws {CheckrdInitError} If `init()` / `initAsync()` has not been
 *   called yet.
 */
export function getEngine(): WasmEngine {
  if (!hasContext()) {
    throw new CheckrdInitError(
      "checkrd.getEngine(): call init() / initAsync() first.",
    );
  }
  return getContext().engine;
}

/**
 * Return the live telemetry sink installed by {@link init} or
 * {@link initAsync}, or `undefined` when no control plane is
 * configured (no `apiKey`/`controlPlaneUrl`). Framework adapters take
 * an optional `sink` — pass this through directly.
 */
export function getSink(): TelemetrySink | undefined {
  return maybeContext()?.sink;
}

/**
 * Per-client wrap: return a Checkrd-enforced fetch without touching any
 * global state. Preferred over `init()` + `instrument*()` for tests and
 * for apps that want explicit control over which clients are enforced.
 * Node / Bun only — see {@link wrapAsync} for edge runtimes.
 */
export function wrap(
  baseFetch: FetchFn | undefined,
  options: InitOptions,
): FetchFn {
  if (isRealBrowser() && !options.dangerouslyAllowBrowser) {
    throw new CheckrdInitError(
      "checkrd.wrap: real browser environment detected. The control-" +
        "plane API key and Ed25519 agent signing key would ship to end " +
        "users. See init() docs and " +
        "https://checkrd.io/errors/browser_use_detected for details.",
    );
  }
  if (isRealBrowser() && options.dangerouslyAllowBrowser) {
    warnRealBrowserUse();
  }
  const base = baseFetch ?? globalThis.fetch.bind(globalThis);
  const settings = resolve({
    agentId: options.agentId,
    apiKey: options.apiKey,
    controlPlaneUrl: options.controlPlaneUrl,
    enforce: options.enforce,
    debug: options.debug ?? false,
    securityMode: options.securityMode,
    apiVersion: options.apiVersion,
  });
  if (settings.disabled) return base;

  const policyResolution = resolvePolicyJson(options.policy);
  const engineOpts: { privateKeyBytes?: Uint8Array } = {};
  if (options.privateKeyBytes !== undefined) {
    engineOpts.privateKeyBytes = options.privateKeyBytes;
  }
  const engine = new WasmEngine(
    policyResolution.json,
    settings.agentId,
    engineOpts,
  );
  return buildWrappedFetch(base, engine, settings, policyResolution.explicit, options);
}

/**
 * Asynchronous variant of {@link wrap}. Mirrors the API shape but uses
 * the runtime-agnostic {@link WasmEngine.create} factory so the call
 * works on Cloudflare Workers / Vercel Edge / Deno / browser.
 */
export async function wrapAsync(
  baseFetch: FetchFn | undefined,
  options: InitAsyncOptions,
): Promise<FetchFn> {
  if (isRealBrowser() && !options.dangerouslyAllowBrowser) {
    throw new CheckrdInitError(
      "checkrd.wrapAsync: real browser environment detected. The control-" +
        "plane API key and Ed25519 agent signing key would ship to end " +
        "users. See initAsync() docs and " +
        "https://checkrd.io/errors/browser_use_detected for details.",
    );
  }
  if (isRealBrowser() && options.dangerouslyAllowBrowser) {
    warnRealBrowserUse();
  }
  const base = baseFetch ?? globalThis.fetch.bind(globalThis);
  const settings = resolve({
    agentId: options.agentId,
    apiKey: options.apiKey,
    controlPlaneUrl: options.controlPlaneUrl,
    enforce: options.enforce,
    debug: options.debug ?? false,
    securityMode: options.securityMode,
    apiVersion: options.apiVersion,
  });
  if (settings.disabled) return base;

  const policyResolution = resolvePolicyJson(options.policy);
  const createOpts: WasmEngineCreateOptions = {};
  if (options.privateKeyBytes !== undefined) {
    createOpts.privateKeyBytes = options.privateKeyBytes;
  }
  if (options.wasm !== undefined) createOpts.wasm = options.wasm;
  const engine = await WasmEngine.create(
    policyResolution.json,
    settings.agentId,
    createOpts,
  );
  return buildWrappedFetch(base, engine, settings, policyResolution.explicit, options);
}

function buildWrappedFetch(
  base: FetchFn,
  engine: WasmEngine,
  settings: ReturnType<typeof resolve>,
  policyWasExplicit: boolean,
  options: InitOptions,
): FetchFn {
  // See `completeInit` for the full rationale — engine is the authority
  // on dry-run vs enforce, the transport just trusts the verdict.
  void policyWasExplicit;
  const enforce = settings.enforceOverride ?? true;

  const wrapOpts: {
    engine: WasmEngine;
    enforce: boolean;
    agentId: string;
    dashboardUrl: string;
    beforeRequest?: BeforeRequestHook | undefined;
    onAllow?: OnAllowHook | undefined;
    onDeny?: OnDenyHook | undefined;
    sink?: TelemetrySink;
    logger?: Logger;
    securityMode: SecurityMode;
  } = {
    engine,
    enforce,
    agentId: settings.agentId,
    dashboardUrl: settings.dashboardUrl,
    beforeRequest: options.beforeRequest,
    onAllow: options.onAllow,
    onDeny: options.onDeny,
    securityMode: settings.securityMode,
  };
  if (options.sink !== undefined) wrapOpts.sink = options.sink;
  if (options.logger !== undefined) {
    wrapOpts.logger = options.logger;
  } else if (options.logLevel !== undefined || options.debug) {
    wrapOpts.logger = resolveLogger({
      logLevel: options.logLevel,
      debug: options.debug,
    });
  }

  return wrapFetch(base, wrapOpts);
}

/** Health-check dict for readiness probes. */
export function healthy(): HealthReport {
  if (isDegraded()) {
    // ``setDegraded(true)`` is set only when the WASM engine refuses
    // to load AND we're in permissive mode (strict mode throws). So
    // the reason at this layer is always ``wasm_failed``; other
    // degradation modes are classified post-context below.
    return {
      status: "degraded",
      degradation_reason: "wasm_failed",
      engine_loaded: false,
      agent_id: null,
      enforce: null,
      last_eval_at: null,
      control_plane_connected: false,
      telemetry: null,
      receiver: null,
    };
  }
  const ctx = maybeContext();
  if (!ctx) {
    return {
      status: "disabled",
      degradation_reason: null,
      engine_loaded: false,
      agent_id: null,
      enforce: null,
      last_eval_at: null,
      control_plane_connected: false,
      telemetry: null,
      receiver: null,
    };
  }
  const telemetry = ctx.batcher ? snapshotBatcher(ctx.batcher) : null;
  const receiver = ctx.receiver ? snapshotReceiver(ctx.receiver) : null;
  // Classify post-init degradation. The engine loaded fine and a
  // context exists; the runtime plumbing (control plane, signing,
  // backpressure) is what's potentially wrong now.
  const [status, degradation_reason] = classifyDegradation(
    ctx.batcher,
    telemetry,
    ctx.settings.hasControlPlane,
  );
  return {
    status,
    degradation_reason,
    engine_loaded: true,
    agent_id: ctx.settings.agentId,
    enforce: ctx.enforce,
    last_eval_at: ctx.lastEvalAt,
    control_plane_connected: ctx.settings.hasControlPlane,
    telemetry,
    receiver,
  };
}

/**
 * Map runtime state to ``[status, degradation_reason]``.
 *
 * Order matters — more-severe degradations win. Circuit breaker
 * open is the strongest signal because it means the SDK is
 * actively fast-failing telemetry; backpressure is weaker because
 * it can be transient under load.
 */
function classifyDegradation(
  batcher: TelemetryBatcher | undefined,
  telemetry: HealthReport["telemetry"],
  hasControlPlane: boolean,
): [HealthReport["status"], DegradationReason | null] {
  // Shared circuit breaker (batcher + receiver). When tripped, the
  // SDK is fast-failing every control-plane call.
  if (batcher !== undefined) {
    const diag = batcher.diagnostics();
    if (diag.circuitBreaker.state === "open") {
      return ["degraded", "control_plane_circuit_open"];
    }
  }
  if (telemetry !== null) {
    // Sustained signing errors with zero successes ⇒ no key
    // configured. Mirror of the Python check.
    if (
      "dropped_send_error" in telemetry &&
      telemetry.sent === 0 &&
      telemetry.dropped_send_error > 0 &&
      hasControlPlane &&
      !batcher
    ) {
      return ["degraded", "control_plane_unreachable"];
    }
    // Backpressure dominating successful sends ⇒ load shed.
    if (
      telemetry.dropped_backpressure > 0 &&
      telemetry.dropped_backpressure > telemetry.sent
    ) {
      return ["degraded", "telemetry_dropping"];
    }
  }
  return ["healthy", null];
}

function snapshotBatcher(batcher: TelemetryBatcher): HealthReport["telemetry"] {
  const d = batcher.diagnostics();
  return {
    sent: d.sent,
    dropped_backpressure: d.droppedBackpressure,
    dropped_send_error: d.droppedSendError,
    pending: d.pending,
  };
}

function snapshotReceiver(receiver: ControlReceiver): HealthReport["receiver"] {
  const d = receiver.diagnostics();
  return {
    running: d.running,
    connected: d.connected,
    reconnects: d.reconnects,
    events_received: d.eventsReceived,
  };
}

// ---------------------------------------------------------------------------
// Per-vendor instrumentation helpers
// ---------------------------------------------------------------------------

function buildInstrumentorOptions(): OpenAIInstrumentorOptions {
  const ctx = getContext();
  const base: OpenAIInstrumentorOptions = {
    engine: ctx.engine,
    enforce: ctx.enforce,
    agentId: ctx.settings.agentId,
    dashboardUrl: ctx.settings.dashboardUrl,
    beforeRequest: ctx.beforeRequest,
    onAllow: ctx.onAllow,
    onDeny: ctx.onDeny,
    logger: ctx.logger,
    securityMode: ctx.settings.securityMode,
  };
  if (ctx.sink) base.sink = ctx.sink;
  return base;
}

let _openaiInstrumentor: OpenAIInstrumentor | null = null;
let _anthropicInstrumentor: AnthropicInstrumentor | null = null;
let _cohereInstrumentor: CohereInstrumentor | null = null;
let _groqInstrumentor: GroqInstrumentor | null = null;
let _mistralInstrumentor: MistralInstrumentor | null = null;
let _togetherInstrumentor: TogetherInstrumentor | null = null;
let _googleGenAIInstrumentor: GoogleGenAIInstrumentor | null = null;

function ensureInitialized(fn: string): void {
  if (!hasContext()) {
    throw new CheckrdInitError(`${fn} called before init()`);
  }
}

/** Patch the `openai` package so every new client routes through Checkrd. */
export function instrumentOpenAI(): void {
  ensureInitialized("instrumentOpenAI()");
  if (isDegraded()) return;
  _openaiInstrumentor ??= new OpenAIInstrumentor(buildInstrumentorOptions());
  _openaiInstrumentor.instrument();
}

/** Revert the `openai` patch installed by {@link instrumentOpenAI}. */
export function uninstrumentOpenAI(): void {
  _openaiInstrumentor?.uninstrument();
}

/** Patch the `@anthropic-ai/sdk` package. */
export function instrumentAnthropic(): void {
  ensureInitialized("instrumentAnthropic()");
  if (isDegraded()) return;
  _anthropicInstrumentor ??= new AnthropicInstrumentor(buildInstrumentorOptions());
  _anthropicInstrumentor.instrument();
}

/** Revert the Anthropic patch installed by {@link instrumentAnthropic}. */
export function uninstrumentAnthropic(): void {
  _anthropicInstrumentor?.uninstrument();
}

/** Patch the `cohere-ai` package. */
export function instrumentCohere(): void {
  ensureInitialized("instrumentCohere()");
  if (isDegraded()) return;
  _cohereInstrumentor ??= new CohereInstrumentor(buildInstrumentorOptions());
  _cohereInstrumentor.instrument();
}

/** Revert the Cohere patch installed by {@link instrumentCohere}. */
export function uninstrumentCohere(): void {
  _cohereInstrumentor?.uninstrument();
}

/** Patch the `groq-sdk` package. */
export function instrumentGroq(): void {
  ensureInitialized("instrumentGroq()");
  if (isDegraded()) return;
  _groqInstrumentor ??= new GroqInstrumentor(buildInstrumentorOptions());
  _groqInstrumentor.instrument();
}

/** Revert the Groq patch installed by {@link instrumentGroq}. */
export function uninstrumentGroq(): void {
  _groqInstrumentor?.uninstrument();
}

/** Patch the `@mistralai/mistralai` package. */
export function instrumentMistral(): void {
  ensureInitialized("instrumentMistral()");
  if (isDegraded()) return;
  _mistralInstrumentor ??= new MistralInstrumentor(buildInstrumentorOptions());
  _mistralInstrumentor.instrument();
}

/** Revert the Mistral patch installed by {@link instrumentMistral}. */
export function uninstrumentMistral(): void {
  _mistralInstrumentor?.uninstrument();
}

/** Patch the `together-ai` package. */
export function instrumentTogether(): void {
  ensureInitialized("instrumentTogether()");
  if (isDegraded()) return;
  _togetherInstrumentor ??= new TogetherInstrumentor(buildInstrumentorOptions());
  _togetherInstrumentor.instrument();
}

/** Revert the Together patch installed by {@link instrumentTogether}. */
export function uninstrumentTogether(): void {
  _togetherInstrumentor?.uninstrument();
}

/** Patch the `@google/genai` package. */
export function instrumentGoogleGenAI(): void {
  ensureInitialized("instrumentGoogleGenAI()");
  if (isDegraded()) return;
  _googleGenAIInstrumentor ??= new GoogleGenAIInstrumentor(buildInstrumentorOptions());
  _googleGenAIInstrumentor.instrument();
}

/** Revert the Google GenAI patch installed by {@link instrumentGoogleGenAI}. */
export function uninstrumentGoogleGenAI(): void {
  _googleGenAIInstrumentor?.uninstrument();
}

/** Apply every available vendor instrumentor in one call. */
export function instrument(): void {
  instrumentOpenAI();
  instrumentAnthropic();
  instrumentCohere();
  instrumentGroq();
  instrumentMistral();
  instrumentTogether();
  instrumentGoogleGenAI();
}

/** Revert every instrumentor installed via {@link instrument}. */
export function uninstrument(): void {
  uninstrumentOpenAI();
  uninstrumentAnthropic();
  uninstrumentCohere();
  uninstrumentGroq();
  uninstrumentMistral();
  uninstrumentTogether();
  uninstrumentGoogleGenAI();
}
