/**
 * `OtelSpanSink` — emit Checkrd telemetry on the caller's existing
 * OpenTelemetry tracer.
 *
 * Mirrors the Python SDK's `OTelSpanSink` class exactly. The sink is
 * the right choice when a host app already has OTel configured
 * (Datadog APM, Honeycomb, Grafana Cloud, a custom exporter) — spans
 * flow through whatever sampler, resource attributes, propagator, and
 * exporter the host set up. If you want Checkrd to OWN the OTel
 * pipeline (install an exporter, send to a specific endpoint), use
 * `OtlpSink` instead — that's an OTLP/HTTP endpoint-first path.
 *
 * `@opentelemetry/api` is a peer dependency. The API package is ~8 KB
 * and a peer dep of every OTel-compatible library in the ecosystem,
 * so in practice any app with OTel already has it installed. We
 * never import it at module load so non-OTel users pay zero cost;
 * construction imports lazily and raises with an actionable error
 * when the package is missing.
 *
 * Attribute shapes match the Python sink exactly so operators running
 * both SDKs see identical dashboard queries.
 */

import type { TelemetryEvent, TelemetrySink } from "./sinks.js";
import { VERSION } from "./_version.js";

/** Opaque OpenTelemetry types we don't want to bring into the hard dep surface. */
interface OtelSpan {
  setAttribute(key: string, value: string | number | boolean): void;
  setStatus(status: { code: number; message?: string }): void;
  end(): void;
}

interface OtelTracer {
  startSpan(name: string, options?: { kind?: number }): OtelSpan;
}

/** Options for {@link OtelSpanSink}. */
export interface OtelSpanSinkOptions {
  /**
   * Explicit tracer to emit on. Default: the global tracer resolved
   * via `@opentelemetry/api`'s `trace.getTracer("checkrd.sdk", VERSION)`.
   * Inject when you want Checkrd's spans to land on a specific tracer
   * (per-tenant providers, internal vs customer traffic, etc.).
   */
  tracer?: OtelTracer;
}

/**
 * OTel trace API status codes. Hard-coded to avoid importing from
 * `@opentelemetry/api` at module load — the sink must be importable
 * by apps that haven't installed OTel yet (they see the ImportError
 * only when they actually construct the sink).
 */
const OTEL_STATUS_UNSET = 0;
const OTEL_STATUS_OK = 1;
const OTEL_STATUS_ERROR = 2;
/**
 * OTel SpanKind — {@link https://opentelemetry.io/docs/specs/otel/trace/api/#spankind}.
 * 3 = CLIENT (outbound HTTP call), which is what every Checkrd
 * telemetry event represents.
 */
const OTEL_SPAN_KIND_CLIENT = 3;

/**
 * Sink that routes Checkrd telemetry through the caller's existing
 * OpenTelemetry tracer. See the module docstring for the rationale
 * and the {@link OtlpSink} alternative.
 */
export class OtelSpanSink implements TelemetrySink {
  private readonly tracer: OtelTracer;
  private stopped = false;

  constructor(opts: OtelSpanSinkOptions = {}) {
    if (opts.tracer !== undefined) {
      this.tracer = opts.tracer;
      return;
    }
    // Lazy import — pay the cost only when the default path is used
    // and only at construction time. `await import(...)` would force
    // the sink constructor to become async; the package is small and
    // synchronously loadable on every runtime we target.
    let trace: {
      getTracer(name: string, version?: string): OtelTracer;
    };
    try {
      // Optional peer dep loaded synchronously so the constructor can
      // fail upfront with a clear error if the package is missing.
      // `await import()` would force this entire surface async (and
      // bundlers code-split it, which breaks edge runtimes that have
      // no dynamic-import support). `require` is the documented escape
      // hatch for optional sync-loaded peers.
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const mod = require("@opentelemetry/api") as {
        trace: {
          getTracer(name: string, version?: string): OtelTracer;
        };
      };
      trace = mod.trace;
    } catch {
      throw new Error(
        "OtelSpanSink requires `@opentelemetry/api` (>=1.4). Install " +
          "with: npm install @opentelemetry/api\n" +
          "Or, if you want Checkrd to own the OTel pipeline (install " +
          "an exporter, send OTLP/HTTP to a specific endpoint), use " +
          "`OtlpSink` instead — it has no @opentelemetry/api peer dep.",
      );
    }
    this.tracer = trace.getTracer("checkrd.sdk", VERSION);
  }

  enqueue(event: TelemetryEvent): void {
    if (this.stopped) return;
    try {
      this.emitSpan(event);
    } catch {
      // Telemetry is best-effort. A bug in attribute shaping cannot
      // crash the request hot path — swallowing matches `OtlpSink`.
    }
  }

  close(): Promise<void> {
    // The caller's TracerProvider owns flushing. We just flip the
    // guard so late enqueues from a stopping process are dropped.
    // ``Promise.resolve()`` rather than ``async`` because there's
    // nothing to await — the interface mandates a Promise return.
    this.stopped = true;
    return Promise.resolve();
  }

  private emitSpan(event: TelemetryEvent): void {
    const method = readString(event, "method");
    const urlHost = readString(event, "url_host");
    const name =
      readString(event, "span_name") ??
      `${method ?? "?"} ${urlHost ?? "?"}`;

    const span = this.tracer.startSpan(name, {
      kind: OTEL_SPAN_KIND_CLIENT,
    });
    try {
      applySemconvAttributes(span, event);
      // OTel status. UNSET (0) is the default — only set when we
      // actually have a status signal.
      const spanStatusCode = readString(event, "span_status_code");
      if (spanStatusCode === "ERROR") {
        const message = readString(event, "span_status_message");
        span.setStatus(
          message !== undefined
            ? { code: OTEL_STATUS_ERROR, message }
            : { code: OTEL_STATUS_ERROR },
        );
      } else if (spanStatusCode === "OK") {
        span.setStatus({ code: OTEL_STATUS_OK });
      } else {
        span.setStatus({ code: OTEL_STATUS_UNSET });
      }
    } finally {
      span.end();
    }
  }
}

/**
 * Stamp OTel semconv + Checkrd namespace attributes on a span.
 * Extracted so any future sink (e.g. a metrics-only sink) can emit
 * the same attribute shapes without duplicating the mapping. Drift
 * between span shapes is a documented regression risk — dashboards
 * hang off these names.
 */
function applySemconvAttributes(span: OtelSpan, event: TelemetryEvent): void {
  // --- HTTP semconv (stable v1.x) --------------------------------
  const method = readString(event, "method");
  if (method !== undefined) span.setAttribute("http.request.method", method);
  const urlHost = readString(event, "url_host");
  const urlPath = readString(event, "url_path") ?? "/";
  if (urlHost !== undefined) {
    span.setAttribute("url.full", `https://${urlHost}${urlPath}`);
  }
  const statusCode = readNumber(event, "status_code");
  if (statusCode !== undefined) {
    span.setAttribute("http.response.status_code", statusCode);
  }
  const latencyMs = readNumber(event, "latency_ms");
  if (latencyMs !== undefined) {
    span.setAttribute("checkrd.latency_ms", latencyMs);
  }

  // --- GenAI semconv (1.27+) -------------------------------------
  // Two attribute-source layers, both stamped here so a span carries
  // the full GenAI picture regardless of which path produced it:
  //
  //   1. URL-derived (always on) — ``gen_ai.provider.name`` and
  //      ``gen_ai.operation.name`` from the request URL
  //      (see ``_genai.attributesForUrl``). Cheap, no body buffering.
  //
  //   2. Body-derived (opt-in via ``CHECKRD_EXTRACT_GENAI_BODY``) —
  //      ``gen_ai.request.model``, ``gen_ai.response.model``,
  //      ``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``,
  //      ``gen_ai.request.stream``. Requires parsing JSON bodies, so
  //      gated by an explicit opt-in to keep the PII surface bounded
  //      (see ``_genai_body.ts``).
  //
  // The transport layer writes these keys directly onto the
  // telemetry event using OTel-spec names, so the sink just passes
  // them through. Iterating a fixed list (rather than
  // ``for (k of Object.keys(event)) if (k.startsWith("gen_ai."))``)
  // keeps the contract auditable — a dashboard query for a specific
  // attribute name has a single source of truth.
  const stringGenAiAttrs = [
    "gen_ai.provider.name",
    "gen_ai.operation.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
  ] as const;
  for (const key of stringGenAiAttrs) {
    const value = readString(event, key);
    if (value !== undefined) span.setAttribute(key, value);
  }
  const numericGenAiAttrs = [
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
  ] as const;
  for (const key of numericGenAiAttrs) {
    const value = readNumber(event, key);
    if (value !== undefined) span.setAttribute(key, value);
  }
  const stream = readBoolean(event, "gen_ai.request.stream");
  if (stream !== undefined) {
    span.setAttribute("gen_ai.request.stream", stream);
  }

  // --- Checkrd namespace -----------------------------------------
  const agentId = readString(event, "agent_id");
  if (agentId !== undefined) span.setAttribute("checkrd.agent_id", agentId);
  const policyResult = readString(event, "policy_result");
  if (policyResult !== undefined) {
    span.setAttribute("checkrd.policy_result", policyResult);
  }
  const denyReason = readString(event, "deny_reason");
  if (denyReason !== undefined) {
    span.setAttribute("checkrd.deny_reason", denyReason);
  }
  const matchedRule = readString(event, "matched_rule");
  if (matchedRule !== undefined) {
    span.setAttribute("checkrd.matched_rule", matchedRule);
  }
  const matchedRuleKind = readString(event, "matched_rule_kind");
  if (matchedRuleKind !== undefined) {
    span.setAttribute("checkrd.matched_rule_kind", matchedRuleKind);
  }
}

function readString(event: TelemetryEvent, key: string): string | undefined {
  const v = event[key];
  return typeof v === "string" ? v : undefined;
}

function readNumber(event: TelemetryEvent, key: string): number | undefined {
  const v = event[key];
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function readBoolean(event: TelemetryEvent, key: string): boolean | undefined {
  const v = event[key];
  return typeof v === "boolean" ? v : undefined;
}
