# Threat Model

This document is the threat model for the Checkrd SDK (Python and
JavaScript wrappers) and the embedded `checkrd_core.wasm` binary. It
follows the four-question OWASP frame:

1. What are we working on?
2. What can go wrong?
3. What are we going to do about it?
4. Did we do a good job?

The document is reviewed on every major release and after every
advisory. Last reviewed: release cut of the current version (see
[CHANGELOG.md](./CHANGELOG.md)).

## 1. What are we working on

The Checkrd SDK intercepts outbound HTTP requests from AI agents,
evaluates each against a signed policy inside a WebAssembly sandbox,
optionally signs and ships telemetry to a control plane, and
optionally receives kill-switch and policy-update events via an SSE
stream.

### Assets

| Asset | Criticality | Location |
| --- | --- | --- |
| Active policy (bytes loaded into WASM) | High | WASM linear memory per `WasmEngine` instance |
| Ed25519 signing private key | Critical | WASM linear memory; zeroized on drop |
| Rate-limit counters | Medium | WASM linear memory |
| Kill-switch state | High | WASM linear memory; one flag per instance |
| Monotonic policy-version counter | High | WASM linear memory; optionally persisted by host |
| `checkrd_core.wasm` binary integrity | Critical | SDK package; SHA-256-pinned |
| Control-plane API key | High | Host process memory; env var |
| Signed telemetry batches in flight | Medium | Host network stack → TLS → control plane |
| Customer PII in request bodies | Critical | Never retained; inspected inside WASM; up to 1 MB per request |

### Actors

| Actor | Trust | Capability |
| --- | --- | --- |
| Agent code (host) | Partially trusted | Can construct engines, call FFI exports, read host memory |
| Policy author (via control plane) | Trusted | Can author, sign, and publish policy bundles |
| Control plane (remote) | Trusted | Signs policy bundles, receives telemetry, sends SSE events |
| Network counterparty (external API) | Untrusted | Receives requests; can respond with adversarial content |
| Local attacker (same host) | Untrusted | Can observe caches, syscalls, and process memory if OS-level isolation is broken |
| Supply-chain attacker | Untrusted | Can attempt to tamper with the published `.wasm`, the SDK package, or upstream crates |

## 2. What can go wrong / 3. What we do about it

The following table enumerates the in-scope threats and the
mitigations in the current release. Threats outside this table are
either covered in the [Out of Scope](#out-of-scope) list or tracked
as residual risks.

| # | Asset | Threat | Mitigation |
| --- | --- | --- | --- |
| 1 | Policy bytes | Adversary substitutes a permissive policy via a forged bundle | DSSE envelope verification in `reload_policy_signed`; trust-list keyid check; error `-5 SIGNATURE_INVALID` / `-6 NO_TRUSTED_SIGNER` |
| 2 | Policy bytes | Adversary replays an older (more permissive) bundle | Monotonic `policy_version` enforced in WASM; error `-11 VERSION_NOT_MONOTONIC`; optional host-side persistence via `set_initial_policy_version` |
| 3 | Policy bytes | Adversary submits a stale bundle that was valid but has expired | `max_age_secs` check against caller-supplied `now_unix_secs`; error `-12 BUNDLE_TOO_OLD` |
| 4 | Policy bytes | Adversary submits a bundle with a future timestamp to extend its validity | Reject bundles with `issued_at > now + tolerance`; error `-13 BUNDLE_IN_FUTURE` |
| 5 | Private signing key | Attacker reads key from host memory | Key lives only in WASM linear memory after `init()`; host copy is zeroed by `bytearray.clear()` (Python) / explicit buffer fill (JS) on the way in |
| 6 | Private signing key | Attacker reads key from core dump | `zeroize::ZeroizeOnDrop` on the Rust side; hosts should disable core dumps or run under a seccomp profile |
| 7 | FFI buffers | Adversarial input corrupts WASM memory | WASM linear memory bounds checking (WebAssembly spec); length-prefixed `(ptr, len)` ABI; oversize buffers are rejected before dispatch |
| 8 | FFI buffers | Host bugs cause double-free or use-after-free | Documented single-`alloc`/single-`dealloc` contract; Python engine holds a `threading.Lock` across every FFI call; JS engine is serialized by the event loop |
| 9 | Signed telemetry | Replay of a captured batch | `created` + `expires` parameters; ingestion rejects outside the 5-minute window; per-batch `nonce` |
| 10 | Signed telemetry | Forgery without the key | Ed25519 verification at the control plane; Wycheproof + RFC 8032 vectors gate every release |
| 11 | Kill switch | Adversary prevents a legitimate kill-switch event from reaching the SDK | Polling fallback (`/v1/agents/.../control/state`) runs on SSE disconnect; `killswitch_file` support for air-gapped file-based activation |
| 12 | Kill switch | Adversary triggers a spurious kill-switch deny | Kill-switch events arrive only via authenticated SSE to the SDK's own agent-id; file-based kill switch is local-only |
| 13 | Rate-limit state | Adversary exhausts the 10K-key cap to force eviction of legitimate keys | Two-phase LRU eviction guarantees furthest-past key evicted first; rate-limit scope (`global` / `endpoint` / `body_field`) isolates namespaces |
| 14 | `checkrd_core.wasm` | Supply-chain tampering of the published binary | SHA-256 integrity verified on every engine construction; Sigstore-signed attestations; `cosign verify-blob-attestation` recipe published in [WASM-CORE.md](./WASM-CORE.md#integrity-verification) |
| 15 | `checkrd_core.wasm` | Upstream Rust crate compromise | `Cargo.lock` committed; `cargo audit` in CI with explicit allow-list (`audit.toml`); `cargo-deny` license and source checks |
| 16 | Transport layer | API key leaks into logs | `SensitiveHeadersFilter` redacts `Authorization`, `X-API-Key`, and vendor-specific headers before any log handler sees them; debug logs rate-limited to 1/60s per call site |
| 17 | Transport layer | API key exfiltrated via forced `http://` downgrade | SDK rejects `http://` control-plane URLs unless `CHECKRD_ALLOW_INSECURE_HTTP=1` is set (dev only, emits a warning on every engine construction) |
| 18 | Evaluation timing | Adversary infers policy contents via regex-matcher timing | All regex matchers use the linear-time `regex` crate mode; no backtracking engines are linked |
| 19 | Body inspection | 1 MB body limit bypass via payload padding | Hosts in `security_mode="strict"` deny requests whose body exceeds the inspection limit rather than silently skipping body matchers |

### Mitigation validation

Every mitigation in the table above is exercised by at least one
automated test. Cross-references:

- Bundle verification and rollback: `tests/test_policy_signing.py`,
  `crates/core/tests/signed_bundle.rs`.
- FFI buffer safety: `tests/test_engine.py`,
  `tests/ffi-properties.test.ts` (fast-check property tests).
- Telemetry signing cross-interop: `tests/test_batcher.py`
  (Python-signed batch verified by PyCA `cryptography`).
- Rate-limit eviction: `crates/core/tests/rate_limit.rs`.
- Header redaction: `tests/test_logging.py`.
- `http://` rejection: `tests/test_settings.py`.

## Out of Scope

The following threats are explicitly not mitigated by the Checkrd
SDK and must be addressed by the host environment:

- **Host process compromise.** If an attacker can execute arbitrary
  code in the host Python or Node process, they can read the private
  key out of WASM linear memory before `init()`'s zeroize, tamper
  with the evaluate-and-enforce control flow, or disable the kill
  switch. Defense here is host hardening: seccomp, AppArmor,
  namespaced containers, disabled core dumps.
- **OS kernel compromise.** Rowhammer, speculative-execution
  side channels that break process isolation, physical DMA attacks.
- **Compiler or toolchain compromise.** A compromised `rustc`,
  `wasm-opt`, or `cargo` could emit a malicious `.wasm`. This is
  addressed upstream by the Rust project's reproducible-builds
  effort; Checkrd's own mitigation is SLSA-aligned build provenance
  (see [WASM-CORE.md § Build Provenance](./WASM-CORE.md#build-provenance)).
- **Physical access.** Disk imaging, cold-boot, JTAG. Out of scope;
  deploy in appropriately hardened hardware.
- **Control-plane compromise.** If Checkrd's own control plane is
  breached, an attacker can issue valid-looking signed bundles. The
  SDK trust-list restricts which keys can sign bundles; rotation
  and revocation is a control-plane concern documented separately.
- **Denial of service by the host.** A malicious host can feed a
  degenerate policy (millions of rules, pathological regex) to
  `init()`. Hosts are trusted not to attack themselves; the
  evaluation-time caps in [Resource Limits](./WASM-CORE.md#resource-limits)
  bound the cost of each individual request but not the cost of
  constructing an engine.
- **Side-channel timing leaks from the WASM runtime.** Documented in
  [WASM-CORE.md § Side-Channel Posture](./WASM-CORE.md#side-channel-posture).
  Customers whose threat model requires bit-level timing resistance
  should deploy the signing root in a dedicated enclave.
- **Customer-authored policy logic errors.** A policy that contains
  a `default: allow` rule without specific denies is a policy
  decision, not a Checkrd vulnerability. The dashboard's policy
  analyzer flags over-permissive patterns; enforcement is the
  author's responsibility.

## Residual risk

The following risks remain accepted in the current release:

1. **No external cryptographic audit has been commissioned.** The
   first independent review is scheduled after the 1.0 release and
   will be published at `https://checkrd.io/security/audits`.
2. **No FIPS 140-3 validation.** Customers with FIPS requirements
   should contact `security@checkrd.io` for the validated-module
   roadmap.
3. **Side-channel timing is best-effort.** Constant-time guarantees
   are reintroduced by WebAssembly JITs and host CPU
   microarchitecture; see
   [WASM-CORE.md § Side-Channel Posture](./WASM-CORE.md#side-channel-posture).
4. **Reproducible builds are not independently verified.** Target
   for 1.0.
5. **Unsigned `reload_policy` can be invoked by any host code** with
   access to the `WasmEngine`. This is intentional for file-based
   Tier 3 deployments; hosts that do not use file-based policy
   should not expose `WasmEngine` outside their own call graph.

## Hardening additions (0.3.0)

The 0.3.0 release tightened several failure modes that were
technically out-of-scope of the original threat model but were
obvious footguns operators would hit in practice.

### WASM integrity bypass in production

The `CHECKRD_SKIP_WASM_INTEGRITY=1` bypass flag is a legitimate
dev-time tool — source-checkout contributors whose
`_wasm_integrity.ts` hasn't been regenerated need a way in. But
a dev env var silently leaking into a production deploy is
exactly the class of mistake that disables supply-chain
verification without anyone noticing.

**Mitigation.** Eleven common environment signals are checked
(`NODE_ENV`, `ENVIRONMENT`, `APP_ENV`, `RAILS_ENV`, `DJANGO_ENV`,
`PYTHON_ENV`, `DEPLOYMENT_ENVIRONMENT`, etc.) against four
production values (`production`, `prod`, `canary`, `live`). If
any is set AND the skip flag is set, the SDK refuses to load
unless the operator types the exact break-glass phrase
`CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK=i-understand-the-risk`.
The phrase is a deliberate muscle-memory barrier, not a
configuration knob. See `tests/wasm_integrity_guard.test.ts`.

### Private key material in memory

The Ed25519 agent private key is the crown jewel: a leaked copy
lets anyone forge telemetry batches from the agent, poisoning
downstream policy decisions and audit trails.

**Boundary.** The key lives in WASM linear memory once the
engine is initialized. JavaScript does not have a true
`SecureBuffer` — the `Uint8Array` the host passes to
`WasmEngine` is zero-filled on our side after the handoff, but
the original bytes may linger until garbage collection. For
production deployments, use `ExternalIdentity` with a KMS
backend so the private key never enters the JS heap. Documented
at https://checkrd.io/docs/identity.

### Real-browser use

Shipping the agent signing key to every end user viewing a
browser bundle is NOT equivalent to shipping an OpenAI API key
(which is a billing/abuse problem). A forged telemetry batch
signed with the leaked key can poison dashboards and audit
trails downstream.

**Mitigation.** The browser guard was tightened from a
too-broad `!process.versions.node` heuristic to a
real-browser detection (`window` + `document` +
`navigator.userAgent` present, NO Deno/Bun/WorkerGlobalScope/
EdgeRuntime markers). Even with the `dangerouslyAllowBrowser`
opt-in, a loud one-time stderr banner fires naming the
forged-telemetry attack specifically. Test coverage in
`tests/browser_guard.test.ts` exercises the full runtime
matrix via injectable globals.

### Debug-logging PII exposure

Checkrd sits in the request path for LLM agent traffic. Debug
logs here can contain prompt payloads — customer data operators
rarely expect in their log aggregator.

**Mitigation.** When `CHECKRD_DEBUG=1` or `debug: true` is
observed, a one-time stderr banner fires before the
`CHECKRD_DISABLED` short-circuit, warning about prompt content
in logs. Banner is idempotent and falls back to `console.warn`
on runtimes without `process.stderr`
(Cloudflare Workers, Vercel Edge, browsers).

### Telemetry sink scrubbing

Before 0.3.0, the OTLP and Console sinks would forward the raw
event object to a third-party collector without scrubbing.
Operators pointing OTLP at an untrusted endpoint (or printing
events to stdout in a CI environment) could leak `Authorization`
headers embedded in event custom attributes.

**Mitigation.** `scrubTelemetryEvent()` runs at the boundary of
every non-Checkrd sink (`OtlpSink`, `ConsoleSink`,
`JsonFileSink`). Recursively redacts sensitive keys
(`api_key`, `authorization`, `token`, etc.) and scrubs URL
query strings containing secret-named parameters. The
Checkrd-owned `ControlPlaneSink` is NOT scrubbed — it signs
over canonical bytes and scrubbing would invalidate the
signature; the control plane is the intended recipient.

### Control-plane version pinning

Server-side API changes can silently break long-running
deployments — a 409 or 422 from a drifted ingestion endpoint
is hard to distinguish from a transient failure.

**Mitigation.** The `Checkrd-Version` date header (Stripe
pattern) is now stamped on every control-plane request —
telemetry POST, key registration POST, SSE subscribe GET, and
state-poll GET. Customers pin via `apiVersion:` or
`CHECKRD_API_VERSION`. Empty means "follow server default".

### SDK version / platform observability

Before 0.3.0, operators had no way to see "what fraction of our
fleet is still on SDK <0.3.0" or "are Cloudflare Workers
callers seeing a different error rate." A compromised
transitive dep that flipped the SDK identifier to a wrong
value would have been invisible.

**Mitigation.** `X-Checkrd-SDK-Lang` / `-Version` / `-Runtime`
/ `-Runtime-Version` / `-OS` / `-Arch` are stamped on every
control-plane request. The Stainless `X-Stainless-*` family is
the industry reference.

## 4. Did we do a good job

Evidence that the mitigations work:

- **Mutation-testing** on the cryptographic verification primitives
  reports 100% kill rate (`cargo-mutants`).
- **Property-based tests** on the FFI JSON-envelope parser generate
  ~100 adversarial inputs per property on every run
  (`tests/ffi-properties.test.ts`).
- **Cross-implementation interop** verifies the wrapper's signing
  path against an independent Ed25519 implementation (PyCA
  `cryptography`). A failure here would indicate our core is
  signing in a non-standard way.
- **Fuzzing** runs on every PR touching `crates/core` plus a
  scheduled nightly job; cumulative CPU-hours are reported in the
  quarterly security summary.
- **Continuous `cargo audit`** with a small, justified allow-list in
  `audit.toml`.
- **No outstanding high-severity issues at the time of this review.**
  Vulnerabilities are disclosed at
  `https://github.com/checkrd-io/checkrd-sdk/security/advisories`.

Review triggers (when this document is updated):

- Every major SDK release.
- Every time a new asset or actor is introduced (for example,
  adding webhook delivery or SPIFFE support).
- Every confirmed vulnerability that falls inside the existing
  asset/actor set.
- Every 6 months, whether or not the above triggers have fired.

## References

- [OWASP Threat Modeling](https://owasp.org/www-community/Threat_Modeling)
- [OWASP Application Threat Modeling Process](https://owasp.org/www-community/Threat_Modeling_Process)
- [WASM-CORE.md](./WASM-CORE.md)
- [SECURITY.md](./SECURITY.md)
- [CNCF TAG Security Self-Assessment Guide](https://github.com/cncf/tag-security/blob/main/community/assessments/guide/self-assessment.md)
