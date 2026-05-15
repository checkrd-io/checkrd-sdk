/**
 * Server-canonical policy bootstrap.
 *
 * On `initAsync()` the SDK fetches the agent's currently-published
 * DSSE-signed policy bundle from `GET /v1/agents/:id/control/state`
 * and installs it via `reload_policy_signed` before returning. Until
 * the bundle is installed, the WASM engine runs the deny-all baseline
 * configured at boot, so every request fails closed — matches OPA's
 * bundle-bootstrap pattern and Envoy xDS's initial-state delivery.
 *
 * The bootstrap fetch is gated on the same hash cache the SSE
 * `ControlReceiver` uses for ongoing updates, so a process restart
 * against an unchanged active bundle is a no-op at the WASM layer.
 */
import { trustedPolicyKeysJson } from "./_trust.js";
import { handleControlEvent, type PolicyUpdateOptions } from "./control.js";
import type { WasmEngine } from "./engine.js";
import type { Logger } from "./_logger.js";
import { defaultControlHeaders } from "./_retry.js";

const DEFAULT_TIMEOUT_MS = 5_000;

/** Options for {@link bootstrapPolicy}. */
export interface BootstrapPolicyOptions {
  /** WASM engine instance. The bundle is installed via `reloadPolicySigned`. */
  engine: WasmEngine;
  /** Control-plane base URL (e.g. `https://api.checkrd.io`). */
  controlPlaneUrl: string;
  /** SDK-scoped API key for this workspace. */
  apiKey: string;
  /** Agent UUID. */
  agentId: string;
  /** Optional logger for bootstrap diagnostics. */
  logger?: Logger | undefined;
  /** Optional `Checkrd-Version` pin. */
  apiVersion?: string | undefined;
  /** HTTP timeout in ms. Defaults to 5 seconds. */
  timeoutMs?: number;
  /** Override for `globalThis.fetch` — test-only. */
  fetch?: typeof fetch;
}

/**
 * Fetch the agent's currently-published signed policy bundle and
 * install it. Mirrors the receiver's `pollStateOnce()` path so the
 * verification, freshness, and hash-cache invariants are identical
 * across the bootstrap and the ongoing-update paths.
 *
 * Fail-closed contract: when the fetch fails, the bundle is malformed,
 * or the server returns nothing, this function logs and returns
 * without installing a policy. The engine continues to run on whatever
 * policy was supplied at boot — typically the deny-all baseline, so
 * every request denies until either a successful bootstrap arrives
 * (next poll cycle) or the SSE receiver delivers a `policy_updated`
 * event.
 *
 * @returns `true` if a bundle was installed, `false` otherwise.
 */
export async function bootstrapPolicy(
  options: BootstrapPolicyOptions,
): Promise<boolean> {
  const {
    engine,
    controlPlaneUrl,
    apiKey,
    agentId,
    logger,
    apiVersion,
    timeoutMs = DEFAULT_TIMEOUT_MS,
  } = options;
  const fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);

  const url =
    controlPlaneUrl.replace(/\/+$/, "") +
    `/v1/agents/${encodeURIComponent(agentId)}/control/state`;
  const versionOpt = apiVersion !== undefined ? { apiVersion } : {};
  const headers = defaultControlHeaders(apiKey, versionOpt);

  const controller = new AbortController();
  const timer = setTimeout(() => {
    controller.abort();
  }, timeoutMs);
  // Watchdog timer for the bootstrap fetch — unref'd so the
  // Node event loop can exit cleanly once the response resolves.
  const nodeTimer = timer as unknown as { unref?: () => void };
  if (typeof nodeTimer.unref === "function") nodeTimer.unref();

  let response: Response;
  try {
    response = await fetchImpl(url, {
      method: "GET",
      headers,
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    logger?.warn(
      "checkrd: policy bootstrap fetch failed — engine remains on deny-all baseline",
      { err, url },
    );
    return false;
  }
  clearTimeout(timer);

  if (!response.ok) {
    logger?.warn(
      `checkrd: policy bootstrap returned HTTP ${response.status.toString()} — engine remains on deny-all baseline`,
    );
    return false;
  }

  let parsed: {
    kill_switch_active?: unknown;
    active_policy_hash?: unknown;
    policy_envelope?: unknown;
  };
  try {
    parsed = (await response.json()) as typeof parsed;
  } catch (err) {
    logger?.warn("checkrd: policy bootstrap response was not JSON", { err });
    return false;
  }

  // Mirror the SSE init handler: stamp the kill-switch first so the
  // engine reflects the live server state even when no policy is
  // published yet.
  if (typeof parsed.kill_switch_active === "boolean") {
    engine.setKillSwitch(parsed.kill_switch_active);
  }

  const envelope = parsed.policy_envelope;
  if (envelope === undefined || envelope === null) {
    logger?.warn(
      "checkrd: control plane has no published policy for this agent — engine remains on deny-all baseline. Publish a policy in the dashboard to enable enforcement.",
    );
    return false;
  }

  // Reuse the same install dispatcher SSE uses so signature
  // verification + monotonic-version + freshness checks run
  // identically across the bootstrap and the streaming paths. We feed
  // the bundle through the `policy_updated` event shape, which is
  // exactly what `handleControlEvent` expects.
  const synthesized = JSON.stringify({
    policy_envelope: envelope,
    active_policy_hash: parsed.active_policy_hash ?? null,
  });
  const policyUpdate: PolicyUpdateOptions = {
    loadTrustedKeys: () => trustedPolicyKeysJson(logger),
  };
  const dispatcherLogger = logger
    ? { warn: logger.warn.bind(logger), error: logger.error.bind(logger) }
    : undefined;
  handleControlEvent(
    engine,
    "policy_updated",
    synthesized,
    dispatcherLogger,
    policyUpdate,
  );
  return true;
}

/**
 * Deny-all baseline policy installed at WASM boot when no local
 * policy is provided. Every request fails closed until the bootstrap
 * fetch installs the server-published bundle.
 *
 * Encoded as JSON so the WASM core's policy parser accepts it
 * unchanged; the wrapper never inspects this string. Keep the
 * structure aligned with the policy schema in `schemas/policy.yaml`.
 */
export const DENY_ALL_BASELINE_POLICY_JSON: string = JSON.stringify({
  agent: "",
  mode: "enforce",
  default: "deny",
  rules: [],
});
