/**
 * OTLP/HTTP-JSON telemetry exporter.
 *
 * Mirrors Python's {@link OtlpSink} but implemented directly on top of
 * `fetch` + the OTLP/HTTP JSON wire format. No dependency on the
 * OpenTelemetry SDK or any protobuf library — the translation from
 * Checkrd's {@link TelemetryEvent} shape to the OTLP span JSON is a
 * few dozen lines and keeps the wrapper lean.
 *
 * Works on every runtime the SDK targets: Node, Bun, Deno, Cloudflare
 * Workers, Vercel Edge, browsers (where allowed). Pushes to any
 * endpoint that speaks OTLP/HTTP JSON on `POST /v1/traces`:
 *
 *   - Datadog Agent / Agentless OTLP endpoints
 *   - Honeycomb (api.honeycomb.io:443/v1/traces)
 *   - Grafana Cloud OTLP gateway
 *   - OpenTelemetry Collector (HTTP receiver)
 *   - Axiom, New Relic, Uptrace, and any other OTLP/HTTP-JSON sink
 *
 * Specification reference:
 *   https://opentelemetry.io/docs/specs/otlp/#otlphttp
 *   https://github.com/open-telemetry/opentelemetry-proto/blob/main/opentelemetry/proto/trace/v1/trace.proto
 */

import { fetchWithRetry } from "./_retry.js";
import type { Logger } from "./_logger.js";
import { scrubTelemetryEvent } from "./_sensitive.js";
import type { TelemetryEvent, TelemetrySink } from "./sinks.js";

/** Options for {@link OtlpSink}. */
export interface OtlpSinkOptions {
  /**
   * OTLP/HTTP-JSON endpoint URL. The sink appends `/v1/traces` if the
   * path is omitted, matching the convention of the OTel SDK and every
   * major collector. Accept either:
   *
   *   - a bare host (`https://otlp.datadoghq.com`)
   *   - a full path (`https://api.honeycomb.io/v1/traces`)
   */
  endpoint: string;
  /**
   * Auth + routing headers. Examples:
   *
   *   - `{ "DD-API-KEY": "..." }` for Datadog
   *   - `{ "x-honeycomb-team": "..." }` for Honeycomb
   *   - `{ "Authorization": "Bearer ..." }` for Grafana Cloud
   */
  headers?: Record<string, string>;
  /** Resource `service.name`. Default: `"checkrd-agent"`. */
  serviceName?: string;
  /** Max spans per batch before a forced flush. Default: 512. */
  maxBatchSize?: number;
  /** Auto-flush interval in ms. Default: 5000. */
  flushIntervalMs?: number;
  /** Per-flush HTTP timeout in ms. Default: 30_000. */
  timeoutMs?: number;
  /** Diagnostic logger. */
  logger?: Logger;
  /** Fetch override — primarily for tests. */
  fetch?: typeof fetch;
}

/** OpenTelemetry span kind: `CLIENT`. Outbound HTTP requests are clients. */
const SPAN_KIND_CLIENT = 3;

/**
 * Sink that translates Checkrd telemetry events into OTLP traces and
 * POSTs them to any OTLP/HTTP-JSON endpoint. Batches internally — calls
 * to {@link enqueue} are non-blocking.
 */
export class OtlpSink implements TelemetrySink {
  private readonly endpoint: string;
  private readonly headers: Record<string, string>;
  private readonly serviceName: string;
  private readonly maxBatchSize: number;
  private readonly flushIntervalMs: number;
  private readonly timeoutMs: number;
  private readonly logger: Logger | undefined;
  private readonly fetchImpl: typeof fetch;

  private buffer: TelemetryEvent[] = [];
  private flushTimer: ReturnType<typeof setInterval> | null = null;
  private stopped = false;
  private inFlight: Promise<void> | null = null;

  constructor(opts: OtlpSinkOptions) {
    this.endpoint = normaliseEndpoint(opts.endpoint);
    this.headers = {
      "Content-Type": "application/json",
      ...(opts.headers ?? {}),
    };
    this.serviceName = opts.serviceName ?? "checkrd-agent";
    this.maxBatchSize = opts.maxBatchSize ?? 512;
    this.flushIntervalMs = opts.flushIntervalMs ?? 5000;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.logger = opts.logger;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);

    // Schedule the periodic flush. `unref()` on Node keeps the sink
    // from pinning the event loop open; edge runtimes do not expose
    // it, which is harmless.
    this.flushTimer = setInterval(() => {
      void this.flush();
    }, this.flushIntervalMs);
    const timer = this.flushTimer as unknown as { unref?: () => void };
    timer.unref?.();
  }

  enqueue(event: TelemetryEvent): void {
    if (this.stopped) return;
    // Scrub on the way in — before the event ever reaches the buffer,
    // the OTLP serializer, or an in-process observer. Means a `flush()`
    // failure or an unexpected buffer read can't expose unredacted
    // data. Cost: one recursive pass + one URL parse per event (the
    // fast-path in `scrubUrl` skips URL parsing when no `?` is present,
    // which covers most telemetry events).
    this.buffer.push(scrubTelemetryEvent(event));
    if (this.buffer.length >= this.maxBatchSize) {
      void this.flush();
    }
  }

  /**
   * Flush the current buffer to the OTLP endpoint. Serialises concurrent
   * calls: if a flush is already in flight, subsequent calls await it
   * rather than interleaving POSTs (which would scramble span ordering
   * at observability back-ends that sort by arrival time).
   */
  async flush(): Promise<void> {
    if (this.inFlight) {
      await this.inFlight;
      return;
    }
    if (this.buffer.length === 0) return;
    const batch = this.buffer;
    this.buffer = [];

    const payload = eventsToOtlpJson(batch, this.serviceName);
    this.inFlight = this.doFlush(payload, batch.length);
    try {
      await this.inFlight;
    } finally {
      this.inFlight = null;
    }
  }

  private async doFlush(payload: string, size: number): Promise<void> {
    try {
      const res = await fetchWithRetry(this.endpoint, {
        method: "POST",
        headers: this.headers,
        body: payload,
      }, {
        fetch: this.fetchImpl,
        timeoutMs: this.timeoutMs,
        ...(this.logger !== undefined ? { logger: this.logger } : {}),
      });
      if (!res.ok) {
        this.logger?.warn("checkrd: OtlpSink flush rejected", {
          status: res.status,
          size,
        });
      }
    } catch (err) {
      this.logger?.warn("checkrd: OtlpSink flush failed", { err, size });
    }
  }

  async close(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    if (this.flushTimer !== null) {
      clearInterval(this.flushTimer);
      this.flushTimer = null;
    }
    await this.flush();
  }
}

/** Append `/v1/traces` if the caller passed a bare endpoint URL. */
function normaliseEndpoint(endpoint: string): string {
  const trimmed = endpoint.replace(/\/$/, "");
  if (/\/v\d+\/traces$/.test(trimmed)) return trimmed;
  return `${trimmed}/v1/traces`;
}

/**
 * Translate a batch of Checkrd events into an OTLP/HTTP JSON payload.
 * Exported only for testing; consumers interact with the sink above.
 */
export function eventsToOtlpJson(
  events: TelemetryEvent[],
  serviceName: string,
): string {
  const spans = events.map(eventToSpan);
  const payload = {
    resourceSpans: [
      {
        resource: {
          attributes: [
            {
              key: "service.name",
              value: { stringValue: serviceName },
            },
            {
              key: "telemetry.sdk.name",
              value: { stringValue: "checkrd" },
            },
          ],
        },
        scopeSpans: [
          {
            scope: { name: "checkrd.otlp_sink" },
            spans,
          },
        ],
      },
    ],
  };
  return JSON.stringify(payload);
}

interface OtlpAttribute {
  key: string;
  value:
    | { stringValue: string }
    | { intValue: string }
    | { doubleValue: number }
    | { boolValue: boolean };
}

interface OtlpSpan {
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  name: string;
  kind: number;
  startTimeUnixNano: string;
  endTimeUnixNano: string;
  attributes: OtlpAttribute[];
  status: { code: number; message?: string };
}

function eventToSpan(event: TelemetryEvent): OtlpSpan {
  const method = readString(event, "method");
  const urlHost = readString(event, "url_host");
  const urlPath = readString(event, "url_path") ?? "/";
  const name = readString(event, "span_name") ?? `${method ?? "?"} ${urlHost ?? "?"}`;

  // Prefer the request_id as a deterministic trace-id seed. Fall back
  // to a random 16-byte trace_id so every span is valid OTLP.
  const requestId = readString(event, "request_id");
  const traceId = requestId !== undefined ? padToHex(requestId, 32) : randomHex(16);
  const spanId = randomHex(8);

  const timestampMs = readNumber(event, "timestamp_ms") ?? Date.now();
  const latencyMs = readNumber(event, "latency_ms") ?? 0;
  const startNanos = BigInt(timestampMs) * 1_000_000n;
  const endNanos = startNanos + BigInt(Math.round(latencyMs * 1_000_000));

  const attributes: OtlpAttribute[] = [];
  if (method !== undefined) pushAttr(attributes, "http.request.method", method);
  if (urlHost !== undefined) {
    pushAttr(attributes, "url.full", `https://${urlHost}${urlPath}`);
  }
  const statusCode = readNumber(event, "status_code");
  if (statusCode !== undefined) pushIntAttr(attributes, "http.response.status_code", statusCode);
  if (latencyMs > 0) pushDoubleAttr(attributes, "checkrd.latency_ms", latencyMs);

  // GenAI attributes (OTel semconv 1.27+).
  const genAiSystem = readString(event, "gen_ai_system");
  if (genAiSystem !== undefined) pushAttr(attributes, "gen_ai.system", genAiSystem);
  const genAiModel = readString(event, "gen_ai_model");
  if (genAiModel !== undefined) pushAttr(attributes, "gen_ai.request.model", genAiModel);
  const inputTokens = readNumber(event, "gen_ai_input_tokens");
  if (inputTokens !== undefined) pushIntAttr(attributes, "gen_ai.usage.input_tokens", inputTokens);
  const outputTokens = readNumber(event, "gen_ai_output_tokens");
  if (outputTokens !== undefined) pushIntAttr(attributes, "gen_ai.usage.output_tokens", outputTokens);

  // Checkrd-specific attributes.
  const agentId = readString(event, "agent_id");
  if (agentId !== undefined) pushAttr(attributes, "checkrd.agent_id", agentId);
  const policyResult = readString(event, "policy_result");
  if (policyResult !== undefined) pushAttr(attributes, "checkrd.policy_result", policyResult);
  const denyReason = readString(event, "deny_reason");
  if (denyReason !== undefined) pushAttr(attributes, "checkrd.deny_reason", denyReason);

  // Status. OTLP: UNSET=0, OK=1, ERROR=2.
  const spanStatusCode = readString(event, "span_status_code");
  let status: { code: number; message?: string } = { code: 0 };
  if (spanStatusCode === "ERROR") {
    const message = readString(event, "span_status_message");
    status = message !== undefined ? { code: 2, message } : { code: 2 };
  } else if (spanStatusCode === "OK") {
    status = { code: 1 };
  }

  return {
    traceId,
    spanId,
    name,
    kind: SPAN_KIND_CLIENT,
    startTimeUnixNano: startNanos.toString(),
    endTimeUnixNano: endNanos.toString(),
    attributes,
    status,
  };
}

function readString(event: TelemetryEvent, key: string): string | undefined {
  const v = event[key];
  return typeof v === "string" ? v : undefined;
}

function readNumber(event: TelemetryEvent, key: string): number | undefined {
  const v = event[key];
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function pushAttr(out: OtlpAttribute[], key: string, value: string): void {
  out.push({ key, value: { stringValue: value } });
}

function pushIntAttr(out: OtlpAttribute[], key: string, value: number): void {
  out.push({ key, value: { intValue: Math.trunc(value).toString() } });
}

function pushDoubleAttr(out: OtlpAttribute[], key: string, value: number): void {
  out.push({ key, value: { doubleValue: value } });
}

/** Generate `n` cryptographically random bytes as lowercase hex. */
function randomHex(n: number): string {
  const arr = new Uint8Array(n);
  globalThis.crypto.getRandomValues(arr);
  let out = "";
  for (const b of arr) {
    out += b.toString(16).padStart(2, "0");
  }
  return out;
}

/** Pad or truncate `input` (stripped of non-hex chars) to exactly `length` hex chars. */
function padToHex(input: string, length: number): string {
  const hex = input.replace(/[^0-9a-fA-F]/g, "").toLowerCase();
  if (hex.length >= length) return hex.slice(0, length);
  return hex.padStart(length, "0");
}
