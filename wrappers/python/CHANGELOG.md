# Changelog

All notable changes to the Checkrd Python SDK will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.3.1 (2026-05-02)

### Changed

- Republished from the public `checkrd-io/checkrd-sdk` mirror. The
  Sigstore certificate identity in the PEP 740 attestation now points
  at `https://github.com/checkrd-io/checkrd-sdk/.github/workflows/publish-python.yml`
  — a repo end users can browse — instead of the private monorepo.
  No SDK behaviour changes vs 0.3.0; verification recipes in
  `WASM-CORE.md § Integrity Verification` now use this repo URL.

## 0.1.0 (2026-04-27)


### Features

* dedicated migration RunTask, rate limits 2500/s, deploy pipeline fix ([7848093](https://github.com/checkrd-io/checkrd/commit/7848093b2e5f42a9eaa72f867a8162548e6805de))
* industry-standard alignment across full stack ([95d7358](https://github.com/checkrd-io/checkrd/commit/95d7358b1f68631f42cfa6eaca9a4e164276aee4))
* org-wide alert history + bundled WIP cleanup ([a0b349a](https://github.com/checkrd-io/checkrd/commit/a0b349a5d33045364ad4ee1db3689b7011a23afc))
* **python-sdk:** Phase 0 fail-closed hardening (security_mode + body-size deny) ([514a33e](https://github.com/checkrd-io/checkrd/commit/514a33eb4e818fcb274ca140c2dcd10b4f5bc407))
* **telemetry:** persist evaluation_path + matched_rule + policy_mode ([9c67a1b](https://github.com/checkrd-io/checkrd/commit/9c67a1bc1f2a91ccf623b4dff6cc7b9eb3cc5d0d))


### Bug Fixes

* **ci:** unblock deploy pipeline — python lint/types + dashboard lint/test ([f144bce](https://github.com/checkrd-io/checkrd/commit/f144bcea4734d6d6cdf3d717460c7da63ee9633a))
* flaky pk registration test + revert telemetry rate limit exemption ([5cbd702](https://github.com/checkrd-io/checkrd/commit/5cbd7026c6728a08a72239c1bc703201f1731aac))
* remove unused OtlpSink import + had_wasmtime variable (ruff F401) ([e9386d4](https://github.com/checkrd-io/checkrd/commit/e9386d4c0c3e24b9402042cb28c46ea20470d407))
* remove unused sys import (cascading from had_wasmtime removal) ([9d1f060](https://github.com/checkrd-io/checkrd/commit/9d1f0607d1d12de3693c1c2239fd599979ffa2d1))
* residual CI failures on PR [#8](https://github.com/checkrd-io/checkrd/issues/8) ([b10b890](https://github.com/checkrd-io/checkrd/commit/b10b890c3a6f93122d0176f28ccb6210f0ed2ed8))

## [Unreleased]

### Added

- **Four agent-framework adapters** (`checkrd.integrations.langchain`,
  `checkrd.integrations.openai_agents`,
  `checkrd.integrations.claude_agent_sdk`,
  `checkrd.integrations.mcp`). Each subclasses or extends the
  framework's documented public extension point — no monkey-patching:
  - LangChain / LangGraph: `CheckrdCallbackHandler` (subclass of
    `BaseCallbackHandler`). Hooks every LLM call, tool call,
    retriever call, and chain invocation. Async chains supported via
    LangChain's automatic dispatcher.
  - OpenAI Agents SDK: `CheckrdTracingProcessor` (observation),
    `CheckrdInputGuardrail` / `CheckrdOutputGuardrail` (enforcement
    via tripwire). Mirrors the SDK's intentional split between
    tracing and guardrails.
  - Anthropic Claude Agent SDK: `attach_to_options()` plus
    `make_pre_tool_use_hook()` / `make_post_tool_use_hook()` /
    `make_user_prompt_submit_hook()` / `make_stop_hook()` factories.
    Idempotent across repeated calls.
  - MCP: `wrap_call_tool_handler()` for server-side enforcement,
    `CheckrdClientSession` (subclass of `mcp.client.session.ClientSession`)
    for client-side enforcement.
- **Optional dependency extras** `[langchain]`, `[openai-agents]`,
  `[claude-agent-sdk]`, `[mcp]` plus an `[all]` umbrella that
  installs every framework peer at once.
- **Hook callback signatures use the SDK's own typed input
  TypedDicts** (`PreToolUseHookInput`, `PostToolUseHookInput`,
  `UserPromptSubmitHookInput`, `StopHookInput`) and return
  `HookJSONOutput`, with a `cast` to the SDK's `HookCallback` union
  at the boundary. Type-safe end to end.
- **Smoke tests** at `tests/integrations/test_frameworks.py`. Each
  adapter exercised in allow / deny / observation-mode against
  `MockEngine` (no WASM dependency); gated on framework presence
  via `pytest.importorskip` so contributors who don't install all
  peers still see green tests.

## [0.3.0] — 2026-04-24

### Added

- **`Checkrd` / `AsyncCheckrd` unified client class.** One object per
  process, OpenAI-SDK-shaped constructor (`api_key=`, `base_url=`,
  `agent_id=`, env-var fallbacks), `.wrap()` / `.with_options()` /
  `.instrument_openai()` / `.healthy()` / `.close()` / context-manager
  methods. Top-level `wrap()` / `wrap_async()` / `init()` /
  `instrument_*()` remain for backwards compatibility — the class
  delegates to them. Tutorials now lead with the class.
- **`X-Checkrd-SDK-*` platform headers** stamped on every
  control-plane request. Six headers (`Lang`, `Version`, `Runtime`,
  `Runtime-Version`, `OS`, `Arch`) memoized once per process. Mirrors
  the Stainless `X-Stainless-*` family shipped by OpenAI / Anthropic
  SDKs.
- **`Checkrd-Version` date-pinned API version** (Stripe pattern). Set
  via `api_version=` constructor arg or `CHECKRD_API_VERSION` env.
  Empty means "follow server default"; any non-empty value is stamped
  on every control-plane request.
- **`OTelSpanSink`** — creates real OpenTelemetry spans on the
  caller's existing tracer. Follows OTel HTTP + GenAI semconv
  (`http.request.method`, `url.full`, `gen_ai.system`,
  `gen_ai.usage.*`) plus the `checkrd.*` namespace for SDK-specific
  fields. Optional dep on `opentelemetry-api`.
- **`TelemetryBatcher.on_drop(reason, count)` callback** with
  separate counters for `"backpressure"`, `"signing_error"`, and
  `"send_error"` drops. Operators can page on sustained signing
  errors (config issue) without the noise of transient send failures.
- **Production guard for `CHECKRD_SKIP_WASM_INTEGRITY`.** Eleven
  framework-standard env signals checked against four production
  values; bypass refused in prod unless the exact phrase
  `CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK=i-understand-the-risk`
  is set.
- **PII-risk stderr banner** when `CHECKRD_DEBUG=1` or `debug=True`
  is observed. One-time-per-process, idempotent, warns about prompt
  payloads in debug logs.
- **`docs_url` property on `CheckrdInitError` and
  `CheckrdPolicyDenied`** — deep link to
  `https://checkrd.io/errors/{code}`. Included in the ASGI / WSGI
  403 JSON envelopes so frontend teams can one-click from a paged
  alert to the remediation page.
- **Streaming-response regression tests** — 7 tests covering sync +
  async httpx streaming through wrapped clients. Verifies byte
  fidelity, line-by-line iteration, clean early-close, and policy
  deny BEFORE the upstream request is made.
- **`OnDropCallback` / `DropReason` types** exported at the package
  root for typed callback authoring.
- **`OtlpSink` + `OTelSpanSink` shared `_apply_semconv_attributes()`**
  helper so both sinks emit identical attribute shapes — prevents
  dashboard drift across the two sink choices.

### Changed

- `TelemetryBatcher.diagnostics()` now returns 5 keys instead of 4 —
  `dropped_signing_error` is broken out separately. Callers using
  `diagnostics()["dropped_send_error"]` as a combined "failed to
  deliver" count should sum the two.
- `CheckrdPolicyDenied` error message now includes a
  ``Docs: https://checkrd.io/errors/{code}`` line.
- `/v1/agents/{id}/public-key` registration, SSE subscribe, state
  poll, and telemetry POST now all share a single
  `default_control_headers()` helper. Operator-facing metadata is
  identical across every control-plane request.

### Security

- **Closed the PII-leak-via-debug-logs footgun.** Debug logging
  now fires a loud stderr banner pointing at the specific risk
  (prompt content in logs) and the mitigation (turn off for prod).
- **Closed the WASM-integrity-skip-in-prod footgun.** Bypass is
  rejected when any of 11 framework env vars flag a production-like
  deploy.
- **Split `signing_error` from `send_error`** so operators can
  alert on config-error conditions without false positives from
  transient network issues.

### Removed

- **Python 3.9 support.** `requires-python` is now `>=3.10`. Python
  3.9 reached end-of-life in October 2025, and the four
  agent-framework adapters added in this release transitively
  depend on libraries (`mcp`, `claude-agent-sdk`, `openai-agents`,
  `langchain-core`) that themselves require 3.10+ syntax. Users on
  3.9 should pin to `checkrd<0.3.0` until they can upgrade.

## [Unreleased]

### Added
- **`security_mode` parameter** (`init()`, `wrap()`, `wrap_async()`). Default
  `"strict"` fails closed on WASM engine init failure — raises
  `CheckrdInitError` instead of silently degrading to pass-through.
  `"permissive"` preserves the pre-0.2 fail-open behavior for gradual
  rollouts. Env-var override: `CHECKRD_SECURITY_MODE`.
- **Fail-closed body-size handling**. Requests with bodies above the
  1 MB WASM inspection limit are now denied (strict mode) with
  `reason="body exceeds 1MB inspection limit"`. Previously these
  requests proceeded with body matchers silently skipped — allowing
  trivial body-matcher bypass by padding the payload.
- `SECURITY.md` with a coordinated-disclosure process, fix-window
  commitments, and supply-chain documentation.
- `[project.urls]` in `pyproject.toml` — Homepage, Documentation,
  Changelog, Issues, Source, Security Policy.
- **Telemetry signing** (continued from prior `[Unreleased]`). When
  `wrap()` / `wrap_async()` is called with both `control_plane_url` and
  `api_key`, every telemetry batch is signed via the WASM core's
  `sign_telemetry_batch` FFI export. Outgoing requests carry:
  - `Signature-Input` / `Signature`
    ([RFC 9421](https://www.rfc-editor.org/rfc/rfc9421.html)) over
    method, target URI, body digest, and signing agent ID, algorithm
    `ed25519`, `created`/`expires`-gated.
  - `Content-Digest`
    ([RFC 9530](https://www.rfc-editor.org/rfc/rfc9530.html)) binding
    the exact body bytes.
  - `X-Checkrd-Signer-Agent` for ingestion-side pubkey lookup.
  Anonymous identities (KMS / external signer) fall back to unsigned
  with a one-shot warning.
- Cross-implementation interop test proving the wrapper's signing path
  is byte-for-byte compatible with any RFC 9421 / RFC 8032 conformant
  implementation (verified by PyCA `cryptography`).

### Changed
- **Classifier bumped from Alpha to Beta** (`Development Status :: 4`).
- **`CHECKRD_DEV` split into two orthogonal env vars**:
  - `CHECKRD_ALLOW_INSECURE_HTTP=1` — allows `http://` control-plane
    URLs (local dev).
  - `CHECKRD_SKIP_WASM_INTEGRITY=1` — skips the WASM SHA-256 check
    (source-checkout dev only).

  Previously a single `CHECKRD_DEV=1` flag bundled both controls, so a
  single leaked env var disabled two independent security measures.

### Fixed
- **`secrets.randbelow(0)` crash in telemetry-batcher backoff**. When
  the server returned `Retry-After: 0` on a retryable response, the
  exponential backoff would compute `base_delay * 500 == 0` on the next
  iteration and `randbelow(0)` raised `ValueError`, killing the batcher
  thread. Now floored to a minimum of 1.

### Deprecated
- `CHECKRD_DEV` env var. Emits `DeprecationWarning` when used; will be
  removed in 1.0. See `SECURITY.md` for the replacement flags.

## [0.1.0] - 2026-03-29

### Added
- `wrap()` and `wrap_async()` to wrap httpx sync and async clients
- WASM-based policy evaluation engine (Rust core compiled to WebAssembly)
- Policy enforcement: URL pattern matching, method filtering, body field inspection, time-of-day restrictions, rate limiting
- Kill switch support via WASM engine
- Policy hot-reload without reinitialization
- Configuration loading from YAML files, JSON files, or Python dicts
- `CHECKRD_DISABLED=1` environment variable to bypass all evaluation
- Telemetry event logging via Python `logging` module
- `CheckrdPolicyDenied` exception with structured `reason` and `request_id` attributes
- Body size limit (1MB) to prevent memory exhaustion
- SDK version in `User-Agent` header for incident tracing
