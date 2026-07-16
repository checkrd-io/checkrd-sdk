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
 * Wired into ``publish-javascript.yml`` in the public
 * ``checkrd-io/checkrd-sdk`` mirror (after ``npm run attw``,
 * before the npm publish step). See KEY-CUSTODY.md §6 for the
 * operator runbook.
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

function readTrustSource() {
  // Source-level inspection: read ``_trust.ts`` once. We don't import
  // the module because that would require building first, and the
  // guard runs before the ``publish`` job (which builds). A regex is
  // more brittle than an import but fits the workflow shape.
  try {
    return readFileSync(TRUST_FILE, "utf-8");
  } catch (err) {
    console.error(`✗ verify-trust-roots: cannot read ${TRUST_FILE}: ${err}`);
    process.exit(2);
  }
}

function readTrustList(source, constName) {
  // Match ``const <constName>: ... = [ <body> ];``. Body is whatever
  // lives between the brackets; we only care whether it has at least
  // one entry.
  const match = source.match(
    new RegExp(`const\\s+${constName}\\s*:[^=]*=\\s*\\[([\\s\\S]*?)\\]`),
  );
  if (match === null) {
    console.error(
      `✗ verify-trust-roots: could not find ${constName} in ${TRUST_FILE}. ` +
        "Did the trust file move or the constant get renamed? Update the regex.",
    );
    process.exit(2);
  }
  const body = match[1].trim();
  // Empty if no content between brackets. Comments are allowed
  // (operator left a "// populate via ..." note); strip them before
  // checking.
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

if (!isProduction) {
  console.log(
    "ok: not a production publish (package.json#name is not 'checkrd'). " +
      "Skipping trust-list check.",
  );
  process.exit(0);
}

const source = readTrustSource();

// Each trust anchor is verified independently. Both must be populated for
// a production publish: an empty policy list silently rejects every signed
// policy update, and an empty PRICING list silently rejects every signed
// price table — both ship-blockers, the second of which an earlier
// policy-only guard would have missed.
const ANCHORS = [
  {
    constName: "PRODUCTION_TRUSTED_KEYS",
    kind: "policy",
    bootstrapRef: "KEY-CUSTODY.md §2",
    // Policy distribution is LIVE — an empty list is always a hard block.
    skipEnv: null,
  },
  {
    constName: "PRICING_TRUSTED_KEYS",
    kind: "pricing",
    bootstrapRef: "KEY-CUSTODY.md §2 (pricing-signing-key)",
    // Cost metering ships DEFAULT-OFF and signed pricing distribution is
    // not yet enabled, so the pricing list is legitimately empty pre-first-
    // pricing-release (exactly like the policy list was pre-1.0). To avoid
    // hard-blocking every SDK publish during that window while still
    // refusing to *silently* pass, an empty pricing list is a hard block
    // UNLESS the operator explicitly acknowledges the pre-release state via
    // `CHECKRD_SKIP_PRICING_TRUST_CHECK=1`. The moment pricing releases are
    // gated, that ack is removed and the guard blocks like the policy one.
    skipEnv: "CHECKRD_SKIP_PRICING_TRUST_CHECK",
  },
];

let anyEmpty = false;
for (const anchor of ANCHORS) {
  const { populated } = readTrustList(source, anchor.constName);
  if (populated) {
    console.log(
      `ok: ${anchor.constName} is populated. Signed ${anchor.kind} ` +
        "updates will be verified at runtime.",
    );
    continue;
  }
  // Empty list. Allow an explicit pre-release acknowledgment for anchors
  // that ship a default-off feature (pricing today).
  if (anchor.skipEnv && process.env[anchor.skipEnv] === "1") {
    console.log(
      `ok: ${anchor.constName} is empty but ${anchor.skipEnv}=1 is set ` +
        `(pre-first-${anchor.kind}-release acknowledgment). The signed ` +
        `${anchor.kind} feature ships default-off; populate the list before ` +
        `enabling it.`,
    );
    continue;
  }
  anyEmpty = true;
  console.error(
    `✗ verify-trust-roots: ${anchor.constName} is empty in src/_trust.ts.\n` +
      "\n" +
      `  This release targets the production package name ('checkrd') but\n` +
      `  no ${anchor.kind}-signing key is pinned, which means every signed\n` +
      `  ${anchor.kind} update the control plane delivers will be silently\n` +
      `  rejected by the SDK.\n` +
      "\n" +
      `  Run the bootstrap ceremony documented in ${anchor.bootstrapRef},\n` +
      `  then commit the populated trust list and re-tag the release.\n` +
      (anchor.skipEnv
        ? `\n  If signed ${anchor.kind} distribution is not yet enabled, set\n` +
          `  ${anchor.skipEnv}=1 to acknowledge the pre-release state.\n`
        : ""),
  );
}

process.exit(anyEmpty ? 1 : 0);
