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
 *
 * # Module layout
 *
 * The `init` / `wrap` / `shutdown` / health surface lives in
 * `./_runtime.js` — a lean module with ZERO dependency on the vendor
 * instrumentors. This file re-exports that surface verbatim and adds
 * the `instrument*()` verbs (which DO pull the six vendor
 * instrumentors). Edge + framework adapters import the lean runtime
 * directly so they never bundle the instrumentors — see `_runtime.ts`.
 */
import { CheckrdInitError } from "./exceptions.js";
import { AnthropicInstrumentor } from "./integrations/_anthropic.js";
import { CohereInstrumentor } from "./integrations/_cohere.js";
import { GoogleGenAIInstrumentor } from "./integrations/_google_genai.js";
import { GroqInstrumentor } from "./integrations/_groq.js";
import {
  OpenAIInstrumentor,
  type OpenAIInstrumentorOptions,
} from "./integrations/_openai.js";
import { TogetherInstrumentor } from "./integrations/_together.js";
import { getContext, hasContext, isDegraded } from "./_state.js";
import { VERSION } from "./_version.js";

// Re-export types and classes that are part of the public surface.
// Errors mirror `wrappers/python/src/checkrd/exceptions.py` one-for-one.
export {
  // Base
  CheckrdError,
  // SDK-local
  CheckrdInitError,
  CheckrdPolicyDenied,
  PolicySignatureError,
  PricingSignatureError,
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
  PRICING_FFI_ERROR_REASONS,
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

// ---------------------------------------------------------------------------
// Lean runtime surface — init / wrap / shutdown / health.
//
// These live in `./_runtime.js` so the edge + framework adapters can
// import them WITHOUT dragging the six vendor instrumentors (below)
// into their bundles. Re-exported here verbatim so `import { init }
// from "checkrd"` keeps working.
// ---------------------------------------------------------------------------
export {
  init,
  initAsync,
  shutdown,
  getEngine,
  getSink,
  wrap,
  wrapAsync,
  healthy,
  isRealBrowser,
} from "./_runtime.js";
export type {
  InitOptions,
  InitAsyncOptions,
  BrowserDetectionGlobals,
  DegradationReason,
  HealthReport,
} from "./_runtime.js";

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
  instrumentTogether();
  instrumentGoogleGenAI();
}

/** Revert every instrumentor installed via {@link instrument}. */
export function uninstrument(): void {
  uninstrumentOpenAI();
  uninstrumentAnthropic();
  uninstrumentCohere();
  uninstrumentGroq();
  uninstrumentTogether();
  uninstrumentGoogleGenAI();
}
