# Security policy

## Reporting a vulnerability

Please report security vulnerabilities privately. Do not open a public
GitHub issue.

Two channels are accepted:

1. **GitHub Security Advisories** —
   [`Report a vulnerability`](https://github.com/checkrd-io/checkrd-sdk/security/advisories/new)
   on this repository. Preferred. Goes directly to the maintainers and
   gives us a private channel to coordinate a fix and disclosure
   timeline with you.
2. **Email** — `security@checkrd.io`. Use the PGP key published at
   [checkrd.io/.well-known/security.txt](https://checkrd.io/.well-known/security.txt)
   if you need to encrypt the report.

When reporting, please include:

- The SDK language and version (`pip show checkrd` or `npm ls checkrd`).
- A description of the issue and its impact.
- Steps to reproduce, or proof-of-concept code if you have one.
- Any suggested mitigation.

We will acknowledge your report within **two business days** and aim
to provide an initial assessment within **five business days**.

## Disclosure process

We follow [coordinated vulnerability disclosure](https://www.first.org/global/sigs/vulnerability-coordination/multiparty/guidelines-v1.1).

1. We confirm the report and reproduce the issue.
2. We develop a fix in a private branch and prepare an advisory.
3. We coordinate a release date with the reporter, typically within
   90 days of the initial report. Embargo windows can be negotiated for
   complex issues that require downstream coordination.
4. We publish the patched releases and the advisory simultaneously.
5. We credit reporters in the advisory unless they ask to remain
   anonymous.

## Supported versions

The SDKs follow SemVer. We patch security issues on the latest minor
release line of each major. During the `0.x` pre-1.0 window, only the
latest minor receives security fixes; please keep your installation
current.

| Package | Latest line | Status |
|---|---|---|
| `checkrd` (Python, PyPI) | `0.x` | Supported |
| `checkrd` (JavaScript, npm) | `0.x` | Supported |

## Supply-chain protections in this repository

The SDKs are designed for production use under regulated workloads. The
release pipeline enforces several controls that you can verify
independently:

- **Pinned actions.** Every GitHub Actions step in this repository
  uses a SHA-pinned reference, not a floating tag. Dependabot proposes
  upgrades; humans review the diff before merging.
- **Trusted Publishing.** PyPI releases use Trusted Publishing
  (OIDC-based; no long-lived token in the repo). JavaScript releases
  ship with `npm publish --provenance` — the Sigstore attestation
  links back to the build run on this public repository. Verify with
  `npm view checkrd --json | jq .dist.attestations`.
- **SBOM per release.** Each release attaches a CycloneDX SBOM as a
  workflow-run artefact.
- **Pre-publish trust-list guard.** Both publish workflows refuse to
  ship a release whose pinned trust list is empty. This prevents
  accidentally shipping an SDK that silently rejects every signed
  policy update.
- **WASM integrity check.** The SDKs verify the SHA-256 of the embedded
  `checkrd_core.wasm` at load time against a hash baked in at build
  time. A tampered WASM binary fails the import.
- **Public build provenance.** Releases publish from this repository
  directly, not from a private mirror. The provenance chain is fully
  auditable: tag → commit → workflow run → published artefact.

## Out of scope

This policy covers the code and release artefacts in this repository.
Issues affecting the hosted control plane at `api.checkrd.io` should
be reported via the same channels — they reach the same team — but
the disclosure timelines and supported versions above apply only to
the SDKs and the WASM engine.
