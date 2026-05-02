## Setting up the environment

```sh
cd wrappers/api-clients/javascript
npm install
```

## Modifying/Adding code

The `src/_generated/` subtree is materialized from `schemas/api/openapi.json` by the
[@hey-api/openapi-ts](https://github.com/hey-api/openapi-ts) generator. **Do not edit
anything under `_generated/` by hand** — your changes will be overwritten the next time
the generator runs.

The hand-written facade layer (`src/index.ts`, `src/client.ts`, `src/errors.ts`,
`src/pagination.ts`, `src/resources/*.ts`) is the source of truth for the public surface.
When the API gets a new resource:

1. Add `#[utoipa::path]` annotations to the new handlers in `crates/api/src/routes/<thing>.rs`.
2. Run `make openapi` to regenerate the spec.
3. Run `make api-clients-js` to regenerate `src/_generated/`.
4. Add a `src/resources/<thing>.ts` mirroring the `src/resources/agents.ts` pattern.
5. Wire it as a field on `Checkrd` in `src/client.ts`.
6. Re-export the resource's types from `src/index.ts`.

CI runs `make openapi-check` and fails on any drift between `crates/api` and the
committed spec — same drift guard as the dashboard's `typeshare`-generated TS.

## Running tests

Most tests require you to [set up a mock server](https://github.com/stoplightio/prism)
against the OpenAPI spec to run the tests.

```sh
# you will need npm installed
npx prism mock schemas/api/openapi.json
```

```sh
npm test
```

## Linting and formatting

```sh
npm run typecheck
npm run lint
```

## Publishing and releases

This package is generated from the OpenAPI spec and currently has no automated
publish pipeline. The `CHANGELOG.md` is maintained by hand if releases resume.
