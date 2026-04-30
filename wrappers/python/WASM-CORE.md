# WASM Core

The `checkrd_core.wasm` binary shipped inside this SDK is the policy-
evaluation, rate-limiting, kill-switch, and signing engine. It is
closed-source but fully specified. This document is the input/output
contract.

The core is a pure function of its inputs. It does no I/O: no network,
no filesystem, no syscalls. The host wrapper (this Python SDK, or the
JavaScript SDK) does all I/O and passes byte buffers across the FFI
boundary.

## Contents

1. [Trust Boundary](#trust-boundary)
2. [Sandbox Model](#sandbox-model)
3. [FFI Surface](#ffi-surface)
4. [Error Codes](#error-codes)
5. [Memory Model](#memory-model)
6. [Resource Limits](#resource-limits)
7. [Cryptographic Primitives](#cryptographic-primitives)
8. [Determinism](#determinism)
9. [Side-Channel Posture](#side-channel-posture)
10. [ABI Stability](#abi-stability)
11. [Build Provenance](#build-provenance)
12. [Integrity Verification](#integrity-verification)
13. [Fuzzing](#fuzzing)
14. [Known Limitations](#known-limitations)
15. [References](#references)

## Trust Boundary

```
  ┌─────────────────────────────────────────────────────┐
  │  Host process (Python 3.9+)                         │
  │                                                     │
  │   ┌───────────────────┐     ┌───────────────────┐   │
  │   │ httpx transport    │     │ TelemetryBatcher │   │
  │   │ control receiver   │     │ sinks            │   │
  │   └────────┬───────────┘     └────────┬──────────┘   │
  │            │ FFI calls (bytes in / bytes out)       │
  │  ──────────┼────────────────────────────────────────│ ← trust boundary
  │            ▼                                        │
  │   ┌─────────────────────────────────────────────┐   │
  │   │  checkrd_core.wasm (wasmtime instance)      │   │
  │   │  ─────────────────────────────────────────  │   │
  │   │  • policy evaluation                        │   │
  │   │  • rate-limit counters                      │   │
  │   │  • kill-switch state                        │   │
  │   │  • Ed25519 signing (RFC 8032)               │   │
  │   │  • RFC 9421 / RFC 9530 / DSSE formatting    │   │
  │   │  • DSSE bundle verification                 │   │
  │   │  • linear memory (bounds-checked)           │   │
  │   └─────────────────────────────────────────────┘   │
  └─────────────────────────────────────────────────────┘
```

The host holds the signing key material until it is passed to
`init()`. After `init()` returns, the private key lives only inside
WASM linear memory and is zeroized on engine drop via
`zeroize::ZeroizeOnDrop`.

## Sandbox Model

The core runs inside a [wasmtime](https://wasmtime.dev/) `Store` /
`Instance` pair owned by the host wrapper. Per the WebAssembly
specification, the module has no raw access to system calls, no
filesystem access, and no network access. All interaction with the
outside world is done through the FFI exports listed below. Linear
memory accesses are bounds-checked at the region level; out-of-bounds
reads or writes trap the module without corrupting host memory.

Each `WasmEngine` instance is backed by a separate wasmtime Store —
rate limiters, kill switches, policy state, and signing keys are
isolated per instance. Constructing two `WasmEngine` objects yields
two independent sandboxes that cannot observe or interfere with each
other.

The Rust source is compiled to `wasm32-wasip1` with `opt-level = 3`,
`lto = "thin"`, and `panic = "abort"`. Panics inside the core abort
the WASM instance; they do not unwind into the host.

## FFI Surface

The core exports 14 `extern "C"` functions. Variable-length returns
are `u64`-packed as `(ptr << 32) | len`. `ptr = 0, len = 0` signals
"no output" (typical for anonymous signing mode or error returns).

### Memory helpers

| Export | Signature | Purpose |
| --- | --- | --- |
| `alloc` | `(len: u32) -> *mut u8` | Allocate `len` bytes in WASM linear memory. The host MUST pair each call with exactly one `dealloc(ptr, len)`. |
| `dealloc` | `(ptr: *mut u8, len: u32) -> ()` | Free a buffer previously returned by `alloc` or by a variable-length export. Calling `dealloc` twice on the same pointer, or with the wrong `len`, is undefined behavior and may trap the instance. |

### Lifecycle

| Export | Signature | Purpose |
| --- | --- | --- |
| `init` | `(policy_ptr, policy_len, agent_ptr, agent_len, key_ptr, key_len, instance_ptr, instance_len) -> i32` | Initialize the engine with a policy JSON blob, agent id, optional 32-byte Ed25519 private key, and optional 16-character instance id. Returns `0` on success or a negative error code. MUST be called exactly once per instance, before any other export except `alloc`/`dealloc` and `generate_keypair`/`derive_public_key`. |

### Evaluation

| Export | Signature | Purpose |
| --- | --- | --- |
| `evaluate_request` | `(request_ptr, request_len) -> u64` | Evaluate a JSON request envelope (method, URL, headers, body, timestamps, trace context) against the currently loaded policy. Returns a u64 packing an allocated result JSON. Caller owns the buffer and MUST `dealloc` it. |
| `set_kill_switch` | `(active: i32) -> ()` | Toggle the kill switch. `active != 0` causes all subsequent `evaluate_request` calls to return `allowed=false, deny_reason="kill switch active"`. `active == 0` restores normal evaluation. |

### Policy reload

| Export | Signature | Purpose |
| --- | --- | --- |
| `reload_policy` | `(policy_ptr, policy_len) -> i32` | Replace the active policy with new policy JSON. Resets rate-limit counters. Unsigned path — intended for file-based Tier 3 deployments where the policy source is local. Returns `0` on success. |
| `reload_policy_signed` | `(envelope_ptr, envelope_len, keys_ptr, keys_len, now_unix_secs, max_age_secs) -> i32` | Install a DSSE-signed policy bundle. Verifies the signature against the caller-provided trusted-keys JSON, rejects rollback (policy version must strictly increase), rejects bundles older than `max_age_secs` or from the future. Returns `0` on success or a negative error code (see [Error Codes](#error-codes)). |
| `get_active_policy_version` | `() -> u64` | Returns the monotonic policy version of the currently loaded signed bundle, or `0` if the current policy came from an unsigned source. |
| `set_initial_policy_version` | `(version: u64) -> i32` | One-shot restore of a persisted monotonic counter across process restarts. Can only be called before the first `reload_policy_signed`; subsequent calls return `-14` (version_already_set). |

### Signing

| Export | Signature | Purpose |
| --- | --- | --- |
| `generate_keypair` | `() -> u64` | Generate a fresh Ed25519 keypair. Returns a u64 packing a 64-byte buffer: bytes `[0..32]` are the private key, bytes `[32..64]` are the public key. Caller MUST `dealloc` after reading. Does not require `init`. |
| `derive_public_key` | `(priv_ptr, priv_len) -> u64` | Derive the 32-byte Ed25519 public key from a 32-byte private key. `priv_len` MUST be exactly `32`. Does not require `init`. |
| `sign` | `(payload_ptr, payload_len) -> u64` | Ed25519-sign an arbitrary payload with the private key passed to `init`. Returns 64 signed bytes, or `(0, 0)` in anonymous mode (engine initialized without a private key). |
| `sign_telemetry_batch` | `(batch_ptr, batch_len, uri_ptr, uri_len, signer_ptr, signer_len, nonce_ptr, nonce_len, created, expires) -> u64` | Produce an RFC 9421 + RFC 9530 + DSSE-formatted signed envelope for a telemetry batch. Returns a JSON blob containing `content_digest`, `signature_input`, `signature`, `dsse_envelope`, `instance_id`, and `expires`. Returns `(0, 0)` in anonymous mode. |

## Error Codes

All negative return values are stable. The host wrapper maps them to
exception reasons; the wire values MUST NOT change within a major
version.

| Code | Constant | Meaning | Recommended host action |
| --- | --- | --- | --- |
| `0` | `CHECKRD_OK` | Success | Continue |
| `-1` | `JSON_PARSE_ERROR` | Malformed JSON envelope | Reject caller input, log and return to caller |
| `-2` | `INVALID_UTF8` | Byte buffer is not valid UTF-8 where UTF-8 was required | Reject caller input |
| `-3` | `INVALID_KEY_LENGTH` | Key buffer was not 32 bytes | Caller programming error |
| `-4` | `PAYLOAD_TYPE_MISMATCH` | DSSE envelope payload type is not `application/vnd.checkrd.policy-bundle+json` | Reject bundle |
| `-5` | `SIGNATURE_INVALID` | Ed25519 signature verification failed | Reject bundle; alert on repeated failures |
| `-6` | `NO_TRUSTED_SIGNER` | Signing key is not on the provided trust list | Reject bundle; check trust-list drift |
| `-7` | `KEY_OUTSIDE_VALIDITY_WINDOW` | Signing key is expired or not yet valid (per `not_before` / `not_after`) | Reject bundle; rotate key |
| `-8` | `PAYLOAD_UNPARSEABLE` | DSSE payload body does not parse as a policy bundle | Reject bundle |
| `-9` | `ENGINE_NOT_INITIALIZED` | A post-init export was called before `init()` | Caller programming error |
| `-10` | `SCHEMA_VERSION_MISMATCH` | Bundle `schema_version` not recognized | Upgrade SDK; reject |
| `-11` | `VERSION_NOT_MONOTONIC` | Bundle version ≤ current active version (rollback attack) | Reject bundle; alert |
| `-12` | `BUNDLE_TOO_OLD` | `(now - bundle.issued_at) > max_age_secs` | Reject; check control-plane clock drift |
| `-13` | `BUNDLE_IN_FUTURE` | `bundle.issued_at > now + tolerance` | Reject; check host clock |
| `-14` | `VERSION_ALREADY_SET` | `set_initial_policy_version` called after an initial version was already established | Caller programming error |

## Memory Model

WASM linear memory is isolated per instance. The host reads and writes
it through the Python `wasmtime` bindings via `Memory.read(offset,
size)` and `Memory.write(offset, data)`.

Ownership rules:

1. **Inputs.** The host allocates a buffer with `alloc(len)`, writes
   data into it, and passes `(ptr, len)` to the target export. After
   the export returns, the host MUST `dealloc(ptr, len)` using the
   same `(ptr, len)` pair.
2. **Outputs.** Variable-length exports (`evaluate_request`, `sign`,
   `sign_telemetry_batch`, `generate_keypair`, `derive_public_key`)
   return `(ptr, len)` packed into `u64`. Caller owns the buffer and
   MUST `dealloc` after reading. The host MUST NOT read the buffer
   after calling `dealloc`.
3. **Zero-length.** Both `(ptr=0, len=0)` and `(ptr=X, len=0)` are
   valid "empty" returns; the host MUST NOT `dealloc` a zero-length
   buffer.
4. **Aliasing.** The same `(ptr, len)` MUST NOT be passed to two
   different exports concurrently. The JavaScript SDK serializes FFI
   calls per instance by construction (single-threaded event loop);
   the Python SDK serializes with a `threading.Lock` held across
   every FFI call.

Secrets — private keys and signed payloads — live in linear memory
for the lifetime of the instance. On instance drop, wasmtime releases
the linear memory pages; the Rust source additionally zeroizes
sensitive buffers via `zeroize::ZeroizeOnDrop` before drop. Host
callers MUST drop the engine before the surrounding process exits if
process memory may be captured (e.g., core dumps) outside their trust
boundary.

## Resource Limits

| Resource | Limit | Behavior on breach |
| --- | --- | --- |
| Request body size inspected by `evaluate_request` | 1 MB | Body matchers receive `null`; evaluation proceeds on method/URL/header matchers only. Hosts in `security_mode="strict"` SHOULD deny requests whose body exceeds this limit. |
| Rate-limit keys per policy | 10,000 | Two-phase LRU eviction. Furthest-past timestamp evicted first. |
| Policy rules per policy | Uncapped (O(n²) validation is sub-millisecond at n=200 in benchmarks) | None — invalid policies are rejected at `init` / `reload_policy` |
| Policy bundle size | 4 MB (recommended) | Larger bundles are accepted but may starve the calling thread; tune `max_age_secs` accordingly |
| Concurrent instances per host | Unbounded | Each instance allocates ~3 MB of linear memory; hosts should size accordingly |

Policy-evaluation time is bounded by rule count. Regex matchers are
compiled with the [`regex`](https://docs.rs/regex) crate's
linear-time mode (no backtracking), so adversarial inputs cannot
cause superlinear time.

## Cryptographic Primitives

Digital signatures use Ed25519 as specified in
[RFC 8032](https://www.rfc-editor.org/rfc/rfc8032.html). The
implementation is provided by
[`ed25519-dalek`](https://docs.rs/ed25519-dalek) v2.x with the
`zeroize` feature enabled. Secret scalar bit-clamping follows
RFC 8032 §5.1.5. Signature verification uses cofactor multiplication
per RFC 8032 §5.1.7.

HTTP message signing follows
[RFC 9421](https://www.rfc-editor.org/rfc/rfc9421.html) with these
signature parameters:

- `alg` — `ed25519`
- `keyid` — 16-character hex of SHA-256 of the public key
- `created` — caller-supplied Unix seconds
- `expires` — `created + signing_window` (default 300 seconds)
- covered components — `@method`, `@target-uri`, `content-digest`,
  `x-checkrd-signer-agent`

Request body binding uses Content-Digest
([RFC 9530](https://www.rfc-editor.org/rfc/rfc9530.html)) with the
`sha-256` algorithm. Digest is computed over the exact batch bytes.

Policy bundles use the DSSE envelope format
([specification](https://github.com/secure-systems-lab/dsse)) with
`payloadType = application/vnd.checkrd.policy-bundle+json` and
pre-authentication encoding (PAE) per the DSSE Protocol spec.

### Test vectors

The core passes the following cryptographic test-vector corpora on
every CI build:

- RFC 8032 §7.1 Ed25519 known-answer vectors (6/6).
- [Project Wycheproof](https://github.com/google/wycheproof)
  `eddsa_test.json` corpus (150/150 vectors, 0 failures).
- RFC 9421 §B.2.6 end-to-end worked example (byte-for-byte).
- Cross-implementation interop against PyCA
  [`cryptography`](https://cryptography.io) (signing path and
  verification path both verified).

Mutation-testing with
[`cargo-mutants`](https://github.com/sourcefrog/cargo-mutants) on
the cryptographic verification primitives reports a 100% kill rate.

### FIPS

The core is **not** FIPS 140-3 validated. `ed25519-dalek` is not a
FIPS-validated module. Customers with FIPS requirements should (a)
deploy the self-hosted control plane with an AWS KMS signer for the
root-of-trust and (b) contact `security@checkrd.io` for the
FIPS-validated-module roadmap.

## Determinism

The core is a deterministic function of its inputs with two
well-defined sources of non-determinism:

1. **Wall-clock time.** `evaluate_request` accepts a caller-supplied
   `timestamp_ms` for rate-limit window arithmetic and a `timestamp`
   (ISO 8601 string) for the emitted telemetry event. The core has no
   clock of its own.
2. **Entropy.** `generate_keypair` draws 32 bytes from the host's
   WASI `random_get` implementation (Python uses `os.urandom`, JS
   uses `globalThis.crypto.getRandomValues`). This is the only entropy
   consumed by the core; all other exports are deterministic.

Replaying the same `(policy, request, timestamp)` triple through
`evaluate_request` returns a byte-identical result. This property is
exercised by the reproducibility test in `tests/test_engine.py`.

## Side-Channel Posture

The Ed25519 implementation is designed to be constant-time on native
targets. When compiled to `wasm32-wasip1` and executed under
wasmtime, data-dependent timing may be reintroduced by:

1. The runtime's JIT code generation — wasmtime's Cranelift backend
   does not guarantee constant-time lowering of all operations.
2. The host CPU's microarchitectural side channels — caches, branch
   predictors, SMT.
3. Memory-access patterns observable through shared caches by
   same-host adversaries.

The WebAssembly specification does not currently provide constant-
time guarantees; see the
[WebAssembly constant-time proposal](https://github.com/WebAssembly/constant-time)
for the status of a future fix.

Customers whose threat model includes local attackers with cache-
timing observation capability should:

- Deploy in the self-hosted mode with a hardened wasmtime build
  (`--cranelift-flag enable_probestack=false` disabled; PKU isolation
  enabled where supported by the host kernel), or
- Deploy in air-gapped mode with the signing root moved to a
  dedicated enclave (AWS Nitro Enclaves, Intel SGX, or a separate
  HSM).

## ABI Stability

The 14 exports listed above are the stable FFI surface. Within a
major version of the core binary:

- Signatures and return types do **not** change.
- Error codes do **not** change meaning.
- New exports MAY be added; the SDKs must not depend on the absence
  of an export.
- Removed or renamed exports require a major version bump and a
  6-month deprecation window with a `[!WARNING]` in the changelog
  one minor release before removal.

The telemetry event schema and policy YAML schema carry independent
`schema_version` fields and evolve on the same tier model as the FFI
surface.

## Build Provenance

Each released `.wasm` binary is produced by a GitHub Actions workflow
running from a tagged commit. The workflow, its inputs, and its
outputs are published as a
[SLSA provenance](https://slsa.dev/spec/v1.0/provenance) attestation
via [Sigstore](https://sigstore.dev/) keyless signing. The signing
identity is the workflow path:

```
https://github.com/checkrd-io/checkrd-sdk/.github/workflows/publish-python.yml@refs/tags/v<VERSION>
```

For the JavaScript SDK the identity is the same repository with
`publish-javascript.yml`.

Checkrd targets alignment with SLSA Build Level 3. The current
attestation level is published in the
[releases page](https://github.com/checkrd-io/checkrd-sdk/releases). A full
compliance statement is not claimed.

The Rust source, `Cargo.lock`, and build configuration are committed
to the repository; the WASM build is deterministic given a pinned
Rust toolchain (`rust-toolchain.toml`). Reproducible-build
verification is out of scope for v0.x; it is on the roadmap for v1.0.

## Integrity Verification

Every published SDK wheel or npm tarball embeds `checkrd_core.wasm`
and, generated at build time, a SHA-256 digest of the bundled binary.
The host wrapper verifies the digest on every engine construction:

- Python: `src/checkrd/_wasm_integrity.py::EXPECTED_SHA256`
- JavaScript: `src/_wasm_integrity.ts::EXPECTED_SHA256`

A mismatch raises `CheckrdInitError("WASM binary integrity check
failed")`. There is a development bypass (`CHECKRD_SKIP_WASM_INTEGRITY=1`)
intended only for source-checkout workflows; setting it in production
removes the only supply-chain defense on the WASM binary and is
logged as a warning.

### Independent verification

To verify the binary against the published Sigstore attestation
without trusting the wheel's self-declared digest, use
[`cosign`](https://docs.sigstore.dev/cosign/) against the release
artifact:

```bash
# Extract the bundled WASM from the installed wheel
python -c "from importlib.resources import files; \
  import shutil; \
  src = files('checkrd') / 'checkrd_core.wasm'; \
  shutil.copy(src, 'checkrd_core.wasm')"

# Fetch the attestation bundle for the installed version
VERSION=$(python -c "import checkrd; print(checkrd.__version__)")
gh attestation download --repo checkrd/checkrd \
  --digest "sha256:$(sha256sum checkrd_core.wasm | awk '{print $1}')"

# Verify
cosign verify-blob-attestation \
  --bundle "checkrd_core.wasm.sigstore.json" \
  --new-bundle-format \
  --certificate-oidc-issuer="https://token.actions.githubusercontent.com" \
  --certificate-identity-regexp="^https://github.com/checkrd-io/checkrd-sdk/.github/workflows/publish-python.yml" \
  checkrd_core.wasm
```

Exit code `0` confirms the binary was signed by the named GitHub
Actions workflow at the expected tag. Any other output indicates a
tampered or unknown binary.

### CycloneDX SBOM

A CycloneDX SBOM is published with every release. For the Python SDK
it is attached to the GitHub Release as `sbom.cdx.json`. The SBOM
includes a Cryptography Bill of Materials (CBOM) component listing
the Ed25519 primitive and its source (`ed25519-dalek v2.x`).

## Fuzzing

The core has `cargo-fuzz` + libFuzzer targets for the four
deserialization boundaries:

- `fuzz_targets/evaluate_request_json.rs` — request envelope parser
- `fuzz_targets/policy_yaml.rs` — policy YAML → JSON pipeline
- `fuzz_targets/dsse_envelope.rs` — signed-bundle envelope parser
- `fuzz_targets/rfc9421_signature_base.rs` — signature-base
  construction

Fuzzing runs on a scheduled GitHub Actions job and on every PR that
touches `crates/core`. Cumulative CPU-hours and corpus coverage are
reported in the quarterly security summary published at
`https://checkrd.io/security/fuzz-report`.

## Known Limitations

- **Constant-time guarantees are best-effort.** See
  [Side-Channel Posture](#side-channel-posture).
- **No formal verification.** Memory safety of the compiled Rust
  source is a design property, not a formally verified theorem.
- **No FIPS validation.** See [Cryptographic Primitives](#cryptographic-primitives).
- **No external cryptographic audit as of this SDK release.** The
  first independent audit is scheduled following the 1.0 release; the
  scope and auditor will be named in `SECURITY.md` once confirmed.
- **Reproducible builds are not yet verified.** Target for v1.0.
- **Rollback of `reload_policy` (unsigned) is possible by design.**
  Unsigned `reload_policy` is intended for Tier 3 (air-gapped) file-
  watcher deployments where the policy source is already trusted.
  Only `reload_policy_signed` enforces the monotonic-version
  invariant.

## References

- [WebAssembly Security](https://webassembly.org/docs/security/)
- [Wasmtime Security Documentation](https://docs.wasmtime.dev/security.html)
- [Wasmtime Stability Tiers](https://docs.wasmtime.dev/stability-tiers.html)
- [RFC 8032 — EdDSA](https://www.rfc-editor.org/rfc/rfc8032.html)
- [RFC 9421 — HTTP Message Signatures](https://www.rfc-editor.org/rfc/rfc9421.html)
- [RFC 9530 — Digest Fields](https://www.rfc-editor.org/rfc/rfc9530.html)
- [DSSE Protocol](https://github.com/secure-systems-lab/dsse)
- [Project Wycheproof](https://github.com/google/wycheproof)
- [SLSA v1.0](https://slsa.dev/spec/v1.0)
- [Sigstore cosign](https://docs.sigstore.dev/cosign/)
- [CycloneDX SBOM](https://cyclonedx.org/)
- [WebAssembly Constant-Time Proposal](https://github.com/WebAssembly/constant-time)
