/**
 * Control-event dispatcher for the Checkrd JavaScript SDK.
 *
 * The SSE receiver (`receiver.ts`) forwards every incoming event to
 * {@link handleControlEvent}. This module is the dispatch contract —
 * mapping wire-format event names emitted by
 * `crates/api/src/routes/control.rs` to engine-side effects — so both
 * the real SSE client and any custom transports (LongPoll, WebSocket
 * bridge, in-process test harness) share one authoritative handler.
 *
 * Every supported event is mirrored from the Python wrapper to keep the
 * two SDKs behaviorally identical:
 *
 *   - `init`               → set kill switch from initial server state
 *   - `kill_switch`        → toggle kill switch
 *   - `policy_updated`     → install signed bundle (server-merged org + agent)
 *   - `policy_deactivated` → install local default-deny policy
 *   - `pricing_updated`    → install signed pricing bundle (cost metering)
 *
 * Bundles delivered on `init` and `policy_updated` are the *effective*
 * policy: the control plane has already merged the org-level deny rules
 * with the agent-level allow rules before signing. The SDK installs the
 * bytes verbatim through `reloadPolicySigned` and never merges -- single
 * source of truth on the server, matching the Envoy xDS / OPA Bundles
 * pattern.
 *
 * Signed *pricing* bundles ride the same `init` payload and a dedicated
 * `pricing_updated` event, installed through `reloadPricingSigned`. The
 * two distribution paths are structurally parallel but differ in one
 * critical dimension: policy is FAIL-CLOSED (a rejected bundle keeps the
 * previous policy, which may already be deny-all), whereas pricing is
 * FAIL-OPEN metering (a rejected price table just means the call's cost
 * isn't metered — never a reason to block or throw into the host). The
 * pricing install verifies against the SEPARATE pricing trust anchor
 * (`trustedPricingKeysJson()`), never the policy one — TUF per-role key
 * separation, since a tampered price table is an integrity attack on
 * money.
 *
 * Locally constructed default-deny is consistent with the SDK's "no
 * unsigned distribution path" rule because the bytes never traverse the
 * network — same justification as `setKillSwitch(true)`.
 */

import type { WasmEngine } from "./engine.js";
import {
  CheckrdInitError,
  PolicySignatureError,
  PricingSignatureError,
} from "./exceptions.js";

/**
 * Locally constructed default-deny policy installed when the control
 * plane signals that the agent's active policy was deactivated. The
 * source is the SDK process itself, not the network — so the "no
 * unsigned distribution path" rule does not apply (same justification
 * as `setKillSwitch(true)`). The next signed bundle from the control
 * plane installs via the verified path and supersedes this fallback.
 */
export const DEFAULT_DENY_POLICY_JSON: string = JSON.stringify({
  agent: "checkrd-deactivated",
  default: "deny",
  rules: [],
});

/**
 * Default freshness window for signed PRICING bundles, in seconds. The
 * pricing analogue of the policy path's inline `24 * 60 * 60` default,
 * and identical to the Python SDK's `_PRICING_BUNDLE_MAX_AGE_SECS`, so
 * both wrappers reject the same stale price tables. Passed to the WASM
 * core's `reload_pricing_signed`, which rejects any bundle whose
 * `signed_at` is older than this (FFI `-22`, `bundle_too_old`).
 */
export const DEFAULT_PRICING_MAX_AGE_SECS = 24 * 60 * 60;

/** Wire-format event names emitted by the control plane SSE stream. */
export type ControlEventName =
  | "init"
  | "kill_switch"
  | "policy_updated"
  | "policy_deactivated"
  | "pricing_updated";

/** Minimum surface required from the engine to dispatch control events. */
export interface ControlEngine {
  setKillSwitch(active: boolean): void;
  reloadPolicy(policyJson: string): void;
  /** Optional: supplied by full engines so `policy_updated` can install. */
  reloadPolicySigned?: (opts: {
    envelopeJson: string;
    trustedKeysJson: string;
    nowUnixSecs: number;
    maxAgeSecs: number;
  }) => void;
  /** Optional: exposed so the receiver can persist the high-water mark. */
  getActivePolicyVersion?: () => number;
  /**
   * Optional: supplied by full engines so `pricing_updated` can install a
   * signed price table. Structurally identical to
   * {@link reloadPolicySigned}, but the `trustedKeysJson` MUST be the
   * pricing trust list — see the module note on per-role key separation.
   */
  reloadPricingSigned?: (opts: {
    envelopeJson: string;
    trustedKeysJson: string;
    nowUnixSecs: number;
    maxAgeSecs: number;
  }) => void;
  /** Optional: pricing high-water mark, mirrored from the policy accessor. */
  getActivePricingVersion?: () => number;
}

/** Options supplied by the SSE receiver to control the DSSE install path. */
export interface PolicyUpdateOptions {
  /**
   * Async loader for the trust list JSON (the caller's set of allowed
   * signing keys). The receiver caches this internally and re-fetches
   * only on trust-list version bumps.
   */
  loadTrustedKeys: () => Promise<string> | string;
  /** Maximum acceptable bundle age, in seconds. Default 86_400 (24h). */
  maxAgeSecs?: number;
  /** Override for the clock source; test-only. */
  nowUnixSecs?: () => number;
  /**
   * Hash of the bundle currently installed by the caller, or `null` if
   * none has been installed yet. When the incoming event carries the
   * SAME hash, `installSignedPolicy` skips the WASM `reload_policy_signed`
   * call entirely — the OPA bundle / TUF "don't re-apply unchanged"
   * pattern. Without this, the WASM core's strict-greater monotonic
   * check rejects every legitimate idempotent replay (SSE reconnect
   * delivering the same active bundle, poll cycle returning the same
   * envelope, etc.).
   *
   * Optional: receivers that don't track an installed hash send every
   * install attempt through the FFI. The strict-greater monotonic
   * check then rejects same-version replays, which a hash-cached
   * receiver would have short-circuited as a no-op.
   */
  getLastHash?: () => string | null;
  /**
   * Invoked after a successful install. Receives the new monotonic
   * version and the server-canonical content hash (SHA-256 of the
   * YAML) — `null` when the event omits the field, in which case the
   * caller's cache should stay empty and the next install will run
   * through the FFI.
   */
  onInstalled?: (version: number, hash: string | null) => void | Promise<void>;
}

/**
 * Options supplied by the SSE receiver to control the signed-PRICING
 * install path. The pricing analogue of {@link PolicyUpdateOptions} — the
 * fields carry the same meaning (trust-list loader, freshness window,
 * hash-cache getter, post-install callback), but they drive
 * `reloadPricingSigned` against the SEPARATE pricing trust anchor.
 *
 * Kept as its own type (not a shared alias) so the two paths can diverge
 * without one silently dragging the other: policy is fail-closed, pricing
 * is fail-open, and their trust lists are disjoint by design.
 */
export interface PricingUpdateOptions {
  /**
   * Async loader for the PRICING trust list JSON. MUST resolve to the
   * pricing key list (`trustedPricingKeysJson()`), never the policy one.
   */
  loadTrustedKeys: () => Promise<string> | string;
  /** Maximum acceptable bundle age, in seconds. Default 86_400 (24h). */
  maxAgeSecs?: number;
  /** Override for the clock source; test-only. */
  nowUnixSecs?: () => number;
  /**
   * Hash of the pricing bundle currently installed by the caller, or
   * `null` if none. Same OPA-bundle / TUF "don't re-apply unchanged"
   * short-circuit the policy path uses — without it the WASM core's
   * strict-greater monotonic check rejects every idempotent replay.
   */
  getLastHash?: () => string | null;
  /**
   * Invoked after a successful pricing install. Receives the new
   * monotonic pricing version and the server-canonical content hash
   * (`null` when the event omits it, in which case the caller's cache
   * stays empty and the next install runs through the FFI).
   */
  onInstalled?: (version: number, hash: string | null) => void | Promise<void>;
}

/** Optional logger sink. Defaults to console.warn / console.error. */
export interface ControlLogger {
  warn(message: string, ...args: unknown[]): void;
  error(message: string, ...args: unknown[]): void;
}

const DEFAULT_LOGGER: ControlLogger = {
  warn: (msg, ...args) => {
    console.warn(`checkrd: ${msg}`, ...args);
  },
  error: (msg, ...args) => {
    console.error(`checkrd: ${msg}`, ...args);
  },
};

/**
 * Dispatch a single SSE event to the engine. Returns `true` when the
 * event was recognized and applied (or a recognized event whose data
 * was malformed and was logged-and-dropped); `false` for unknown event
 * types. Callers (real SSE clients, test harnesses) should use the
 * return value to decide whether to forward the event further.
 *
 * Mirrors `ControlReceiver._handle_event` in
 * `wrappers/python/src/checkrd/control.py`.
 */
export function handleControlEvent(
  engine: ControlEngine,
  eventName: string,
  rawData: string,
  logger: ControlLogger = DEFAULT_LOGGER,
  policyUpdate?: PolicyUpdateOptions,
  pricingUpdate?: PricingUpdateOptions,
): boolean {
  switch (eventName) {
    case "init": {
      const data = parseJson(rawData, eventName, logger);
      if (data === null) return true;
      const raw = (data as { kill_switch_active?: unknown }).kill_switch_active;
      // Strict boolean typing: a compromised control plane sending
      // `{"kill_switch_active": "false"}` would be truthy under
      // `Boolean(raw)`, tripping the kill switch across the fleet. Require
      // the field to be either a real boolean or absent (default off).
      if (raw !== undefined && typeof raw !== "boolean") {
        logger.warn(
          "control event %s has non-boolean kill_switch_active; ignoring",
          eventName,
        );
        return true;
      }
      engine.setKillSwitch(raw === true);
      // Self-bootstrap: the init payload carries the full signed envelope
      // of the agent's active policy. Without this branch a fresh SDK only
      // learns the kill-switch state and waits for a `policy_updated`
      // event that only fires on policy *change* — so a process starting
      // against an existing-active-policy agent would never enforce.
      // Reuse the same install path `policy_updated` uses so verification
      // + rollback-protection + freshness run identically.
      const initEnvelope = (data as { policy_envelope?: unknown })
        .policy_envelope;
      if (initEnvelope !== undefined && initEnvelope !== null) {
        if (!policyUpdate || !engine.reloadPolicySigned) {
          logger.warn(
            "init event carries policy_envelope but no signed-bundle " +
              "installer is wired up; ignoring. Pass `policyUpdate` to enable.",
          );
        } else {
          const wrapped = JSON.stringify({
            policy_envelope: initEnvelope,
            active_policy_hash: (data as { active_policy_hash?: unknown })
              .active_policy_hash,
          });
          void installSignedPolicy(engine, wrapped, policyUpdate, logger).catch(
            (err: unknown) => {
              logger.error("init policy install failed", err);
            },
          );
        }
      }
      // Self-bootstrap the price table too: the init payload carries the
      // agent's active *pricing* bundle alongside the policy one (M-14).
      // Reuse the same install path `pricing_updated` uses so the signature
      // verification, rollback protection, and freshness checks run
      // identically — and, critically, fail-open (a rejected price table
      // never blocks the host). Without this branch, cost metering would
      // stay dark until a rare runtime `pricing_updated` event fired.
      const initPricingEnvelope = (data as { pricing_envelope?: unknown })
        .pricing_envelope;
      if (initPricingEnvelope !== undefined && initPricingEnvelope !== null) {
        if (!pricingUpdate || !engine.reloadPricingSigned) {
          logger.warn(
            "init event carries pricing_envelope but no pricing installer " +
              "is wired up; cost metering stays disabled. Pass " +
              "`pricingUpdate` to enable.",
          );
        } else {
          const wrapped = JSON.stringify({
            pricing_envelope: initPricingEnvelope,
            active_pricing_hash: (data as { active_pricing_hash?: unknown })
              .active_pricing_hash,
          });
          void installSignedPricing(
            engine,
            wrapped,
            pricingUpdate,
            logger,
          ).catch((err: unknown) => {
            logger.error("init pricing install failed", err);
          });
        }
      }
      return true;
    }
    case "kill_switch": {
      const data = parseJson(rawData, eventName, logger);
      if (data === null) return true;
      const activeRaw = (data as { active?: unknown }).active;
      if (typeof activeRaw !== "boolean") {
        logger.warn(
          "control event %s missing required boolean `active` field",
          eventName,
        );
        return true;
      }
      engine.setKillSwitch(activeRaw);
      return true;
    }
    case "policy_updated":
      // Signed-bundle install. The wire-format event carries the DSSE
      // envelope in its `policy_envelope` field; verification happens
      // inside the WASM core via `reloadPolicySigned` against the
      // trust list the caller supplies.
      if (!policyUpdate || !engine.reloadPolicySigned) {
        logger.warn(
          "policy_updated event received but no signed-bundle installer " +
            "is wired up; ignoring. Pass `policyUpdate` to enable.",
        );
        return true;
      }
      void installSignedPolicy(engine, rawData, policyUpdate, logger).catch(
        (err: unknown) => {
          logger.error("policy_updated install failed", err);
        },
      );
      return true;
    case "policy_deactivated":
      onPolicyDeactivated(engine, logger);
      return true;
    case "pricing_updated":
      // Signed price-table install. The wire event carries the DSSE
      // envelope in `pricing_envelope`; verification runs in the WASM
      // core via `reloadPricingSigned` against the pricing trust list.
      // Fail-open: a rejected bundle logs and leaves the old table in
      // place — it never blocks or throws into the host (unlike the
      // fail-closed `policy_updated` path).
      if (!pricingUpdate || !engine.reloadPricingSigned) {
        logger.warn(
          "pricing_updated event received but no pricing installer is " +
            "wired up; cost metering stays disabled. Pass `pricingUpdate` " +
            "to enable.",
        );
        return true;
      }
      void installSignedPricing(engine, rawData, pricingUpdate, logger).catch(
        (err: unknown) => {
          logger.error("pricing_updated install failed", err);
        },
      );
      return true;
    default:
      // heartbeat / unknown / future events fall through.
      return false;
  }
}

async function installSignedPolicy(
  engine: ControlEngine,
  rawData: string,
  opts: PolicyUpdateOptions,
  logger: ControlLogger,
): Promise<void> {
  const data = parseJson(rawData, "policy_updated", logger);
  if (data === null) return;
  const envelope = (data as { policy_envelope?: unknown }).policy_envelope;
  if (envelope === undefined) {
    logger.warn("policy_updated missing policy_envelope; ignoring");
    return;
  }

  // Idempotency at the wrapper layer: if the incoming bundle's hash
  // matches the last one we installed, skip the WASM call entirely.
  // OPA bundle / TUF "don't re-apply unchanged" pattern — without it
  // the WASM core's strict-greater monotonic check rejects every
  // legitimate replay (reconnect, poll-cycle, init re-delivery).
  // Source ordering: explicit `hash` field > `active_policy_hash`
  // co-field > computed-after-install fallback.
  const incomingHashRaw =
    (data as { hash?: unknown; active_policy_hash?: unknown }).hash ??
    (data as { active_policy_hash?: unknown }).active_policy_hash;
  const incomingHash = isHexHash64(incomingHashRaw) ? incomingHashRaw : null;
  const lastHash = opts.getLastHash?.() ?? null;
  if (incomingHash !== null && lastHash !== null && incomingHash === lastHash) {
    return; // idempotent no-op; bundle already installed
  }

  const envelopeJson =
    typeof envelope === "string" ? envelope : JSON.stringify(envelope);
  const trustedKeysJson = await opts.loadTrustedKeys();
  const nowUnixSecs =
    (opts.nowUnixSecs ?? ((): number => Math.floor(Date.now() / 1000)))();
  const maxAgeSecs = opts.maxAgeSecs ?? 24 * 60 * 60;
  if (!engine.reloadPolicySigned) {
    // Caller-side check in handleControlEvent should have prevented
    // this; guard defensively so we never silently fall through.
    logger.warn("policy_updated fired without reloadPolicySigned; dropping");
    return;
  }
  try {
    // Call as method so `this` binds to the engine. Earlier code held a
    // bare reference `const fn = engine.reloadPolicySigned; fn({...})`
    // which dropped `this`; the WASM-backed implementation accesses
    // `this.exports.reload_policy_signed` and `this.writeString(...)`
    // internally so calling it unbound threw a `TypeError: Cannot read
    // properties of undefined (reading 'writeString')` at runtime.
    engine.reloadPolicySigned({
      envelopeJson,
      trustedKeysJson,
      nowUnixSecs,
      maxAgeSecs,
    });
  } catch (err) {
    if (err instanceof PolicySignatureError) {
      logger.error(
        "policy_updated bundle rejected by WASM core; previous policy " +
          "remains in effect",
        { code: err.code, ffiCode: err.ffiCode },
      );
      return;
    }
    throw err;
  }
  // The server's `hash` / `active_policy_hash` field is the canonical
  // SHA-256(yaml_content) computed at publish time. The SDK does NOT
  // synthesize a fallback: the only bytes available locally are the
  // DSSE payload (JSON-wrapped PolicyBundle), and SHA-256 of those
  // bytes ≠ SHA-256 of the source YAML — any computed-locally hash
  // would silently mismatch the server's forever, defeating the cache.
  // If `incomingHash` is null (malformed event), the cache stays
  // empty and the next install runs through the FFI normally.
  if (opts.onInstalled && engine.getActivePolicyVersion) {
    try {
      await opts.onInstalled(engine.getActivePolicyVersion(), incomingHash);
    } catch (cbErr) {
      logger.warn("onInstalled callback threw", { err: cbErr });
    }
  }
  logger.warn("policy_updated installed", {
    version: engine.getActivePolicyVersion?.(),
  });
}

/**
 * Install a signed PRICING bundle via the WASM core verifier. The
 * fail-open cost-metering analogue of {@link installSignedPolicy}.
 *
 * Same shape — parse, hash-cache short-circuit, malformed guard, FFI
 * install against the pricing trust list, hash-cache update — but with
 * one deliberate difference that is the whole point of keeping it
 * separate: **it never throws or blocks the host.** Pricing is fail-open
 * metering. A rejected, stale, or rolled-back price table leaves the
 * previous table in effect and logs a structured warning; the worst case
 * is that a call's cost isn't metered, which is never a reason to
 * interrupt the agent (unlike a policy rejection, which is fail-closed).
 *
 * Every WASM rejection surfaces as {@link PricingSignatureError} (FFI
 * `-15`..`-24`); we catch it, log, and return. We do NOT rethrow it the
 * way {@link installSignedPolicy} rethrows non-signature errors, because
 * even a genuinely broken price table must not be allowed to take down
 * request flow.
 */
async function installSignedPricing(
  engine: ControlEngine,
  rawData: string,
  opts: PricingUpdateOptions,
  logger: ControlLogger,
): Promise<void> {
  const data = parseJson(rawData, "pricing_updated", logger);
  if (data === null) return;
  const envelope = (data as { pricing_envelope?: unknown }).pricing_envelope;
  if (envelope === undefined || envelope === null) {
    logger.warn("pricing_updated missing pricing_envelope; ignoring");
    return;
  }

  // Idempotency short-circuit, identical to the policy path. Source
  // ordering: explicit `hash` field (on `pricing_updated`) >
  // `active_pricing_hash` co-field (on `init` / poll) > none (the SDK
  // never synthesizes one — SHA-256 of the DSSE payload is not the
  // server's SHA-256 of the canonical bundle bytes).
  const incomingHashRaw =
    (data as { hash?: unknown; active_pricing_hash?: unknown }).hash ??
    (data as { active_pricing_hash?: unknown }).active_pricing_hash;
  const incomingHash = isHexHash64(incomingHashRaw) ? incomingHashRaw : null;
  const lastHash = opts.getLastHash?.() ?? null;
  if (incomingHash !== null && lastHash !== null && incomingHash === lastHash) {
    return; // idempotent no-op; price table already installed
  }

  const envelopeJson =
    typeof envelope === "string" ? envelope : JSON.stringify(envelope);
  const trustedKeysJson = await opts.loadTrustedKeys();
  const nowUnixSecs =
    (opts.nowUnixSecs ?? ((): number => Math.floor(Date.now() / 1000)))();
  const maxAgeSecs = opts.maxAgeSecs ?? DEFAULT_PRICING_MAX_AGE_SECS;
  if (!engine.reloadPricingSigned) {
    logger.warn(
      "pricing_updated fired without reloadPricingSigned; dropping",
    );
    return;
  }
  try {
    // Call as a method so `this` binds to the engine (the WASM-backed
    // implementation reads `this.exports` / `this.writeString`
    // internally — same binding hazard the policy path documents).
    engine.reloadPricingSigned({
      envelopeJson,
      trustedKeysJson,
      nowUnixSecs,
      maxAgeSecs,
    });
  } catch (err) {
    if (err instanceof PricingSignatureError) {
      // Fail-open: keep the previous price table, log, and carry on.
      // Metrics label by `reason` for cost-metering health dashboards.
      logger.warn(
        "pricing_updated bundle rejected by WASM core; previous price " +
          "table remains in effect (cost metering unaffected)",
        { code: err.code, ffiCode: err.ffiCode, reason: err.reason },
      );
      return;
    }
    // Any OTHER error (e.g. a malformed trust-list JSON that made the
    // FFI throw something unexpected) is still swallowed to a warning:
    // the fail-open contract means the price table's health can never
    // interrupt the host. This is the one place the pricing path
    // deliberately diverges from the policy path's `throw err`.
    logger.warn("pricing_updated install failed; keeping previous price table", {
      err,
    });
    return;
  }
  // Server-canonical hash only (see the policy path for why the SDK does
  // not synthesize one). Null ⇒ leave the cache empty; the next install
  // runs through the FFI.
  if (opts.onInstalled && engine.getActivePricingVersion) {
    try {
      await opts.onInstalled(engine.getActivePricingVersion(), incomingHash);
    } catch (cbErr) {
      logger.warn("pricing onInstalled callback threw", { err: cbErr });
    }
  }
  logger.warn("pricing_updated installed", {
    version: engine.getActivePricingVersion?.(),
  });
}

function isHexHash64(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length === 64 &&
    /^[0-9a-f]{64}$/.test(value)
  );
}

function onPolicyDeactivated(
  engine: ControlEngine,
  logger: ControlLogger,
): void {
  logger.warn(
    "policy deactivated; switching to default-deny until a new policy is installed",
  );
  try {
    engine.reloadPolicy(DEFAULT_DENY_POLICY_JSON);
  } catch (err) {
    if (err instanceof CheckrdInitError) {
      logger.error(
        "failed to install default-deny policy after deactivation (%s); " +
          "the previous policy is still in effect",
        err.message,
      );
      return;
    }
    throw err;
  }
}

function parseJson(
  raw: string,
  eventName: string,
  logger: ControlLogger,
): unknown {
  try {
    return JSON.parse(raw);
  } catch (err) {
    logger.warn(
      "malformed JSON in control event %s: %s",
      eventName,
      err instanceof Error ? err.message : String(err),
    );
    return null;
  }
}

// Re-export the engine type for callers wiring up a real SSE client.
export type { WasmEngine };
