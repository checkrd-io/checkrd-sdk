/**
 * Trusted public keys for verifying signed policy bundles from the
 * control plane. Mirrors `wrappers/python/src/checkrd/_trust.py`
 * one-for-one — both SDKs ship the same key list so a single rotation
 * reaches every runtime.
 *
 * The Checkrd control plane signs every policy update with an Ed25519
 * key held in AWS Secrets Manager. The SDK ships with a list of trusted
 * public keys — each with a key ID and a validity window — and refuses
 * to install any policy update whose signature can't be verified against
 * an in-window trusted key.
 *
 * This file is the trust root for the entire policy distribution path.
 * Compile-time pinning means an attacker who compromises the network or
 * DNS can't substitute their own signing key, even on first use; the
 * same threat model browser TLS root CAs and OPA bundle signing use.
 *
 * # Format
 *
 * Each entry is an object with four fields:
 *
 *   - ``keyid`` — stable identifier matching the ``keyid`` in DSSE
 *     signatures emitted by the control plane.
 *   - ``public_key_hex`` — 64 lowercase hex chars (32-byte Ed25519 pub key).
 *   - ``valid_from`` — Unix seconds when this key starts being trusted.
 *   - ``valid_until`` — Unix seconds when this key stops being trusted.
 *
 * # Test override
 *
 * The ``CHECKRD_POLICY_TRUST_OVERRIDE_JSON`` environment variable accepts
 * a JSON array in the same format. When set AND
 * ``CHECKRD_ALLOW_TRUST_OVERRIDE=1`` is also set, it REPLACES the
 * production list. The double-gate prevents accidental or malicious
 * override in production — a single compromised env var is not enough.
 *
 * NEVER set these in production.
 */

import type { Logger } from "./_logger.js";

/** Snake-case shape matching the WASM core's ``TrustedKey`` JSON. */
export interface TrustedKey {
  keyid: string;
  public_key_hex: string;
  valid_from: number;
  valid_until: number;
}

// Production trusted keys. Populated by the bootstrap script
// `scripts/generate-policy-signing-key.py` before the first signed
// release; empty during pre-1.0 development. The CI publish workflow
// MUST verify this list is non-empty (see `productionTrustStatus`) —
// shipping an empty list silently disables every signed policy update.
const PRODUCTION_TRUSTED_KEYS: readonly TrustedKey[] = [
  // Bootstrap key for the production control plane. Private half lives in
  // AWS Secrets Manager `checkrd/prod/policy-signing-key`; this entry is
  // the public-key half pinned into the SDK so DSSE-signed policy bundles
  // from api.checkrd.io verify on every install.
  //
  // Validity window: 10 years — Sigstore Fulcio / Apple WWDR / TLS root CA
  // convention. Long-lived trust *roots* mean SDK versions in the field
  // keep verifying for years without forced upgrades. Rotation happens via
  // the overlap pattern (append a new entry, ship a new SDK release, then
  // switch the control plane), not by shortening this window.
  //
  // Mirrors `wrappers/python/src/checkrd/_trust.py::_PRODUCTION_TRUSTED_KEYS`
  // one-for-one — both SDKs ship the same list. See KEY-CUSTODY.md for the
  // full rotation runbook.
  {
    keyid: "checkrd-control-plane",
    public_key_hex: "5b6bd586744b59f28b2ff02aac7817447175610deb973db253030e8abee5ae01",
    valid_from: 1777329219,   // 2026-04-27T22:33:39Z
    valid_until: 2092689219,  // 2036-04-24T22:33:39Z (10 years)
  },
];

// Substring identifying a production-shaped control plane URL. Used by
// `productionTrustStatus` to decide whether an empty trust list is
// benign (dev/test) or a release blocker (production target).
const PRODUCTION_HOST_MARKER = "checkrd.io";

import { readEnv } from "./_env.js";

/** Distinct states the trust configuration can be in. Mirrors the Python
 * ``TrustStatusLevel`` literal. Stable labels so dashboards / CI guards
 * can branch without parsing the message text. */
export type TrustStatusLevel =
  | "ok"
  | "override"
  | "empty_dev"
  | "empty_production";

/** Result of {@link productionTrustStatus}. */
export interface TrustStatus {
  level: TrustStatusLevel;
  message: string;
}

/**
 * Return the list of trusted policy-signing keys, JSON-encoded for the
 * WASM core's ``reload_policy_signed`` FFI export. Mirrors Python's
 * :func:`trusted_policy_keys`.
 *
 * Override discipline (matches Python):
 *   - ``CHECKRD_POLICY_TRUST_OVERRIDE_JSON`` AND
 *     ``CHECKRD_ALLOW_TRUST_OVERRIDE=1`` must BOTH be set to use the
 *     override. A single compromised env var is not enough.
 *   - An override of an empty array is honored but logged loudly — every
 *     subsequent signed policy update will be rejected (the SDK never
 *     installs an unverified policy).
 */
export function trustedPolicyKeysJson(logger?: Logger): string {
  const override = readEnv("CHECKRD_POLICY_TRUST_OVERRIDE_JSON");
  if (override !== undefined && override.length > 0) {
    const gate = readEnv("CHECKRD_ALLOW_TRUST_OVERRIDE") ?? "";
    if (gate !== "1" && gate !== "true" && gate !== "yes") {
      logger?.warn(
        "CHECKRD_POLICY_TRUST_OVERRIDE_JSON is set but " +
          "CHECKRD_ALLOW_TRUST_OVERRIDE is not '1'. Ignoring override.",
      );
      return JSON.stringify(PRODUCTION_TRUSTED_KEYS);
    }
    try {
      const parsed: unknown = JSON.parse(override);
      if (Array.isArray(parsed)) {
        if (parsed.length === 0) {
          logger?.warn(
            "checkrd trust override is an empty list — all signed " +
              "policy updates will be rejected.",
          );
        }
        logger?.warn(
          `checkrd: using ${parsed.length.toString()} trust-override ` +
            "key(s) instead of production keys. DO NOT use in production.",
        );
        return JSON.stringify(parsed);
      }
    } catch {
      logger?.warn(
        "CHECKRD_POLICY_TRUST_OVERRIDE_JSON is not valid JSON; " +
          "falling back to production keys.",
      );
    }
  }
  return JSON.stringify(PRODUCTION_TRUSTED_KEYS);
}

// Module-level guard so {@link warnIfMisconfigured} fires at most once
// per process. Operators see one critical line at startup, not one per
// reconnect. JS doesn't fork() like Python, so a single boolean suffices.
let misconfiguredWarningFired = false;

/**
 * Diagnose the trust list configuration for the current environment.
 *
 * Pure function — takes the relevant inputs explicitly so tests don't
 * have to monkeypatch globals. The runtime callers (startup warning,
 * future CLI) pass `baseUrl` and the live env.
 *
 * Levels:
 *
 *   - ``ok`` — production keys present and no override. Steady state.
 *   - ``override`` — ``CHECKRD_POLICY_TRUST_OVERRIDE_JSON`` is in effect.
 *     Expected in dev/test; unexpected in production.
 *   - ``empty_dev`` — production list is empty AND the URL is not
 *     production-shaped. Benign for local development.
 *   - ``empty_production`` — production list is empty AND the URL points
 *     at a production control plane. Signed policy distribution is
 *     effectively disabled; ship-blocker.
 */
export function productionTrustStatus(opts: {
  baseUrl?: string | undefined;
  env?: ((name: string) => string | undefined) | undefined;
  /**
   * Test seam — defaults to the SDK's pinned production trust list.
   * Pass an empty array in tests to exercise the ``empty_dev`` /
   * ``empty_production`` branches without depending on production
   * state. Not part of the public API.
   */
  keys?: readonly TrustedKey[] | undefined;
}): TrustStatus {
  const env = opts.env ?? readEnv;
  const keys = opts.keys ?? PRODUCTION_TRUSTED_KEYS;
  const override = env("CHECKRD_POLICY_TRUST_OVERRIDE_JSON") ?? "";
  const gate = env("CHECKRD_ALLOW_TRUST_OVERRIDE") ?? "";
  const overrideActive =
    override.length > 0 && (gate === "1" || gate === "true" || gate === "yes");

  if (overrideActive) {
    return {
      level: "override",
      message:
        "trust list override active via CHECKRD_POLICY_TRUST_OVERRIDE_JSON. " +
        "Acceptable for dev/test; never set in production.",
    };
  }

  if (keys.length > 0) {
    return {
      level: "ok",
      message:
        `production trust list contains ${keys.length.toString()} ` +
        "key(s). Signed policy updates will be verified against this list.",
    };
  }

  if (opts.baseUrl?.includes(PRODUCTION_HOST_MARKER) === true) {
    return {
      level: "empty_production",
      message:
        "production trust list is empty AND the control plane URL " +
        `(${JSON.stringify(opts.baseUrl)}) targets a production endpoint. ` +
        "Every signed policy update will be rejected. Run " +
        "scripts/generate-policy-signing-key.py and update " +
        "PRODUCTION_TRUSTED_KEYS in src/_trust.ts before shipping.",
    };
  }

  return {
    level: "empty_dev",
    message:
      "production trust list is empty. Acceptable for local development; " +
      "set CHECKRD_POLICY_TRUST_OVERRIDE_JSON to a dev key to verify signed " +
      "bundles, or run scripts/generate-policy-signing-key.py to bootstrap " +
      "a production key.",
  };
}

/**
 * One-shot startup warning for misconfigured trust roots.
 *
 * Called from {@link ControlReceiver.start} so the warning fires at the
 * moment the SDK begins listening for signed bundles — not at module
 * import, where `baseUrl` isn't known. Safe to call from multiple
 * receivers or repeatedly; the underlying log fires at most once per
 * process.
 */
export function warnIfMisconfigured(opts: {
  baseUrl: string | undefined;
  logger?: Logger | undefined;
  /** Test seam, see {@link productionTrustStatus}. */
  keys?: readonly TrustedKey[] | undefined;
}): void {
  const { level, message } = productionTrustStatus({
    baseUrl: opts.baseUrl,
    keys: opts.keys,
  });
  if (level !== "empty_production") return;
  if (misconfiguredWarningFired) return;
  misconfiguredWarningFired = true;
  // No `console` fallback: the Logger interface is the single sink, and
  // we don't want to print to stderr when callers explicitly opted out
  // of logging by omitting the logger.
  opts.logger?.error(`checkrd: ${message}`);
}

/** Reset the one-shot warning flag. Tests only. */
export function _resetWarningStateForTests(): void {
  misconfiguredWarningFired = false;
}
