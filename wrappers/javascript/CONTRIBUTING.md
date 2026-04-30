# Contributing

Thank you for considering a contribution. This repo tracks both the
WASM core (closed-source, in a private repository) and the
language-wrapper SDKs. Wrapper changes flow through this repository;
core changes flow through the private repository and land here as a
binary update.

## Getting set up

```bash
git clone https://github.com/checkrd-io/checkrd-sdk.git
cd checkrd/wrappers/javascript
npm install
```

## Running the test suite

```bash
npm run typecheck
npm run lint
npm run test
npm run test:coverage
npm run build
npm run publint
npm run attw
```

The full pipeline runs via `npm run ci`.

Coverage thresholds are enforced at 80% lines / statements /
functions and 75% branches. If your PR lowers coverage, add tests
before asking for review.

## Coding style

- TypeScript strict mode (`noUncheckedIndexedAccess`,
  `exactOptionalPropertyTypes`, `verbatimModuleSyntax`).
- ESM-first source; both ESM and CJS are emitted by `tsup`.
- TSDoc on every public export (`publicOnly` JSDoc rule is
  enforced).
- Never introduce runtime dependencies on `node:*` modules in new
  public APIs without wrapping them in a lazy import and a
  platform-capability check. See `src/sinks.ts::JsonFileSink` for
  the pattern.
- Security-relevant changes must be called out in the PR
  description so we can flag them in the changelog under a
  `[security]` tag.

## Filing a pull request

1. One focused change per PR. Refactors land separately from
   behavior changes.
2. Update `CHANGELOG.md` under `## [Unreleased]` with a one-line
   summary.
3. If your change affects the FFI surface, the telemetry event
   schema, or the policy schema, update `WASM-CORE.md` at the
   same time.
4. If your change changes the threat model (new asset, new actor,
   new mitigation), update `THREAT-MODEL.md`.
5. CI must be green before review.

## Reporting a security issue

**Do not open a public GitHub issue.** See
[SECURITY.md](./SECURITY.md) for the coordinated-disclosure
process.

## License

By contributing, you agree that your contributions will be licensed
under the Apache License 2.0.
