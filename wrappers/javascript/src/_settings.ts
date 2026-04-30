/**
 * Settings resolution mirroring `checkrd/_settings.py`. Precedence:
 * explicit argument → environment variable → derived default.
 *
 * The agent-ID fallback chain recognizes common serverless and container
 * platforms (Vercel, Cloudflare, Fly, Cloud Run, Lambda, Kubernetes) so
 * that a user who never sets `CHECKRD_AGENT_ID` still gets a stable,
 * human-readable identifier on the dashboard.
 */

/** Strict = fail-closed. Permissive = degrade to pass-through on engine error. */
export type SecurityMode = "strict" | "permissive";

/**
 * Enforcement mode — three values:
 *
 *   - `true`: always block on engine deny. Operator override.
 *   - `false`: never block; record deny verdicts in telemetry only.
 *     Use for phased rollouts where you want to log "would have been
 *     denied" without breaking real traffic.
 *   - `"auto"` (default): trust the engine. Block on engine deny;
 *     since the WASM engine respects the policy's `mode` field
 *     internally (`mode: dry_run` returns allowed=true even on a
 *     deny match), this never over-enforces. Mirrors how every
 *     comparable enforcement point handles it (OPA-PEP, Envoy
 *     ext_authz, Stripe Radar, AWS Config, Cloudflare WAF — policy
 *     carries the mode, the enforcement point trusts the verdict).
 */
export type EnforceMode = "auto" | boolean;

/**
 * Named deployment environments. Each maps to a default control-plane
 * URL when no explicit URL is provided. An explicit `controlPlaneUrl`
 * always wins — the environment is a convenience for common cases.
 */
export type Environment = "production" | "staging" | "development";

/** Default control-plane URL per {@link Environment}. */
export const ENVIRONMENT_URLS: Readonly<Record<Environment, string>> = Object.freeze({
  production: "https://api.checkrd.io",
  staging: "https://api-staging.checkrd.io",
  development: "http://localhost:8080",
});

/** Snapshot of resolved settings. Frozen after construction. */
export interface Settings {
  /** Stable identifier for this agent, shown on the dashboard. */
  agentId: string;
  /** Base URL of the control plane. Empty string when running offline. */
  controlPlaneUrl: string;
  /** Dashboard URL used for error-message deep links. */
  dashboardUrl: string;
  /** API key for control-plane ingestion. Empty string when offline. */
  apiKey: string;
  /** Whether init()/wrap() should be a no-op (CHECKRD_DISABLED=1). */
  disabled: boolean;
  /** Whether debug-level logging is enabled. */
  debug: boolean;
  /** Explicit enforcement override, if set. null = use auto-detection. */
  enforceOverride: boolean | null;
  /** Fail-closed vs. degrade-to-pass-through. */
  securityMode: SecurityMode;
  /** True when the control plane is reachable via controlPlaneUrl + apiKey. */
  hasControlPlane: boolean;
  /** Resolved environment. `null` when the caller set `controlPlaneUrl` directly. */
  environment: Environment | null;
  /**
   * API pin for the control plane. Sent on every request as the
   * `Checkrd-Version` header (Stripe pattern — floor match, forward-
   * only). Empty string means "follow the server default".
   */
  apiVersion: string;
  /**
   * Fraction of allowed telemetry events to emit, in `[0, 1]`. Denied
   * events always emit regardless. Default `1.0` (emit everything).
   */
  samplingRate: number;
}

/** Options for {@link resolve}. All fields are optional overrides. */
export interface ResolveOptions {
  agentId?: string | undefined;
  apiKey?: string | undefined;
  controlPlaneUrl?: string | undefined;
  enforce?: EnforceMode | undefined;
  debug?: boolean | undefined;
  securityMode?: SecurityMode | undefined;
  /** Named environment; overridden by explicit `controlPlaneUrl`. */
  environment?: Environment | undefined;
  /** API version pin (e.g. `"2026-04-01"`). */
  apiVersion?: string | undefined;
  /** Telemetry sampling rate for allowed events, in `[0, 1]`. */
  samplingRate?: number | undefined;
}

import { readEnv as env } from "./_env.js";

/**
 * Parse a boolean-flavored env var. Only exact "1" counts as truthy;
 * everything else is falsy. Security-critical flags
 * (`CHECKRD_SKIP_WASM_INTEGRITY`, `CHECKRD_ALLOW_INSECURE_HTTP`) ride on
 * this helper, so loose variants ("TRUE", " 1\n") don't accidentally
 * disable a defense the operator didn't mean to turn off.
 */
const envBool = (name: string): boolean => env(name) === "1";

/**
 * Hostnames that are never legitimate targets for a control plane URL.
 * These are the classic SSRF sentinels: loopback, link-local, and
 * broadcast. We keep the check string-based rather than parsing as IP
 * because the `URL` constructor in Node normalizes these spellings
 * before we see them ("127.1", "0x7f.1" → "127.0.0.1").
 */
const BLOCKED_HOSTS: ReadonlySet<string> = new Set([
  "localhost",
  "127.0.0.1",
  "::1",
  "0.0.0.0",
  "169.254.169.254", // AWS / GCP / Azure instance metadata
]);

/**
 * Validate a user-supplied control-plane URL. Rejects schemes other
 * than http/https, empty hostnames, and SSRF-adjacent hosts unless the
 * operator explicitly opts in via `CHECKRD_ALLOW_INSECURE_HTTP=1`.
 *
 * Returns the canonical URL (trailing slash trimmed). Throws on any
 * failure so misconfigurations surface at resolve time rather than
 * silently pointing telemetry at the wrong host.
 */
function validateControlPlaneUrl(raw: string, allowInsecure: boolean): string {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error(
      `CHECKRD_BASE_URL is not a valid URL (got ${JSON.stringify(raw)})`,
    );
  }
  const scheme = parsed.protocol.replace(":", "").toLowerCase();
  if (scheme !== "https" && !(scheme === "http" && allowInsecure)) {
    throw new Error(
      `CHECKRD_BASE_URL must use https:// (got ${scheme}://). ` +
        "Set CHECKRD_ALLOW_INSECURE_HTTP=1 only for local development.",
    );
  }
  const host = parsed.hostname.toLowerCase();
  if (host.length === 0) {
    throw new Error("CHECKRD_BASE_URL has no hostname");
  }
  if (BLOCKED_HOSTS.has(host) && !allowInsecure) {
    throw new Error(
      `CHECKRD_BASE_URL points at a blocked host (${host}). ` +
        "Set CHECKRD_ALLOW_INSECURE_HTTP=1 for local development.",
    );
  }
  return raw.replace(/\/$/, "");
}

function deriveAgentId(): string {
  // Each branch mirrors a platform convention used by the Python SDK:
  // same identifiers, same ordering, so an agent that bounces between
  // SDKs gets the same label.
  const fromEnv = env("CHECKRD_AGENT_ID");
  if (fromEnv) return fromEnv;
  const platform =
    env("VERCEL_URL") ??
    env("CF_PAGES_URL") ??
    env("FLY_APP_NAME") ??
    env("K_SERVICE") ??
    env("AWS_LAMBDA_FUNCTION_NAME") ??
    env("KUBERNETES_POD_NAME");
  if (platform) return platform;
  // Script-name + host fallback: deterministic within a machine, avoids
  // the "random UUID on every process start" anti-pattern that breaks
  // telemetry correlation across restarts.
  const script =
    (process.argv[1]?.split("/").pop()) ?? "node";
  const host = env("HOSTNAME") ?? "localhost";
  return `${script}-${host}`;
}

function resolveEnvironment(
  explicit: Environment | undefined,
): Environment | null {
  if (explicit) return explicit;
  const raw = env("CHECKRD_ENVIRONMENT");
  if (raw === "production" || raw === "staging" || raw === "development") {
    return raw;
  }
  return null;
}

function clampSamplingRate(raw: number | undefined, envRaw: string | undefined): number {
  const candidate = raw ?? (envRaw !== undefined ? Number.parseFloat(envRaw) : NaN);
  if (!Number.isFinite(candidate)) return 1;
  if (candidate < 0) return 0;
  if (candidate > 1) return 1;
  return candidate;
}

/** Resolve final settings from options + env. */
export function resolve(opts: ResolveOptions = {}): Settings {
  const agentId = opts.agentId ?? deriveAgentId();
  const environment = resolveEnvironment(opts.environment);
  const environmentDefault = environment !== null ? ENVIRONMENT_URLS[environment] : "";
  const rawControlPlaneUrl =
    opts.controlPlaneUrl ?? env("CHECKRD_BASE_URL") ?? environmentDefault;
  const apiKey = opts.apiKey ?? env("CHECKRD_API_KEY") ?? "";
  const disabled = envBool("CHECKRD_DISABLED");
  const debug = (opts.debug ?? false) || envBool("CHECKRD_DEBUG");
  const allowInsecureHttp = envBool("CHECKRD_ALLOW_INSECURE_HTTP");
  const securityMode: SecurityMode =
    opts.securityMode ??
    (env("CHECKRD_SECURITY_MODE") === "permissive" ? "permissive" : "strict");

  let enforceOverride: boolean | null = null;
  if (opts.enforce === true) enforceOverride = true;
  else if (opts.enforce === false) enforceOverride = false;
  else {
    const envEnforce = env("CHECKRD_ENFORCE");
    if (envEnforce === "1") enforceOverride = true;
    else if (envEnforce === "0") enforceOverride = false;
  }

  const controlPlaneUrl =
    rawControlPlaneUrl.length > 0
      ? validateControlPlaneUrl(rawControlPlaneUrl, allowInsecureHttp)
      : "";
  const dashboardUrl = controlPlaneUrl;

  return Object.freeze({
    agentId,
    controlPlaneUrl,
    dashboardUrl,
    apiKey,
    disabled,
    debug,
    enforceOverride,
    securityMode,
    hasControlPlane: controlPlaneUrl.length > 0 && apiKey.length > 0,
    environment,
    apiVersion: opts.apiVersion ?? env("CHECKRD_API_VERSION") ?? "",
    samplingRate: clampSamplingRate(opts.samplingRate, env("CHECKRD_SAMPLING_RATE")),
  });
}
