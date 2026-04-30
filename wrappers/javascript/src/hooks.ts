/**
 * Callback hook types mirroring `checkrd/hooks.py`. Hook implementations
 * must be pure and non-blocking — they run on the request critical path.
 */

/** Metadata passed to every hook. Headers are redacted before they land here. */
export interface CheckrdEvent {
  /** HTTP method (upper-case, e.g., "GET"). */
  method: string;
  /** Full request URL. */
  url: string;
  /** Whether the request was allowed (undefined for before_request). */
  allowed?: boolean;
  /** Deny reason, set only when allowed is false. */
  denyReason?: string;
  /** Correlation ID. */
  requestId: string;
  /** Request body, if available and under 1 MB. */
  body?: string;
  /** Request headers with auth-related values redacted. */
  headers: [string, string][];
  /**
   * W3C Trace Context trace-id (32 lowercase hex chars), extracted
   * from the request's ``traceparent`` header when present so hook
   * callers can correlate the SDK's policy decision with the user's
   * distributed-trace span. ``undefined`` when the request carries
   * no ``traceparent``. Mirrors the value the telemetry batcher
   * stamps on its ``POST /v1/telemetry`` so a single ``trace_id``
   * spans agent code → policy eval → telemetry ingestion → ClickHouse.
   */
  traceId?: string;
}

/**
 * Cross-realm Symbol used as the property key for the SDK's
 * correlation request-id on every wrapped fetch ``Response``. Callers
 * read it to tie a specific call to a telemetry event without
 * re-instrumenting the request path:
 *
 *     const response = await myFetch(url, init);
 *     const requestId = (response as Response & { [CHECKRD_REQUEST_ID]?: string })[CHECKRD_REQUEST_ID];
 *     // → paste into a Checkrd support ticket and the on-call can
 *     //   locate the exact policy evaluation row.
 *
 * Symbol.for("checkrd.request_id") makes the same Symbol resolvable
 * from Workers, dynamic imports, and other-realm code paths so a
 * caller using a different SDK instance still gets the same key.
 * Mirrors the OpenAI Node SDK's ``_request_id`` convention but uses
 * a Symbol (not a string property) to avoid colliding with vendor
 * SDK fields.
 */
export const CHECKRD_REQUEST_ID = Symbol.for("checkrd.request_id");

/**
 * Type alias for a ``Response`` carrying Checkrd's request-id. Use
 * when you need the strict type of a wrapped response without the
 * cast at the call site:
 *
 *     const response: CheckrdResponse = await myFetch(url, init);
 *     const id = response[CHECKRD_REQUEST_ID];
 */
export type CheckrdResponse = Response & {
  readonly [CHECKRD_REQUEST_ID]?: string;
};

/**
 * Extract the trace-id (32 hex chars) from a W3C ``traceparent``
 * header. Returns ``undefined`` for any malformed input rather than
 * throwing — a bad header from customer code must never break the
 * request path.
 *
 * Format: ``{version}-{trace-id}-{parent-id}-{flags}`` with
 * ``version=00`` for the current spec. We accept the version field
 * but drop traces from any other version (forward-compatible
 * extraction can't reliably parse unknown versions).
 */
export function extractTraceId(
  headers: [string, string][] | Headers,
): string | undefined {
  let raw: string | null | undefined;
  if (headers instanceof Headers) {
    raw = headers.get("traceparent");
  } else {
    for (const [k, v] of headers) {
      if (k.toLowerCase() === "traceparent") {
        raw = v;
        break;
      }
    }
  }
  if (raw === null || raw === undefined || typeof raw !== "string") return undefined;
  const parts = raw.split("-");
  if (parts.length !== 4) return undefined;
  const [version, traceId] = parts as [string, string, string, string];
  if (version !== "00") return undefined;
  if (traceId.length !== 32 || !/^[0-9a-f]{32}$/.test(traceId)) return undefined;
  // All-zero trace-id is invalid per W3C spec.
  if (traceId === "0".repeat(32)) return undefined;
  return traceId;
}

/** Fires after policy evaluation if the request was allowed. Must return quickly. */
export type OnAllowHook = (event: CheckrdEvent) => void;

/**
 * Fires after policy evaluation if the request was denied. Must return quickly.
 * Enforcement is already decided by this point; the hook cannot reverse it.
 */
export type OnDenyHook = (event: CheckrdEvent) => void;

/**
 * Fires before policy evaluation. Return `false` to short-circuit the
 * request (it will not be evaluated or forwarded); return `true` or
 * `undefined` to proceed normally.
 */
export type BeforeRequestHook = (event: CheckrdEvent) => boolean | undefined;
