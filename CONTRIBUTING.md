# Contributing

Thanks for your interest in improving the Checkrd SDKs. This repository
holds the Python and JavaScript wrappers, the WebAssembly policy engine
they embed, and the JSON Schemas for policy and telemetry payloads.

## Reporting issues

- **Bugs and feature requests:** open a GitHub issue. Include the SDK
  language and version, the runtime (Python/Node version, OS), a minimal
  reproduction, and what you expected to happen.
- **Security vulnerabilities:** do not open a public issue. Follow the
  process in [SECURITY.md](SECURITY.md).

## Local setup

You need a recent Rust toolchain, Python 3.9+, and Node 18+.

```sh
# WASM target — required to build the policy engine.
rustup target add wasm32-wasip1

# Build the WASM engine and stage it inside both wrappers. Re-run any
# time you change crates/core or crates/shared.
cargo build --package checkrd-core --target wasm32-wasip1 --release
./scripts/copy-wasm.sh
```

### Python wrapper

```sh
cd wrappers/python
pip install -e ".[test]"
pytest
ruff check src/ tests/
mypy src/
```

### JavaScript wrapper

```sh
cd wrappers/javascript
npm ci
npm test
npm run typecheck
npm run lint
```

### Rust core

```sh
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all --check
```

## Pull requests

1. Fork and create a feature branch.
2. Make your change. Keep it focused — unrelated cleanup belongs in a
   separate PR.
3. Add or update tests. The CI matrix runs every PR against Python
   3.9–3.13 and Node 18/20/22, and a regression here will block merge.
4. If you change behaviour observable from the SDK surface, update the
   matching wrapper's `CHANGELOG.md`.
5. If you change the WASM engine (`crates/core`) or shared types
   (`crates/shared`), make sure the same change is reflected in both
   wrappers' tests — the two SDKs are intentionally one-for-one in
   behaviour.
6. Open a PR. Describe what changed, why, and how you verified it.

We don't require a CLA. By submitting a pull request, you agree that
your contribution is licensed under the same Apache 2.0 terms as the
rest of the repository.

## Coding conventions

- **Rust:** `cargo fmt`, `clippy --all-targets -- -D warnings`. Public
  items in `crates/core` carry doc comments; security-critical paths
  also carry property tests in `crates/core/src/*` modules.
- **Python:** ruff for lint, mypy strict for types, pyright in CI.
  Public types are TypedDicts; everything else is private (`_`-prefix).
  No `time.sleep` in tests — use the `wait_for` helper in
  `tests/conftest.py`.
- **JavaScript:** eslint + prettier defaults from `wrappers/javascript`.
  Curated public API in `src/index.ts`; advanced surface in
  `src/advanced.ts`. Every subpath export has a contract test in
  `tests/subpath_exports.test.ts`.

## Commit messages

Follow the [Conventional Commits](https://www.conventionalcommits.org/)
style where possible:

```
feat(python): add async control receiver
fix(wasm): correct rate-limit eviction order under contention
docs(js): clarify edge-runtime polyfill requirements
```

## Releases

Releases publish from this repository directly. Two tag-triggered
workflows handle the heavy lifting:

- **Python** — push a `python-vX.Y.Z` tag to fire
  `.github/workflows/publish-python.yml`. The full test matrix runs
  (3.10–3.13), the wheel + sdist are built, twine and
  check-wheel-contents validate the artefacts, a fresh-venv install
  sanity check imports the published wheel, a CycloneDX SBOM is
  attached, and the wheel is uploaded to PyPI via Trusted Publishing
  (no long-lived token in the repo).

- **JavaScript** — push a `javascript-vX.Y.Z` tag to fire
  `.github/workflows/publish-javascript.yml`. Cross-runtime test
  matrix (Node 20 + 22), `publint` and `attw` validate the package
  shape, an install-sanity step packs the tarball and exercises both
  ESM and CJS imports plus end-to-end FFI evaluation, a CycloneDX
  SBOM is attached, and the package is published to npm with
  `--provenance` (the Sigstore attestation links back to the public
  build run; consumers can verify with
  `npm view checkrd --json | jq .dist.attestations`).

A pre-publish guard in each pipeline refuses to ship if the SDK's
pinned trust list is empty (which would silently reject every signed
policy update at install time).

To cut a release:

1. Bump the version in `wrappers/python/src/checkrd/_version.py`
   (Python) or `wrappers/javascript/package.json` (JavaScript).
2. Update the relevant `CHANGELOG.md`.
3. Land the bump commit on `main` via PR.
4. Tag from `main`:
   `git tag -a python-vX.Y.Z -m "Python SDK vX.Y.Z" && git push origin python-vX.Y.Z`
5. Watch the workflow run in the Actions tab.

See each wrapper's `API-STABILITY.md` for the public-API stability
commitments.
