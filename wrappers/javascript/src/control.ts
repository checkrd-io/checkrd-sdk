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
 *   - `policy_updated`     → reserved (signed bundle install path)
 *   - `policy_deactivated` → install local default-deny policy
 *
 * Locally constructed default-deny is consistent with the SDK's "no
 * unsigned distribution path" rule because the bytes never traverse the
 * network — same justification as `setKillSwitch(true)`.
 */

import type { WasmEngine } from "./engine.js";
import { CheckrdInitError, PolicySignatureError } from "./exceptions.js";

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

/** Wire-format event names emitted by the control plane SSE stream. */
export type ControlEventName =
  | "init"
  | "kill_switch"
  | "policy_updated"
  | "policy_deactivated";

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
          return true;
        }
        const wrapped = JSON.stringify({ policy_envelope: initEnvelope });
        void installSignedPolicy(engine, wrapped, policyUpdate, logger).catch(
          (err: unknown) => {
            logger.error("init policy install failed", err);
          },
        );
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
  const reloadSigned = engine.reloadPolicySigned;
  if (!reloadSigned) {
    // Caller-side check in handleControlEvent should have prevented
    // this; guard defensively so we never silently fall through.
    logger.warn("policy_updated fired without reloadPolicySigned; dropping");
    return;
  }
  try {
    reloadSigned({
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
