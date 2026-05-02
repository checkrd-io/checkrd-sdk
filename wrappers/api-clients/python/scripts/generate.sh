#!/usr/bin/env bash
# Regenerate the Python control-plane client from the committed
# OpenAPI document. Run via `make api-clients-python` from the repo
# root.
#
# Generator: openapi-python-client (OSS, attrs + httpx runtime).
# Generator output is the private ENGINE under
# `src/checkrd_api/_generated/`; the public surface
# (`from checkrd_api import Checkrd`) is hand-written in
# `src/checkrd_api/__init__.py` and the sibling files. Pattern is
# identical to Stainless: codegen produces low-level call sites and
# models, humans wrap them in a polished resource-based facade. The
# facade is what users import and what the README documents.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

SPEC="schemas/api/openapi.json"
PKG_DIR="wrappers/api-clients/python/src/checkrd_api"
GEN_DIR="$PKG_DIR/_generated"

if [[ ! -s "$SPEC" ]]; then
  echo "error: $SPEC missing or empty. Run 'make openapi' first." >&2
  exit 1
fi

if ! command -v openapi-python-client >/dev/null 2>&1; then
  cat >&2 <<'EOF'
error: openapi-python-client not on PATH.
Install with:
  pip install --user 'openapi-python-client>=0.21,<0.30'
or via the dev extras:
  pip install -e 'wrappers/api-clients/python[dev]'
EOF
  exit 2
fi

# Generate to a temp dir first so an aborted run can't leave a
# half-written package on disk.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Generate under package name `_generated` so the resulting module
# layout is `checkrd_api._generated.{client,models,api}` — private
# from the user, consumable from the facade.
cat > "$TMP/config.yaml" <<'EOF'
package_name_override: _generated
project_name_override: checkrd-api-generated
use_path_prefixes_for_title_model_names: false
EOF

openapi-python-client generate \
  --path "$SPEC" \
  --config "$TMP/config.yaml" \
  --output-path "$TMP/out" \
  --overwrite

# Replace the previous _generated tree atomically. Hand-written
# files in src/checkrd_api/ (everything that is not `_generated/`,
# `models/`, `api/`, `client.py`, `errors.py`, `types.py`) are
# preserved.
rm -rf "$GEN_DIR"
mkdir -p "$PKG_DIR"
mv "$TMP/out/_generated" "$GEN_DIR"

# PEP 561 marker so type-checkers honor the package's annotations.
touch "$PKG_DIR/py.typed"

echo "regenerated $GEN_DIR ($(find "$GEN_DIR" -name '*.py' | wc -l | tr -d ' ') python files)"
