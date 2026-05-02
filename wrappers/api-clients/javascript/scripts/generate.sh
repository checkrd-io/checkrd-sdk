#!/usr/bin/env bash
# Regenerate the JS/TS control-plane client from the committed
# OpenAPI document. Run via `make api-clients-js` from the repo
# root, or directly: `npm run generate` from this package.
#
# Generator: @hey-api/openapi-ts (OSS, MIT, ESM-first). Output is
# reproducible — same spec in, same file tree out.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

SPEC="schemas/api/openapi.json"
PKG_DIR="wrappers/api-clients/javascript"

if [[ ! -s "$SPEC" ]]; then
  echo "error: $SPEC missing or empty. Run 'make openapi' first." >&2
  exit 1
fi

# `npm install` if node_modules is missing — the generator depends
# on @hey-api/openapi-ts and its peers. We don't run install on
# every invocation because it's slow and CI installs separately.
if [[ ! -d "$PKG_DIR/node_modules/@hey-api/openapi-ts" ]]; then
  ( cd "$PKG_DIR" && npm install --silent --no-audit --no-fund )
fi

# Wipe only the generator's output directory (`src/_generated/`).
# Hand-written facade files (`src/index.ts`, `src/client.ts`,
# `src/resources/*.ts`, `src/errors.ts`, `src/pagination.ts`) sit
# alongside it and are preserved across regeneration.
rm -rf "$PKG_DIR/src/_generated"
mkdir -p "$PKG_DIR/src/_generated"

( cd "$PKG_DIR" && npx --no-install @hey-api/openapi-ts )

# The generator emits an index.ts; we just confirm it's there.
if [[ ! -s "$PKG_DIR/src/_generated/index.ts" ]]; then
  echo "error: generation completed but $PKG_DIR/src/_generated/index.ts is missing" >&2
  exit 3
fi

echo "regenerated $PKG_DIR/src/_generated ($(find "$PKG_DIR/src/_generated" -name '*.ts' | wc -l | tr -d ' ') TypeScript files)"
