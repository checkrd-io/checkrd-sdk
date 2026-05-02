# Security Policy — checkrd (Python)

Checkrd sits between AI agents and every external API they call. A
vulnerability here can silently bypass customer policy or leak signed
telemetry credentials, so security reports are prioritized over feature
work.

Related documents:

- [`crates/core/SECURITY.md`](../../crates/core/SECURITY.md) — the
  embedded WASM core's threat model and integrity-verification recipe
  (shipped with both wrappers; same binary).
- [`SECURITY.md` for the JavaScript SDK](../javascript/SECURITY.md) —
  the sibling wrapper. Both SDKs use identical Ed25519 / RFC 9421 /
  DSSE primitives via the shared WASM core; their security postures
  are intended to stay in lockstep.

## Reporting a Vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

Email: **security@checkrd.io**

For encrypted reports, fetch our PGP key at
`https://checkrd.io/.well-known/security.asc`. A machine-readable
`security.txt` ([RFC 9116](https://www.rfc-editor.org/rfc/rfc9116.html))
is published at `https://checkrd.io/.well-known/security.txt`.

Include in the report:

1. A clear description of the issue and the affected component
   (`checkrd` Python SDK version + commit SHA if from source).
2. Steps to reproduce, ideally with a minimal proof-of-concept.
3. The impact you believe this has (what can an attacker do?).
4. Your name and affiliation if you want attribution in the advisory.

## Response Commitment

- **Acknowledgement within 2 business days** — we confirm receipt and
  give you a case ID.
- **Triage within 5 business days** — we confirm the severity class
  (none / low / medium / high / critical) and the expected fix window.
- **Fix windows** — critical shipped within 7 days; high within 30 days;
  medium / low on the next minor release.
- **Coordinated disclosure** — we publish a CVE and GitHub Security
  Advisory once a fix is available. Default embargo is 72 hours from
  CVE issuance, longer if patch complexity requires it. Reporter
  credit is included unless you ask otherwise.

## Scope

In scope:

- The `checkrd` Python package published on PyPI.
- The bundled WASM core (`checkrd_core.wasm`) and its SHA-256 integrity
  verification.
- The control-plane wire protocol as exercised by this SDK
  (`/v1/telemetry`, `/v1/agents/.../control`,
  `/v1/agents/.../public-key`).
- Identity and key handling in `identity.py` + `engine.py` (signing,
  zeroization, key-file permissions).
- Policy signature verification via `_trust.py` + `_policy_state.py` +
  `control.py`'s DSSE install path.
- The seven vendor instrumentors under `integrations/` and the httpx
  transport in `transports/_httpx.py`.

Out of scope (report to the control-plane repository's `SECURITY.md`):

- Server-side policy evaluation, authentication, or rate-limiting
  bugs in the hosted control plane.
- The dashboard UI.

## Supported Versions

| Version | Security support until |
| ------- | ---------------------- |
| 0.2.x   | 12 months after 0.2.0  |
| 0.1.x   | Best effort until 0.3  |

Checkrd follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Security fixes are backported to the current major and the previous
major (N-1) for 12 months from the new major's release.

## Fail-Closed Defaults

The SDK ships with several fail-closed defaults. **Do not weaken them
in production.**

1. **`security_mode="strict"`** (default). If the WASM engine fails to
   load, the SDK raises `CheckrdInitError`. Set
   `security_mode="permissive"` (or `CHECKRD_SECURITY_MODE=permissive`)
   only during a controlled rollout — never as steady state.
2. **1 MB body-inspection limit**. In strict mode, requests with
   bodies above 1 MB are denied with
   `reason="body exceeds 1MB inspection limit"` rather than silently
   skipping body matchers. Permissive mode logs a warning and passes
   through. Policy-as-defense must not fail silent.
3. **Deterministic `agent_id`**. `derive_agent_id` raises
   `CheckrdInitError` (code `agent_id_undetectable`) when no PaaS
   service-name env var or hostname is available. A random fallback
   would silently break kill-switch scoping and telemetry signature
   verification on every container restart, so the SDK refuses to
   invent an identity. Set `CHECKRD_AGENT_ID` explicitly in
   environments without a stable hostname.

### Developer-only env vars

Both emit loud warnings and must never be set in production:

- `CHECKRD_ALLOW_INSECURE_HTTP=1` — accepts `http://` control-plane
  URLs (otherwise API keys would travel in plaintext).
- `CHECKRD_SKIP_WASM_INTEGRITY=1` — skips the WASM SHA-256 check for
  source checkouts without a rebuilt hash file. In production-looking
  environments this requires the additional explicit acknowledgement
  `CHECKRD_I_UNDERSTAND_WASM_INTEGRITY_RISK=i-understand-the-risk` —
  a deliberately awkward phrase to catch accidents.

The deprecated `CHECKRD_DEV=1` flag toggled both at once. It emits a
`DeprecationWarning` and will be removed in 1.0.

## Supply Chain

- Python wheels are published to PyPI via **Trusted Publishing**
  (`pypa/gh-action-pypi-publish` + GitHub OIDC). Long-lived API
  tokens are not used.
- Wheels carry [PEP 740 attestations](https://peps.python.org/pep-0740/)
  signed with Sigstore via the publishing OIDC flow. Available at
  `https://pypi.org/integrity/checkrd/<version>/<file>/provenance` and
  surfaced on the PyPI release page under "Sigstore signatures".
- The WASM core binary shipped inside the wheel is verified at import
  time against a SHA-256 recorded at build time
  (`_wasm_integrity.py::EXPECTED_SHA256`). Independent verification
  against the PEP 740 attestation (`pypi-attestations verify pypi …`)
  is documented in [WASM-CORE.md § Integrity Verification](./WASM-CORE.md#integrity-verification).
  Stand-alone GitHub attestations (`actions/attest-build-provenance`)
  are on the roadmap, blocked on Enterprise-plan gating for private
  orgs.
- A CycloneDX SBOM is attached to each GitHub Release (generated via
  `cyclonedx-py` against the project's locked dependency set).
- Runtime dependencies are version-range pinned in `pyproject.toml`;
  a reproducible lockfile with hashes is published at
  `requirements-lock.txt` for offline / air-gapped installs.

## Telemetry Signing

Every telemetry batch is signed before it leaves the process, using
IETF-standard primitives:

- **Ed25519** ([RFC 8032](https://www.rfc-editor.org/rfc/rfc8032.html))
  via `ed25519-dalek`, tested against RFC 8032 §7.1 vectors and the
  full [Project Wycheproof v1](https://github.com/C2SP/wycheproof) set
  (150 vectors, 0 failures).
- **HTTP Message Signatures**
  ([RFC 9421](https://www.rfc-editor.org/rfc/rfc9421.html)),
  verified byte-for-byte against the §B.2.6 worked example.
- **Content-Digest** ([RFC 9530](https://www.rfc-editor.org/rfc/rfc9530.html))
  binding the request body into the signature.
- **Replay protection** via `created` + `expires` parameters with a
  5-minute signing window.

Mutation-tested to 100% kill rate on the cryptographic verification
primitives (`cargo-mutants`). Interop-tested against the PyCA
[`cryptography`](https://cryptography.io) Ed25519 implementation and
against the JavaScript SDK on every release.

## Signed Policy-Bundle Install

The `policy_updated` SSE event is wired through `control.py`'s
`_apply_policy_update` to the WASM core's `reload_policy_signed`. Every
update goes through the full verification path:

- DSSE signature verification against the trusted key list
  (`_trust.py::trusted_policy_keys`).
- Monotonic version check (rollback rejection).
- Bundle freshness check (max age 24 h, default).
- Cross-type replay defense via DSSE payload-type binding.

On any failure the previous policy is left in place and a structured
warning is logged (`PolicySignatureError.code` carries the stable
reason label for dashboard grouping). The high-water mark is
persisted across restarts so the rollback defense survives a process
restart, not just a process lifetime.

## Idempotency

Every control-plane POST (telemetry batch, public-key registration)
carries an `Idempotency-Key` header generated once before the retry
loop and reused across every attempt — Stripe convention. A retry of
an already-accepted request is deduplicated server-side.
`new_idempotency_key()` in `_platform.py` is the single source of
truth.

## Known Limitations

- **No external cryptographic audit as of this release.** First audit
  scheduled for post-1.0; scope will be named here on confirmation.
- **No FIPS 140-3 validation.** Contact `security@checkrd.io` for the
  roadmap.
- **Side-channel timing is best-effort** under WebAssembly runtimes
  (wasmtime). The WASM core's verifier uses constant-time comparison
  primitives, but JIT compilation does not guarantee they are
  preserved at machine-code level.
