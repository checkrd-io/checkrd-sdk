# Changelog

All notable changes to the Checkrd JavaScript SDK are documented in
this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Three new framework adapters** matching the Python SDK
  one-for-one. Each uses the framework's documented public extension
  point — no monkey-patching:
  - `checkrd/langchain` (`CheckrdCallbackHandler` for `@langchain/core`):
    subclass of LangChain.js's `BaseCallbackHandler`, hooks every
    LLM call, tool call, retriever call, and chain invocation.
  - `checkrd/openai-agents` (`CheckrdTracingProcessor` +
    `checkrdInputGuardrail()` / `checkrdOutputGuardrail()` for
    `@openai/agents`): tracing for observability, guardrails for
    enforcement (mirrors the SDK's intentional split).
  - `checkrd/claude-agent-sdk` (`attachToOptions()` + four hook
    factories for `@anthropic-ai/claude-agent-sdk`): adds Checkrd
    hooks to `ClaudeAgentOptions` for `PreToolUse`, `PostToolUse`,
    `UserPromptSubmit`, and `Stop`. Idempotent.
- **Optional `peerDependenciesMeta` declarations** for
  `@langchain/core`, `@openai/agents`, and
  `@anthropic-ai/claude-agent-sdk`. Consumers install only the peer
  for the framework they use.

### Changed

- **`tests/integrations/test_*.ts` files now run in CI.** The Vitest
  `include` glob was previously only `tests/**/*.test.ts`, leaving
  the per-vendor and per-framework integration tests orphaned. The
  glob now matches both `*.test.ts` and the
  `tests/integrations/test_*.ts` mirror of the Python wrapper layout
  — 70+ previously-orphaned integration tests are now part of CI.

## [0.3.0] — 2026-04-24

### Removed

- **Node 18 support.** `engines.node` is now `>=20`. Node 18 reached
  Maintenance LTS end-of-life in April 2025; vitest's forks pool on
  18 also doesn't expose `globalThis.crypto` by default, which the
  SDK's Web Crypto codepaths (Ed25519 signing, SHA-256 integrity
  check, HMAC webhook verification) require. Users on 18 should pin
  to `checkrd@<0.3.0` until they can upgrade.

### Added

- **`Checkrd` unified client class.** `new Checkrd({ apiKey, agentId })`
  with `.wrap()` / `.withOptions()` / `.instrumentOpenAI()` /
  `.healthy()` / `.close()`. OpenAI-SDK-shaped single entry point
  that bundles the previous `init()` + `wrap()` + `instrument*()`
  surface. Top-level functions remain for backwards compatibility.
- **`X-Checkrd-SDK-*` platform headers** stamped on every
  control-plane request. Six headers (`Lang`, `Version`, `Runtime`,
  `Runtime-Version`, `OS`, `Arch`). Runtime detection covers Node,
  Bun, Deno, Workerd, Edge-light, and browser.
- **`Checkrd-Version` date-pinned API version** (Stripe pattern).
  `apiVersion:` option or `CHECKRD_API_VERSION` env var. Stamped on
  every control-plane request — telemetry POST, key-register POST,
  SSE subscribe GET, state-poll GET.
- **`OtelSpanSink`** — creates real OpenTelemetry spans on the
  caller's existing tracer. Peer dep on `@opentelemetry/api`,
  lazy-imported so non-OTel users pay zero cost. Structural typing
  keeps the OTel types out of the public surface.
- **Production guard for `CHECKRD_SKIP_WASM_INTEGRITY`.** Eleven
  framework env signals × four production values checked; bypass
  refused unless the exact phrase
  `CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK=i-understand-the-risk`
  is set.
- **Real-browser detection** — `isRealBrowser()` requires
  `window` + `document` + `navigator.userAgent`, excludes
  `Deno` / `Bun` / `WorkerGlobalScope` / `EdgeRuntime`. Fixes the
  previous overly-broad `!process.versions.node` heuristic that
  false-flagged every non-Node server runtime.
- **`warnRealBrowserUse()` banner** — names the agent-signing-key
  forgery attack specifically when the operator opts in via
  `dangerouslyAllowBrowser: true` from a real browser.
- **`warnDebugPiiRisk()` banner** — one-time stderr notice when
  `CHECKRD_DEBUG=1` or `debug: true` observed. Falls back to
  `console.warn` on runtimes without `process.stderr`.
- **`scrubTelemetryEvent()`** — runs at the boundary of every
  non-Checkrd sink (`OtlpSink`, `ConsoleSink`, `JsonFileSink`).
  Recursively redacts `authorization` / `api_key` / `token`-shaped
  keys; scrubs URL query params on known URL fields.
- **`ControlReceiver.readTimeoutMs`** default raised from `0` (no
  timeout) to `120_000` (120s) — above typical LB idle timeouts
  (ALB 60s / Cloudflare 100s / nginx 60s) so heartbeats don't
  trigger spurious reconnects. Set to `0` explicitly to opt out.
- **`publint --strict` + `attw --pack`** wired into `npm run ci`.
  Catches exports-map regressions and type-resolution issues
  before publish. Matches the TanStack Query / Stainless CI bar.
- **Size-limit budget** (`npm run size`). Main entry capped at
  60 KB gzipped (currently 51 KB); per-integration subpath at 8 KB
  gzipped (currently ~4.4 KB). Fails CI if either exceeds budget.
- **Streaming-response regression tests** — 5 tests covering byte
  fidelity, chunked delivery, clean early-close, pre-stream deny,
  and Content-Type preservation. Exercises `wrapFetch` against
  synthetic `ReadableStream` bodies.

### Changed

- Unused `dangerouslyAllowBrowser: true` defaults removed from
  `_cloudflare.ts` and `_next.ts` — the browser guard now
  correctly recognizes those runtimes as server-side so the
  defensive override is no longer needed.
- `defaultControlHeaders()` is the single source of truth for the
  base header set on every control-plane request. Batcher and
  key registrar now share it; receiver uses the same helper for
  its GET variant.
- `TelemetryBatcher` no longer manually assembles headers —
  delegates entirely to `defaultControlHeaders(this.apiKey, {
  apiVersion: this.apiVersion })`.

### Security

- Closed the WASM-integrity-skip-in-prod footgun (see above).
- Closed the OTLP-sink-leaks-headers footgun — `scrubTelemetryEvent`
  now runs at the sink boundary.
- Tightened browser-use detection to require all three DOM
  signals, removing false positives from Deno/Bun/Workers that
  were forcing operators to sprinkle `dangerouslyAllowBrowser:
  true` defensively.

## [Unreleased]

### Added

- **Full error hierarchy** modeled after the OpenAI and Anthropic
  SDKs. New base `CheckrdError`, new control-plane `CheckrdAPIError`
  with subclasses `CheckrdBadRequestError` (400),
  `CheckrdAuthenticationError` (401),
  `CheckrdPermissionDeniedError` (403), `CheckrdNotFoundError`
  (404), `CheckrdConflictError` (409),
  `CheckrdUnprocessableEntityError` (422),
  `CheckrdRateLimitError` (429), `CheckrdInternalServerError`
  (>=500), `CheckrdConnectionError`,
  `CheckrdConnectionTimeoutError`, and `CheckrdUserAbortError`.
  Dispatch table helper `makeAPIError()` maps
  `{ status, body, headers }` → subclass. Pre-existing
  `CheckrdInitError`, `CheckrdPolicyDenied`, and
  `PolicySignatureError` now all extend `CheckrdError`.
- **Structured logger injection**. New `logger` and `logLevel`
  options on `init()` / `wrap()` accept any pino / winston /
  bunyan / console-shaped logger. New helpers:
  `createConsoleLogger`, `noopLogger`, `redactSensitive`,
  `wrapWithRedaction`. Every logged payload is run through a
  sensitive-data redaction pass (`Authorization`,
  `X-API-Key`, `Anthropic-API-Key`, `OpenAI-API-Key`, and common
  secret key names — `apiKey`, `api_key`, `token`, `password`,
  `bearer`, `privateKey`, etc.). `CHECKRD_LOG_LEVEL` env override
  added.
- **Retry + idempotency primitives** for control-plane calls.
  `fetchWithRetry()` implements the OpenAI/Anthropic exponential-
  backoff formula (`0.5 * 2^retries` seconds, 25% down-jitter,
  max 8s ceiling). Honors `Retry-After-Ms` (milliseconds) and
  `Retry-After` (seconds or HTTP-date). Auto-generates
  `Idempotency-Key` headers via `newIdempotencyKey()`.
  `defaultControlHeaders()` helper stamps `Content-Type`,
  `X-API-Key`, and `Idempotency-Key`.
- **Telemetry batcher + HTTP ingestion**. New `TelemetryBatcher`
  class with background flushing (100 events / 5 seconds),
  back-pressure counters (`sent`, `droppedBackpressure`,
  `droppedSendError`, `pending`), RFC 9421 + RFC 9530 + DSSE
  signing via the WASM core, and bounded graceful shutdown. Runs
  automatically when `init()` is called with both `apiKey` and
  `controlPlaneUrl`.
- **Pluggable telemetry sinks**. New `TelemetrySink` interface.
  Ships `ConsoleSink`, `JsonFileSink` (Node only, lazy `node:fs`
  import so the file is absent from edge-runtime bundles),
  `ControlPlaneSink`, and `CompositeSink` for fan-out.
- **SSE control receiver**. New `ControlReceiver` class subscribes
  to `GET /v1/agents/{id}/control` using `fetch` +
  `ReadableStream.tee()` and a streaming `parseSSE()` iterable.
  Reconnects with exponential backoff (1 s → 60 s). Polls
  `GET /v1/agents/{id}/control/state` while waiting for the next
  attempt so the kill switch is still applied if the SSE stream
  stalls.
- **Graceful shutdown registry**. New `registerDisposable()` +
  `shutdownAll()`. `init()` registers `SIGTERM`, `SIGINT`, and
  `beforeExit` handlers (Node only) so containerized deployments
  flush pending telemetry before the process exits. `shutdown()`
  is now awaitable.
- **Stream token capture** for OpenAI and Anthropic streaming
  responses. `teeResponseForTokens()` clones the response body with
  `ReadableStream.tee()`; one half feeds the consumer, the other
  is parsed by `captureStreamTokens()` to extract
  `input_tokens`, `output_tokens`, and `finish_reason`. Works for
  both `text/event-stream` paths (OpenAI `data: [DONE]`, Anthropic
  `message_start` / `message_delta` / `message_stop`).
- **Vercel AI SDK middleware**. New `checkrd/ai-sdk` subpath
  export `checkrdMiddleware()` compatible with `wrapLanguageModel`
  from the `ai` package. Evaluates policy in `wrapGenerate` and
  `wrapStream`; emits a `ai_sdk_completion` telemetry event with
  per-call token counts. Structural typing keeps the middleware
  compatible across AI SDK v4, v5, and v7-beta.
- **Five additional vendor instrumentors**. `CohereInstrumentor`,
  `GroqInstrumentor`, `MistralInstrumentor`, `TogetherInstrumentor`,
  `GoogleGenAIInstrumentor`. Total vendor coverage now 7, matching
  the Python SDK. Each is exposed as its own subpath export
  (`checkrd/cohere`, `checkrd/groq`, `checkrd/mistral`,
  `checkrd/together`, `checkrd/google-genai`) for tree-shaking.
- **Documentation**. `SECURITY.md`, `THREAT-MODEL.md`,
  `WASM-CORE.md`, `LICENSE`, `CHANGELOG.md`, and `CONTRIBUTING.md`
  at the package root. `README.md` rewritten to match the
  Stripe/OpenAI/Anthropic section structure with a complete error
  hierarchy table, request-ID guidance, retry policy, deployment-
  mode guide, and subpath-export reference.

### Changed

- `init()` now returns `void` and registers process-lifecycle
  handlers. `shutdown()` is now `async` and flushes the batcher
  before resolving.
- `healthy()` output includes new top-level `control_plane_connected`,
  `telemetry` (batcher counters), and `receiver` (SSE receiver
  state) fields.
- Sensitive-header list in `src/transports/fetch.ts` now includes
  `Anthropic-API-Key` and `OpenAI-API-Key` in addition to the
  prior set.
- Coverage threshold enforced at 80% (lines, statements,
  functions) / 75% (branches) in `vitest.config.ts`.

### Fixed

- Two `@typescript-eslint/no-confusing-void-expression` errors in
  `src/control.ts` that were blocking CI.
- Three `no-unsafe-*` test-file lint errors in
  `tests/control.test.ts`.

## [0.2.0] — 2026-04-17

### Added

- WASM core integration with SHA-256 integrity verification at
  engine construction time.
- `wrap()` and `wrapFetch()` — Checkrd-enforced `fetch` wrapping
  for per-client integration.
- `init()` / `instrument()` / `instrumentOpenAI()` /
  `instrumentAnthropic()` for global instrumentation.
- Policy loading from YAML, JSON, file path, or Python-style
  inline object.
- Kill-switch support via the WASM `set_kill_switch` FFI export.
- Dual ESM + CJS build via `tsup` with generated `.d.ts` /
  `.d.cts` declarations.
- Subpath exports `checkrd/openai` and `checkrd/anthropic` for
  tree-shaking.
- `publint` and `arethetypeswrong` checks in CI.
- `CheckrdInitError`, `CheckrdPolicyDenied`, `PolicySignatureError`
  error classes.
- Property-based FFI tests via `fast-check`.
