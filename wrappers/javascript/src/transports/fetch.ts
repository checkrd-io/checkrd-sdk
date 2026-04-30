/**
 * Fetch interception — the JS analogue of `transports/_httpx.py`.
 *
 * Exposes `wrapFetch()` which returns a drop-in replacement for the
 * global `fetch`. Every modern LLM SDK (OpenAI v4+, Anthropic, Cohere,
 * Mistral, Groq, Together, Google GenAI) accepts a custom `fetch`
 * through its client constructor, so the interception model is uniform:
 * the SDK thinks it's talking to upstream; the wrapped fetch runs
 * policy evaluation first, forwards on allow, and tees streaming
 * responses for token-usage telemetry.
 */
import { CheckrdPolicyDenied } from "../exceptions.js";
import { CHECKRD_REQUEST_ID, extractTraceId } from "../hooks.js";
import type {
  BeforeRequestHook,
  CheckrdEvent,
  OnAllowHook,
  OnDenyHook,
} from "../hooks.js";
import type { EvaluateRequest, WasmEngine } from "../engine.js";
import type { TelemetrySink, TelemetryEvent } from "../sinks.js";
import { attributesForUrl } from "../_genai.js";
import type { Logger } from "../_logger.js";
import { isSensitiveHeader, REDACTED } from "../_sensitive.js";
import type { SecurityMode } from "../_settings.js";
import { teeResponseForTokens, vendorForUrl } from "../_stream_capture.js";

/** Native fetch type alias — the shape every LLM SDK accepts. */
export type FetchFn = typeof fetch;

/** Options for {@link wrapFetch}. */
export interface WrapFetchOptions {
  /** Policy engine instance — typically the one from `init()` or `wrap()`. */
  engine: WasmEngine;
  /** True = raise on deny; false = log only. */
  enforce: boolean;
  /** Agent identifier used for telemetry correlation. */
  agentId: string;
  /** Base dashboard URL, used in error messages for deep links. */
  dashboardUrl?: string;
  /** Called before policy evaluation; return `false` to short-circuit. */
  beforeRequest?: BeforeRequestHook | undefined;
  /** Called after an allowed evaluation. */
  onAllow?: OnAllowHook | undefined;
  /** Called after a denied evaluation (regardless of enforce flag). */
  onDeny?: OnDenyHook | undefined;
  /** Optional sink for telemetry events. When present, evaluate() events and stream-usage events are enqueued here. */
  sink?: TelemetrySink | undefined;
  /** Optional logger for observability of the transport itself. */
  logger?: Logger | undefined;
  /**
   * Fail-closed posture on boundary conditions the WASM engine cannot
   * see. Default `"strict"` denies requests whose body exceeds the 1 MB
   * inspection limit (matcher-bypass defense). `"permissive"` logs a
   * warning and passes the request through with body matchers
   * effectively disabled — only for controlled rollouts.
   */
  securityMode?: SecurityMode | undefined;
}

/** 1 MB body cap — matches the Python wrapper to prevent matcher bypass. */
const MAX_BODY_BYTES = 1024 * 1024;

function redactHeaders(
  headers: Headers,
): [string, string][] {
  const out: [string, string][] = [];
  headers.forEach((value, key) => {
    out.push([key, isSensitiveHeader(key) ? REDACTED : value]);
  });
  return out;
}

function rawHeaders(headers: Headers): [string, string][] {
  const out: [string, string][] = [];
  headers.forEach((value, key) => {
    out.push([key, value]);
  });
  return out;
}

/**
 * Outcome of a body extraction attempt. We distinguish "no body" (nothing
 * to inspect) from "oversized body" (inspection limit exceeded) because
 * the policy-evaluation layer must behave differently: the first is
 * benign, the second is a potential matcher bypass and — in strict mode
 * — causes the request to be denied before it ever reaches the engine.
 */
type BodyOutcome =
  | { kind: "empty" }
  | { kind: "inline"; text: string }
  | { kind: "unreadable" }
  | { kind: "oversized"; byteLength: number };

async function extractBody(request: Request): Promise<BodyOutcome> {
  if (request.body === null) return { kind: "empty" };
  let text: string;
  try {
    const cloned = request.clone();
    text = await cloned.text();
  } catch {
    // Body already consumed or not text — treat as unreadable so hooks
    // and policy see a null body rather than a partial one.
    return { kind: "unreadable" };
  }
  if (text.length === 0) return { kind: "empty" };
  // Measure in *bytes*, not UTF-16 code units. `String.prototype.length`
  // undercounts any character outside the BMP (emoji, astral-plane CJK,
  // supplementary-plane code points); an attacker who can encode the
  // payload in 4-byte UTF-8 characters could otherwise pass up to ~4× the
  // documented 1 MB limit through the body-matcher inspection cap.
  const byteLength = new TextEncoder().encode(text).length;
  if (byteLength > MAX_BODY_BYTES) return { kind: "oversized", byteLength };
  return { kind: "inline", text };
}

function enqueueEvalEvent(
  sink: TelemetrySink | undefined,
  logger: Logger | undefined,
  telemetryJson: string,
  agentId: string,
): void {
  if (!sink || telemetryJson.length === 0) return;
  try {
    const event = JSON.parse(telemetryJson) as TelemetryEvent;
    event.agent_id = agentId;
    // Stamp the OTel GenAI semantic conventions
    // (``gen_ai.provider.name``, ``gen_ai.operation.name``) when the
    // request URL matches a known LLM endpoint. Performed once here —
    // not in each vendor instrumentor — so AWS Bedrock, Azure OpenAI,
    // Vertex AI, and any future provider all benefit without per-vendor
    // glue. Mirrors the same enrichment in the Python httpx transport.
    const request = (event as { request?: { url_host?: unknown; url_path?: unknown } })
      .request;
    if (request !== undefined) {
      const host = typeof request.url_host === "string" ? request.url_host : "";
      const path = typeof request.url_path === "string" ? request.url_path : "";
      const attrs = attributesForUrl(host, path);
      Object.assign(event, attrs);
    }
    sink.enqueue(event);
  } catch (err) {
    logger?.debug("failed to parse telemetry_json from engine", { err });
  }
}

/**
 * Wrap a base fetch function with Checkrd policy enforcement.
 *
 * Returns a new fetch-shaped function. Calling it runs the request
 * through the engine, dispatches hooks, and either forwards to
 * `baseFetch` (allow) or throws {@link CheckrdPolicyDenied} (deny +
 * enforce). With enforce=false, deny only logs.
 */
export function wrapFetch(
  baseFetch: FetchFn,
  options: WrapFetchOptions,
): FetchFn {
  const {
    engine,
    enforce,
    agentId,
    dashboardUrl = "",
    beforeRequest,
    onAllow,
    onDeny,
    sink,
    logger,
    securityMode = "strict",
  } = options;

  return async function checkrdFetch(
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    const request =
      input instanceof Request ? input.clone() : new Request(input, init);
    const requestId = globalThis.crypto.randomUUID();
    const url = request.url;
    const method = request.method.toUpperCase();
    const headersRaw = rawHeaders(request.headers);
    const bodyOutcome = await extractBody(request);

    // Strict mode fails closed on bodies that exceed the WASM inspection
    // limit. A body matcher that evaluates against `null` is trivially
    // bypassed by padding the payload beyond 1 MB — treat this as a
    // denial condition before the engine is even asked.
    if (bodyOutcome.kind === "oversized" && enforce && securityMode === "strict") {
      logger?.warn("request denied: body exceeds 1MB inspection limit", {
        requestId,
        url,
        byteLength: bodyOutcome.byteLength,
      });
      throw new CheckrdPolicyDenied({
        reason: "body exceeds 1MB inspection limit",
        requestId,
        url,
        dashboardUrl,
      });
    }

    const body: string | null =
      bodyOutcome.kind === "inline" ? bodyOutcome.text : null;
    const startMs = Date.now();

    const traceId = extractTraceId(request.headers);
    if (beforeRequest) {
      const event: CheckrdEvent = {
        method,
        url,
        requestId,
        headers: redactHeaders(request.headers),
      };
      if (body !== null) event.body = body;
      if (traceId !== undefined) event.traceId = traceId;
      const result = beforeRequest(event);
      if (result === false) {
        throw new CheckrdPolicyDenied({
          reason: "short-circuited by before_request hook",
          requestId,
          url,
          dashboardUrl,
        });
      }
    }

    const now = new Date();
    const evalReq: EvaluateRequest = {
      request_id: requestId,
      method,
      url,
      headers: headersRaw,
      body,
      timestamp: now.toISOString(),
      timestamp_ms: now.getTime(),
    };
    const result = engine.evaluate(evalReq);
    enqueueEvalEvent(sink, logger, result.telemetry_json, agentId);

    const redacted = redactHeaders(request.headers);
    const event: CheckrdEvent = {
      method,
      url,
      requestId: result.request_id,
      headers: redacted,
      allowed: result.allowed,
    };
    if (body !== null) event.body = body;
    if (result.deny_reason !== undefined) event.denyReason = result.deny_reason;
    if (traceId !== undefined) event.traceId = traceId;

    if (result.allowed) {
      if (onAllow) {
        try {
          onAllow(event);
        } catch {
          // User hook errors must never crash the request path.
        }
      }
      const response = await baseFetch(request);
      // Stamp the SDK's correlation request-id on the response via
      // a Symbol-keyed property so the caller can paste it into a
      // support ticket without re-instrumenting the request path.
      // OpenAI Node uses a string ``_request_id`` for the same
      // purpose; Symbol form avoids colliding with vendor SDK fields.
      attachRequestId(response, result.request_id);
      // If the response is an SSE stream from OpenAI/Anthropic and we
      // have a sink, tee it to count tokens. vendor=unknown responses
      // pass through unchanged.
      const vendor = vendorForUrl(url);
      if (sink && vendor !== "unknown") {
        const teed = teeResponseForTokens(response, {
          vendor,
          requestId: result.request_id,
          url,
          method,
          agentId,
          sink,
          startMs,
          ...(logger !== undefined ? { logger } : {}),
        });
        // ``teeResponseForTokens`` builds a fresh ``Response`` for the
        // consumer side; re-stamp the request-id on the new instance
        // so the wrapped flow is observable end-to-end.
        if (teed !== response) attachRequestId(teed, result.request_id);
        return teed;
      }
      return response;
    }

    if (onDeny) {
      try {
        onDeny(event);
      } catch {
        // same rationale as onAllow above
      }
    }
    logger?.warn("request denied by policy", {
      requestId: result.request_id,
      url,
      reason: result.deny_reason,
    });
    if (enforce) {
      throw new CheckrdPolicyDenied({
        reason: result.deny_reason ?? "policy denied",
        requestId: result.request_id,
        url,
        dashboardUrl,
      });
    }
    // Observe-only: forward the request anyway, but telemetry has
    // already recorded the denial.
    const response = await baseFetch(request);
    attachRequestId(response, result.request_id);
    return response;
  };
}

/**
 * Stamp the SDK's correlation request-id on a ``Response`` via the
 * cross-realm Symbol exported from ``hooks.ts``. ``Object.defineProperty``
 * (rather than direct assignment) so the property is non-enumerable
 * and doesn't pollute ``JSON.stringify(response)`` output that some
 * vendor SDKs rely on for response-shape detection.
 */
function attachRequestId(response: Response, requestId: string): void {
  try {
    Object.defineProperty(response, CHECKRD_REQUEST_ID, {
      value: requestId,
      writable: false,
      enumerable: false,
      configurable: true,
    });
  } catch {
    // ``Response`` instances are normal objects in every supported
    // runtime, so ``defineProperty`` should never throw — but if a
    // future runtime makes them frozen, we don't want the stamp
    // failure to cascade into a request error.
  }
}
