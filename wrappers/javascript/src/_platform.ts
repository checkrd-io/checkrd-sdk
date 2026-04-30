/**
 * Platform / runtime / SDK-version headers stamped on every control-plane
 * request. Mirrors the ``X-Stainless-*`` header family shipped by the
 * OpenAI and Anthropic SDKs; the ``X-Checkrd-SDK-*`` prefix keeps them
 * clearly namespaced to this project and makes them easy to filter in
 * ingestion logs.
 *
 * Why they matter:
 *   - An operator rolling out an SDK upgrade can see "we're seeing old
 *     (<0.3.0) clients still calling" from the dashboard, without asking
 *     every service owner to upgrade (Stripe / OpenAI pattern).
 *   - Runtime-specific bugs (e.g. "Cloudflare Workers pre-May-2026 miss
 *     traceparent") are identifiable without per-customer forensics.
 *   - Supply-chain incidents (a compromised transitive dep that flips the
 *     ``X-Checkrd-SDK-Lang`` to a wrong value) surface loudly.
 *
 * Detection is runtime-agnostic: the Node case reads ``process.versions``
 * / ``process.platform`` / ``process.arch``, other runtimes fall back to
 * ``"unknown"``. Never throws — the telemetry path must not gain a new
 * failure mode because of a platform-detection bug.
 */
import { VERSION } from "./_version.js";

/**
 * Runtime kinds we recognize explicitly. Anything else collapses to
 * ``"unknown"`` so the header is always a known finite set.
 */
export type RuntimeKind =
  | "node"
  | "bun"
  | "deno"
  | "workerd"
  | "edge-light"
  | "browser"
  | "unknown";

/**
 * Snapshot of platform detection, computed once per process and
 * memoized. The object returned by {@link platformHeaders} reads from
 * this snapshot — we do NOT recompute per request because nothing in
 * this module can change at runtime within a single process.
 */
export interface PlatformInfo {
  /** Language: always ``"javascript"`` for this SDK. */
  lang: "javascript";
  /** SDK package version (``VERSION`` constant). */
  sdkVersion: string;
  /** Runtime kind, see {@link RuntimeKind}. */
  runtime: RuntimeKind;
  /** Runtime version string (e.g. ``"20.11.1"``), or ``"unknown"``. */
  runtimeVersion: string;
  /** OS family: ``"linux"`` / ``"darwin"`` / ``"win32"`` / ``"unknown"``. */
  os: string;
  /** CPU architecture: ``"x64"`` / ``"arm64"`` / ``"unknown"``. */
  arch: string;
}

let _cached: PlatformInfo | null = null;

/**
 * Detect the current JS runtime. Cheap but runs on every import path;
 * we memoize via {@link platformInfo}.
 */
function detectRuntime(): { kind: RuntimeKind; version: string } {
  const g = globalThis as unknown as {
    Deno?: { version?: { deno?: string } };
    Bun?: { version?: string };
    EdgeRuntime?: unknown;
    navigator?: { userAgent?: unknown };
    process?: {
      versions?: { node?: string; bun?: string; workerd?: string };
    };
  };

  if (g.Deno?.version?.deno) {
    return { kind: "deno", version: g.Deno.version.deno };
  }
  if (g.Bun?.version) {
    return { kind: "bun", version: g.Bun.version };
  }
  if (g.process?.versions?.workerd) {
    return { kind: "workerd", version: g.process.versions.workerd };
  }
  if (g.EdgeRuntime !== undefined) {
    return { kind: "edge-light", version: "unknown" };
  }
  if (g.process?.versions?.node) {
    return { kind: "node", version: g.process.versions.node };
  }
  // A real browser is an acknowledged-dangerous deployment (see
  // `isRealBrowser` in index.ts) but still a valid runtime label —
  // operators may want to see these to decide whether to block.
  if (typeof g.navigator?.userAgent === "string") {
    return { kind: "browser", version: "unknown" };
  }
  return { kind: "unknown", version: "unknown" };
}

/**
 * Return the memoized {@link PlatformInfo}. Computed on first call,
 * reused thereafter. Safe on every runtime — never throws.
 */
export function platformInfo(): PlatformInfo {
  if (_cached !== null) return _cached;
  const rt = detectRuntime();
  const proc = (globalThis as unknown as {
    process?: { platform?: string; arch?: string };
  }).process;
  _cached = {
    lang: "javascript",
    sdkVersion: VERSION,
    runtime: rt.kind,
    runtimeVersion: rt.version,
    os: proc?.platform ?? "unknown",
    arch: proc?.arch ?? "unknown",
  };
  return _cached;
}

/**
 * Reset the memoized snapshot. Test-only — production code has no
 * reason to refresh platform detection mid-process.
 */
export function __resetPlatformInfoForTesting(): void {
  _cached = null;
}

/**
 * Return the platform headers that should be attached to every
 * control-plane request. All six are always set — a missing value is
 * sent as ``"unknown"`` rather than omitted so ingestion can
 * distinguish "old SDK that didn't send the header" from "newer SDK
 * that couldn't detect the field".
 *
 * Header names mirror ``X-Stainless-*`` semantics but live under
 * ``X-Checkrd-SDK-*`` to keep the namespace clean and greppable.
 */
export function platformHeaders(
  info: PlatformInfo = platformInfo(),
): Record<string, string> {
  return {
    "X-Checkrd-SDK-Lang": info.lang,
    "X-Checkrd-SDK-Version": info.sdkVersion,
    "X-Checkrd-SDK-Runtime": info.runtime,
    "X-Checkrd-SDK-Runtime-Version": info.runtimeVersion,
    "X-Checkrd-SDK-OS": info.os,
    "X-Checkrd-SDK-Arch": info.arch,
  };
}
