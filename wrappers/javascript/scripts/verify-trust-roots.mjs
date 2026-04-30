#!/usr/bin/env node
/**
 * Pre-publish guard — refuse to ship a release whose SDK would
 * silently reject every signed policy update because the production
 * trust list is empty.
 *
 * Mirror of the Python ``checkrd policy trust-status`` CLI used by
 * the Python publish workflow. The two SDKs ship the same trust list
 * (one rotation reaches both), so the guard is symmetric: empty list
 * targeting ``api.checkrd.io`` is a hard block; any other state is OK.
 *
 * Wired into ``.github/workflows/publish-javascript.yml`` after
 * ``npm run attw`` and before the npm publish step. See
 * KEY-CUSTODY.md §6 for the operator runbook.
 *
 * Usage:
 *
 *     node scripts/verify-trust-roots.mjs
 *
 * Exit codes:
 *
 *     0  — trust list is populated, OR no production URL configured
 *           (i.e., this is a dev / staging release)
 *     1  — trust list is empty AND we're targeting api.checkrd.io;
 *           the bootstrap ceremony documented in KEY-CUSTODY.md has
 *           not been run
 *     2  — script-level error (file missing, parse failure)
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TRUST_FILE = resolve(__dirname, "..", "src", "_trust.ts");
const PRODUCTION_HOST_MARKER = "checkrd.io";

function readTrustList() {
  // Source-level inspection: read ``_trust.ts`` and find the
  // ``PRODUCTION_TRUSTED_KEYS`` declaration. We don't import the
  // module because that would require building first, and the
  // guard runs before the ``publish`` job (which builds). A regex
  // is more brittle than an import but fits the workflow shape.
  let source;
  try {
    source = readFileSync(TRUST_FILE, "utf-8");
  } catch (err) {
    console.error(`✗ verify-trust-roots: cannot read ${TRUST_FILE}: ${err}`);
    process.exit(2);
  }
  // Match ``const PRODUCTION_TRUSTED_KEYS: ... = [ <body> ];``.
  // Body is whatever lives between the brackets; we only care
  // whether it has at least one entry.
  const match = source.match(
    /const\s+PRODUCTION_TRUSTED_KEYS\s*:[^=]*=\s*\[([\s\S]*?)\]/,
  );
  if (match === null) {
    console.error(
      `✗ verify-trust-roots: could not find PRODUCTION_TRUSTED_KEYS in ${TRUST_FILE}. ` +
        "Did the trust file move? Update the regex.",
    );
    process.exit(2);
  }
  const body = match[1].trim();
  // Empty if no content between brackets. Comments are allowed
  // (operator left a "// populate via scripts/generate-policy-signing-key.py"
  // note); strip them before checking.
  const stripped = body.replace(/\/\/[^\n]*/g, "").replace(/\s+/g, "");
  return stripped.length === 0
    ? { populated: false, raw: body }
    : { populated: true, raw: body };
}

function detectProductionTarget() {
  // The Python ``trust-status`` CLI checks against an explicit
  // ``--base-url``. For the JS workflow, the equivalent signal is
  // "is this an `npm publish` for the production package name?".
  // Reading ``package.json`` is the cleanest pre-build check.
  const pkgPath = resolve(__dirname, "..", "package.json");
  let pkg;
  try {
    pkg = JSON.parse(readFileSync(pkgPath, "utf-8"));
  } catch (err) {
    console.error(`✗ verify-trust-roots: cannot read ${pkgPath}: ${err}`);
    process.exit(2);
  }
  // ``checkrd`` is the production package name. A dev / staging
  // build that overrides ``package.json#name`` (e.g.,
  // ``@checkrd/sdk-staging``) would skip the guard, which is the
  // intended behaviour — staging publishes don't need production
  // trust roots populated.
  return pkg.name === "checkrd";
}

const isProduction = detectProductionTarget();
const { populated } = readTrustList();

if (!isProduction) {
  console.log(
    "ok: not a production publish (package.json#name is not 'checkrd'). " +
      "Skipping trust-list check.",
  );
  process.exit(0);
}

if (populated) {
  console.log(
    "ok: PRODUCTION_TRUSTED_KEYS is populated. Signed policy " +
      "updates will be verified at runtime.",
  );
  process.exit(0);
}

console.error(
  "✗ verify-trust-roots: PRODUCTION_TRUSTED_KEYS is empty in src/_trust.ts.\n" +
    "\n" +
    "  This release targets the production package name ('checkrd') but\n" +
    "  no signing key is pinned, which means every signed policy update\n" +
    "  the control plane delivers will be silently rejected by the SDK.\n" +
    "\n" +
    "  Run the bootstrap ceremony documented in KEY-CUSTODY.md §2,\n" +
    "  then commit the populated trust list and re-tag the release.\n",
);
process.exit(1);
