/**
 * Advanced surface — power-user primitives that don't belong on the
 * main `checkrd` entry.
 *
 * Why a separate barrel:
 *
 * The main entry (`./index.ts`) has been curated down to the ~25
 * symbols a typical integration touches: client class, init helpers,
 * errors, webhook verifiers. Everything else — engine internals,
 * sinks, identity providers, watchers, retry primitives, pagination
 * scaffolding — moved here. That keeps IntelliSense on the bare
 * `import { ... } from "checkrd"` import sane (a typical OpenAI SDK
 * shows ~10–15 completions; this file's 60+ symbols would dominate
 * otherwise) without removing anything power users rely on.
 *
 * Same pattern Stripe Node, OpenAI Node, and Anthropic SDK use —
 * minimal main entry, deeper subpath for "I know what I'm doing"
 * usage.
 *
 * Stability: these symbols ARE part of the public API and follow
 * SemVer. The "advanced" label is about *discoverability*, not about
 * stability or compatibility.
 *
 *     import { TelemetryBatcher, ConsoleSink } from "checkrd/advanced";
 */

// ---------------------------------------------------------------------------
// Engine + signing
// ---------------------------------------------------------------------------
export { WasmEngine } from "./engine.js";
export type {
  EvalResult,
  EvaluateRequest,
  Keypair,
  SignedBatch,
  WasmEngineOptions,
  WasmEngineCreateOptions,
  WasmSource,
} from "./engine.js";

// ---------------------------------------------------------------------------
// Identity providers
// ---------------------------------------------------------------------------
export {
  LocalIdentity,
  ExternalIdentity,
  DEFAULT_KEY_ENV_VAR,
} from "./identity.js";
export type { IdentityProvider, ExternalIdentityOptions } from "./identity.js";

// ---------------------------------------------------------------------------
// Telemetry pipeline — batcher, sinks, control receiver
// ---------------------------------------------------------------------------
export { TelemetryBatcher, URGENT_FLUSH_BODY_LIMIT_BYTES } from "./batcher.js";
export type {
  BatcherOptions,
  BatcherDiagnostics,
  TelemetryEvent,
} from "./batcher.js";

export {
  ConsoleSink,
  CompositeSink,
  ControlPlaneSink,
  JsonFileSink,
  OtelSpanSink,
  OtlpSink,
} from "./sinks.js";
export type {
  ConsoleSinkOptions,
  JsonFileSinkOptions,
  OtelSpanSinkOptions,
  OtlpSinkOptions,
  TelemetrySink,
} from "./sinks.js";

export { ControlReceiver, parseSSE } from "./receiver.js";
export type {
  ReceiverOptions,
  ReceiverDiagnostics,
  SSEEvent,
} from "./receiver.js";

// ---------------------------------------------------------------------------
// File-watcher entry points (Tier 3 / air-gapped deployments)
// ---------------------------------------------------------------------------
export { PolicyFileWatcher, KillSwitchFileWatcher } from "./watchers.js";
export type {
  PolicyFileWatcherOptions,
  KillSwitchFileWatcherOptions,
} from "./watchers.js";

// ---------------------------------------------------------------------------
// Resilience primitives
// ---------------------------------------------------------------------------
export { CircuitBreaker } from "./_circuit_breaker.js";
export type {
  CircuitBreakerOptions,
  CircuitBreakerDiagnostics,
  CircuitState,
} from "./_circuit_breaker.js";

export {
  fetchWithRetry,
  newIdempotencyKey,
  defaultControlHeaders,
} from "./_retry.js";
export type { RetryOptions } from "./_retry.js";

// ---------------------------------------------------------------------------
// Lifecycle / logging primitives
// ---------------------------------------------------------------------------
export {
  createConsoleLogger,
  noopLogger,
  redactSensitive,
  wrapWithRedaction,
} from "./_logger.js";
export type { LogAttributes } from "./_logger.js";

export { registerDisposable, shutdownAll } from "./_shutdown.js";
export type { Disposable } from "./_shutdown.js";

export { deprecationWarning } from "./_deprecation.js";

// ---------------------------------------------------------------------------
// Configuration helpers
// ---------------------------------------------------------------------------
export { loadConfig } from "./config.js";
export type { PolicyInput } from "./config.js";
export { ENVIRONMENT_URLS } from "./_settings.js";
export type { Environment, Settings } from "./_settings.js";

// ---------------------------------------------------------------------------
// Control-plane plumbing (low-level — most users go via init())
// ---------------------------------------------------------------------------
export { DEFAULT_DENY_POLICY_JSON, handleControlEvent } from "./control.js";
export type {
  ControlEngine,
  ControlEventName,
  ControlLogger,
} from "./control.js";

// ---------------------------------------------------------------------------
// Response + pagination scaffolding
// ---------------------------------------------------------------------------
export { APIResponse, StreamingAPIResponse } from "./_response.js";
export {
  BasePage,
  SinglePage,
  CursorPage,
  OffsetPage,
} from "./_pagination.js";

// ---------------------------------------------------------------------------
// Instrumentor classes — also exposed at vendor subpaths
// (`checkrd/openai`, `checkrd/anthropic`, etc.) so consumers who only
// need one don't drag the whole vendor matrix into their bundle. Power
// users running the unified surface can grab them from here.
// ---------------------------------------------------------------------------
export { Instrumentor } from "./integrations/_base.js";
export type { InstrumentorOptions } from "./integrations/_base.js";
export { OpenAIInstrumentor } from "./integrations/_openai.js";
export type { OpenAIInstrumentorOptions } from "./integrations/_openai.js";
export { AnthropicInstrumentor } from "./integrations/_anthropic.js";
export type { AnthropicInstrumentorOptions } from "./integrations/_anthropic.js";
export { CohereInstrumentor } from "./integrations/_cohere.js";
export type { CohereInstrumentorOptions } from "./integrations/_cohere.js";
export { GroqInstrumentor } from "./integrations/_groq.js";
export type { GroqInstrumentorOptions } from "./integrations/_groq.js";
export { MistralInstrumentor } from "./integrations/_mistral.js";
export type { MistralInstrumentorOptions } from "./integrations/_mistral.js";
export { TogetherInstrumentor } from "./integrations/_together.js";
export type { TogetherInstrumentorOptions } from "./integrations/_together.js";
export { GoogleGenAIInstrumentor } from "./integrations/_google_genai.js";
export type { GoogleGenAIInstrumentorOptions } from "./integrations/_google_genai.js";
